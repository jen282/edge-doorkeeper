// edge-doorkeeper 펌웨어: 프레임 소스(Flash 재생 또는 카메라) → int8 추론 → 후처리 → 서버 전송 + 실시간 영상
// 시리얼 출력 형식 (tools/device_parity.py가 읽는다)
//   EVT <scenario_id> <in|out> <ts_offset_ms> <frame>
//   SCN <scenario_id> <in 수> <out 수> <frames>
//   PERF <key>=<value> ...
//   DONE  (재생 모드에서 모든 시나리오 재생 후. 이후에도 heartbeat는 계속 보낸다)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "camera.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
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
#include "stream.h"
#include "uplink.h"

#if CONFIG_DOORKEEPER_SOURCE_CAMERA
#define SOURCE_MODE "camera"
#else
#define SOURCE_MODE "replay"
#endif
#ifndef CONFIG_DOORKEEPER_REPLAY_REALTIME  // 카메라 빌드에서는 정의되지 않는다
#define CONFIG_DOORKEEPER_REPLAY_REALTIME 0
#endif

static const char *TAG = "main";

#define MODEL_VERSION "hm-a-14b60e8"  // ml/model.tflite를 만든 커밋
#define FPS 8
#define FRAME_MS (1000 / FPS)
#define HB_EVERY_FRAMES 16  // 8 FPS에서 2초
#define HB_IDLE_MS 2000
#define RECENT_FRAMES 8     // 계수 직후 이 프레임 수 동안 박스 색을 바꾼다 (1초)
#define MAX_TIMED_FRAMES 1024

static uint32_t s_boot_id;
static esp_reset_reason_t s_reason;
static uint32_t s_infer_us[MAX_TIMED_FRAMES];  // 전체 추론 시간 (PERF 중앙값)
static int s_n_timed;
static uint32_t s_read_max_us, s_infer_max_us, s_post_max_us;

// 추론 태스크 하나만 쓰는 상태 (정적 할당)
static float s_hm[PP_GRID_H * PP_GRID_W];
static pp_peak_t s_peaks[PP_MAX_PEAKS];
static pp_event_t s_ev[PP_MAX_TRACKS];
static pp_counter_t s_ctr;
static pp_config_t s_cfg;

static struct {  // heartbeat 한 번 사이의 추론 통계
    uint32_t us[HB_EVERY_FRAMES];
    int n;
    int64_t start_us;
} s_win;

static struct {  // 최근 계수된 트랙 (실시간 영상 박스 색)
    int tid, frame, dir;
} s_recent[PP_MAX_TRACKS * 2];
static int s_recent_next;

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

#if CONFIG_DOORKEEPER_SOURCE_CAMERA
static int64_t wall_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}
#endif

