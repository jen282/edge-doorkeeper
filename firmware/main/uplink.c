#include "uplink.h"

#include <stdio.h>
#include <string.h>

#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "net.h"
#include "sdkconfig.h"

#define QUEUE_LEN 64
#define BATCH_MAX 12       // 12건이면 본문 약 4KB (서버 한도 16KB)
#define FLUSH_COUNT 8
#define FLUSH_MS 1000
#define BACKOFF_MAX_MS 10000
#define FW_VERSION "0.2.0"

static const char *TAG = "uplink";
static QueueHandle_t s_q;
static uint32_t s_boot_id;
static const char *s_model_version;
static const char *s_mode;
static uint32_t s_seq;
static volatile uint32_t s_dropped;
static uint32_t s_rejected;

void uplink_init(uint32_t boot_id, const char *model_version, const char *mode)
{
    s_boot_id = boot_id;
    s_model_version = model_version;
    s_mode = mode;
    s_q = xQueueCreate(QUEUE_LEN, sizeof(uplink_event_t));  // 초기화 때 한 번만 할당
}

void uplink_push(uplink_event_t *ev)
{
    ev->seq = __atomic_fetch_add(&s_seq, 1, __ATOMIC_RELAXED);
    if (xQueueSend(s_q, ev, 0) != pdTRUE) {
        uplink_event_t old;
        xQueueReceive(s_q, &old, 0);  // 가장 오래된 것을 버린다
        xQueueSend(s_q, ev, 0);
        s_dropped++;
    }
}

uint32_t uplink_dropped(void) { return s_dropped; }

static int write_event(char *buf, size_t cap, const uplink_event_t *e)
{
    switch (e->kind) {
    case UP_PASSAGE:
        return snprintf(buf, cap, "{\"kind\":\"passage\",\"seq\":%u,\"ts\":%lld,\"direction\":\"%s\",\"conf\":%.3f}",
                        (unsigned)e->seq, (long long)e->ts, e->passage.direction > 0 ? "in" : "out",
                        e->passage.conf);
    case UP_HEARTBEAT:
        return snprintf(buf, cap,
                        "{\"kind\":\"heartbeat\",\"seq\":%u,\"ts\":%lld,\"fps\":%.2f,\"infer_ms_p50\":%u,"
                        "\"infer_ms_max\":%u,\"free_heap\":%u,\"min_free_heap\":%u,\"stack_hwm\":%u,"
                        "\"arena_used\":%u,\"dropped\":%u,\"wifi_rssi\":%d,\"mode\":\"%s\",\"scenario_id\":%s%s%s}",
                        (unsigned)e->seq, (long long)e->ts, e->hb.fps, (unsigned)e->hb.infer_ms_p50,
                        (unsigned)e->hb.infer_ms_max, (unsigned)e->hb.free_heap, (unsigned)e->hb.min_free_heap,
                        (unsigned)e->hb.stack_hwm, (unsigned)e->hb.arena_used, (unsigned)s_dropped, e->hb.rssi, s_mode,
                        // 카메라 모드는 시나리오가 없으므로 null
                        e->hb.scenario_id[0] ? "\"" : "null", e->hb.scenario_id, e->hb.scenario_id[0] ? "\"" : "");
    case UP_BOOT:
        return snprintf(buf, cap, "{\"kind\":\"boot\",\"seq\":%u,\"ts\":%lld,\"reset_reason\":\"%s\"}",
                        (unsigned)e->seq, (long long)e->ts, e->boot.reset_reason);
    }
    return 0;
}

static int serialize(char *buf, size_t cap, const uplink_event_t *ev, int n)
{
    int len = snprintf(buf, cap, "{\"device_id\":\"%s\",\"boot_id\":%u,\"fw_version\":\"%s\",\"model_version\":\"%s\","
                       "\"events\":[", CONFIG_DOORKEEPER_DEVICE_ID, (unsigned)s_boot_id, FW_VERSION, s_model_version);
    for (int i = 0; i < n; i++) {
        if (i) buf[len++] = ',';
        len += write_event(buf + len, cap - len, &ev[i]);
    }
    len += snprintf(buf + len, cap - len, "]}");
    return len;
}

static void uplink_task(void *arg)
{
    static uplink_event_t pending[BATCH_MAX];
    static char body[BATCH_MAX * 360 + 256];
    int n = 0;
    uint32_t backoff = 1000;

    char url[160];
    snprintf(url, sizeof(url), "%s/v1/device/events", CONFIG_DOORKEEPER_SERVER_URL);
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 2000,
        .keep_alive_enable = true,  // 연결 하나를 재사용 (힙 단편화 방지)
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "X-Device-Token", CONFIG_DOORKEEPER_DEVICE_TOKEN);

    while (true) {
        // 1초마다 또는 8건이 모이면 보낸다. 실패한 배치(pending)는 그대로 들고 재시도한다
        TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(FLUSH_MS);
        while (n < FLUSH_COUNT) {
            TickType_t now = xTaskGetTickCount();
            TickType_t wait = deadline > now ? deadline - now : 0;
            if (xQueueReceive(s_q, &pending[n], wait) != pdTRUE) break;
            n++;
        }
        while (n < BATCH_MAX && xQueueReceive(s_q, &pending[n], 0) == pdTRUE) n++;
        if (n == 0) continue;
        if (!net_connected()) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        int len = serialize(body, sizeof(body), pending, n);
        esp_http_client_set_post_field(client, body, len);
        esp_err_t err = esp_http_client_perform(client);
        int status = err == ESP_OK ? esp_http_client_get_status_code(client) : 0;
        if (status == 200) {
            n = 0;
            backoff = 1000;
        } else if (status >= 400 && status < 500) {
            // 다시 보내도 실패하므로 버린다 (무한 재시도 방지)
            s_rejected++;
            ESP_LOGE(TAG, "배치 거부 HTTP %d (누적 %u)", status, (unsigned)s_rejected);
            n = 0;
        } else {
            ESP_LOGW(TAG, "전송 실패 (%s, HTTP %d). %ums 뒤 재시도", esp_err_to_name(err), status, (unsigned)backoff);
            vTaskDelay(pdMS_TO_TICKS(backoff));
            backoff = backoff * 2 > BACKOFF_MAX_MS ? BACKOFF_MAX_MS : backoff * 2;
        }
    }
}

void uplink_start(void)
{
    xTaskCreatePinnedToCore(uplink_task, "uplink", 8 * 1024, NULL, 4, NULL, 0);
}
