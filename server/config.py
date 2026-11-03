"""서버 설정. 값의 근거는 docs/08-alerts-dashboard.md 1.1절."""

import os
from dataclasses import dataclass, field

# 대시보드 드롭다운에서 고를 수 있는 장기 체류 기준(ms): 10초·30초(데모), 5분, 10분, 30분, 1시간
ALLOWED_T_LONG_MS = (10_000, 30_000, 300_000, 600_000, 1_800_000, 3_600_000)


@dataclass(frozen=True)
class Config:
    db_path: str = field(default_factory=lambda: os.environ.get("DOORKEEPER_DB", "doorkeeper.db"))
    # 기기·모의 인증 주입기가 X-Device-Token 헤더로 보내는 값. 실제 값은 환경변수로만 준다
    device_token: str = field(default_factory=lambda: os.environ.get("DOORKEEPER_DEVICE_TOKEN", "dev-token"))
    session_gap_ms: int = 5_000
    session_lookback_ms: int = 10 * 60 * 1000
    offline_ms: int = 10_000
    watchdog_interval_s: float = 5.0  # 0이면 오프라인 감시 태스크를 띄우지 않는다(테스트)
    night_start_h: int = 0
    night_end_h: int = 5
    tz_offset_h: int = 9  # Asia/Seoul (서머타임 없음)
    max_body_bytes: int = 16 * 1024
    stream_port: int = 81
