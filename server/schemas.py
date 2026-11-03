"""기기·인증 메시지 스키마. 정의되지 않은 필드는 거부한다(이미지·식별 정보 유입 차단). docs/06 1.3절."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Passage(Strict):
    kind: Literal["passage"]
    seq: int = Field(ge=0)
    ts: int  # epoch ms UTC (재생 모드에서는 시나리오 시각)
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


DeviceEvent = Annotated[Passage | Heartbeat | Boot, Field(discriminator="kind")]


class DeviceBatch(Strict):
    device_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    boot_id: int = Field(ge=0)
    fw_version: str = Field(max_length=16)
    model_version: str = Field(max_length=32)
    events: list[DeviceEvent] = Field(min_length=1, max_length=32)


class AuthEvent(Strict):
    """인증 성공 시각과 허용 인원만 받는다. 회원 ID·카드번호 같은 식별자 필드는 없다."""

    auth_id: str = Field(max_length=40)
    door_id: str = Field(max_length=16)
    ts: int
    allowed_count: int = Field(default=1, ge=0, le=10)
    method: Literal["mock"] = "mock"


class SettingValue(Strict):
    value: int


class ResetRequest(Strict):
    device_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,32}$")
    ts: int | None = None
    reason: Literal["manual", "test"] = "manual"
