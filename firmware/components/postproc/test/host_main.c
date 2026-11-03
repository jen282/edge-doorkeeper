// PC용 드라이버: 히트맵 파일을 읽어 C 후처리 결과를 출력한다 (tools/postproc_parity.py가 사용).
// 입력: int32 시나리오 수, 시나리오마다 int32 프레임 수 + float32 [프레임 수 * 9 * 12]
// 출력: "EVT <시나리오 번호> <in|out> <ts_offset_ms> <frame>"
#include <stdio.h>
#include <stdlib.h>

#include "postproc.h"

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s heatmaps.bin\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) return 2;
    int32_t n_scen;
    if (fread(&n_scen, 4, 1, f) != 1) return 2;
    pp_config_t cfg;
    pp_config_default(&cfg);
    float hm[PP_GRID_H * PP_GRID_W];
    pp_peak_t peaks[PP_MAX_PEAKS];
    pp_event_t ev[PP_MAX_TRACKS];
    for (int s = 0; s < n_scen; s++) {
        int32_t n_frames;
        if (fread(&n_frames, 4, 1, f) != 1) return 2;
        pp_counter_t ctr;
        pp_counter_init(&ctr, &cfg, 8.0);
        for (int k = 0; k < n_frames; k++) {
            if (fread(hm, sizeof(hm), 1, f) != 1) return 2;
            int np = pp_find_peaks(hm, &cfg, peaks, PP_MAX_PEAKS);
            int ne = pp_counter_update(&ctr, peaks, np, k, ev, PP_MAX_TRACKS);
            for (int e = 0; e < ne; e++) {
                printf("EVT %d %s %lld %d\n", s, ev[e].direction > 0 ? "in" : "out", (long long)ev[e].ts_offset_ms,
                       ev[e].frame);
            }
        }
    }
    fclose(f);
    return 0;
}
