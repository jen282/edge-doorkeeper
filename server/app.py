"""FastAPI 서버: 기기 이벤트·모의 인증 수집, 규칙 평가, 대시보드용 조회·설정 API. docs/06-communication-server.md

    uvicorn server.app:create_app --factory --host 0.0.0.0 --port 8000

핸들러는 모두 async로 이벤트 루프 한 곳에서 돌기 때문에 SQLite 쓰기가 자연히 직렬화된다.
영상·이미지를 받는 경로는 없다(실시간 뷰는 ESP32 → 브라우저 직결).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter, ValidationError

from server import rules
from server.config import ALLOWED_T_LONG_MS, Config
from server.db import connect, get_setting
from server.schemas import AuthEvent, DeviceBatch, ResetRequest, SettingValue

STATIC_DIR = Path(__file__).parent / "static"
_AUTH_LIST = TypeAdapter(list[AuthEvent])
log = logging.getLogger("doorkeeper")


def wall_ms() -> int:
    return int(time.time() * 1000)


def _validation_detail(e: ValidationError) -> list:
    # 입력값을 응답에 되돌려주지 않는다 (잘못 들어온 데이터를 다시 노출하지 않기 위함)
    return e.errors(include_url=False, include_context=False, include_input=False)


def _ingest(conn: sqlite3.Connection, batch: DeviceBatch, now_wall: int, ip: str | None, cfg: Config) -> dict:
    accepted = duplicates = 0
    hb_ts: int | None = None
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO devices (device_id, fw_version, model_version, first_seen_ms, last_seen_ms, last_ip) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(device_id) DO UPDATE SET fw_version = excluded.fw_version, "
            "model_version = excluded.model_version, last_seen_ms = excluded.last_seen_ms, last_ip = excluded.last_ip",
            (batch.device_id, batch.fw_version, batch.model_version, now_wall, now_wall, ip),
        )
        for ev in batch.events:
            event_id = f"{batch.device_id}:{batch.boot_id}:{ev.seq}"
            if ev.kind == "passage":
                cur = conn.execute(
                    "INSERT OR IGNORE INTO passages (event_id, device_id, ts, direction, conf, ambiguous, received_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, batch.device_id, ev.ts, ev.direction, ev.conf, int(ev.ambiguous), now_wall),
                )
            elif ev.kind == "heartbeat":
                cur = conn.execute(
                    "INSERT OR IGNORE INTO device_logs (event_id, device_id, kind, ts, boot_id, fps, infer_ms_p50, "
                    "infer_ms_max, free_heap, min_free_heap, stack_hwm, arena_used, dropped, wifi_rssi, mode, "
                    "scenario_id, received_ms) VALUES (?, ?, 'heartbeat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, batch.device_id, ev.ts, batch.boot_id, ev.fps, ev.infer_ms_p50, ev.infer_ms_max,
                     ev.free_heap, ev.min_free_heap, ev.stack_hwm, ev.arena_used, ev.dropped, ev.wifi_rssi,
                     ev.mode, ev.scenario_id, now_wall),
                )
                hb_ts = ev.ts if hb_ts is None else max(hb_ts, ev.ts)
            else:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO device_logs (event_id, device_id, kind, ts, boot_id, reset_reason, "
                    "received_ms) VALUES (?, ?, 'boot', ?, ?, ?, ?)",
                    (event_id, batch.device_id, ev.ts, batch.boot_id, ev.reset_reason, now_wall),
                )
            if cur.rowcount == 1:
                accepted += 1
            else:
                duplicates += 1
        new = rules.evaluate(conn, batch.device_id, hb_ts, now_wall, cfg) if hb_ts is not None else []
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    for key in new:
        log.warning("ALERT %s", key)
    return {"accepted": accepted, "duplicates": duplicates}


def _current(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """가장 최근에 본 기기와 그 기기의 마지막 heartbeat. 대시보드의 '기준 시각'은 이 heartbeat의 ts다."""
    dev = conn.execute("SELECT * FROM devices ORDER BY last_seen_ms DESC LIMIT 1").fetchone()
    if dev is None:
        return None, None
    hb = conn.execute(
        "SELECT * FROM device_logs WHERE device_id = ? AND kind = 'heartbeat' ORDER BY ts DESC LIMIT 1",
        (dev["device_id"],),
    ).fetchone()
    return dev, hb


def _kst(ts: int | None, cfg: Config) -> str | None:
    return None if ts is None else rules.local_dt(ts, cfg).strftime("%Y-%m-%d %H:%M:%S")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    conn = connect(cfg.db_path)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if cfg.watchdog_interval_s > 0:
            async def watchdog() -> None:
                while True:
                    await asyncio.sleep(cfg.watchdog_interval_s)
                    for key in rules.check_offline(conn, wall_ms(), cfg):
                        log.warning("ALERT %s", key)

            task = asyncio.create_task(watchdog())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="edge-doorkeeper", lifespan=lifespan)
    app.state.conn = conn
    app.state.cfg = cfg

    def require_token(token: str | None) -> None:
        if token is None or not hmac.compare_digest(token.encode(), cfg.device_token.encode()):
            raise HTTPException(401, "invalid device token")

    async def read_body(request: Request) -> bytes:
        body = await request.body()
        if len(body) > cfg.max_body_bytes:
            raise HTTPException(413, f"body larger than {cfg.max_body_bytes} bytes")
        return body

    def reevaluate() -> list[str]:
        dev, hb = _current(conn)
        if dev is None or hb is None:
            return []
        return rules.evaluate(conn, dev["device_id"], hb["ts"], wall_ms(), cfg)

    @app.post("/v1/device/events")
    async def device_events(request: Request, x_device_token: str | None = Header(default=None)):
        require_token(x_device_token)
        body = await read_body(request)
        try:
            batch = DeviceBatch.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(422, _validation_detail(e)) from None
        return _ingest(conn, batch, wall_ms(), request.client.host if request.client else None, cfg)

    @app.post("/v1/auth")
    async def auth(request: Request, x_device_token: str | None = Header(default=None)):
        require_token(x_device_token)
        body = await read_body(request)
        try:
            data = json.loads(body)
            items = _AUTH_LIST.validate_python(data if isinstance(data, list) else [data])
        except json.JSONDecodeError:
            raise HTTPException(422, "invalid JSON") from None
        except ValidationError as e:
            raise HTTPException(422, _validation_detail(e)) from None
        now = wall_ms()
        accepted = 0
        conn.execute("BEGIN")
        for a in items:
            cur = conn.execute(
                "INSERT OR IGNORE INTO auth_events (auth_id, door_id, ts, allowed_count, method, received_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a.auth_id, a.door_id, a.ts, a.allowed_count, a.method, now),
            )
            accepted += cur.rowcount
        conn.execute("COMMIT")
        return {"accepted": accepted, "duplicates": len(items) - accepted}

    @app.get("/v1/occupancy")
    async def get_occupancy():
        """대시보드 우측 집계를 한 번에 반환."""
        t_long = get_setting(conn, "t_long_ms")
        dev, hb = _current(conn)
        if dev is None:
            return {"as_of_ts": None, "as_of_kst": None, "mode": None, "occupancy_now": 0, "in_today": 0,
                    "long_stay_confirmed": 0, "t_long_ms": t_long, "unacked_alerts": 0, "device": None}
        now = hb["ts"] if hb else (conn.execute("SELECT MAX(ts) FROM passages").fetchone()[0] or 0)
        occ = rules.occupancy(conn, dev["device_id"], now, t_long)
        in_today = conn.execute(
            "SELECT COUNT(*) FROM passages WHERE direction = 'in' AND ts >= ? AND ts <= ?",
            (rules.local_day_start_ms(now, cfg), now),
        ).fetchone()[0]
        unacked = conn.execute("SELECT COUNT(*) FROM alerts WHERE acked = 0").fetchone()[0]
        return {
            "as_of_ts": now,
            "as_of_kst": _kst(now, cfg),
            "mode": hb["mode"] if hb else None,
            "occupancy_now": max(0, occ["occ_now"]),
            "in_today": in_today,
            "long_stay_confirmed": occ["long_stay_confirmed"],
            "t_long_ms": t_long,
            "unacked_alerts": unacked,
            "device": {
                "device_id": dev["device_id"],
                "online": wall_ms() - dev["last_seen_ms"] <= cfg.offline_ms,
                "fps": hb["fps"] if hb else None,
                "infer_ms_p50": hb["infer_ms_p50"] if hb else None,
                "min_free_heap": hb["min_free_heap"] if hb else None,
                "stream_url": f"http://{dev['last_ip']}:{cfg.stream_port}/stream" if dev["last_ip"] else None,
            },
        }

    @app.get("/v1/alerts")
    async def list_alerts(type: str | None = None, from_: int | None = Query(None, alias="from"),
                          to: int | None = None, unacked: bool = False, limit: int = Query(50, le=500)):
        sql, args = "SELECT * FROM alerts WHERE 1 = 1", []
        if type:
            sql += " AND type = ?"
            args.append(type)
        if from_ is not None:
            sql += " AND ts >= ?"
            args.append(from_)
        if to is not None:
            sql += " AND ts < ?"
            args.append(to)
        if unacked:
            sql += " AND acked = 0"
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r["id"], "type": r["type"], "severity": r["severity"], "ts": r["ts"], "ts_end": r["ts_end"],
                 "at_kst": _kst(r["ts"], cfg), "until_kst": _kst(r["ts_end"], cfg),
                 "details": json.loads(r["details"]), "acked": bool(r["acked"])} for r in rows]

    @app.post("/v1/alerts/{alert_id}/ack")
    async def ack_alert(alert_id: int):
        cur = conn.execute("UPDATE alerts SET acked = 1 WHERE id = ?", (alert_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "alert not found")
        return {"id": alert_id, "acked": True}

    @app.get("/v1/events")
    async def cctv_events(tag: str | None = None, from_: int | None = Query(None, alias="from"),
                          to: int | None = None, limit: int = Query(200, le=1000)):
        """CCTV 검색 보조: 통과와 알림을 하나의 시간축으로."""
        sql, args = "SELECT * FROM cctv_tags WHERE 1 = 1", []
        if tag:
            tags = tag.split(",")
            sql += f" AND tag IN ({','.join('?' * len(tags))})"
            args += tags
        if from_ is not None:
            sql += " AND ts >= ?"
            args.append(from_)
        if to is not None:
            sql += " AND ts < ?"
            args.append(to)
        rows = conn.execute(sql + " ORDER BY ts LIMIT ?", (*args, limit)).fetchall()
        return [{"ts": r["ts"], "ts_end": r["ts_end"], "at_kst": _kst(r["ts"], cfg),
                 "until_kst": _kst(r["ts_end"], cfg), "tag": r["tag"], "ref_id": r["ref_id"]} for r in rows]

    @app.get("/v1/stats/hourly")
    async def hourly(from_: int = Query(alias="from"), to: int = Query()):
        tz = f"+{cfg.tz_offset_h} hours"
        rows = conn.execute(
            """
            WITH h_pass AS (
              SELECT strftime('%Y-%m-%d %H:00', ts / 1000, 'unixepoch', :tz) AS hour_kst,
                     SUM(direction = 'in') AS n_in, SUM(direction = 'out') AS n_out
              FROM passages WHERE ts >= :from AND ts < :to GROUP BY hour_kst
            ),
            h_auth AS (
              SELECT strftime('%Y-%m-%d %H:00', ts / 1000, 'unixepoch', :tz) AS hour_kst,
                     SUM(allowed_count) AS n_allowed
              FROM auth_events WHERE ts >= :from AND ts < :to GROUP BY hour_kst
            )
            SELECT h_pass.hour_kst, h_pass.n_in, h_pass.n_out, COALESCE(h_auth.n_allowed, 0) AS n_allowed
            FROM h_pass LEFT JOIN h_auth USING (hour_kst)
            ORDER BY h_pass.hour_kst
            """,
            {"tz": tz, "from": from_, "to": to},
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/v1/settings")
    async def get_settings():
        return {"t_long_ms": get_setting(conn, "t_long_ms"), "allowed_t_long_ms": list(ALLOWED_T_LONG_MS)}

    @app.put("/v1/settings/t_long_ms")
    async def put_t_long(body: SettingValue):
        if body.value not in ALLOWED_T_LONG_MS:
            raise HTTPException(422, f"value must be one of {list(ALLOWED_T_LONG_MS)}")
        now = wall_ms()
        old = get_setting(conn, "t_long_ms")
        conn.execute("BEGIN")
        conn.execute("UPDATE settings SET value = ?, updated_ms = ? WHERE key = 't_long_ms'", (body.value, now))
        conn.execute("INSERT INTO settings_log (key, old_value, new_value, changed_ms) VALUES ('t_long_ms', ?, ?, ?)",
                     (old, body.value, now))
        new = reevaluate()  # 기준을 낮추면 지금 바로 해당되는 장기 체류가 알림으로 뜬다
        conn.execute("COMMIT")
        for key in new:
            log.warning("ALERT %s", key)
        return {"t_long_ms": body.value, "allowed_t_long_ms": list(ALLOWED_T_LONG_MS)}

    @app.post("/v1/occupancy/reset")
    async def reset_occupancy(body: ResetRequest | None = None):
        """인원 0의 기준점을 넣는다. 본문이 없으면 '지금(마지막 heartbeat) 수동 보정'.

        재생 데모는 시나리오마다 시작 시각에 reason=test 기준점을 미리 넣는다 (tools/auth_injector.py).
        """
        body = body or ResetRequest()
        device_id, ts = body.device_id, body.ts
        if device_id is None or ts is None:
            dev, hb = _current(conn)
            if dev is None or hb is None:
                raise HTTPException(409, "no device heartbeat yet")
            device_id = device_id or dev["device_id"]
            ts = ts if ts is not None else hb["ts"]
        conn.execute("INSERT INTO occupancy_resets (device_id, ts, reason) VALUES (?, ?, ?)",
                     (device_id, ts, body.reason))
        return {"device_id": device_id, "ts": ts, "reason": body.reason}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
