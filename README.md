# 무인가게 출입 이상 탐지 설계

천장의 XIAO ESP32-S3 Sense가 **누구인지가 아니라 몇 명이 어느 방향으로 지나갔는지만** 온디바이스(int8)로 판단한다. 서버는 그 결과를 모의 출입 인증 기록과 대조해 동반입장·장기 체류를 감지하고, 대시보드에 표시한다.

## 폴더 구조

| 경로 | 내용 |
|---|---|
| `docs/` | 설계 문서 01~12 (요구사항 → 일정) |
| `sim/` | 합성 천장뷰 시나리오 생성기 |
| `ml/` | 학습, int8 양자화, 평가 게이트, 후처리(Python) |
| `firmware/` | ESP-IDF v5.3 펌웨어 (추론, 후처리(C), 전송, 실시간 뷰) |
| `server/` | FastAPI + SQLite + 규칙 엔진 + 대시보드 |
| `tools/` | 프레임 패키징, 모의 인증 주입기, 종단(e2e) 재생 |
| `.github/workflows/` | CI (`docs/09-ci.md`) |

## 실행 (시연)

```bash
tools/flash.sh replay     # 모드를 바꿀 때만: 펌웨어·프레임 쓰기 (replay | camera)
tools/demo.sh replay      # 매번: 서버(새 DB) → 인증 주입 → 보드 리셋. 대시보드 http://localhost:8000
```

Wi-Fi 정보와 토큰은 커밋하지 않는 `firmware/sdkconfig.local`에 둔다. 결과는 `docs/results.md`.
