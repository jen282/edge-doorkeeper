#include "postproc.h"

#include <math.h>
#include <string.h>

#define LINE_A 24.0
#define LINE_MID 36.0
#define LINE_C 48.0

void pp_config_default(pp_config_t *cfg)
{
    cfg->threshold = 0.5;
    cfg->merge_cells = 1.5;
    cfg->gate_cells = 2.5;
    cfg->gate_init_cells = 3.5;
    cfg->max_tracks = PP_MAX_TRACKS;
    cfg->max_misses = 2;
}

int pp_zone(double y)
{
    return y < LINE_A ? PP_ZONE_A : (y > LINE_C ? PP_ZONE_C : PP_ZONE_B);
}

typedef struct {
    double x, y, p;
    int i, j;
} cand_t;

// Python의 정렬 키 (-점수, 행, 열)
static bool cand_before(const cand_t *a, const cand_t *b)
{
    if (a->p != b->p) return a->p > b->p;
    if (a->i != b->i) return a->i < b->i;
    return a->j < b->j;
}

int pp_find_peaks(const float *hm, const pp_config_t *cfg, pp_peak_t *out, int max_out)
{
    cand_t cands[PP_GRID_H * PP_GRID_W];
    int nc = 0;
    for (int i = 0; i < PP_GRID_H; i++) {
        for (int j = 0; j < PP_GRID_W; j++) {
            double p = (double)hm[i * PP_GRID_W + j];
            double win[3][3];
            double wmax = -1.0;
            for (int di = -1; di <= 1; di++) {
                for (int dj = -1; dj <= 1; dj++) {
                    int ii = i + di, jj = j + dj;
                    bool in = ii >= 0 && ii < PP_GRID_H && jj >= 0 && jj < PP_GRID_W;
                    double v = in ? (double)hm[ii * PP_GRID_W + jj] : -1.0;  // 패딩 -1
                    win[di + 1][dj + 1] = v;
                    if (v > wmax) wmax = v;
                }
            }
            if (p < cfg->threshold || p < wmax) continue;
            // 3x3 가중 평균으로 칸 안의 위치 보정 (음수 패딩은 가중치 0)
            double row[3] = {0}, col[3] = {0}, s = 0.0;
            for (int r = 0; r < 3; r++) {
                for (int c = 0; c < 3; c++) {
                    double w = win[r][c] > 0.0 ? win[r][c] : 0.0;
                    row[r] += w;
                    col[c] += w;
                    s += w;
                }
            }
            double dy = (-row[0] + row[2]) / s;
            double dx = (-col[0] + col[2]) / s;
            cand_t c = {(j + 0.5 + dx) * PP_STRIDE, (i + 0.5 + dy) * PP_STRIDE, p, i, j};
            int k = nc++;
            while (k > 0 && cand_before(&c, &cands[k - 1])) {  // 삽입 정렬 (최대 108개)
                cands[k] = cands[k - 1];
                k--;
            }
            cands[k] = c;
        }
    }
    double lim = cfg->merge_cells * PP_STRIDE;
    int n = 0;
    for (int k = 0; k < nc && n < max_out; k++) {
        bool keep = true;
        for (int q = 0; q < n; q++) {
            if (hypot(cands[k].x - out[q].x, cands[k].y - out[q].y) <= lim) {
                keep = false;
                break;
            }
        }
        if (keep) out[n++] = (pp_peak_t){cands[k].x, cands[k].y, cands[k].p};
    }
    return n;
}

void pp_counter_init(pp_counter_t *c, const pp_config_t *cfg, double fps)
{
    memset(c, 0, sizeof(*c));
    c->cfg = *cfg;
    c->frame_ms = 1000.0 / fps;
}

typedef struct {
    int n_tracks, n_peaks;
    bool feasible[PP_MAX_TRACKS][PP_MAX_PEAKS];
    double cost[PP_MAX_TRACKS][PP_MAX_PEAKS];
    bool used[PP_MAX_PEAKS];
    int cur[PP_MAX_TRACKS];
    int best_n;
    double best_total;
    int best[PP_MAX_TRACKS];
} search_t;

