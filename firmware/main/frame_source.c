#include "frame_source.h"

#include <string.h>

#include "esp_log.h"
#include "esp_partition.h"

#define FRAME_W 96
#define FRAME_H 72
#define MAX_SCENARIOS 32

static const char *TAG = "frames";

typedef struct __attribute__((packed)) {
    char magic[4];
    uint16_t version, width, height, fps;
    uint32_t n_scenarios, reserved;
} header_t;

typedef struct __attribute__((packed)) {
    char id[16];
    int64_t start_ms;
    uint32_t n_frames, offset, idle_gap_s, reserved;
} entry_t;

static const esp_partition_t *s_part;
static header_t s_hdr;
static frame_scenario_t s_scen[MAX_SCENARIOS];

bool frames_open(void)
{
    s_part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "frames");
    if (!s_part) {
        ESP_LOGE(TAG, "frames 파티션 없음");
        return false;
    }
    if (esp_partition_read(s_part, 0, &s_hdr, sizeof(s_hdr)) != ESP_OK || memcmp(s_hdr.magic, "EDFR", 4) != 0 ||
        s_hdr.version != 1 || s_hdr.width != FRAME_W || s_hdr.height != FRAME_H || s_hdr.n_scenarios == 0 ||
        s_hdr.n_scenarios > MAX_SCENARIOS) {
        ESP_LOGE(TAG, "frames.bin 헤더가 올바르지 않음 (parttool.py로 써 넣었는지 확인)");
        return false;
    }
    for (uint32_t i = 0; i < s_hdr.n_scenarios; i++) {
        entry_t e;
        if (esp_partition_read(s_part, sizeof(header_t) + i * sizeof(entry_t), &e, sizeof(e)) != ESP_OK) return false;
        memcpy(s_scen[i].id, e.id, 16);
        s_scen[i].id[16] = '\0';
        s_scen[i].start_ms = e.start_ms;
        s_scen[i].n_frames = e.n_frames;
        s_scen[i].offset = e.offset;
        s_scen[i].idle_gap_s = e.idle_gap_s;
    }
    return true;
}

int frames_count(void) { return (int)s_hdr.n_scenarios; }

int frames_fps(void) { return s_hdr.fps; }

const frame_scenario_t *frames_scenario(int idx) { return &s_scen[idx]; }

bool frames_read(int scenario, int frame, int8_t *dst)
{
    const frame_scenario_t *s = &s_scen[scenario];
    size_t n = FRAME_W * FRAME_H;
    if (esp_partition_read(s_part, s->offset + (size_t)frame * n, dst, n) != ESP_OK) return false;
    uint8_t *p = (uint8_t *)dst;
    for (size_t i = 0; i < n; i++) p[i] ^= 0x80;  // uint8 - 128 (모델 입력 scale 1/255, zero point -128)
    return true;
}
