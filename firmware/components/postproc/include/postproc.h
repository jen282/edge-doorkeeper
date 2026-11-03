// 후처리: 히트맵 → 머리 피크 → 추적 → 라인 통과 이벤트.
// ml/postproc.py와 같은 알고리즘이다(CI 평가는 Python 구현으로 한다). 동점 처리 순서까지 맞춘다.
// 모든 상태는 호출자가 가진 구조체 안에 있고 동적 할당이 없다.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PP_GRID_H 9
#define PP_GRID_W 12
#define PP_STRIDE 8
#define PP_MAX_TRACKS 4
#define PP_MAX_PEAKS 16  // Python 구현은 상한이 없다. 정상 입력에서는 피크가 수 개라 차이가 나지 않는다

enum { PP_ZONE_A = 0, PP_ZONE_B = 1, PP_ZONE_C = 2 };

typedef struct {
    double threshold;        // 피크 최소 확률
    double merge_cells;      // 이 거리(칸) 이내 피크는 병합
    double gate_cells;       // 속도를 아는 트랙의 매칭 반경
    double gate_init_cells;  // 새 트랙의 매칭 반경 (빠른 보행은 프레임당 약 23px)
    int max_tracks;
    int max_misses;
} pp_config_t;

typedef struct {
    double x, y, score;
} pp_peak_t;

typedef struct {
    int tid;
    double x, y, t_ms;  // 마지막 관측
    int start;          // 계수 기준 구역 (A 또는 C)
    double vx, vy;      // 프레임당 이동량
    int misses;
    int observed;
    bool has_mid;
    double mid_t_ms;    // 가운데 선을 마지막으로 지난 시각
} pp_track_t;

typedef struct {
    pp_config_t cfg;
    double frame_ms;
    pp_track_t tracks[PP_MAX_TRACKS];
    int n_tracks;
    int next_id;
} pp_counter_t;

typedef struct {
    int track;
    int direction;  // +1 입장(IN), -1 퇴장(OUT)
    int64_t ts_offset_ms;
    int frame;
    double score;  // 계수 순간의 피크 확률 (서버의 conf)
} pp_event_t;

void pp_config_default(pp_config_t *cfg);
int pp_zone(double y);
// hm: 확률 [9*12] 행 우선. 반환: 피크 수(점수 내림차순)
int pp_find_peaks(const float *hm, const pp_config_t *cfg, pp_peak_t *out, int max_out);
void pp_counter_init(pp_counter_t *c, const pp_config_t *cfg, double fps);
// 반환: 이번 프레임에서 생긴 이벤트 수
int pp_counter_update(pp_counter_t *c, const pp_peak_t *peaks, int n_peaks, int frame_idx, pp_event_t *out, int max_out);

#ifdef __cplusplus
}
#endif
