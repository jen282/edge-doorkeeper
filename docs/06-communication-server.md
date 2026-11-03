# 06. 통신 및 서버 설계

## 1. 결정 사항

### 1.1 프로토콜: HTTP/1.1 + JSON (LAN), 배치 전송

| 항목 | 결정 |
|---|---|
| ESP32 → 서버 | `POST /v1/device/events`. 여러 종류의 이벤트를 배치로 보내며 최대 32건 |
| 전송 주기 | 1초마다, 또는 큐에 8건이 쌓이면 즉시 |
| 연결 | `esp_http_client` 핸들 1개를 재사용 (keep-alive), 타임아웃 2초 |
| 기기 인증 | `X-Device-Token` 헤더. 토큰은 `menuconfig` 로컬 설정에 두고 커밋하지 않음 |
| 서버 주소 | `CONFIG_DOORKEEPER_SERVER_URL` (예: `http://192.168.0.10:8000`) |
| 서버 구성 | FastAPI + uvicorn 워커 1개 (SQLite 쓰기를 한 곳에서만 하도록) |

### 1.2 엔드포인트 명세

| 메서드 | 경로 | 호출 주체 | 설명 | 우선순위 |
|---|---|---|---|---|
| POST | `/v1/device/events` | ESP32 | passage, heartbeat, boot 이벤트 배치 수집. heartbeat가 들어오면 규칙 평가 실행 | 반드시 |
| POST | `/v1/auth` | 모의 인증 주입기 | 인증 이벤트 수집. 배열도 허용 | 반드시 |
| GET | `/v1/events?tag=&from=&to=&limit=` | 운영자 | **CCTV 태그 검색** (통과, 알림 통합) | 반드시 |
| GET | `/v1/alerts?type=&from=&to=` | 운영자 | 알림 목록 | 반드시 |
| GET | `/v1/stats/hourly?from=&to=` | 운영자 | 시간대별 IN/OUT 수, 인증 수 | 반드시 |
| GET | `/v1/occupancy` | 대시보드 | 대시보드 우측 집계를 한 번에 반환 (아래 예시) | 반드시 |
| GET | `/v1/settings` | 대시보드 | 현재 설정과 허용 값 목록 | 반드시 |
| PUT | `/v1/settings/t_long_ms` | 대시보드 | 장기 체류 기준 변경. 허용 값만 받음 (그 외 422). `settings_log`에 기록 | 반드시 |
| POST | `/v1/alerts/{id}/ack` | 대시보드 | 알림 확인 처리 | 반드시 |
| GET | `/` | 브라우저 | 대시보드 정적 HTML | 반드시 |
| GET | `/healthz` | CI, 운영자 | 상태 확인 | 반드시 |
| POST | `/v1/occupancy/reset` | 운영자 | 인원 수 기준점 초기화 (카운트 오차 보정) | 시간이 되면 |

**ESP32 쪽 엔드포인트** (서버가 아니라 기기의 두 번째 httpd)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `http://<기기 IP>:81/stream?token=...` | 박스 오버레이 MJPEG (`multipart/x-mixed-replace`). 동시 1명, 초과하면 503, 토큰이 틀리면 401 |

- **기기 IP 확보**: 서버는 `/v1/device/events` 요청의 출발지 IP를 `devices.last_ip`에 저장한다.
- **대시보드 연결**: `/v1/occupancy` 응답의 `stream_url`로 대시보드가 기기에 접속한다. 토큰은 대시보드에 수동으로 입력하고, 브라우저의 localStorage에만 보관한다.

**`GET /v1/occupancy` 응답 예**

```json
{
  "as_of_ts": 1793682131000,
  "mode": "replay",
  "occupancy_now": 2,
  "in_today": 42,
  "long_stay_confirmed": 1,
  "t_long_ms": 1800000,
  "device": {"online": true, "fps": 8.0, "infer_ms_p50": 41, "stream_url": "http://192.168.0.23:81/stream"}
}
```

`as_of_ts`는 기기가 마지막으로 보낸 heartbeat의 시각이다. 재생 모드에서는 시나리오 시각이 되므로, 화면 숫자와 알림 판정이 같은 시각을 기준으로 한다.

시간 파라미터(`from`, `to`)는 epoch ms(UTC)로 받는다. 표시할 때는 서버 설정의 시간대(`Asia/Seoul`)로 변환한다.

