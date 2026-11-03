#include "stream.h"

#include <stdbool.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "img_converters.h"
#include "sdkconfig.h"

#define W 96
#define H 72
#define BOX_W 34  // 어깨 폭 0.45m를 어깨 높이(약 1.45m)에서 투영한 크기 (docs/04-model.md 1.9절)
#define BOX_H 20
#define JPEG_CAP (24 * 1024)
#define BOUNDARY "edframe"

static const char *TAG = "stream";
static SemaphoreHandle_t s_lock;
static uint8_t *s_pending;  // 추론 전에 캡처한 프레임 (추론 태스크만 쓴다)
static uint8_t *s_gray;     // 인코딩할 최신 프레임 1장 (PSRAM, 덮어쓴다)
static uint8_t *s_rgb;   // RGB565 big-endian (esp32-camera 인코더 형식)
static uint8_t *s_jpg;
static size_t s_jpg_len;
static stream_box_t s_boxes[STREAM_MAX_BOXES];
static int s_n_boxes;
static volatile uint32_t s_seq;
static volatile bool s_busy;
static QueueHandle_t s_req_q;  // 시청 요청을 전송 태스크로 넘긴다

void stream_capture(const int8_t *frame)
{
    if (!s_lock) return;
    for (int i = 0; i < W * H; i++) s_pending[i] = (uint8_t)frame[i] ^ 0x80;  // int8 → uint8
}

void stream_publish(const stream_box_t *boxes, int n_boxes)
{
    if (!s_lock) return;
    if (xSemaphoreTake(s_lock, 0) != pdTRUE) return;  // 인코딩 중이면 이번 프레임은 건너뛴다 (추론을 막지 않음)
    memcpy(s_gray, s_pending, W * H);
    s_n_boxes = n_boxes > STREAM_MAX_BOXES ? STREAM_MAX_BOXES : n_boxes;
    memcpy(s_boxes, boxes, s_n_boxes * sizeof(stream_box_t));
    s_seq++;
    xSemaphoreGive(s_lock);
}

static inline void put(int x, int y, uint16_t c)
{
    if (x < 0 || x >= W || y < 0 || y >= H) return;
    s_rgb[2 * (y * W + x)] = c >> 8;
    s_rgb[2 * (y * W + x) + 1] = c & 0xFF;
}

static uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

static void render(void)
{
    for (int i = 0; i < W * H; i++) {
        uint8_t v = s_gray[i];
        uint16_t c = rgb565(v, v, v);
        s_rgb[2 * i] = c >> 8;
        s_rgb[2 * i + 1] = c & 0xFF;
    }
    uint16_t line = rgb565(0, 210, 230);  // 하늘색: 회색 바닥에서도 보이게
    for (int x = 0; x < W; x += 3) {  // 가상 라인 A(24px), C(48px) 점선
        put(x, 24, line);
        put(x, 48, line);
    }
    for (int b = 0; b < s_n_boxes; b++) {
        uint16_t c = s_boxes[b].state == BOX_IN ? rgb565(40, 120, 255)
                   : s_boxes[b].state == BOX_OUT ? rgb565(255, 150, 0) : rgb565(0, 220, 0);
        int x0 = (int)(s_boxes[b].x - BOX_W / 2), y0 = (int)(s_boxes[b].y - BOX_H / 2);
        for (int x = x0; x < x0 + BOX_W; x++) {
            put(x, y0, c);
            put(x, y0 + BOX_H - 1, c);
        }
        for (int y = y0; y < y0 + BOX_H; y++) {
            put(x0, y, c);
            put(x0 + BOX_W - 1, y, c);
        }
    }
}

static size_t jpg_out(void *arg, size_t index, const void *data, size_t len)
{
    if (index + len > JPEG_CAP) return 0;
    memcpy(s_jpg + index, data, len);
    s_jpg_len = index + len;
    return len;
}

static bool token_ok(httpd_req_t *req)
{
    const char *want = CONFIG_DOORKEEPER_STREAM_TOKEN;
    char query[96], got[64];
    if (strlen(want) == 0) return false;  // 패스워드를 정하지 않았으면 열지 않는다
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK) return false;
    if (httpd_query_key_value(query, "token", got, sizeof(got)) != ESP_OK) return false;
    size_t n = strlen(want);
    if (strlen(got) != n) return false;
    uint8_t diff = 0;
    for (size_t i = 0; i < n; i++) diff |= want[i] ^ got[i];  // 길이가 같으면 끝까지 비교 (시간 차 노출 방지)
    return diff == 0;
}

