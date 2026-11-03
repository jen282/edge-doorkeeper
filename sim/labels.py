"""정답 생성: 통과 이벤트와 기대 알림. 기준은 docs/05-data.md 1.3절."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from sim.camera import LINE_A, LINE_C, LINE_MID, Camera

if TYPE_CHECKING:
    from sim.scenario import Actor

SESSION_GAP_MS = 5000  # 서버 규칙과 같은 값 (docs/07-database.md 4.1절)
_DT = 0.005  # 이벤트 시각 계산용 시간 해상도(초). 프레임(125ms)보다 훨씬 촘촘하게 본다


def head_v(actor: Actor, cam: Camera, t: np.ndarray) -> np.ndarray:
    """시각 배열 t에서 머리 중심의 영상 y좌표(px)."""
    ys = np.array([actor.path.at(float(ti))[1] for ti in t])
    _, sy = cam.px_per_m(actor.body.head_z)
    return LINE_MID + sy * ys


def compute_events(actors: list[Actor], cam: Camera, duration_s: float) -> list[dict]:
    """머리 중심이 A 구역에서 C 구역까지 가면 IN, 반대면 OUT. 시각은 가운데 선을 마지막으로 지난 순간.

    후처리(라인 2개 히스테리시스)와 같은 규칙이라 U턴·서성임은 이벤트가 되지 않는다.
    """
    t = np.arange(0.0, duration_s + _DT, _DT)
    events = []
    for actor in actors:
        if actor.body.kind != "person":
            continue
        v = head_v(actor, cam, t)
        zone = np.where(v < LINE_A, 0, np.where(v > LINE_C, 2, 1))
        start_zone = None
        mid_cross_t = None
        for i in range(len(t)):
            if i > 0 and (v[i - 1] - LINE_MID) * (v[i] - LINE_MID) <= 0 and v[i] != v[i - 1]:
                mid_cross_t = t[i - 1] + (LINE_MID - v[i - 1]) / (v[i] - v[i - 1]) * _DT
            z = zone[i]
            if z == 1:
                continue
            if start_zone is None:
                start_zone = z
            elif z != start_zone:
                events.append({
                    "pid": actor.pid,
                    "direction": "in" if start_zone == 0 else "out",
                    "ts_offset_ms": int(round(mid_cross_t * 1000)),
                })
                start_zone = z
    return sorted(events, key=lambda e: (e["ts_offset_ms"], e["pid"]))


def expected_alerts(events: list[dict], auth: list[dict]) -> list[str]:
    """인증과 IN을 시각순으로 묶은 출입 세션에서 IN 수가 허용 인원을 넘으면 알림."""
    items = [(a["ts_offset_ms"], a["allowed_count"], 0) for a in auth]
    items += [(e["ts_offset_ms"], 0, 1) for e in events if e["direction"] == "in"]
    items.sort(key=lambda x: x[0])
    alerts = []
    allowed = n_in = 0
    prev_ts = None
    for ts, a, n in items:
        if prev_ts is not None and ts - prev_ts > SESSION_GAP_MS:
            alerts += _judge(allowed, n_in)
            allowed = n_in = 0
        allowed += a
        n_in += n
        prev_ts = ts
    if prev_ts is not None:
        alerts += _judge(allowed, n_in)
    return alerts


def _judge(allowed: int, n_in: int) -> list[str]:
    if n_in <= allowed:
        return []
    return ["tailgate" if allowed > 0 else "unauth_entry"]
