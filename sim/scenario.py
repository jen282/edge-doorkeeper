"""시나리오 샘플링: 등장 인물, 궤적, 화면 변이, 모의 인증. 분포 근거는 docs/05-data.md."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from sim.camera import FPS, HEIGHT, WIDTH, Camera
from sim.labels import compute_events, expected_alerts

BASE_START_MS = 1_793_682_000_000  # 2026-11-03 14:00 KST
SLOT_MS = 60_000  # 시나리오끼리 출입 세션이 섞이지 않도록 1분 간격으로 배치
SPLIT_SEED_BASE = {"train": 100_000, "val": 200_000, "test": 300_000}
DEFAULT_COUNT = {"train": 2500, "val": 200, "test": 300}

TEST_MIX = {"single": 35, "side_by_side": 15, "front_back": 15, "opposite": 10,
            "negative": 15, "group3": 5, "occluded_pair": 5}
# 학습은 밀착 통과를 과대 샘플링한다
TRAIN_MIX = {"single": 25, "side_by_side": 20, "front_back": 20, "opposite": 10,
             "negative": 15, "group3": 5, "occluded_pair": 5}
MIX = {"train": TRAIN_MIX, "val": TEST_MIX, "test": TEST_MIX}
ID_PREFIX = {"single": "sg", "side_by_side": "sb", "front_back": "fb", "opposite": "op",
             "negative": "ng", "group3": "g3", "occluded_pair": "oc"}

# test의 '학습에 있던 질감' 절반. 이 목록을 바꾸면 test 난수 흐름과 정답 해시가 바뀐다
SEEN_TEXTURES = ("plain", "tiles", "noise", "speckle")
# 학습·검증은 질감을 넓게 무작위화한다(도메인 랜덤화). 처음엔 위 4종만 썼다가, 처음 보는 줄무늬
# 바닥에서 바닥 전체를 머리로 판단하는 지름길 학습이 드러나 추가했다 (docs/05-data.md 1.4절)
TRAIN_TEXTURES = (*SEEN_TEXTURES, "grating", "grid", "mats", "multiscale")
HELDOUT_TEXTURE = "terrazzo"  # test 전용: 학습 질감과 통계가 다른 작은 돌 조각(블롭) 바닥

Y_FAR = 1.0  # 시작·끝 위치(m). 아이·카트의 어깨·윗면까지 화면 밖에 있도록 충분히 멀리


@dataclass
class Body:
    kind: str  # "person" | "object"
    height_m: float  # 사람은 키, 물체는 윗면 높이
    shoulder_w: float
    shoulder_d: float
    head_w: float
    head_d: float
    head_tone: float
    body_tone: float
    yaw: float
    backpack: bool = False
    pack_tone: float = 0.5

    @property
    def head_z(self) -> float:
        return self.height_m - 0.04  # 정수리보다 약간 아래(머리 윤곽이 가장 넓은 높이)

    @property
    def shoulder_z(self) -> float:
        return 0.82 * self.height_m if self.kind == "person" else self.height_m


@dataclass
class Path:
    """웨이포인트(시각, x, y)를 선형 보간하고, 걷는 동안만 좌우 흔들림을 더한다."""

    t: list[float]
    x: list[float]
    y: list[float]
    sway_amp: float = 0.0
    sway_hz: float = 1.0
    sway_phase: float = 0.0
    dir0: int = 1

    def at(self, t: float) -> tuple[float, float, int]:
        x = float(np.interp(t, self.t, self.x))
        y = float(np.interp(t, self.t, self.y))
        k = int(np.clip(np.searchsorted(self.t, t) - 1, 0, len(self.t) - 2))
        dt = self.t[k + 1] - self.t[k]
        vy = (self.y[k + 1] - self.y[k]) / dt if dt > 0 and self.t[0] <= t <= self.t[-1] else 0.0
        moving = min(abs(vy) / 0.5, 1.0)
        x += moving * self.sway_amp * math.sin(2 * math.pi * self.sway_hz * t + self.sway_phase)
        heading = (1 if vy > 0 else -1) if abs(vy) > 0.05 else self.dir0
        return x, y, heading

    @property
    def end_t(self) -> float:
        return self.t[-1]


@dataclass
class Actor:
    pid: int
    body: Body
    path: Path


@dataclass
class Scenario:
    scenario_id: str
    seed: int
    split: str
    category: str
    subtype: str
    n_frames: int
    start_ms: int
    texture: str
    base_tone: float
    light_gain: float
    light_gamma: float
    vignette: float
    noise_sigma: float
    blur_s: float
    actors: list[Actor]
    gt_events: list[dict] = field(default_factory=list)
    auth: list[dict] = field(default_factory=list)
    expected_alerts: list[str] = field(default_factory=list)
    camera: Camera = field(default_factory=Camera)

    def meta(self) -> dict:
        """프레임 라벨을 제외한 시나리오 메타데이터 (scenarios.json 항목)."""
        return {
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "split": self.split,
            "category": self.category,
            "subtype": self.subtype,
            "fps": FPS,
            "width": WIDTH,
            "height": HEIGHT,
            "scenario_start_ms": self.start_ms,
            "n_frames": self.n_frames,
            "idle_gap_s": 0,
            "camera": self.camera.to_dict(),
            "texture": self.texture,
            "lighting": {"gain": round(self.light_gain, 3), "gamma": round(self.light_gamma, 3),
                         "vignette": round(self.vignette, 3), "noise_sigma": round(self.noise_sigma, 2),
                         "blur_s": round(self.blur_s, 3)},
            "persons": [{"pid": a.pid, "kind": a.body.kind, "height_m": round(a.body.height_m, 3),
                         "backpack": a.body.backpack} for a in self.actors],
            "gt_events": self.gt_events,
            "auth": self.auth,
            "expected_alerts": self.expected_alerts,
        }


# ---------- 인물 ----------

def sample_person(rng: np.random.Generator, child: bool = False) -> Body:
    h = rng.uniform(1.0, 1.3) if child else rng.uniform(1.50, 1.90)
    s = h / 1.75  # 체격을 키에 비례시킨다
    head_tone = rng.uniform(0.05, 0.25) if rng.random() < 0.7 else rng.uniform(0.6, 0.95)
    body_tone = rng.uniform(0.1, 0.9)
    while abs(body_tone - head_tone) < 0.1:  # 머리와 어깨가 한 덩어리로 보이지 않게
        body_tone = rng.uniform(0.1, 0.9)
    return Body(
        kind="person", height_m=h,
        shoulder_w=rng.uniform(0.40, 0.50) * s, shoulder_d=rng.uniform(0.22, 0.28) * s,
        head_w=rng.uniform(0.15, 0.20) * (0.85 + 0.15 * s), head_d=rng.uniform(0.18, 0.22) * (0.85 + 0.15 * s),
        head_tone=head_tone, body_tone=body_tone, yaw=rng.uniform(-0.26, 0.26),
        backpack=bool(rng.random() < 0.2), pack_tone=rng.uniform(0.1, 0.9),
    )


def sample_object(rng: np.random.Generator) -> Body:
    """머리가 없는 이동 물체(카트·상자). 머리로 착각하면 안 되는 블롭."""
    return Body(kind="object", height_m=rng.uniform(0.8, 1.0), shoulder_w=rng.uniform(0.35, 0.5),
                shoulder_d=rng.uniform(0.45, 0.65), head_w=0.0, head_d=0.0, head_tone=0.0,
                body_tone=rng.uniform(0.1, 0.9), yaw=rng.uniform(-0.2, 0.2))


# ---------- 궤적 ----------

def _sway(rng: np.random.Generator, amp_max: float = 0.05) -> dict:
    return {"sway_amp": rng.uniform(0.02, amp_max), "sway_hz": rng.uniform(0.8, 1.2),
            "sway_phase": rng.uniform(0, 2 * math.pi)}


def walk(t0: float, x0: float, direction: int, speed: float, sway: dict,
         pause_y: float | None = None, pause_s: float = 0.0) -> Path:
    """바깥(-Y_FAR) ↔ 안쪽(+Y_FAR) 직선 통과. direction=+1이 입장."""
    y0, y1 = -direction * Y_FAR, direction * Y_FAR
    if pause_y is None:
        return Path([t0, t0 + 2 * Y_FAR / speed], [x0, x0], [y0, y1], dir0=direction, **sway)
    t_p = t0 + abs(pause_y - y0) / speed
    t_end = t_p + pause_s + abs(y1 - pause_y) / speed
    return Path([t0, t_p, t_p + pause_s, t_end], [x0] * 4, [y0, pause_y, pause_y, y1], dir0=direction, **sway)


def uturn(t0: float, x0: float, direction: int, speed: float, sway: dict, turn_y: float, pause_s: float) -> Path:
    y0 = -direction * Y_FAR
    t_turn = t0 + abs(turn_y - y0) / speed
    t_back = t_turn + pause_s + abs(turn_y - y0) / speed
    return Path([t0, t_turn, t_turn + pause_s, t_back], [x0] * 4, [y0, turn_y, turn_y, y0], dir0=direction, **sway)


def loiter(rng: np.random.Generator, t0: float, x0: float, direction: int, speed: float, sway: dict,
           cross: bool) -> Path:
    """가운데 선 근처에서 앞뒤로 흔들리다가 통과하거나 되돌아간다."""
    y0 = -direction * Y_FAR
    # 키 1.9m 머리 기준 ±0.105m가 라인 A·C(±12px)이므로 흔들림을 ±0.09m 안에 둔다
    yc = rng.uniform(-0.02, 0.02)
    t_arr = t0 + abs(yc - y0) / speed
    amp, hz, dur = rng.uniform(0.04, 0.07), rng.uniform(0.5, 0.9), rng.uniform(1.5, 3.0)
    ts, xs, ys = [t0], [x0], [y0]
    for tt in np.arange(0.0, dur, 0.1):
        ts.append(t_arr + tt)
        xs.append(x0)
        ys.append(yc + amp * math.sin(2 * math.pi * hz * tt))
    y_end = direction * Y_FAR if cross else y0
    ts.append(ts[-1] + abs(y_end - ys[-1]) / speed)
    xs.append(x0)
    ys.append(y_end)
    return Path(ts, xs, ys, dir0=direction, **sway)


# ---------- 범주별 구성 ----------

def _pair_direction(rng: np.random.Generator) -> int:
    return 1 if rng.random() < 0.7 else -1


def build_actors(rng: np.random.Generator, category: str) -> tuple[list[Actor], str]:
    t0 = rng.uniform(0.2, 1.0)
    if category == "single":
        d = 1 if rng.random() < 0.6 else -1
        speed = rng.uniform(0.8, 1.6)
        if rng.random() < 0.2:
            path = walk(t0, rng.uniform(-0.4, 0.4), d, speed, _sway(rng),
                        pause_y=rng.uniform(-0.3, 0.3), pause_s=rng.uniform(0.3, 1.0))
            sub = "pause"
        else:
            path = walk(t0, rng.uniform(-0.4, 0.4), d, speed, _sway(rng))
            sub = "walk"
        return [Actor(0, sample_person(rng), path)], f"{sub}_{'in' if d > 0 else 'out'}"

    if category in ("side_by_side", "occluded_pair") and (category == "side_by_side" or rng.random() < 0.5):
        d = _pair_direction(rng)
        sep = rng.uniform(0.20, 0.50) if category == "side_by_side" else rng.uniform(0.10, 0.18)
        c, speed = rng.uniform(-0.2, 0.2), rng.uniform(0.8, 1.5)
        sway = _sway(rng, 0.03)
        child = category == "occluded_pair" and rng.random() < 0.3
        a = Actor(0, sample_person(rng), walk(t0, c - sep / 2, d, speed, sway))
        b = Actor(1, sample_person(rng, child=child),
                  walk(t0 + rng.uniform(0, 0.15), c + sep / 2, d, speed + rng.uniform(-0.05, 0.05), sway))
        return [a, b], f"side_{'in' if d > 0 else 'out'}"

    if category in ("front_back", "occluded_pair"):
        d = _pair_direction(rng)
        gap = rng.uniform(0.30, 0.80) if category == "front_back" else rng.uniform(0.10, 0.18)
        c, speed = rng.uniform(-0.3, 0.3), rng.uniform(0.8, 1.5)
        child = category == "occluded_pair" and rng.random() < 0.3
        a = Actor(0, sample_person(rng), walk(t0, c, d, speed, _sway(rng, 0.03)))
        b = Actor(1, sample_person(rng, child=child),
                  walk(t0 + gap / speed, c + rng.uniform(-0.1, 0.1), d, speed + rng.uniform(-0.05, 0.05),
                       _sway(rng, 0.03)))
        return [a, b], f"front_back_{'in' if d > 0 else 'out'}"

    if category == "group3":
        d = _pair_direction(rng)
        c, speed = rng.uniform(-0.2, 0.2), rng.uniform(0.8, 1.4)
        actors, t = [], t0
        for pid in range(3):
            actors.append(Actor(pid, sample_person(rng),
                                walk(t, c + rng.uniform(-0.15, 0.15), d, speed + rng.uniform(-0.05, 0.05),
                                     _sway(rng, 0.03))))
            t += rng.uniform(0.35, 0.9) / speed
        return actors, f"chain_{'in' if d > 0 else 'out'}"

    if category == "opposite":
        c, sep = rng.uniform(-0.15, 0.15), rng.uniform(0.35, 0.6)
        t_cross = t0 + 1.0
        actors = []
        for pid, (d, x) in enumerate(((1, c - sep / 2), (-1, c + sep / 2))):
            speed = rng.uniform(0.8, 1.5)
            start = t_cross + rng.uniform(-0.4, 0.4) - Y_FAR / speed
            actors.append(Actor(pid, sample_person(rng), walk(start, x, d, speed, _sway(rng, 0.03))))
        shift = max(0.0, 0.2 - min(a.path.t[0] for a in actors))
        for a in actors:
            a.path.t = [t + shift for t in a.path.t]
        return actors, "cross"

    if category == "negative":
        r = rng.random()
        d = 1 if rng.random() < 0.6 else -1
        speed = rng.uniform(0.8, 1.4)
        if r < 0.3:
            path = uturn(t0, rng.uniform(-0.4, 0.4), d, speed, _sway(rng),
                         turn_y=rng.uniform(-0.06, 0.06), pause_s=rng.uniform(0.0, 0.8))
            return [Actor(0, sample_person(rng), path)], "uturn"
        if r < 0.6:
            cross = bool(rng.random() < 0.5)
            path = loiter(rng, t0, rng.uniform(-0.3, 0.3), d, speed, _sway(rng), cross)
            return [Actor(0, sample_person(rng), path)], "loiter_cross" if cross else "loiter_return"
        if r < 0.8:
            return [], "empty"
        path = walk(t0, rng.uniform(-0.3, 0.3), d, rng.uniform(0.6, 1.2), _sway(rng, 0.02))
        return [Actor(0, sample_object(rng), path)], "object"

    raise ValueError(f"unknown category: {category}")


# ---------- 모의 인증 ----------

def build_auth(rng: np.random.Generator, scenario_id: str, category: str, subtype: str,
               actors: list[Actor], events: list[dict]) -> list[dict]:
    """패턴: 정상(인증=입장), 함께 인증, 동반입장(인증 1 + 입장 여러 명), 미인증, 인증 후 U턴."""
    ins = [e for e in events if e["direction"] == "in"]
    times: list[float] = []
    if ins:
        all_auth_p = {"single": 0.85, "opposite": 1.0, "side_by_side": 0.5, "front_back": 0.3,
                      "group3": 0.3, "occluded_pair": 0.3, "negative": 0.85}[category]
        if rng.random() < all_auth_p:
            times = [e["ts_offset_ms"] - rng.uniform(500, 3000) for e in ins]
        elif len(ins) > 1:
            times = [ins[0]["ts_offset_ms"] - rng.uniform(500, 3000)]
        # 단독 입장에서 인증을 건너뛰면 미인증 입장
    elif subtype in ("uturn", "loiter_return") and actors and actors[0].path.dir0 > 0 and rng.random() < 0.3:
        times = [actors[0].path.t[0] * 1000 + rng.uniform(500, 1500)]  # 인증만 하고 들어오지 않음
    return [{"auth_id": f"{scenario_id}-a{k}", "ts_offset_ms": int(round(t)), "allowed_count": 1}
            for k, t in enumerate(sorted(times))]


# ---------- 시나리오 ----------

def assign_categories(split: str, n: int) -> list[str]:
    """비율대로 범주를 정확히 배분(최대 잉여 방식)한 뒤 split 시드로 섞는다."""
    mix = MIX[split]
    total = sum(mix.values())
    raw = {k: n * v / total for k, v in mix.items()}
    counts = {k: int(math.floor(x)) for k, x in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: n - sum(counts.values())]:
        counts[k] += 1
    cats = [k for k in mix for _ in range(counts[k])]
    np.random.default_rng(SPLIT_SEED_BASE[split]).shuffle(cats)
    return cats


def build_scenario(split: str, index: int, category: str, camera: Camera | None = None) -> Scenario:
    cam = camera or Camera()
    seed = SPLIT_SEED_BASE[split] + index
    rng = np.random.default_rng(seed)
    scenario_id = f"{ID_PREFIX[category]}_{index:04d}"
    actors, subtype = build_actors(rng, category)

    end = max((a.path.end_t for a in actors), default=rng.uniform(2.0, 4.0))
    duration = max(end + rng.uniform(0.3, 0.8), 1.5)
    n_frames = int(math.ceil(duration * FPS))

    if split == "test":
        if rng.random() < 0.5:
            texture = HELDOUT_TEXTURE
        else:
            texture = SEEN_TEXTURES[int(rng.integers(len(SEEN_TEXTURES)))]
    else:
        texture = TRAIN_TEXTURES[int(rng.integers(len(TRAIN_TEXTURES)))]
    if split == "test" and rng.random() < 0.2:  # 극단 조명
        gain = rng.uniform(0.45, 0.6) if rng.random() < 0.5 else rng.uniform(1.5, 1.8)
    else:
        gain = rng.uniform(0.75, 1.25)

    events = compute_events(actors, cam, n_frames / FPS)
    auth = build_auth(rng, scenario_id, category, subtype, actors, events)
    return Scenario(
        scenario_id=scenario_id, seed=seed, split=split, category=category, subtype=subtype,
        n_frames=n_frames, start_ms=BASE_START_MS + index * SLOT_MS, texture=texture,
        base_tone=rng.uniform(0.25, 0.75), light_gain=gain, light_gamma=rng.uniform(0.85, 1.2),
        vignette=rng.uniform(0.0, 0.3), noise_sigma=rng.uniform(2.0, 8.0),
        blur_s=rng.uniform(0.01, 0.04) if rng.random() < 0.5 else 0.0,
        actors=actors, gt_events=events, auth=auth, expected_alerts=expected_alerts(events, auth), camera=cam,
    )
