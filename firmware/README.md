# firmware — XIAO ESP32-S3 Sense (ESP-IDF v5.3)

Flash 파티션 재생(또는 카메라) → TFLM int8 추론 → 후처리(피크·추적·라인 판정) → 서버 HTTP 배치 전송, 그리고 실시간 뷰(:81/stream).

- 설계: `docs/03-hardware.md`, `docs/04-model.md`
- 예정 구조: `main/`, `components/postproc/`, `partitions.csv`, `sdkconfig.defaults`, `CMakeLists.txt`
