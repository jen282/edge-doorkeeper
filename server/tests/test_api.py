"""API 통합 테스트와 개인정보 경계 테스트. TC 번호는 docs/10-test-plan.md 1.2절."""

import json

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Config

T0 = 1_793_682_000_000  # 2026-11-03 14:00 KST
TOKEN = {"X-Device-Token": "t"}


@pytest.fixture
def client():
    app = create_app(Config(db_path=":memory:", device_token="t", watchdog_interval_s=0))
    with TestClient(app) as c:
        c.app_conn = app.state.conn
        yield c


def batch(*events, boot_id=1):
    return {"device_id": "door-01", "boot_id": boot_id, "fw_version": "0.1.0", "model_version": "hm-a",
            "events": list(events)}


def ps(seq, ts, direction="in"):
    return {"kind": "passage", "seq": seq, "ts": ts, "direction": direction, "conf": 0.9}


def hb(seq, ts):
    return {"kind": "heartbeat", "seq": seq, "ts": ts, "fps": 8.0, "infer_ms_p50": 41, "infer_ms_max": 55,
            "free_heap": 150_000, "min_free_heap": 148_000, "stack_hwm": 5000, "arena_used": 60_000,
            "dropped": 0, "wifi_rssi": -55, "mode": "replay", "scenario_id": "fb_0001"}


def post(client, body):
    return client.post("/v1/device/events", json=body, headers=TOKEN)


def test_ingest_and_resend_is_deduplicated(client):  # TC-17
    body = batch({"kind": "boot", "seq": 0, "ts": T0, "reset_reason": "poweron"}, ps(1, T0 + 1000), hb(2, T0 + 2000))
    assert post(client, body).json() == {"accepted": 3, "duplicates": 0}
    assert post(client, body).json() == {"accepted": 0, "duplicates": 3}
    assert client.app_conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0] == 1


def test_bad_token_is_rejected(client):
    r = client.post("/v1/device/events", json=batch(ps(1, T0)), headers={"X-Device-Token": "wrong"})
    assert r.status_code == 401


def test_unknown_field_is_rejected_and_not_echoed(client):  # TC-19
    ev = ps(1, T0) | {"image": "SECRET_PIXELS"}
    r = post(client, batch(ev))
    assert r.status_code == 422
    assert "SECRET_PIXELS" not in r.text
    assert client.app_conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0] == 0


def test_body_over_16kb_is_rejected(client):
    body = json.dumps(batch(ps(1, T0))) + " " * 17_000
    r = client.post("/v1/device/events", content=body, headers=TOKEN | {"Content-Type": "application/json"})
    assert r.status_code == 413


def test_tailgate_end_to_end(client):  # TC-04
    r = client.post("/v1/auth", json=[{"auth_id": "a0", "door_id": "door-01", "ts": T0}], headers=TOKEN)
    assert r.json() == {"accepted": 1, "duplicates": 0}
    post(client, batch(ps(1, T0 + 1500), ps(2, T0 + 1900), hb(3, T0 + 8000)))
    (a,) = client.get("/v1/alerts").json()
    assert (a["type"], a["at_kst"], a["details"]) == ("tailgate", "2026-11-03 14:00:00", {"allowed": 1, "n_in": 2})
    tags = [e["tag"] for e in client.get("/v1/events", params={"tag": "tailgate,passage_in"}).json()]
    assert tags == ["tailgate", "passage_in", "passage_in"]
    assert client.post(f"/v1/alerts/{a['id']}/ack").status_code == 200
    assert client.get("/v1/alerts", params={"unacked": True}).json() == []


