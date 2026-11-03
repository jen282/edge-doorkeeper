// XIAO ESP32-S3 Sense 카메라(OV2640/OV3660): 회색조 QVGA(320x240)를 찍어 모델 입력 96x72로 줄인다.
#pragma once

#include <stdbool.h>
#include <stdint.h>

bool camera_init(void);
// 최신 프레임을 영역 평균으로 축소해 모델 입력 형식(int8 = uint8 - 128)으로 dst[72*96]에 쓴다
bool camera_read(int8_t *dst);
