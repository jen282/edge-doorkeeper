import numpy as np
import pytest

from ml.postproc import PostprocConfig, find_peaks, run_scenario
from ml.train import make_target


def heatmap(*heads_px: tuple[float, float]) -> np.ndarray:
    """학습 정답(ml.train.make_target)과 같은 이상적인 히트맵. 화면 밖 머리는 보이지 않는다(vis=0)."""
    heads = np.array([(x, y, float(0 <= x < 96 and 0 <= y < 72)) for x, y in heads_px], np.float32).reshape(-1, 3)
    return make_target(heads)[..., 0]


def walk(ys: np.ndarray, *xs: float, dy: float = 0.0) -> np.ndarray:
    return np.stack([heatmap(*[(x, y + i * dy) for i, x in enumerate(xs)]) for y in ys])


def directions(events: list[dict]) -> list[str]:
    return [e["direction"] for e in events]


def test_peak_position_is_refined_within_cell():
    (x, y, _), = find_peaks(heatmap((50.0, 30.0)), PostprocConfig())
    assert x == pytest.approx(50.0, abs=2.0) and y == pytest.approx(30.0, abs=2.0)


def test_close_peaks_merge_but_adjacent_heads_stay_apart():
    cfg = PostprocConfig()
    assert len(find_peaks(heatmap((40, 36), (46, 36)), cfg)) == 1  # 0.75칸: 같은 머리
    assert len(find_peaks(heatmap((36, 36), (60, 36)), cfg)) == 2  # 3칸: 나란히 선 두 사람


def test_single_in_and_out():
    ys = np.arange(4, 72, 12.0)  # 프레임당 12px
    assert directions(run_scenario(walk(ys, 48))) == ["in"]
    assert directions(run_scenario(walk(ys[::-1], 48))) == ["out"]


def test_fast_walker_is_tracked():
    # 키 큰 사람이 빠르게 걸으면 프레임당 약 23px. 새 트랙 게이트(3.5칸=28px) 안
    assert directions(run_scenario(walk(np.arange(6, 72, 22.0), 48))) == ["in"]


def test_side_by_side_counts_two():
    assert directions(run_scenario(walk(np.arange(4, 72, 10.0), 34, 58))) == ["in", "in"]


def test_front_back_counts_two():
    # 앞뒤 간격 28px(3.5칸), 같은 속도로 함께 이동
    hms = walk(np.arange(-24, 110, 10.0), 48, 48, dy=-28.0)
    assert directions(run_scenario(hms)) == ["in", "in"]


def test_uturn_has_no_event():
    ys = np.concatenate([np.arange(4, 40, 8.0), np.arange(40, 0, -8.0)])
    assert run_scenario(walk(ys, 48)) == []


def test_jitter_on_line_counts_once():
    ys = np.concatenate([np.arange(4, 50, 10.0), [47, 50, 46, 51, 47, 52], np.arange(55, 72, 8.0)])
    assert directions(run_scenario(walk(ys, 48))) == ["in"]


def test_event_time_is_mid_line_crossing():
    ys = np.arange(4, 72, 8.0)  # y=36은 프레임 4 (500ms)
    (e,) = run_scenario(walk(ys, 48))
    assert e["ts_offset_ms"] == pytest.approx(500, abs=30)
