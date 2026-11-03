// Flash 'frames' 파티션의 재생용 프레임 묶음. 형식은 tools/pack_frames.py 참고.
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    char id[17];
    int64_t start_ms;
    uint32_t n_frames;
    uint32_t offset;
    uint32_t idle_gap_s;
} frame_scenario_t;

bool frames_open(void);
int frames_count(void);
int frames_fps(void);
const frame_scenario_t *frames_scenario(int idx);
// 프레임을 모델 입력 형식(int8 = uint8 - 128)으로 dst에 읽는다. dst는 72*96 바이트
bool frames_read(int scenario, int frame, int8_t *dst);