static int cmp_u32(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

static void push_boot(int64_t ts)
{
    uplink_event_t e = {.kind = UP_BOOT, .ts = ts};
    strlcpy(e.boot.reset_reason, reset_reason_str(s_reason), sizeof(e.boot.reset_reason));
    uplink_push(&e);
}

static void push_heartbeat(int64_t ts, const char *scenario_id)
{
    uplink_event_t e = {.kind = UP_HEARTBEAT, .ts = ts};
    if (s_win.n > 0) {
        uint32_t sorted[HB_EVERY_FRAMES];
        memcpy(sorted, s_win.us, s_win.n * sizeof(uint32_t));
        qsort(sorted, s_win.n, sizeof(uint32_t), cmp_u32);
        int64_t elapsed = esp_timer_get_time() - s_win.start_us;
        e.hb.fps = elapsed > 0 ? s_win.n * 1e6f / elapsed : 0.0f;
        e.hb.infer_ms_p50 = (sorted[s_win.n / 2] + 500) / 1000;
        e.hb.infer_ms_max = (sorted[s_win.n - 1] + 500) / 1000;
    }
    e.hb.free_heap = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    e.hb.min_free_heap = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    e.hb.stack_hwm = uxTaskGetStackHighWaterMark(NULL);
    e.hb.arena_used = model_arena_used();
    e.hb.rssi = net_rssi();
    strlcpy(e.hb.scenario_id, scenario_id, sizeof(e.hb.scenario_id));
    uplink_push(&e);
    s_win.n = 0;
    s_win.start_us = esp_timer_get_time();
}

static void reset_counter(void)
{
    pp_counter_init(&s_ctr, &s_cfg, FPS);
    memset(s_recent, 0, sizeof(s_recent));
    for (size_t i = 0; i < sizeof(s_recent) / sizeof(s_recent[0]); i++) s_recent[i].tid = -1;
    s_win.n = 0;
    s_win.start_us = esp_timer_get_time();
}

static stream_box_state_t box_state(int tid, int frame)
{
    for (size_t i = 0; i < sizeof(s_recent) / sizeof(s_recent[0]); i++) {
        if (s_recent[i].tid == tid && frame - s_recent[i].frame < RECENT_FRAMES) {
            return s_recent[i].dir > 0 ? BOX_IN : BOX_OUT;
        }
    }
    return BOX_TRACKING;
}

// 입력 텐서에 프레임이 들어 있는 상태에서 한 프레임을 처리한다.
// base_ms: 계수기 시작 시각(이벤트 ts = base_ms + ts_offset), frame_ts: 이 프레임의 시각(heartbeat용)
static void process_frame(int k, int64_t base_ms, int64_t frame_ts, uint32_t read_us, const char *sid,
                          int *n_in, int *n_out)
{
    stream_capture(model_input());  // 추론이 입력 텐서 메모리를 덮어쓰기 전에
    int64_t t1 = esp_timer_get_time();
    if (!model_invoke(s_hm)) {
        ESP_LOGE(TAG, "추론 실패");
        return;
    }
    int64_t t2 = esp_timer_get_time();
    int np = pp_find_peaks(s_hm, &s_cfg, s_peaks, PP_MAX_PEAKS);
    int ne = pp_counter_update(&s_ctr, s_peaks, np, k, s_ev, PP_MAX_TRACKS);
    int64_t t3 = esp_timer_get_time();
    for (int e = 0; e < ne; e++) {
        printf("EVT %s %s %lld %d\n", sid, s_ev[e].direction > 0 ? "in" : "out", (long long)s_ev[e].ts_offset_ms,
               s_ev[e].frame);
        if (s_ev[e].direction > 0) (*n_in)++; else (*n_out)++;
        uplink_event_t up = {.kind = UP_PASSAGE, .ts = base_ms + s_ev[e].ts_offset_ms};
        up.passage.direction = s_ev[e].direction;
        up.passage.conf = s_ev[e].score > 1.0 ? 1.0f : (float)s_ev[e].score;
        uplink_push(&up);
        s_recent[s_recent_next].tid = s_ev[e].track;
        s_recent[s_recent_next].frame = k;
        s_recent[s_recent_next].dir = s_ev[e].direction;
        s_recent_next = (s_recent_next + 1) % (int)(sizeof(s_recent) / sizeof(s_recent[0]));
    }

    stream_box_t boxes[STREAM_MAX_BOXES];
    int nb = 0;
    for (int t = 0; t < s_ctr.n_tracks && nb < STREAM_MAX_BOXES; t++) {
        const pp_track_t *tr = &s_ctr.tracks[t];
        if (tr->misses > 0) continue;  // 이번 프레임에 관측된 트랙만 그린다
        boxes[nb++] = (stream_box_t){(float)tr->x, (float)tr->y, box_state(tr->tid, k)};
    }
    stream_publish(boxes, nb);

    uint32_t inf = t2 - t1, pst = t3 - t2;
    if (read_us > s_read_max_us) s_read_max_us = read_us;
    if (inf > s_infer_max_us) s_infer_max_us = inf;
    if (pst > s_post_max_us) s_post_max_us = pst;
    if (s_n_timed < MAX_TIMED_FRAMES) s_infer_us[s_n_timed++] = inf;
    s_win.us[s_win.n++] = inf;
    if (s_win.n == HB_EVERY_FRAMES) push_heartbeat(frame_ts, sid);
}

static void print_perf(void)
{
    uint32_t sorted_p50 = 0;
    if (s_n_timed > 0) {
        qsort(s_infer_us, s_n_timed, sizeof(uint32_t), cmp_u32);
        sorted_p50 = s_infer_us[s_n_timed / 2];
    }
    printf("PERF frames=%d infer_p50_us=%u infer_max_us=%u read_max_us=%u post_max_us=%u arena_used=%u "
           "free_internal=%u min_free_internal=%u stack_hwm=%u dropped=%u boot_id=%u\n",
           s_n_timed, (unsigned)sorted_p50, (unsigned)s_infer_max_us, (unsigned)s_read_max_us,
           (unsigned)s_post_max_us, (unsigned)model_arena_used(),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)uxTaskGetStackHighWaterMark(NULL), (unsigned)uplink_dropped(), (unsigned)s_boot_id);
}

