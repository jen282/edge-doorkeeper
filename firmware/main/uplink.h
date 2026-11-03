// 서버 전송: 고정 크기 큐 → 1초마다(또는 8건) 배치 POST /v1/device/events. docs/06-communication-server.md
// 추론 태스크는 큐에 넣기만 하고 기다리지 않는다. 큐가 차면 가장 오래된 이벤트를 버리고 dropped를 늘린다.
#pragma once

#include <stdint.h>

typedef enum { UP_PASSAGE, UP_HEARTBEAT, UP_BOOT } uplink_kind_t;

typedef struct {
    uplink_kind_t kind;
    uint32_t seq;  // uplink_push가 채운다
    int64_t ts;    // epoch ms (재생 모드에서는 시나리오 시각)
    union {
        struct {
            int direction;  // +1 in, -1 out
            float conf;
        } passage;
        struct {
            float fps;
            uint32_t infer_ms_p50, infer_ms_max;
            uint32_t free_heap, min_free_heap, stack_hwm, arena_used;
            int rssi;
            char scenario_id[17];
        } hb;
        struct {
            char reset_reason[24];
        } boot;
    };
} uplink_event_t;

void uplink_init(uint32_t boot_id, const char *model_version, const char *mode);  // mode: replay | camera
void uplink_start(void);  // 전송 태스크 (코어 0)
void uplink_push(uplink_event_t *ev);
uint32_t uplink_dropped(void);
