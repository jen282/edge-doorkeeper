"""후처리: 히트맵 → 머리 피크 → 추적 → 라인 통과 이벤트. docs/04-model.md 1.4절.

펌웨어 C 구현(firmware/components/postproc)과 같은 알고리즘이어야 한다(CI 평가는 이 구현으로 한다).
동점 처리 순서까지 정해 두어 두 구현의 결과가 같게 만든다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

STRIDE = 8
LINE_A, LINE_MID, LINE_C = 24.0, 36.0, 48.0
ZONE_A, ZONE_B, ZONE_C = 0, 1, 2


@dataclass(frozen=True)
class PostprocConfig:
    threshold: float = 0.5  # 피크 최소 확률
    merge_cells: float = 1.5  # 이 거리(칸) 이내 피크는 하나로 병합. 붙은 두 머리(약 3칸)는 보존
    gate_cells: float = 2.5  # 예측 위치와 검출의 최대 거리 (속도를 아는 트랙)
    gate_init_cells: float = 3.5  # 속도를 아직 모르는 새 트랙. 빠른 보행은 프레임당 약 23px 이동
    max_tracks: int = 4
    max_misses: int = 2  # 연속으로 이만큼 놓치면 트랙 삭제


def zone_of(y: float) -> int:
    return ZONE_A if y < LINE_A else ZONE_C if y > LINE_C else ZONE_B


def find_peaks(hm: np.ndarray, cfg: PostprocConfig) -> list[tuple[float, float, float]]:
    """hm [9, 12] 확률 → [(x_px, y_px, score)], 점수 내림차순."""
    h, w = hm.shape
    pad = np.pad(hm.astype(np.float64), 1, constant_values=-1.0)
    cands = []
    for i in range(h):
        for j in range(w):
            p = float(hm[i, j])
            win = pad[i:i + 3, j:j + 3]
            if p < cfg.threshold or p < win.max():
                continue
            # 3×3 가중 평균으로 칸 안의 위치를 보정 (음수 패딩은 가중치 0)
            wts = np.clip(win, 0.0, None)
            s = wts.sum()
            dy = (wts.sum(axis=1) * np.array([-1.0, 0.0, 1.0])).sum() / s
            dx = (wts.sum(axis=0) * np.array([-1.0, 0.0, 1.0])).sum() / s
            cands.append(((j + 0.5 + dx) * STRIDE, (i + 0.5 + dy) * STRIDE, p, i, j))
    cands.sort(key=lambda c: (-c[2], c[3], c[4]))  # 점수, 행, 열 순 (C 구현과 같은 순서)
    peaks: list[tuple[float, float, float]] = []
    lim = cfg.merge_cells * STRIDE
    for x, y, p, _, _ in cands:  # 평탄한 최대값(동점 이웃)도 여기서 하나로 정리된다
        if all(math.hypot(x - qx, y - qy) > lim for qx, qy, _ in peaks):
            peaks.append((x, y, p))
    return peaks


@dataclass
class Track:
    tid: int
    x: float  # 마지막으로 관측한 위치
    y: float
    t_ms: float  # 마지막 관측 시각
    start: int  # 계수 기준 구역 (A 또는 C)
    vx: float = 0.0  # 프레임당 이동량
    vy: float = 0.0
    misses: int = 0
    observed: int = 1
    mid_t_ms: float | None = None  # 가운데 선을 마지막으로 지난 시각

    def predict(self) -> tuple[float, float]:
        k = self.misses + 1
        return self.x + self.vx * k, self.y + self.vy * k


class LineCounter:
    """프레임마다 피크를 받아 트랙을 갱신하고, 구역 A↔C를 건넌 트랙을 한 번씩 계수한다."""

    def __init__(self, cfg: PostprocConfig | None = None, fps: float = 8.0):
        self.cfg = cfg or PostprocConfig()
        self.frame_ms = 1000.0 / fps
        self.tracks: list[Track] = []
        self.next_id = 0

    def _assign(self, peaks: list[tuple[float, float, float]]) -> dict[int, int]:
        """트랙 수 ≤ 4라 모든 조합을 따져 '매칭 수 최대 → 거리 합 최소'를 고른다."""
        cost = []
        for tr in self.tracks:
            px, py = tr.predict()
            gate = (self.cfg.gate_cells if tr.observed > 1 else self.cfg.gate_init_cells) * STRIDE
            row = []
            for x, y, _ in peaks:
                d = math.hypot(x - px, y - py)
                row.append(d if d <= gate else None)
            cost.append(row)

        best: tuple[int, float, dict[int, int]] = (0, 0.0, {})

        def search(ti: int, used: set[int], n: int, total: float, cur: dict[int, int]) -> None:
            nonlocal best
            if ti == len(self.tracks):
                if n > best[0] or (n == best[0] and total < best[1] - 1e-9):
                    best = (n, total, dict(cur))
                return
            for pi, d in enumerate(cost[ti]):
                if d is not None and pi not in used:
                    used.add(pi)
                    cur[ti] = pi
                    search(ti + 1, used, n + 1, total + d, cur)
                    del cur[ti]
                    used.discard(pi)
            search(ti + 1, used, n, total, cur)

        search(0, set(), 0, 0.0, {})
        return best[2]

    def update(self, peaks: list[tuple[float, float, float]], frame_idx: int) -> list[dict]:
        t_ms = frame_idx * self.frame_ms
        assign = self._assign(peaks)
        events = []
        for ti, tr in enumerate(self.tracks):
            if ti not in assign:
                tr.misses += 1
                continue
            x, y, _ = peaks[assign[ti]]
            k = tr.misses + 1
            if tr.observed == 1:
                tr.vx, tr.vy = (x - tr.x) / k, (y - tr.y) / k
            else:
                tr.vx = 0.5 * tr.vx + 0.5 * (x - tr.x) / k
                tr.vy = 0.5 * tr.vy + 0.5 * (y - tr.y) / k
            if (tr.y - LINE_MID) * (y - LINE_MID) <= 0 and y != tr.y:
                tr.mid_t_ms = tr.t_ms + (LINE_MID - tr.y) / (y - tr.y) * (t_ms - tr.t_ms)
            tr.x, tr.y, tr.t_ms = x, y, t_ms
            tr.misses = 0
            tr.observed += 1
            z = zone_of(y)
            if z != ZONE_B and z != tr.start:
                events.append({
                    "track": tr.tid,
                    "direction": "in" if tr.start == ZONE_A else "out",
                    "ts_offset_ms": round(tr.mid_t_ms if tr.mid_t_ms is not None else t_ms),
                    "frame": frame_idx,
                })
                tr.start = z
        self.tracks = [tr for tr in self.tracks if tr.misses < self.cfg.max_misses]

        matched = set(assign.values())
        for pi, (x, y, _) in enumerate(peaks):  # 점수 순이라 트랙 수가 차면 약한 피크부터 버려진다
            if pi in matched or len(self.tracks) >= self.cfg.max_tracks:
                continue
            z = zone_of(y)
            start = z if z != ZONE_B else (ZONE_A if y < LINE_MID else ZONE_C)
            self.tracks.append(Track(self.next_id, x, y, t_ms, start))
            self.next_id += 1
        return events


def run_scenario(heatmaps: np.ndarray, cfg: PostprocConfig | None = None, fps: float = 8.0) -> list[dict]:
    """[n, 9, 12] 확률 히트맵 → 통과 이벤트 목록."""
    counter = LineCounter(cfg, fps)
    events = []
    for k, hm in enumerate(heatmaps):
        events += counter.update(find_peaks(hm, counter.cfg), k)
    return events
