// edge-doorkeeper 펌웨어 (F2: Flash 재생 → int8 추론 → 후처리 → 서버 전송)
// 시리얼 출력 형식 (tools/device_parity.py가 읽는다)
//   EVT <scenario_id> <in|out> <ts_offset_ms> <frame>
//   SCN <scenario_id> <in 수> <out 수> <frames>
//   PERF <key>=<value> ...
//   DONE  (모든 시나리오 재생 후. 이후에도 heartbeat는 계속 보낸다)
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
#include "net.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "postproc.h"
#include "sdkconfig.h"
#include "uplink.h"

static const char *TAG = "main";

#define MODEL_VERSION "hm-a-14b60e8"  // ml/model.tflite를 만든 커밋
#define HB_EVERY_FRAMES 16            // 8 FPS에서 2초
#define HB_IDLE_MS 2000
#define MAX_TIMED_FRAMES 1024

static uint32_t s_boot_id;
static uint32_t s_infer_us[MAX_TIMED_FRAMES];  // 전체 추론 시간 (PERF 중앙값)
static int s_n_timed;

typedef struct {  // heartbeat 한 번 사이의 추론 통계
    uint32_t us[HB_EVERY_FRAMES];
    int n;
    int64_t start_us;
} window_t;

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

static const char *reset_reason_str(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_SW: return "sw";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_EXT: return "ext";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    default: return "other";
    }
}

