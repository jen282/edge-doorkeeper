# CLAUDE.md

무인가게 출입 이상 감지 시스템 (edge-doorkeeper). XIAO ESP32-S3 Sense가 천장에서 **몇 명이 어느 방향으로 지나갔는지만** 판단하고(int8 온디바이스 추론), 서버가 모의 인증 기록과 대조해 동반입장·장기 체류를 감지해 대시보드에 표시한다.

설계의 기준 문서는 `docs/01-requirements.md` ~ `docs/12-schedule.md`이다. 작업 전에 관련 문서를 먼저 읽는다.

## 1. Git 규칙

### Claude를 공동 기여자로 넣지 않는다 (항상 적용)

- 커밋 메시지에 `Co-Authored-By: Claude ...` 같은 트레일러를 **절대 넣지 않는다**.
- 커밋 메시지, PR 본문, 이슈, 코드 주석 어디에도 다음을 넣지 않는다.
  - "Generated with Claude Code" 같은 생성 도구 문구
  - Claude 서명
  - `noreply@anthropic.com` 주소
- 커밋 작성자는 로컬 git 설정의 사용자 본인뿐이다.
- 이 규칙은 도구가 기본으로 제안하는 서명·공동 작성자 규칙보다 **우선**한다.

### 커밋·푸시

- 커밋과 푸시는 **사용자가 요청할 때만** 한다.
- 1인 프로젝트라 `main`에 직접 커밋해도 된다.
- 금지 사항 (사용자가 명시적으로 요청한 경우만 예외):
  - `--force` 푸시
  - 히스토리 재작성 (rebase, amend로 이미 푸시한 커밋 변경)
  - 브랜치 삭제
- 한 커밋에는 하나의 논리적 변경만 담는다.
- 커밋 메시지 형식: `type(scope): 한국어 요약`
  - type: `feat`, `fix`, `docs`, `test`, `ci`, `refactor`, `chore`
  - scope: `sim`, `ml`, `firmware`, `server`, `tools`, `docs`, `ci` (여러 영역이면 생략)
  - 예: `feat(server): 출입 세션 기반 동반입장 규칙 추가`
- 테스트 세트 생성기를 바꿔 `sim/test_set.sha256`이 달라지면 **이유를 커밋 메시지 본문에** 적는다.

### 커밋하지 않는 것

| 대상 | 이유 |
|---|---|
| `firmware/sdkconfig`, Wi-Fi 비밀번호, 기기·스트림 토큰, `.env` | 비밀값. 기본값은 비밀값 없이 `sdkconfig.defaults`에만 둔다 |
| `data/`, `report/`, `frames.bin`, 학습 체크포인트 | 생성물. CI가 고정 시드로 다시 만든다 |
| `*.db`, `*.sqlite` | 로컬 실행 데이터 |
| `build/`, `managed_components/`, `.venv/`, `__pycache__/` | 빌드·환경 산출물 |

`ml/model.tflite`, `ml/model_float.tflite`, `sim/test_set.sha256`은 **커밋한다**. CI가 학습 없이 이 파일들을 평가한다.

## 2. 절대 원칙 (개인정보)

설계 결정이다. 바꾸려면 먼저 사용자와 합의하고 `docs/02-architecture.md` 1.6절과 `docs/01-requirements.md` 5절 결정 이력을 갱신한다.

- 이미지·영상을 **서버로 보내거나 DB에 저장하지 않는다**. 실시간 뷰는 ESP32 → 브라우저 직결만 허용하고, 녹화 기능은 만들지 않는다.
- DB에 BLOB 컬럼을 만들지 않는다. 서버 스키마는 `extra="forbid"`로 정의되지 않은 필드를 거부한다.
- 얼굴 인식, 외형 재식별, 동일인 매칭을 하지 않는다. 트랙 번호는 기기 안에서만 쓰고 서버로 보내지 않는다.
- 인증 기록에 회원 ID·카드번호 같은 식별자를 저장하지 않는다.
- 보드의 마이크(PDM)를 초기화하지 않는다 (녹음 금지).

