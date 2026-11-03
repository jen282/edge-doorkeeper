// TFLite Micro int8 히트맵 모델. 입력 [1,72,96,1] int8, 출력 [1,9,12,1] int8 (ml/gates.yaml 계약)
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

bool model_init(void);
int8_t *model_input(void);
// 출력 히트맵을 확률로 역양자화해 hm[9*12]에 쓴다
bool model_invoke(float *hm);
size_t model_arena_used(void);

#ifdef __cplusplus
}
#endif