### 1.3 메시지 JSON 스키마

**ESP32 → 서버: `POST /v1/device/events`**

```json
{
  "device_id": "door-01",
  "boot_id": 7,
  "fw_version": "0.1.0",
  "model_version": "hm-a-20261103",
  "events": [
    {"kind": "boot", "seq": 0, "ts": 1793682000000, "reset_reason": "poweron"},
    {"kind": "passage", "seq": 1042, "ts": 1793682002875, "direction": "in", "conf": 0.87, "ambiguous": false},
    {"kind": "heartbeat", "seq": 1043, "ts": 1793682004000,
     "fps": 9.4, "infer_ms_p50": 41, "infer_ms_max": 55,
     "free_heap": 151234, "min_free_heap": 148900, "stack_hwm": 5120, "arena_used": 61234,
     "dropped": 0, "wifi_rssi": -58, "mode": "replay", "scenario_id": "fb_0007"}
  ]
}
```

- **이벤트 ID**: 서버가 `event_id = device_id:boot_id:seq`로 조합하고 DB에서 UNIQUE로 둔다. 재전송된 이벤트는 `duplicates`로 세고 무시한다.
- **시각(`ts`)**: 재생 모드에서는 boot를 포함한 모든 이벤트가 **시나리오 시각**이다. 카메라 모드에서는 SNTP 시각이다.

**응답**

| 코드 | 의미 | ESP32 동작 |
|---|---|---|
| 200 | `{"accepted": 3, "duplicates": 0}` | 큐에서 제거 |
| 401 | 토큰 오류 | 배치를 버리고 `rejected`를 1 증가 (재시도해도 실패하므로 무한 반복 방지) |
| 413 | 본문 16KB 초과 | 위와 같음 |
| 422 | 스키마 위반 | 위와 같음 |
| 5xx, 타임아웃 | 서버 장애 | 재시도. 간격을 1→2→4→10초로 늘림 (최대 10초) |

**모의 인증 주입기 → 서버: `POST /v1/auth`**

```json
[
  {"auth_id": "fb_0007-a0", "door_id": "door-01", "ts": 1793682001500, "allowed_count": 1, "method": "mock"}
]
```

- **개인 식별자 없음**: 인증 이벤트에는 카드번호, 회원 ID 같은 식별자 필드가 **없다**. 실제 인증 장치와 연동할 때도 "인증 성공 시각과 허용 인원"만 받는다.
- **주입 시점**: 재생 데모에서는 주입기가 시나리오의 인증 이벤트를 **재생 시작 전에 한꺼번에** 보낸다. 규칙은 이벤트 시각으로 평가하고, 아직 오지 않은 시각(`ts > now`)은 제외하므로 결과가 같다. 그래서 주입기를 실시간으로 동기화할 필요가 없다.

**서버 스키마 정의 (pydantic v2)**: 정의되지 않은 필드는 거부한다.

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Passage(Strict):
    kind: Literal["passage"]
    seq: int = Field(ge=0)
    ts: int                      # epoch ms UTC
    direction: Literal["in", "out"]
    conf: float = Field(ge=0, le=1)
    ambiguous: bool = False

class Heartbeat(Strict):
    kind: Literal["heartbeat"]
    seq: int = Field(ge=0)
    ts: int
    fps: float = Field(ge=0)
    infer_ms_p50: int = Field(ge=0)
    infer_ms_max: int = Field(ge=0)
    free_heap: int = Field(ge=0)
    min_free_heap: int = Field(ge=0)
    stack_hwm: int = Field(ge=0)
    arena_used: int = Field(ge=0)
    dropped: int = Field(ge=0)
    wifi_rssi: int = Field(ge=-127, le=0)
    mode: Literal["replay", "camera"]
    scenario_id: str | None = Field(default=None, max_length=32)

class Boot(Strict):
    kind: Literal["boot"]
    seq: int = Field(ge=0)
    ts: int
    reset_reason: str = Field(max_length=24)

DeviceEvent = Annotated[Union[Passage, Heartbeat, Boot], Field(discriminator="kind")]

class DeviceBatch(Strict):
    device_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    boot_id: int = Field(ge=0)
    fw_version: str = Field(max_length=16)
    model_version: str = Field(max_length=32)
    events: list[DeviceEvent] = Field(min_length=1, max_length=32)