## 3. 인터페이스 계약 (한쪽을 바꾸면 반드시 함께 수정)

| 계약 | 함께 바뀌는 곳 |
|---|---|
| 모델 입출력: 입력 `[1,72,96,1]` int8, 출력 `[1,9,12,1]` int8 | `ml/` 학습·변환, `firmware/` 전처리·후처리, `ml/gates.yaml` |
| 모델 연산자: CONV_2D, DEPTHWISE_CONV_2D, LOGISTIC | 펌웨어 `MicroMutableOpResolver`, `ml/gates.yaml` |
| 후처리 알고리즘 (피크, 추적, 라인 히스테리시스) | `ml/postproc.py`(CI 평가용)와 `firmware/components/postproc`(C) |
| 기기 → 서버 JSON 스키마 | 펌웨어 직렬화, `server/schemas.py`, `docs/06-communication-server.md` |
| `event_id = device_id:boot_id:seq`, 시각은 epoch ms(UTC) | 펌웨어, 서버, DB. 화면에 표시할 때만 KST로 변환 |
| Flash 파티션 표 (frames 0x210000, 5MB) | `firmware/partitions.csv`, `tools/pack_frames.py` |

## 4. 개발 환경과 명령

- **보드**: XIAO ESP32-S3 Sense, 포트 `/dev/cu.usbmodem101` (재연결하면 바뀔 수 있음)
- **ESP-IDF v5.3**: 셸에서 `get_idf`로 활성화

  ```bash
  get_idf
  cd firmware
  idf.py set-target esp32s3        # 최초 1회
  idf.py build
  idf.py -p /dev/cu.usbmodem101 flash monitor
  parttool.py -p /dev/cu.usbmodem101 write_partition --partition-name frames --input ../data/frames.bin
  ```

- **Python 3.12** (CI와 동일. 시스템의 3.14는 쓰지 않는다)

  ```bash
  python3.12 -m venv .venv && source .venv/bin/activate
  pip install -r sim/requirements.txt -r ml/requirements.txt -r server/requirements.txt -r server/requirements-dev.txt
  pytest -q ml/tests server/tests
  uvicorn server.app:app --host 0.0.0.0 --port 8000
  ```

- **하드웨어 작업은 실행 전에 사용자에게 알린다**: 보드 플래시, 파티션 쓰기, 시리얼 모니터 점유.

## 5. 코드 작성 규칙

- **언어**: 코드 식별자는 영어, 주석과 문서는 한국어.
- **주석**: "무엇"보다 "왜"를 쓴다.
- **Python**:
  - ruff를 통과해야 한다.
  - 공개 함수에는 타입 힌트를 단다.
  - 규칙 엔진은 HTTP와 분리된 순수 함수로 두고, 현재 시각(`now`)을 인자로 받는다.
- **C (펌웨어)**:
  - 초기화 이후에는 동적 할당을 하지 않는다. 루프 안에서 malloc을 쓰지 않는다.
  - 추론은 코어 1, Wi-Fi와 httpd는 코어 0에서 돌린다.
  - 태스크 워치독을 끄지 않는다.
- **SQL**: 표준에 가깝게 쓴다 (나중에 PostgreSQL로 옮길 수 있도록). 시각 컬럼은 `INTEGER` epoch ms.
- 기존 코드의 관습(이름, 구조, 주석 밀도)을 따른다.

## 6. 작업 방식

- **우선순위**: `docs/12-schedule.md`의 시간 상자와 축소 순서를 따른다. 필수 조건 3개를 먼저 끝낸다.
  - 온디바이스 추론
  - DB 저장과 규칙
  - CI 게이트
- **수치 표기**: 측정값과 추정값을 구분해서 쓴다. 추정치에는 "추정"을 붙인다.
- **결과 보고**: 테스트·게이트가 실패하거나 목표에 못 미치면 숨기지 않고 수치 그대로 보고한다. 게이트 기준을 낮춰서 통과시키지 않는다.
- **설계 변경**: 해당 `docs/` 문서와 `docs/01-requirements.md` 5절 결정 이력을 같은 작업에서 갱신한다.