def test_settings_validation_and_immediate_reevaluation(client):  # TC-15, TC-16
    assert client.put("/v1/settings/t_long_ms", json={"value": 12345}).status_code == 422
    client.post("/v1/auth", json={"auth_id": "a0", "door_id": "door-01", "ts": T0 - 1000}, headers=TOKEN)
    post(client, batch(ps(1, T0), hb(2, T0 + 40_000)))  # 40초째 머무는 중 (기본 30분 기준에서는 해당 없음)
    assert [a["type"] for a in client.get("/v1/alerts").json()] == []
    r = client.put("/v1/settings/t_long_ms", json={"value": 30_000})
    assert r.status_code == 200 and r.json()["t_long_ms"] == 30_000
    assert [a["type"] for a in client.get("/v1/alerts").json()] == ["long_stay"]
    log = client.app_conn.execute("SELECT old_value, new_value FROM settings_log").fetchall()
    assert [tuple(r) for r in log] == [(1_800_000, 30_000)]


def test_occupancy_payload(client):
    post(client, batch(ps(1, T0), ps(2, T0 + 5000), ps(3, T0 + 9000, "out"), hb(4, T0 + 40_000)))
    client.put("/v1/settings/t_long_ms", json={"value": 30_000})
    o = client.get("/v1/occupancy").json()
    assert (o["occupancy_now"], o["in_today"], o["long_stay_confirmed"]) == (1, 2, 1)
    assert o["as_of_kst"] == "2026-11-03 14:00:40" and o["mode"] == "replay"
    assert o["device"]["online"] and o["device"]["stream_url"] == "http://testclient:81/stream"


def test_hourly_stats(client):
    client.post("/v1/auth", json={"auth_id": "a0", "door_id": "door-01", "ts": T0}, headers=TOKEN)
    post(client, batch(ps(1, T0 + 1000), ps(2, T0 + 2000, "out"), hb(3, T0 + 3000)))
    rows = client.get("/v1/stats/hourly", params={"from": T0, "to": T0 + 3_600_000}).json()
    assert rows == [{"hour_kst": "2026-11-03 14:00", "n_in": 1, "n_out": 1, "n_allowed": 1}]


def test_reset_before_device_connects_isolates_scenarios(client):
    # 재생 데모: 기기가 접속하기 전에 시나리오 시작 시각마다 기준점을 넣어 둔다
    r = client.post("/v1/occupancy/reset", json={"device_id": "door-01", "ts": T0 + 60_000, "reason": "test"})
    assert r.status_code == 200
    client.put("/v1/settings/t_long_ms", json={"value": 30_000})
    client.post("/v1/auth", json={"auth_id": "a0", "door_id": "door-01", "ts": T0 - 1000}, headers=TOKEN)
    post(client, batch(ps(1, T0), hb(2, T0 + 120_000)))  # 첫 시나리오의 입장자는 다음 시나리오에서 세지 않는다
    o = client.get("/v1/occupancy").json()
    assert (o["occupancy_now"], o["long_stay_confirmed"]) == (0, 0)


def test_current_heartbeat_is_latest_received_not_latest_ts(client):
    # 이전 부팅의 심야 시나리오(늦은 시각) heartbeat 뒤에 새 재생(이른 시각)이 시작된 경우
    post(client, batch(hb(1, T0 + 11 * 3_600_000), boot_id=6))
    post(client, batch(hb(1, T0 + 2000), boot_id=7))
    assert client.get("/v1/occupancy").json()["as_of_kst"] == "2026-11-03 14:00:02"


def test_dashboard_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "무인가게 출입 이상 탐지" in r.text
    assert client.get("/app.js").status_code == 200


def test_schema_has_no_blob_or_image_columns(client):  # 개인정보: 이미지 저장 경로 자체가 없다
    conn = client.app_conn
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for t in tables:
        for col in conn.execute(f"PRAGMA table_info({t})"):
            assert "BLOB" not in col["type"].upper(), (t, col["name"])
            assert not any(w in col["name"].lower() for w in ("image", "frame", "photo", "video")), (t, col["name"])


def test_no_route_accepts_binary_uploads(client):
    spec = json.dumps(client.get("/openapi.json").json())
    assert "multipart/form-data" not in spec and '"format": "binary"' not in spec