// 매칭 수 최대 → 거리 합 최소. Python 구현과 같은 깊이 우선 순서라 동점이면 먼저 찾은 해가 남는다
static void search(search_t *s, int ti, int n, double total)
{
    if (ti == s->n_tracks) {
        if (n > s->best_n || (n == s->best_n && total < s->best_total - 1e-9)) {
            s->best_n = n;
            s->best_total = total;
            memcpy(s->best, s->cur, sizeof(s->best));
        }
        return;
    }
    for (int pi = 0; pi < s->n_peaks; pi++) {
        if (s->feasible[ti][pi] && !s->used[pi]) {
            s->used[pi] = true;
            s->cur[ti] = pi;
            search(s, ti + 1, n + 1, total + s->cost[ti][pi]);
            s->cur[ti] = -1;
            s->used[pi] = false;
        }
    }
    search(s, ti + 1, n, total);
}

int pp_counter_update(pp_counter_t *c, const pp_peak_t *peaks, int n_peaks, int frame_idx, pp_event_t *out, int max_out)
{
    if (n_peaks > PP_MAX_PEAKS) n_peaks = PP_MAX_PEAKS;
    double t_ms = frame_idx * c->frame_ms;

    static search_t s;  // 스택 대신 정적 영역 (재진입하지 않는다)
    memset(&s, 0, sizeof(s));
    s.n_tracks = c->n_tracks;
    s.n_peaks = n_peaks;
    for (int ti = 0; ti < c->n_tracks; ti++) {
        const pp_track_t *tr = &c->tracks[ti];
        int k = tr->misses + 1;
        double px = tr->x + tr->vx * k, py = tr->y + tr->vy * k;
        double gate = (tr->observed > 1 ? c->cfg.gate_cells : c->cfg.gate_init_cells) * PP_STRIDE;
        for (int pi = 0; pi < n_peaks; pi++) {
            double d = hypot(peaks[pi].x - px, peaks[pi].y - py);
            s.cost[ti][pi] = d;
            s.feasible[ti][pi] = d <= gate;
        }
        s.cur[ti] = -1;
        s.best[ti] = -1;
    }
    search(&s, 0, 0, 0.0);

    int n_ev = 0;
    bool matched[PP_MAX_PEAKS] = {false};
    for (int ti = 0; ti < c->n_tracks; ti++) {
        pp_track_t *tr = &c->tracks[ti];
        int pi = s.best[ti];
        if (pi < 0) {
            tr->misses++;
            continue;
        }
        matched[pi] = true;
        double x = peaks[pi].x, y = peaks[pi].y;
        int k = tr->misses + 1;
        if (tr->observed == 1) {
            tr->vx = (x - tr->x) / k;
            tr->vy = (y - tr->y) / k;
        } else {
            tr->vx = 0.5 * tr->vx + 0.5 * (x - tr->x) / k;
            tr->vy = 0.5 * tr->vy + 0.5 * (y - tr->y) / k;
        }
        if ((tr->y - LINE_MID) * (y - LINE_MID) <= 0 && y != tr->y) {
            tr->mid_t_ms = tr->t_ms + (LINE_MID - tr->y) / (y - tr->y) * (t_ms - tr->t_ms);
            tr->has_mid = true;
        }
        tr->x = x;
        tr->y = y;
        tr->t_ms = t_ms;
        tr->misses = 0;
        tr->observed++;
        int z = pp_zone(y);
        if (z != PP_ZONE_B && z != tr->start) {
            if (n_ev < max_out) {
                // nearbyint: 기본 반올림 모드(짝수 쪽)라 Python round()와 같다
                out[n_ev++] = (pp_event_t){tr->tid, tr->start == PP_ZONE_A ? 1 : -1,
                                           (int64_t)nearbyint(tr->has_mid ? tr->mid_t_ms : t_ms), frame_idx,
                                           peaks[pi].score};
            }
            tr->start = z;
        }
    }

    int kept = 0;
    for (int ti = 0; ti < c->n_tracks; ti++) {
        if (c->tracks[ti].misses < c->cfg.max_misses) c->tracks[kept++] = c->tracks[ti];
    }
    c->n_tracks = kept;

    for (int pi = 0; pi < n_peaks; pi++) {  // 점수 순이라 트랙 수가 차면 약한 피크부터 버려진다
        if (matched[pi] || c->n_tracks >= c->cfg.max_tracks) continue;
        double y = peaks[pi].y;
        int z = pp_zone(y);
        pp_track_t *tr = &c->tracks[c->n_tracks++];
        memset(tr, 0, sizeof(*tr));
        tr->tid = c->next_id++;
        tr->x = peaks[pi].x;
        tr->y = y;
        tr->t_ms = t_ms;
        tr->start = z != PP_ZONE_B ? z : (y < LINE_MID ? PP_ZONE_A : PP_ZONE_C);
        tr->observed = 1;
    }
    return n_ev;
}
