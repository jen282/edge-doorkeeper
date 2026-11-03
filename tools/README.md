# tools — 보조 도구

- `pack_frames.py`: 시나리오 프레임을 Flash 파티션용 `frames.bin`으로 패키징 (`docs/03-hardware.md` 1.4절)
- `auth_injector.py`: 시나리오의 모의 인증 이벤트를 서버로 전송
- `e2e_replay.py`: PC에서 모델·후처리로 시나리오를 재생해 서버 규칙을 검증 (CI `e2e` 잡)
