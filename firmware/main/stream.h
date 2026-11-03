// 실시간 영상: 모델이 보는 96x72 프레임에 박스·가상 라인을 그려 MJPEG(:81/stream)로 브라우저에 직접 보낸다.
// 서버를 거치지 않고, 최신 1프레임만 메모리에 두며 녹화하지 않는다. docs/04-model.md 1.9절
#pragma once

#include <stdint.h>

#define STREAM_MAX_BOXES 4

typedef enum { BOX_TRACKING, BOX_IN, BOX_OUT } stream_box_state_t;

typedef struct {
    float x, y;  // 머리 중심 (96x72 좌표)
    stream_box_state_t state;
} stream_box_t;

void stream_start(void);
// 추론 '전에' 호출한다. TFLM은 추론 중에 입력 텐서 메모리를 중간 결과로 재사용하므로 추론 뒤에는 프레임이 남아 있지 않다
void stream_capture(const int8_t *frame);
// 후처리 뒤에 호출한다. 직전에 캡처한 프레임과 박스를 한 장으로 확정한다 (6.9KB 복사만 하고 바로 돌아온다)
void stream_publish(const stream_box_t *boxes, int n_boxes);
