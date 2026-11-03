"""규칙 엔진. HTTP와 분리된 함수로, 현재 시각(now_ms)을 인자로 받는다.

SQL은 docs/07-database.md 4.1~4.2절, 정책(임계값·심야 판단)은 Python에서 처리한다.
알림은 dedup_key UNIQUE + INSERT OR IGNORE로 같은 상황에 한 번만 생긴다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.db import get_setting

SESSION_SQL = """
WITH ev AS (
  SELECT ts, allowed_count AS allowed, 0 AS n_in
  FROM auth_events
  WHERE ts > :since AND ts <= :now
  UNION ALL
  SELECT ts, 0, 1
  FROM passages
  WHERE direction = 'in' AND ts > :since AND ts <= :now
),
marked AS (
  SELECT ts, allowed, n_in,
         CASE WHEN LAG(ts) OVER w IS NULL OR ts - LAG(ts) OVER w > :gap_ms
              THEN 1 ELSE 0 END AS is_new
  FROM ev
  WINDOW w AS (ORDER BY ts)
),
sess AS (
  SELECT ts, allowed, n_in,
         SUM(is_new) OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING) AS sid
  FROM marked
)
SELECT MIN(ts) AS start_ts, MAX(ts) AS end_ts, SUM(allowed) AS allowed, SUM(n_in) AS n_in
FROM sess
GROUP BY sid
HAVING MAX(ts) < :now - :gap_ms
   AND SUM(n_in) > SUM(allowed)
"""

OCCUPANCY_SQL = """
WITH base AS (
  SELECT COALESCE(MAX(ts), 0) AS reset_ts
  FROM occupancy_resets
  WHERE device_id = :device_id AND ts <= :now
),
p AS (
  SELECT passages.ts, passages.direction
  FROM passages, base
  WHERE passages.device_id = :device_id
    AND passages.ts > base.reset_ts AND passages.ts <= :now
)
SELECT
  COALESCE(SUM(direction = 'in'), 0) - COALESCE(SUM(direction = 'out'), 0)  AS occ_now,
  COALESCE(SUM(direction = 'out'), 0)                                       AS out_total,
  MAX(0, COALESCE(SUM(direction = 'in'), 0) - COALESCE(SUM(direction = 'out'), 0)
         - COALESCE(SUM(direction = 'in' AND ts > :now - :t_long_ms), 0))   AS long_stay_confirmed
FROM p
"""

EPISODE_SQL = """
WITH base AS (
  SELECT COALESCE(MAX(ts), 0) AS reset_ts
  FROM occupancy_resets
  WHERE device_id = :device_id AND ts <= :now
),
seq AS (
  SELECT p.ts,
         SUM(CASE p.direction WHEN 'in' THEN 1 ELSE -1 END)
           OVER (ORDER BY p.ts, p.id ROWS UNBOUNDED PRECEDING) AS occ
  FROM passages p, base
  WHERE p.device_id = :device_id AND p.ts > base.reset_ts AND p.ts <= :now
)
SELECT MIN(ts) AS episode_start
FROM seq
WHERE ts > COALESCE((SELECT MAX(ts) FROM seq WHERE occ <= 0), 0)
"""


def local_dt(ts_ms: int, cfg: Config) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone(timedelta(hours=cfg.tz_offset_h)))


def local_day_start_ms(ts_ms: int, cfg: Config) -> int:
    d = local_dt(ts_ms, cfg)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def occupancy(conn: sqlite3.Connection, device_id: str, now_ms: int, t_long_ms: int) -> dict:
    row = conn.execute(OCCUPANCY_SQL, {"device_id": device_id, "now": now_ms, "t_long_ms": t_long_ms}).fetchone()
    return {"occ_now": row["occ_now"], "out_total": row["out_total"], "long_stay_confirmed": row["long_stay_confirmed"]}


def insert_alert(conn: sqlite3.Connection, now_wall_ms: int, type_: str, severity: str, ts: int,
                 ts_end: int | None, dedup_key: str, details: dict) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO alerts (type, severity, ts, ts_end, dedup_key, details, created_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (type_, severity, ts, ts_end, dedup_key, json.dumps(details, ensure_ascii=False), now_wall_ms),
    )
    return cur.rowcount == 1


def evaluate(conn: sqlite3.Connection, device_id: str, now_ms: int, now_wall_ms: int, cfg: Config) -> list[str]:
    """heartbeat(또는 설정 변경) 시점에 규칙을 평가하고 새로 생긴 알림의 dedup_key 목록을 돌려준다."""
    new: list[str] = []

    # 동반입장·미인증 입장: 닫힌 출입 세션만 판정 (인증 대조를 끈 시연에서는 건너뜀)
    params = {"since": now_ms - cfg.session_lookback_ms, "now": now_ms, "gap_ms": cfg.session_gap_ms}
    for s in conn.execute(SESSION_SQL, params).fetchall() if cfg.auth_rules else []:
        type_ = "tailgate" if s["allowed"] > 0 else "unauth_entry"
        key = f"{type_}:{s['start_ts']}"
        if insert_alert(conn, now_wall_ms, type_, "warn", s["start_ts"], s["end_ts"], key,
                        {"allowed": s["allowed"], "n_in": s["n_in"]}):
            new.append(key)

    # 장기 체류: 확정값(현재 인원 - 최근 T 안의 입장)이 1 이상
    t_long = get_setting(conn, "t_long_ms")
    occ = occupancy(conn, device_id, now_ms, t_long)
    if occ["long_stay_confirmed"] >= 1:
        start = conn.execute(EPISODE_SQL, {"device_id": device_id, "now": now_ms}).fetchone()["episode_start"]
        key = f"long_stay:{start}:{occ['long_stay_confirmed']}"
        if insert_alert(conn, now_wall_ms, "long_stay", "warn", start, now_ms - t_long, key,
                        {"confirmed": occ["long_stay_confirmed"], "t_long_ms": t_long}):
            new.append(key)

    local = local_dt(now_ms, cfg)
    if occ["occ_now"] > 0 and cfg.night_start_h <= local.hour < cfg.night_end_h:
        key = f"night_stay:{local:%Y-%m-%d}:{local.hour:02d}"
        if insert_alert(conn, now_wall_ms, "night_stay", "info", now_ms, None, key, {"occupancy": occ["occ_now"]}):
            new.append(key)
    if occ["occ_now"] < 0:
        key = f"count_anomaly:{local:%Y-%m-%d}"
        if insert_alert(conn, now_wall_ms, "count_anomaly", "info", now_ms, None, key, {"occupancy": occ["occ_now"]}):
            new.append(key)
    return new


def check_offline(conn: sqlite3.Connection, now_wall_ms: int, cfg: Config) -> list[str]:
    """유일하게 서버 벽시계를 쓰는 규칙. 끊긴 구간 1회마다 알림 1건."""
    new = []
    for d in conn.execute("SELECT device_id, last_seen_ms FROM devices").fetchall():
        if now_wall_ms - d["last_seen_ms"] <= cfg.offline_ms:
            continue
        last = conn.execute("SELECT MAX(ts) AS ts FROM device_logs WHERE device_id = ?", (d["device_id"],)).fetchone()
        key = f"device_offline:{d['device_id']}:{d['last_seen_ms']}"
        ts = last["ts"] if last["ts"] is not None else d["last_seen_ms"]  # 감시 공백 시작(이벤트 시각 기준)
        if insert_alert(conn, now_wall_ms, "device_offline", "crit", ts, None, key,
                        {"device_id": d["device_id"], "silent_ms": now_wall_ms - d["last_seen_ms"]}):
            new.append(key)
    return new