static int cmp_u32(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

static void push_heartbeat(int64_t ts, const char *scenario_id, window_t *w)
{
    uplink_event_t e = {.kind = UP_HEARTBEAT, .ts = ts};
    if (w->n > 0) {
        uint32_t sorted[HB_EVERY_FRAMES];
        memcpy(sorted, w->us, w->n * sizeof(uint32_t));
        qsort(sorted, w->n, sizeof(uint32_t), cmp_u32);
        int64_t elapsed = esp_timer_get_time() - w->start_us;
        e.hb.fps = elapsed > 0 ? w->n * 1e6f / elapsed : 0.0f;
        e.hb.infer_ms_p50 = (sorted[w->n / 2] + 500) / 1000;
        e.hb.infer_ms_max = (sorted[w->n - 1] + 500) / 1000;
    }
    e.hb.free_heap = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    e.hb.min_free_heap = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    e.hb.stack_hwm = uxTaskGetStackHighWaterMark(NULL);
    e.hb.arena_used = model_arena_used();
    e.hb.rssi = net_rssi();
    strlcpy(e.hb.scenario_id, scenario_id, sizeof(e.hb.scenario_id));
    uplink_push(&e);
    w->n = 0;
    w->start_us = esp_timer_get_time();
}

static void replay_task(void *arg)
{
    static float hm[PP_GRID_H * PP_GRID_W];
    static pp_peak_t peaks[PP_MAX_PEAKS];
    static pp_event_t ev[PP_MAX_TRACKS];
    static pp_counter_t ctr;
    static window_t win;
    pp_config_t cfg;
    pp_config_default(&cfg);
    int8_t *input = model_input();
    const int fps = frames_fps();
    const int frame_ms = 1000 / fps;
    uint32_t read_max_us = 0, infer_max_us = 0, post_max_us = 0;
    int64_t clock_ms = 0;  // 시나리오 시각
    const char *last_id = "";

    for (int s = 0; s < frames_count(); s++) {
        const frame_scenario_t *sc = frames_scenario(s);
        last_id = sc->id;
        pp_counter_init(&ctr, &cfg, fps);
        int n_in = 0, n_out = 0;
        win.n = 0;
        win.start_us = esp_timer_get_time();
        TickType_t wake = xTaskGetTickCount();
        for (uint32_t k = 0; k < sc->n_frames; k++) {
            clock_ms = sc->start_ms + (int64_t)k * frame_ms;
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
                uplink_event_t up = {.kind = UP_PASSAGE, .ts = sc->start_ms + ev[e].ts_offset_ms};
                up.passage.direction = ev[e].direction;
                up.passage.conf = ev[e].score > 1.0 ? 1.0f : (float)ev[e].score;
                uplink_push(&up);
            }
            uint32_t r = t1 - t0, inf = t2 - t1, pst = t3 - t2;
            if (r > read_max_us) read_max_us = r;
            if (inf > infer_max_us) infer_max_us = inf;
            if (pst > post_max_us) post_max_us = pst;
            if (s_n_timed < MAX_TIMED_FRAMES) s_infer_us[s_n_timed++] = inf;
            win.us[win.n++] = inf;
            if (win.n == HB_EVERY_FRAMES) push_heartbeat(clock_ms, sc->id, &win);
#if CONFIG_DOORKEEPER_REPLAY_REALTIME
            vTaskDelayUntil(&wake, pdMS_TO_TICKS(frame_ms));
#else
            vTaskDelay(1);  // 최대 속도라도 다른 태스크(IDLE·워치독)에 양보
#endif
        }
        printf("SCN %s %d %d %u\n", sc->id, n_in, n_out, (unsigned)sc->n_frames);
        // 유휴 구간: 프레임 없이 시계만 진행하며 heartbeat를 보낸다 (출입 세션 종료, 장기 체류 판정)
        int64_t end_ms = sc->start_ms + (int64_t)sc->n_frames * frame_ms;
        for (int64_t t = HB_IDLE_MS; t <= (int64_t)sc->idle_gap_s * 1000; t += HB_IDLE_MS) {
            clock_ms = end_ms + t;
            push_heartbeat(clock_ms, sc->id, &win);
            vTaskDelay(pdMS_TO_TICKS(CONFIG_DOORKEEPER_REPLAY_REALTIME ? HB_IDLE_MS : 10));
        }
    }

    qsort(s_infer_us, s_n_timed, sizeof(uint32_t), cmp_u32);
    printf("PERF frames=%d infer_p50_us=%u infer_max_us=%u read_max_us=%u post_max_us=%u arena_used=%u "
           "free_internal=%u min_free_internal=%u stack_hwm=%u dropped=%u boot_id=%u\n",
           s_n_timed, (unsigned)s_infer_us[s_n_timed / 2], (unsigned)infer_max_us, (unsigned)read_max_us,
           (unsigned)post_max_us, (unsigned)model_arena_used(),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)uxTaskGetStackHighWaterMark(NULL), (unsigned)uplink_dropped(), (unsigned)s_boot_id);
    printf("DONE\n");
    // 재생이 끝나도 heartbeat를 계속 보내 '온라인'을 유지한다 (끊기면 서버가 감시 공백으로 기록)
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(HB_IDLE_MS));
        clock_ms += HB_IDLE_MS;
        push_heartbeat(clock_ms, last_id, &win);
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    s_boot_id = next_boot_id();
    esp_reset_reason_t reason = esp_reset_reason();
    ESP_LOGI(TAG, "boot_id=%u reset_reason=%s device=%s", (unsigned)s_boot_id, reset_reason_str(reason),
             CONFIG_DOORKEEPER_DEVICE_ID);

    if (!frames_open() || !model_init()) {
        // 재부팅을 반복하지 않도록 멈춘다 (docs/04-model.md 1.8절)
        ESP_LOGE(TAG, "초기화 실패. 정지");
        return;
    }
    uplink_init(s_boot_id, MODEL_VERSION);
    bool online = net_start(15000);
    ESP_LOGI(TAG, "네트워크 %s, 시나리오 %d개", online ? "연결" : "미연결", frames_count());

    uplink_event_t boot = {.kind = UP_BOOT, .ts = frames_scenario(0)->start_ms};  // 재생 모드: 시나리오 시각
    strlcpy(boot.boot.reset_reason, reset_reason_str(reason), sizeof(boot.boot.reset_reason));
    uplink_push(&boot);
    uplink_start();  // 코어 0 (Wi-Fi와 같은 코어)
    xTaskCreatePinnedToCore(replay_task, "replay", 16 * 1024, NULL, 5, NULL, 1);  // 추론은 코어 1
}
