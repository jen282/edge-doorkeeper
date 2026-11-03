#include "model_runner.h"

#include <cstring>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/schema/schema_generated.h"  // esp-tflite-micro 1.4 경로

extern const uint8_t model_start[] asm("_binary_model_tflite_start");
extern const uint8_t model_end[] asm("_binary_model_tflite_end");

namespace {
const char *TAG = "model";
constexpr size_t kArenaSize = 96 * 1024;  // 추정치(docs/04-model.md 1.7절). 실제 사용량은 부팅 로그로 확인
// PSRAM에 둔다. 처음엔 속도를 위해 내부 SRAM에 뒀으나, 카메라+실시간 영상 시청 중 내부 힙 최소 여유가
// 5.5KB까지 떨어져(실측) 옮겼다. 추론은 조금 느려질 수 있다
uint8_t *g_arena = nullptr;
tflite::MicroInterpreter *g_interp = nullptr;
TfLiteTensor *g_in = nullptr;
TfLiteTensor *g_out = nullptr;
}  // namespace

extern "C" bool model_init(void)
{
    const uint8_t *buf = model_start;
    size_t size = model_end - model_start;
    if (reinterpret_cast<uintptr_t>(buf) % 16 != 0) {  // 플랫버퍼 정렬 보장
        uint8_t *copy = static_cast<uint8_t *>(heap_caps_aligned_alloc(16, size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        if (!copy) return false;
        memcpy(copy, buf, size);
        buf = copy;
        ESP_LOGW(TAG, "모델이 16바이트 정렬이 아니라 내부 RAM으로 복사 (%u bytes)", (unsigned)size);
    }
    const tflite::Model *model = tflite::GetModel(buf);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "스키마 버전 불일치");
        return false;
    }
    // 연산자 목록은 ml/gates.yaml의 allowed_ops와 같아야 한다 (CI가 모델 쪽을 검사)
    static tflite::MicroMutableOpResolver<3> resolver;
    resolver.AddConv2D();
    resolver.AddDepthwiseConv2D();
    resolver.AddLogistic();
    g_arena = static_cast<uint8_t *>(heap_caps_aligned_alloc(16, kArenaSize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!g_arena) {
        ESP_LOGE(TAG, "아레나 할당 실패 (PSRAM)");
        return false;
    }
    static tflite::MicroInterpreter interp(model, resolver, g_arena, kArenaSize);
    if (interp.AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors 실패: 아레나(%u bytes) 부족", (unsigned)kArenaSize);
        return false;
    }
    g_interp = &interp;
    g_in = interp.input(0);
    g_out = interp.output(0);
    bool ok = g_in->type == kTfLiteInt8 && g_in->bytes == 72 * 96 && g_out->type == kTfLiteInt8 &&
              g_out->bytes == 9 * 12 && g_in->params.zero_point == -128;
    ESP_LOGI(TAG, "모델 %u bytes, 아레나 사용 %u / %u bytes, 입력 zp=%d, 출력 scale=%f zp=%d", (unsigned)size,
             (unsigned)interp.arena_used_bytes(), (unsigned)kArenaSize, (int)g_in->params.zero_point,
             g_out->params.scale, (int)g_out->params.zero_point);
    if (!ok) ESP_LOGE(TAG, "입출력 형식이 계약과 다름");
    return ok;
}

extern "C" int8_t *model_input(void) { return g_in->data.int8; }

extern "C" bool model_invoke(float *hm)
{
    if (g_interp->Invoke() != kTfLiteOk) return false;
    const float scale = g_out->params.scale;
    const int zp = g_out->params.zero_point;
    for (int i = 0; i < 9 * 12; i++) {
        hm[i] = static_cast<float>(g_out->data.int8[i] - zp) * scale;  // Python 평가와 같은 float32 연산
    }
    return true;
}

extern "C" size_t model_arena_used(void) { return g_interp ? g_interp->arena_used_bytes() : 0; }
