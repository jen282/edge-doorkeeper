import json

import numpy as np
import pytest

from sim.camera import HEIGHT, LINE_MID, WIDTH, Camera
from sim.labels import compute_events, expected_alerts
from sim.render import render
from sim.scenario import (
    TEST_MIX,
    Actor,
    assign_categories,
    build_scenario,
    sample_person,
    uturn,
    walk,
)

CAM = Camera()


def _person(seed: int = 0):
    return sample_person(np.random.default_rng(seed))


def test_camera_center_projects_to_image_center():
    assert CAM.project(0.0, 0.0, 1.7) == (WIDTH / 2, HEIGHT / 2)


def test_walk_in_creates_one_in_event_at_mid_line():
    speed = 1.2
    actor = Actor(0, _person(), walk(0.5, 0.0, +1, speed, {}))
    events = compute_events([actor], CAM, 3.0)
    assert [e["direction"] for e in events] == ["in"]
    # y=0(가운데 선)에 도달하는 시각 = 출발 + Y_FAR / speed
    assert events[0]["ts_offset_ms"] == pytest.approx((0.5 + 1.0 / speed) * 1000, abs=10)


def test_walk_out_creates_out_event():
    actor = Actor(0, _person(), walk(0.2, 0.1, -1, 1.0, {}))
    assert [e["direction"] for e in compute_events([actor], CAM, 3.0)] == ["out"]


def test_uturn_creates_no_event():
    actor = Actor(0, _person(), uturn(0.2, 0.0, +1, 1.0, {}, turn_y=0.0, pause_s=0.5))
    assert compute_events([actor], CAM, 4.0) == []


@pytest.mark.parametrize(("auth_ms", "in_ms", "expected"), [
    ([0], [1500], []),                              # 정상
    ([0], [1500, 1900], ["tailgate"]),              # 인증 1 + 입장 2 (TC-04)
    ([0, 1000], [1500, 2000], []),                  # 연속 인증 2 + 입장 2 (TC-06)
    ([], [1500], ["unauth_entry"]),                 # 미인증 (TC-05)
    ([0], [], []),                                  # 인증 후 들어오지 않음
    ([0], [1500, 60_000], ["unauth_entry"]),        # 두 번째 입장은 별도 세션
])
def test_expected_alerts_session_rule(auth_ms, in_ms, expected):
    auth = [{"ts_offset_ms": t, "allowed_count": 1} for t in auth_ms]
    events = [{"pid": i, "direction": "in", "ts_offset_ms": t} for i, t in enumerate(in_ms)]
    assert expected_alerts(events, auth) == expected


def test_category_mix_is_exact_for_test_split():
    cats = assign_categories("test", 300)
    for cat, pct in TEST_MIX.items():
        assert cats.count(cat) == 300 * pct // 100


def test_scenario_is_deterministic():
    a = build_scenario("test", 7, "front_back")
    b = build_scenario("test", 7, "front_back")
    assert json.dumps(a.meta()) == json.dumps(b.meta())
    fa, la = render(a)
    fb, lb = render(b)
    assert np.array_equal(fa, fb) and la == lb


def test_render_shapes_and_head_labels():
    scn = build_scenario("test", 3, "single")
    frames, labels = render(scn)
    assert frames.shape == (scn.n_frames, HEIGHT, WIDTH) and frames.dtype == np.uint8
    # 가운데 선 근처 프레임에서 머리는 거의 다 보여야 한다
    near_mid = [h for f in labels for h in f["heads"] if abs(h["y"] - LINE_MID) < 6 and 10 < h["x"] < WIDTH - 10]
    assert near_mid and all(h["vis"] > 0.9 for h in near_mid)


def test_occluded_pair_hides_part_of_a_head():
    vis = []
    for i in range(10):
        _, labels = render(build_scenario("test", 1000 + i, "occluded_pair"))
        vis += [h["vis"] for f in labels for h in f["heads"] if 20 < h["y"] < 52]
    assert min(vis) < 0.9  # 붙은 두 사람 중 한쪽 머리는 일부가 가려진다
