#include "net.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "sdkconfig.h"

#define CONNECTED_BIT BIT0

static const char *TAG = "net";
static EventGroupHandle_t s_events;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        // 실패 사유 코드만 남긴다 (예: 201 AP 없음, 15·204 비밀번호/핸드셰이크, 202 인증 실패, 210 보안 방식 불일치)
        static uint8_t last_reason;
        static unsigned count;
        uint8_t reason = ((wifi_event_sta_disconnected_t *)data)->reason;
        if (reason != last_reason || ++count % 20 == 0) ESP_LOGW(TAG, "Wi-Fi 연결 실패 reason=%u", reason);
        last_reason = reason;
        xEventGroupClearBits(s_events, CONNECTED_BIT);
        esp_wifi_connect();  // 계속 재시도. 그동안 이벤트는 큐에 쌓이고, 차면 오래된 것부터 버린다
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "Wi-Fi 연결됨");
        xEventGroupSetBits(s_events, CONNECTED_BIT);
    }
}

bool net_start(int timeout_ms)
{
    if (strlen(CONFIG_DOORKEEPER_WIFI_SSID) == 0) {
        ESP_LOGW(TAG, "SSID 미설정: 네트워크 없이 동작 (firmware/sdkconfig.local)");
        return false;
    }
    // Wi-Fi 라이브러리 로그에는 SSID·IP가 찍히므로 경고 이상만 남긴다
    esp_log_level_set("wifi", ESP_LOG_WARN);
    esp_log_level_set("wifi_init", ESP_LOG_WARN);
    esp_log_level_set("esp_netif_handlers", ESP_LOG_WARN);

    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, CONFIG_DOORKEEPER_WIFI_SSID, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, CONFIG_DOORKEEPER_WIFI_PASSWORD, sizeof(cfg.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);  // 전송 지연을 줄인다 (전력·발열은 조금 늘어난다)

    // 진단: 대상 SSID가 보이는지만 기록한다 (이름은 남기지 않는다)
    static wifi_ap_record_t recs[20];
    uint16_t n = 20;
    wifi_scan_config_t scan = {0};
    if (esp_wifi_scan_start(&scan, true) == ESP_OK && esp_wifi_scan_get_ap_records(&n, recs) == ESP_OK) {
        int found = -1;
        for (int i = 0; i < n; i++) {
            if (strcmp((const char *)recs[i].ssid, CONFIG_DOORKEEPER_WIFI_SSID) == 0) found = i;
        }
        if (found >= 0) {
            ESP_LOGI(TAG, "주변 AP %u개, 대상 SSID 발견 (채널 %u, RSSI %d dBm, 보안 방식 %d)", n,
                     recs[found].primary, recs[found].rssi, recs[found].authmode);
        } else {
            ESP_LOGW(TAG, "주변 AP %u개, 대상 SSID 없음 (2.4GHz인지, 이름·대소문자, 안테나 연결 확인)", n);
        }
    }
    esp_wifi_connect();

    EventBits_t bits = xEventGroupWaitBits(s_events, CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    if (!(bits & CONNECTED_BIT)) ESP_LOGW(TAG, "Wi-Fi 연결 대기 시간 초과. 백그라운드에서 계속 재시도");
    return bits & CONNECTED_BIT;
}

bool net_connected(void) { return s_events && (xEventGroupGetBits(s_events) & CONNECTED_BIT); }

int net_rssi(void)
{
    wifi_ap_record_t ap;
    return (net_connected() && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : -127;
}
