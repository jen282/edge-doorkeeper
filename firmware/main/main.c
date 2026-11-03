// edge-doorkeeper 펌웨어 (F1: Flash 재생 → int8 추론 → 후처리 → 시리얼 로그)
// 시리얼 출력 형식 (tools/device_parity.py가 읽는다)
//   EVT <scenario_id> <in|out> <ts_offset_ms> <frame>
//   SCN <scenario_id> <in 수> <out 수> <frames>
//   PERF <key>=<value> ...
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "frame_source.h"
#include "model_runner.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "postproc.h"
#include "sdkconfig.h"

static const char *TAG = "main";

#define MAX_TIMED_FRAMES 1024

static uint32_t s_boot_id;
static uint32_t s_infer_us[MAX_TIMED_FRAMES];  // 추론 시간 기록 (중앙값 계산용, 정적 할당)
static int s_n_timed;

static uint32_t next_boot_id(void)
{
    // 부팅마다 1씩 늘린다. event_id = device_id:boot_id:seq 의 중복을 막는다
    nvs_handle_t h;
    uint32_t id = 0;
    if (nvs_open("doorkeeper", NVS_READWRITE, &h) == ESP_OK) {
        nvs_get_u32(h, "boot_id", &id);
        id++;
        nvs_set_u32(h, "boot_id", id);
        nvs_commit(h);
        nvs_close(h);
    }
    return id;
}

static int cmp_u32(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

static void replay_task(void *arg)
{
    static float hm[PP_GRID_H * PP_GRID_W];
    static pp_peak_t peaks[PP_MAX_PEAKS];
    static pp_event_t ev[PP_MAX_TRACKS];
    static pp_counter_t ctr;
    pp_config_t cfg;
    pp_config_default(&cfg);
    int8_t *input = model_input();
    const int fps = frames_fps();
    uint32_t read_max_us = 0, infer_max_us = 0, post_max_us = 0;

    for (int s = 0; s < frames_count(); s++) {
        const frame_scenario_t *sc = frames_scenario(s);
        pp_counter_init(&ctr, &cfg, fps);
        int n_in = 0, n_out = 0;
        TickType_t wake = xTaskGetTickCount();
        for (uint32_t k = 0; k < sc->n_frames; k++) {
            int64_t t0 = esp_timer_get_time();
            if (!frames_read(s, k, input)) {
                ESP_LOGE(TAG, "프레임 읽기 실패 %s/%u", sc->id, (unsigned)k);
                break;
            }
            int64_t t1 = esp_timer_get_time();
            if (!model_invoke(hm)) {
                ESP_LOGE(TAG, "추론 실패");
                break;
            }
            int64_t t2 = esp_timer_get_time();
            int np = pp_find_peaks(hm, &cfg, peaks, PP_MAX_PEAKS);
            int ne = pp_counter_update(&ctr, peaks, np, k, ev, PP_MAX_TRACKS);
            int64_t t3 = esp_timer_get_time();
            for (int e = 0; e < ne; e++) {
                printf("EVT %s %s %lld %d\n", sc->id, ev[e].direction > 0 ? "in" : "out",
                       (long long)ev[e].ts_offset_ms, ev[e].frame);
                if (ev[e].direction > 0) n_in++; else n_out++;
            }
            uint32_t r = t1 - t0, inf = t2 - t1, pst = t3 - t2;
            if (r > read_max_us) read_max_us = r;
            if (inf > infer_max_us) infer_max_us = inf;
            if (pst > post_max_us) post_max_us = pst;
            if (s_n_timed < MAX_TIMED_FRAMES) s_infer_us[s_n_timed++] = inf;
#if CONFIG_DOORKEEPER_REPLAY_REALTIME
            vTaskDelayUntil(&wake, pdMS_TO_TICKS(1000 / fps));
#else
            vTaskDelay(1);  // 최대 속도라도 다른 태스크(IDLE·워치독)에 양보
#endif
        }
        printf("SCN %s %d %d %u\n", sc->id, n_in, n_out, (unsigned)sc->n_frames);
    }

    qsort(s_infer_us, s_n_timed, sizeof(uint32_t), cmp_u32);
    printf("PERF frames=%d infer_p50_us=%u infer_max_us=%u read_max_us=%u post_max_us=%u arena_used=%u "
           "free_internal=%u min_free_internal=%u stack_hwm=%u boot_id=%u\n",
           s_n_timed, (unsigned)s_infer_us[s_n_timed / 2], (unsigned)infer_max_us, (unsigned)read_max_us,
           (unsigned)post_max_us, (unsigned)model_arena_used(),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)uxTaskGetStackHighWaterMark(NULL), (unsigned)s_boot_id);
    printf("DONE\n");
    vTaskDelete(NULL);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    s_boot_id = next_boot_id();
    ESP_LOGI(TAG, "boot_id=%u reset_reason=%d device=%s", (unsigned)s_boot_id, (int)esp_reset_reason(),
             CONFIG_DOORKEEPER_DEVICE_ID);

    if (!frames_open() || !model_init()) {
        // 재부팅을 반복하지 않도록 멈춘다 (docs/04-model.md 1.8절)
        ESP_LOGE(TAG, "초기화 실패. 정지");
        return;
    }
    for (int s = 0; s < frames_count(); s++) {
        const frame_scenario_t *sc = frames_scenario(s);
        ESP_LOGI(TAG, "시나리오 %s 프레임 %u 유휴 %us 시작 %lld", sc->id, (unsigned)sc->n_frames,
                 (unsigned)sc->idle_gap_s, (long long)sc->start_ms);
    }
    // 추론은 코어 1, Wi-Fi·httpd는 코어 0 (F2부터)
    xTaskCreatePinnedToCore(replay_task, "replay", 16 * 1024, NULL, 5, NULL, 1);
}
