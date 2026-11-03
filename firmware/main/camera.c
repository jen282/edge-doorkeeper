#include "camera.h"

#include "esp_camera.h"
#include "esp_log.h"
#include "sdkconfig.h"

// 반전 설정은 카메라 빌드에서만 정의된다 (재생 빌드에서도 이 파일은 컴파일된다)
#ifndef CONFIG_DOORKEEPER_CAMERA_HMIRROR
#define CONFIG_DOORKEEPER_CAMERA_HMIRROR 0
#endif
#ifndef CONFIG_DOORKEEPER_CAMERA_VFLIP
#define CONFIG_DOORKEEPER_CAMERA_VFLIP 0
#endif

#define SRC_W 320
#define SRC_H 240
#define DST_W 96
#define DST_H 72

static const char *TAG = "camera";
static uint16_t s_x0[DST_W + 1], s_y0[DST_H + 1];  // 축소 구간 경계 (320/96 = 240/72 = 10/3)

bool camera_init(void)
{
    // 핀 배치: Seeed XIAO ESP32-S3 Sense 카메라 확장 보드
    camera_config_t cfg = {
        .pin_pwdn = -1, .pin_reset = -1, .pin_xclk = 10, .pin_sccb_sda = 40, .pin_sccb_scl = 39,
        .pin_d7 = 48, .pin_d6 = 11, .pin_d5 = 12, .pin_d4 = 14, .pin_d3 = 16, .pin_d2 = 18, .pin_d1 = 17,
        .pin_d0 = 15, .pin_vsync = 38, .pin_href = 47, .pin_pclk = 13,
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_GRAYSCALE,
        .frame_size = FRAMESIZE_QVGA,
        .fb_count = 2,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,  // 처리가 늦어도 가장 최근 프레임을 쓴다
    };
    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "카메라 초기화 실패: %s", esp_err_to_name(err));
        return false;
    }
    sensor_t *s = esp_camera_sensor_get();
    s->set_hmirror(s, CONFIG_DOORKEEPER_CAMERA_HMIRROR);
    s->set_vflip(s, CONFIG_DOORKEEPER_CAMERA_VFLIP);
    for (int i = 0; i <= DST_W; i++) s_x0[i] = i * SRC_W / DST_W;
    for (int i = 0; i <= DST_H; i++) s_y0[i] = i * SRC_H / DST_H;
    // 센서는 보드 리비전에 따라 OV2640 또는 OV3660 (실측: OV3660). 드라이버가 자동 인식한다
    ESP_LOGI(TAG, "센서 PID 0x%x, 회색조 %dx%d → %dx%d (좌우 반전 %d, 상하 반전 %d)", s->id.PID, SRC_W, SRC_H,
             DST_W, DST_H, CONFIG_DOORKEEPER_CAMERA_HMIRROR, CONFIG_DOORKEEPER_CAMERA_VFLIP);
    return true;
}

bool camera_read(int8_t *dst)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) return false;
    if (fb->width != SRC_W || fb->height != SRC_H || fb->format != PIXFORMAT_GRAYSCALE) {
        esp_camera_fb_return(fb);
        return false;
    }
    // 영역 평균 축소: 합성 데이터가 서브픽셀 평균으로 그려진 것과 같은 성질
    for (int oy = 0; oy < DST_H; oy++) {
        for (int ox = 0; ox < DST_W; ox++) {
            uint32_t sum = 0, n = 0;
            for (int y = s_y0[oy]; y < s_y0[oy + 1]; y++) {
                const uint8_t *row = fb->buf + y * SRC_W;
                for (int x = s_x0[ox]; x < s_x0[ox + 1]; x++) sum += row[x];
                n += s_x0[ox + 1] - s_x0[ox];
            }
            dst[oy * DST_W + ox] = (int8_t)((int)((sum + n / 2) / n) - 128);
        }
    }
    esp_camera_fb_return(fb);
    return true;
}
