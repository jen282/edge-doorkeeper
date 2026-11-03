"""규칙 엔진 단위 테스트. TC 번호는 docs/10-test-plan.md 1.2절."""

import json

import pytest

from server.config import Config
from server.db import connect
from server.rules import check_offline, evaluate, occupancy

T0 = 1_793_682_000_000  # 2026-11-03 14:00 KST
MIN = 60_000
CFG = Config(db_path=":memory:", device_token="t", watchdog_interval_s=0)


@pytest.fixture
def conn():
    c = connect(":memory:")
    c.execute("INSERT INTO devices (device_id, first_seen_ms, last_seen_ms) VALUES ('door-01', 0, 0)")
    return c


def passage(conn, ts, direction="in"):
    n = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
    conn.execute("INSERT INTO passages (event_id, device_id, ts, direction, conf, received_ms) "
                 "VALUES (?, 'door-01', ?, ?, 0.9, 0)", (f"door-01:1:{n}", ts, direction))


def auth(conn, ts, allowed=1):
    n = conn.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0]
    conn.execute("INSERT INTO auth_events (auth_id, door_id, ts, allowed_count, received_ms) "
                 "VALUES (?, 'door-01', ?, ?, 0)", (f"a{n}", ts, allowed))


def set_t_long(conn, ms):
    conn.execute("UPDATE settings SET value = ? WHERE key = 't_long_ms'", (ms,))


def run(conn, now):
    return evaluate(conn, "door-01", now, 0, CFG)


def alerts(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id")]


def test_tailgate_only_after_session_closes(conn):  # TC-04
    auth(conn, T0)
    passage(conn, T0 + 1500)
    passage(conn, T0 + 1900)
    assert run(conn, T0 + 3000) == []  # 아직 같은 세션에 이벤트가 더 올 수 있다
    assert run(conn, T0 + 8000) == [f"tailgate:{T0}"]
    assert run(conn, T0 + 9000) == []  # 같은 세션은 한 번만
    (a,) = alerts(conn)
    assert (a["ts"], a["ts_end"], json.loads(a["details"])) == (T0, T0 + 1900, {"allowed": 1, "n_in": 2})


def test_consecutive_auth_is_not_tailgate(conn):  # TC-06
    auth(conn, T0)
    auth(conn, T0 + 1000)
    passage(conn, T0 + 1500)
    passage(conn, T0 + 2000)
    assert run(conn, T0 + 10_000) == []


def test_unauthenticated_entry(conn):  # TC-05
    passage(conn, T0 + 1500)
    assert run(conn, T0 + 10_000) == [f"unauth_entry:{T0 + 1500}"]


def test_late_auth_arrival_gives_same_result(conn):  # TC-18: 통과가 먼저 저장되고 인증이 늦게 도착
    passage(conn, T0 + 1500)
    passage(conn, T0 + 1900)
    auth(conn, T0)
    assert run(conn, T0 + 8000) == [f"tailgate:{T0}"]


def test_long_stay_low_traffic(conn):  # TC-11
    set_t_long(conn, 30_000)
    auth(conn, T0 - 1000)
    passage(conn, T0)
    assert run(conn, T0 + 20_000) == []
    assert run(conn, T0 + 31_000) == [f"long_stay:{T0}:1"]
    (a,) = [a for a in alerts(conn) if a["type"] == "long_stay"]
    assert a["ts"] <= T0 <= a["ts_end"]  # CCTV 검색 구간에 실제 입장 시각이 들어 있다


def test_long_stay_is_delayed_while_busy(conn):  # TC-12: 알려진 한계의 동작을 고정
    set_t_long(conn, 30 * MIN)
    passage(conn, T0)  # 계속 머무는 사람
    for k in range(1, 12):  # 다른 손님이 5분 간격으로 4분씩 머문다
        passage(conn, T0 + k * 5 * MIN)
        passage(conn, T0 + k * 5 * MIN + 4 * MIN, "out")
    assert occupancy(conn, "door-01", T0 + 60 * MIN, 30 * MIN)["long_stay_confirmed"] == 0
    assert [k for k in run(conn, T0 + 60 * MIN) if k.startswith("long_stay")] == []
    assert run(conn, T0 + 86 * MIN) == [f"long_stay:{T0}:1"]  # 마지막 입장(55분) + 30분 뒤 확정


def test_night_stay(conn):  # TC-13
    night = T0 + 11 * 60 * MIN  # 다음 날 01:00 KST
    passage(conn, night)
    assert "night_stay:2026-11-04:01" in run(conn, night + 1000)


def test_count_anomaly(conn):  # TC-14
    passage(conn, T0, "out")
    assert run(conn, T0 + 1000) == ["count_anomaly:2026-11-03"]
    assert occupancy(conn, "door-01", T0 + 1000, 30 * MIN)["long_stay_confirmed"] == 0


def test_reset_starts_count_from_zero(conn):
    set_t_long(conn, 30_000)
    auth(conn, T0 - 1000)
    passage(conn, T0)
    conn.execute("INSERT INTO occupancy_resets (device_id, ts, reason) VALUES ('door-01', ?, 'test')", (T0 + 1000,))
    assert run(conn, T0 + 60_000) == []


def test_auth_rules_can_be_disabled_for_camera_demo(conn):
    cfg = Config(db_path=":memory:", device_token="t", watchdog_interval_s=0, auth_rules=False)
    set_t_long(conn, 30_000)
    passage(conn, T0)  # 인증 없는 입장
    assert evaluate(conn, "door-01", T0 + 40_000, 0, cfg) == [f"long_stay:{T0}:1"]  # 미인증 입장 알림 없음


def test_device_offline_once_per_gap(conn):  # TC-22 (서버 쪽)
    assert check_offline(conn, 5_000, CFG) == []
    assert check_offline(conn, 20_000, CFG) == ["device_offline:door-01:0"]
    assert check_offline(conn, 30_000, CFG) == []