class AuthEvent(Strict):
    auth_id: str = Field(max_length=40)
    door_id: str = Field(max_length=16)
    ts: int
    allowed_count: int = Field(default=1, ge=0, le=10)
    method: Literal["mock"] = "mock"
```

### 1.4 서버 내부 구조

```
server/
├─ app.py          # FastAPI 앱 팩토리 create_app(cfg), 라우터, 오프라인 감시 태스크(lifespan)
├─ config.py       # 설정값, 장기 체류 기준 허용 목록
├─ schemas.py      # 위 pydantic 모델
├─ db.py           # sqlite3 연결, 스키마 초기화(WAL)
├─ rules.py        # 규칙 엔진: evaluate(conn, device_id, now_ms, ...), check_offline(conn, wall_ms, ...)
├─ static/         # 대시보드 HTML·JS (알림은 여기에만 표시, 외부 채널 없음)
└─ tests/          # pytest: 규칙 단위 테스트, API 통합·개인정보 경계 테스트
```

- **실행**: `uvicorn server.app:create_app --factory --host 0.0.0.0 --port 8000`. 환경변수 `DOORKEEPER_DB`, `DOORKEEPER_DEVICE_TOKEN`
- **동시성**: 핸들러가 모두 async라 이벤트 루프 한 곳에서 돈다. 그래서 SQLite 쓰기가 자연히 직렬화된다.
- **오프라인 감지**: 유일하게 서버 벽시계를 쓰는 규칙이라 별도 모듈 대신 앱 lifespan의 5초 주기 태스크로 둔다. 판정 함수(`check_offline`)는 규칙 엔진에 두어, 벽시계를 주입해 테스트한다.
- **검증 오류 응답**: 입력값을 되돌려주지 않는다 (잘못 들어온 데이터를 다시 노출하지 않기 위함).

- **처리 순서**: heartbeat 수신 → 같은 트랜잭션 안에서 이벤트 저장 → `rules.evaluate(now = heartbeat.ts)` 실행 → 새 알림 INSERT → 커밋. 대시보드는 1초 폴링으로 새 알림을 가져간다
- **테스트 용이성**: 규칙 엔진은 HTTP와 분리된 순수 함수다. 단위 테스트에서 DB에 이벤트를 넣고 `now`를 바꿔 가며 검증할 수 있다.

## 2. 결정 근거

- **HTTP를 택한 이유**:
  - 이벤트 빈도가 낮다 (초당 1건 미만, heartbeat 0.5건/초). 그래서 MQTT의 경량성 이점이 작다.
  - 브로커 설치·운영이 필요 없다.
  - FastAPI `TestClient`로 CI 통합 테스트를 바로 짤 수 있다.
  - 응답 코드로 수신 확인(ack)을 받을 수 있다. 여기에 이벤트 ID 기반 중복 제거를 더하면 "**최소 1회 전달 + 중복 제거**"가 되어, 사실상 정확히 1회 저장된다.
- **배치 엔드포인트 1개로 통합한 이유**:
  - 펌웨어의 전송 코드가 하나로 끝난다.
  - 서버가 복구된 뒤 밀린 큐를 빨리 비울 수 있다.
  - 이벤트 순서가 배치 안에서 유지된다.
- **스키마를 엄격히 정의한 이유**: 이미지나 식별 정보가 실수로 섞여 들어오는 것을 **API 경계에서 원천 차단**한다. CI에서 "알 수 없는 필드가 있으면 422" 테스트로 증명한다.

## 3. 대안과 트레이드오프

| 대안 | 장점 | 단점 | 판단 |
|---|---|---|---|
| MQTT (QoS 1) + mosquitto | 상시 연결, pub/sub로 대시보드 실시간 연동 쉬움, 기기 수가 늘 때 유리 | 브로커 운영, 토픽 권한 설정, CI에서 브로커 컨테이너 필요 | 기기가 여러 대가 되면 전환. 그때 토픽은 `store/{store_id}/door/{device_id}/events` |
| 이벤트 종류별 엔드포인트 분리 | REST답게 명확함 | 펌웨어 코드 중복 | 기각 |
| HTTPS + 기기별 인증서 | 실서비스 보안 | 3시간 범위 밖 | 리스크 문서(11단계)에 실서비스 요구사항으로 기록 |
| 서버 벽시계로 규칙 평가 | 실시간성 | 재현성 없음 | 기기 오프라인 감지에만 사용 |
