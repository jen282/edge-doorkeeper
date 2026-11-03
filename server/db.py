"""SQLite 연결과 스키마. DDL은 docs/07-database.md 2.2절과 같아야 한다."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id     TEXT PRIMARY KEY,
  fw_version    TEXT,
  model_version TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms  INTEGER NOT NULL,            -- 서버 수신 시각 (오프라인 감지용)
  last_ip       TEXT                         -- 실시간 뷰 접속 주소용 (기기 IP)
);

CREATE TABLE IF NOT EXISTS passages (
  id          INTEGER PRIMARY KEY,
  event_id    TEXT    NOT NULL UNIQUE,       -- device_id:boot_id:seq
  device_id   TEXT    NOT NULL REFERENCES devices(device_id),
  ts          INTEGER NOT NULL,
  direction   TEXT    NOT NULL CHECK (direction IN ('in', 'out')),
  conf        REAL    NOT NULL CHECK (conf BETWEEN 0 AND 1),
  ambiguous   INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
  received_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passages_ts     ON passages(ts);
CREATE INDEX IF NOT EXISTS idx_passages_dir_ts ON passages(direction, ts);

CREATE TABLE IF NOT EXISTS auth_events (
  id            INTEGER PRIMARY KEY,
  auth_id       TEXT    NOT NULL UNIQUE,
  door_id       TEXT    NOT NULL,
  ts            INTEGER NOT NULL,
  allowed_count INTEGER NOT NULL DEFAULT 1 CHECK (allowed_count >= 0),
  method        TEXT    NOT NULL DEFAULT 'mock',
  received_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_ts ON auth_events(ts);

CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY,
  type       TEXT    NOT NULL CHECK (type IN
               ('tailgate', 'unauth_entry', 'long_stay', 'night_stay', 'device_offline', 'count_anomaly')),
  severity   TEXT    NOT NULL CHECK (severity IN ('info', 'warn', 'crit')),
  ts         INTEGER NOT NULL,
  ts_end     INTEGER,
  dedup_key  TEXT    NOT NULL UNIQUE,
  details    TEXT    NOT NULL DEFAULT '{}',
  created_ms INTEGER NOT NULL,
  acked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts      ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_type_ts ON alerts(type, ts);

CREATE TABLE IF NOT EXISTS device_logs (
  id            INTEGER PRIMARY KEY,
  event_id      TEXT    NOT NULL UNIQUE,
  device_id     TEXT    NOT NULL REFERENCES devices(device_id),
  kind          TEXT    NOT NULL CHECK (kind IN ('heartbeat', 'boot')),
  ts            INTEGER NOT NULL,
  boot_id       INTEGER NOT NULL,
  fps           REAL,
  infer_ms_p50  INTEGER,
  infer_ms_max  INTEGER,
  free_heap     INTEGER,
  min_free_heap INTEGER,
  stack_hwm     INTEGER,
  arena_used    INTEGER,
  dropped       INTEGER,
  wifi_rssi     INTEGER,
  mode          TEXT,
  scenario_id   TEXT,
  reset_reason  TEXT,
  received_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_dev_ts ON device_logs(device_id, ts);

CREATE TABLE IF NOT EXISTS occupancy_resets (
  id        INTEGER PRIMARY KEY,
  device_id TEXT    NOT NULL,              -- 외래키 없음: 기기가 접속하기 전에도 기준점을 넣을 수 있게(재생 데모)
  ts        INTEGER NOT NULL,
  reason    TEXT    NOT NULL CHECK (reason IN ('manual', 'scheduled', 'test'))
);
CREATE INDEX IF NOT EXISTS idx_resets_dev_ts ON occupancy_resets(device_id, ts);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT    PRIMARY KEY,
  value      INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
INSERT OR IGNORE INTO settings (key, value, updated_ms) VALUES ('t_long_ms', 1800000, 0);

CREATE TABLE IF NOT EXISTS settings_log (
  id         INTEGER PRIMARY KEY,
  key        TEXT    NOT NULL REFERENCES settings(key),
  old_value  INTEGER,
  new_value  INTEGER NOT NULL,
  changed_ms INTEGER NOT NULL
);

CREATE VIEW IF NOT EXISTS cctv_tags AS
  SELECT ts, NULL AS ts_end, 'passage_' || direction AS tag, id AS ref_id FROM passages
  UNION ALL
  SELECT ts, ts_end, type AS tag, id AS ref_id FROM alerts;
"""


def connect(path: str) -> sqlite3.Connection:
    # 요청 스레드 여러 개가 연결 하나를 공유하므로 호출부(app)에서 잠금으로 직렬화한다
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> int:
    return int(conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0])