// httpd는 요청 처리 태스크가 하나라 스트림 루프가 그 태스크를 붙잡으면 다른 요청(503 응답 포함)을 못 받는다.
// 그래서 요청을 비동기로 떼어 내 전용 태스크에서 보낸다. 태스크는 시작할 때 한 번만 만든다
static void stream_task(void *arg)
{
    while (true) {
        httpd_req_t *req;
        xQueueReceive(s_req_q, &req, portMAX_DELAY);
        ESP_LOGI(TAG, "시청 시작");
        httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=" BOUNDARY);
        httpd_resp_set_hdr(req, "Cache-Control", "no-store");
        uint32_t last = s_seq;
        esp_err_t res = ESP_OK;
        while (res == ESP_OK) {
            if (s_seq == last) {
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            xSemaphoreTake(s_lock, portMAX_DELAY);
            last = s_seq;
            render();
            s_jpg_len = 0;
            bool ok = fmt2jpg_cb(s_rgb, W * H * 2, W, H, PIXFORMAT_RGB565, 85, jpg_out, NULL);
            xSemaphoreGive(s_lock);
            if (!ok) continue;
            char part[96];
            int n = snprintf(part, sizeof(part),
                             "\r\n--" BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                             (unsigned)s_jpg_len);
            res = httpd_resp_send_chunk(req, part, n);
            if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)s_jpg, s_jpg_len);
        }
        httpd_req_async_handler_complete(req);
        s_busy = false;
        ESP_LOGI(TAG, "시청 종료");
    }
}

static esp_err_t stream_handler(httpd_req_t *req)
{
    if (!token_ok(req)) {
        httpd_resp_set_status(req, "401 Unauthorized");
        return httpd_resp_send(req, "invalid token", HTTPD_RESP_USE_STRLEN);
    }
    if (s_busy) {  // 동시 시청 1명
        httpd_resp_set_status(req, "503 Service Unavailable");
        return httpd_resp_send(req, "busy", HTTPD_RESP_USE_STRLEN);
    }
    httpd_req_t *async_req;
    if (httpd_req_async_handler_begin(req, &async_req) != ESP_OK) return ESP_FAIL;
    s_busy = true;
    xQueueSend(s_req_q, &async_req, 0);
    return ESP_OK;
}

void stream_start(void)
{
#if CONFIG_DOORKEEPER_LIVE_VIEW
    if (strlen(CONFIG_DOORKEEPER_STREAM_TOKEN) == 0) {
        ESP_LOGW(TAG, "조회 패스워드 미설정: 실시간 영상을 열지 않음");
        return;
    }
    // 버퍼는 초기화 때 한 번만 PSRAM에 잡는다
    s_pending = heap_caps_malloc(W * H, MALLOC_CAP_SPIRAM);
    s_gray = heap_caps_malloc(W * H, MALLOC_CAP_SPIRAM);
    s_rgb = heap_caps_malloc(W * H * 2, MALLOC_CAP_SPIRAM);
    s_jpg = heap_caps_malloc(JPEG_CAP, MALLOC_CAP_SPIRAM);
    s_lock = xSemaphoreCreateMutex();
    s_req_q = xQueueCreate(1, sizeof(httpd_req_t *));
    if (!s_pending || !s_gray || !s_rgb || !s_jpg || !s_lock || !s_req_q) {
        ESP_LOGE(TAG, "버퍼 할당 실패");
        s_lock = NULL;
        return;
    }
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 81;
    cfg.ctrl_port = 32769;
    cfg.max_open_sockets = 2;
    cfg.core_id = 0;             // 추론(코어 1)과 분리
    httpd_handle_t server;
    if (httpd_start(&server, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "httpd 시작 실패");
        return;
    }
    // JPEG 인코더가 스택을 많이 쓰므로 전송 태스크 스택을 넉넉히 (코어 0, 추론과 분리)
    xTaskCreatePinnedToCore(stream_task, "stream", 16 * 1024, NULL, 3, NULL, 0);
    httpd_uri_t uri = {.uri = "/stream", .method = HTTP_GET, .handler = stream_handler};
    httpd_register_uri_handler(server, &uri);
    ESP_LOGI(TAG, "실시간 영상 :81/stream 준비");
#endif
}