#if CONFIG_DOORKEEPER_SOURCE_REPLAY
static void replay_task(void *arg)
{
    int64_t clock_ms = 0;  // 시나리오 시각
    const char *last_id = "";
    for (int s = 0; s < frames_count(); s++) {
        const frame_scenario_t *sc = frames_scenario(s);
        last_id = sc->id;
        reset_counter();
        int n_in = 0, n_out = 0;
        TickType_t wake = xTaskGetTickCount();
        for (uint32_t k = 0; k < sc->n_frames; k++) {
            clock_ms = sc->start_ms + (int64_t)k * FRAME_MS;
            int64_t t0 = esp_timer_get_time();
            if (!frames_read(s, k, model_input())) {
                ESP_LOGE(TAG, "프레임 읽기 실패 %s/%u", sc->id, (unsigned)k);
                break;
            }
            process_frame(k, sc->start_ms, clock_ms, esp_timer_get_time() - t0, sc->id, &n_in, &n_out);
#if CONFIG_DOORKEEPER_REPLAY_REALTIME
            vTaskDelayUntil(&wake, pdMS_TO_TICKS(FRAME_MS));
#else
            vTaskDelay(1);  // 최대 속도라도 다른 태스크(IDLE·워치독)에 양보
#endif
        }
        printf("SCN %s %d %d %u\n", sc->id, n_in, n_out, (unsigned)sc->n_frames);
        // 유휴 구간: 프레임 없이 시계만 진행하며 heartbeat를 보낸다 (출입 세션 종료, 장기 체류 판정)
        int64_t end_ms = sc->start_ms + (int64_t)sc->n_frames * FRAME_MS;
        for (int64_t t = HB_IDLE_MS; t <= (int64_t)sc->idle_gap_s * 1000; t += HB_IDLE_MS) {
            clock_ms = end_ms + t;
            push_heartbeat(clock_ms, sc->id);
            vTaskDelay(pdMS_TO_TICKS(CONFIG_DOORKEEPER_REPLAY_REALTIME ? HB_IDLE_MS : 10));
        }
    }
    print_perf();
    printf("DONE\n");
    // 재생이 끝나도 heartbeat를 계속 보내 '온라인'을 유지한다 (끊기면 서버가 감시 공백으로 기록)
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(HB_IDLE_MS));
        clock_ms += HB_IDLE_MS;
        push_heartbeat(clock_ms, last_id);
    }
}
#endif

#if CONFIG_DOORKEEPER_SOURCE_CAMERA
static void camera_task(void *arg)
{
    // 실제 시각이 맞기 전에는 이벤트를 만들지 않는다 (docs/11-risks.md T10)
    while (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(5000)) != ESP_OK) ESP_LOGW(TAG, "SNTP 시각 동기화 대기 중");
    ESP_LOGI(TAG, "시각 동기화 완료, 카메라 계수 시작");
    push_boot(wall_ms());
    reset_counter();
    int64_t base_ms = wall_ms();
    int n_in = 0, n_out = 0;
    TickType_t wake = xTaskGetTickCount();
    for (int k = 0;; k++) {
        int64_t t0 = esp_timer_get_time();
        if (!camera_read(model_input())) {
            ESP_LOGW(TAG, "카메라 프레임 없음");
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        process_frame(k, base_ms, wall_ms(), esp_timer_get_time() - t0, "camera", &n_in, &n_out);
        if (k % (FPS * 30) == FPS * 30 - 1) {  // 30초마다 성능 요약
            printf("CAM in=%d out=%d ", n_in, n_out);
            print_perf();
            s_n_timed = 0;
        }
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(FRAME_MS));
    }
}
#endif

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    s_boot_id = next_boot_id();
    s_reason = esp_reset_reason();
    ESP_LOGI(TAG, "boot_id=%u reset_reason=%s device=%s source=%s", (unsigned)s_boot_id,
             reset_reason_str(s_reason), CONFIG_DOORKEEPER_DEVICE_ID, SOURCE_MODE);
    pp_config_default(&s_cfg);

#if CONFIG_DOORKEEPER_SOURCE_CAMERA
    bool source_ok = camera_init();
#else
    bool source_ok = frames_open();
#endif
    if (!source_ok || !model_init()) {
        // 재부팅을 반복하지 않도록 멈춘다 (docs/04-model.md 1.8절)
        ESP_LOGE(TAG, "초기화 실패. 정지");
        return;
    }
    uplink_init(s_boot_id, MODEL_VERSION, SOURCE_MODE);
    bool online = net_start(15000);
    ESP_LOGI(TAG, "네트워크 %s", online ? "연결" : "미연결");
    stream_start();
    uplink_start();  // 코어 0 (Wi-Fi와 같은 코어)

#if CONFIG_DOORKEEPER_SOURCE_CAMERA
    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&sntp);
    xTaskCreatePinnedToCore(camera_task, "camera", 16 * 1024, NULL, 5, NULL, 1);  // 추론은 코어 1
#else
    ESP_LOGI(TAG, "시나리오 %d개", frames_count());
    push_boot(frames_scenario(0)->start_ms);  // 재생 모드: 시나리오 시각
    xTaskCreatePinnedToCore(replay_task, "replay", 16 * 1024, NULL, 5, NULL, 1);  // 추론은 코어 1
#endif
}
