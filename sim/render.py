"""시나리오를 96×72 그레이스케일 프레임과 프레임별 머리 라벨로 렌더링한다."""

from __future__ import annotations

import math

import numpy as np

from sim.camera import FPS, HEIGHT, WIDTH
from sim.scenario import Scenario

SS = 3  # 서브픽셀 배율 (가장자리 안티앨리어싱)
_SUB_U = (np.arange(WIDTH * SS) + 0.5) / SS
_SUB_V = (np.arange(HEIGHT * SS) + 0.5) / SS
_YY, _XX = np.mgrid[0:HEIGHT, 0:WIDTH] + 0.5

HEAD_SHADE = 0.35  # 정수리 하이라이트 (위에서 본 머리의 둥근 느낌)


def _ellipse(cu: float, cv: float, a: float, b: float, theta: float, shade: float = 0.0):
    """타원이 덮는 픽셀 면적 비율(cov)과 음영 가중 밝기(lum)를 전체 프레임 크기로 반환."""
    r = max(a, b)
    j0 = max(0, int(math.floor((cu - r) * SS)) // SS * SS)
    j1 = min(WIDTH * SS, (int(math.ceil((cu + r) * SS)) // SS + 1) * SS)
    i0 = max(0, int(math.floor((cv - r) * SS)) // SS * SS)
    i1 = min(HEIGHT * SS, (int(math.ceil((cv + r) * SS)) // SS + 1) * SS)
    if j0 >= j1 or i0 >= i1:
        return None
    dx = _SUB_U[None, j0:j1] - cu
    dy = _SUB_V[i0:i1, None] - cv
    c, s = math.cos(theta), math.sin(theta)
    q = ((dx * c + dy * s) / a) ** 2 + ((-dx * s + dy * c) / b) ** 2
    inside = q <= 1.0
    h, w = (i1 - i0) // SS, (j1 - j0) // SS
    cov = np.zeros((HEIGHT, WIDTH))
    lum = np.zeros((HEIGHT, WIDTH))
    cov[i0 // SS:i1 // SS, j0 // SS:j1 // SS] = inside.reshape(h, SS, w, SS).mean(axis=(1, 3))
    lum[i0 // SS:i1 // SS, j0 // SS:j1 // SS] = np.where(inside, 1.0 + shade * (1.0 - q), 0.0).reshape(
        h, SS, w, SS).mean(axis=(1, 3))
    return cov, lum


def _surfaces(scn: Scenario, t: float) -> list[tuple]:
    """시각 t에 보이는 표면(어깨, 가방, 머리)을 높이 순으로. 높은 표면이 낮은 표면을 가린다."""
    cam = scn.camera
    out = []
    for actor in scn.actors:
        body = actor.body
        x, y, heading = actor.path.at(t)
        # 어깨(물체는 윗면)
        z = body.shoulder_z
        sx, sy = cam.px_per_m(z)
        cu, cv = cam.project(x, y, z)
        out.append((z, "body", actor.pid, cu, cv, body.shoulder_w / 2 * sx, body.shoulder_d / 2 * sy,
                    body.yaw, body.body_tone, 0.0))
        if body.kind != "person":
            continue
        if body.backpack:  # 진행 방향 뒤쪽
            z = 0.75 * body.height_m
            sx, sy = cam.px_per_m(z)
            cu, cv = cam.project(x, y - heading * (body.shoulder_d / 2 + 0.06), z)
            out.append((z, "pack", actor.pid, cu, cv, 0.15 * sx, 0.08 * sy, body.yaw, body.pack_tone, 0.0))
        z = body.head_z
        sx, sy = cam.px_per_m(z)
        cu, cv = cam.project(x, y, z)
        out.append((z, "head", actor.pid, cu, cv, body.head_w / 2 * sx, body.head_d / 2 * sy,
                    body.yaw, body.head_tone, HEAD_SHADE))
    return sorted(out, key=lambda s: s[0])


def _composite(scn: Scenario, bg: np.ndarray, t: float) -> tuple[np.ndarray, list[dict]]:
    img = bg.copy()
    head_maps: dict[int, np.ndarray] = {}
    heads = []
    for _z, kind, pid, cu, cv, a, b, theta, tone, shade in _surfaces(scn, t):
        res = _ellipse(cu, cv, a, b, theta, shade)
        if kind == "head":
            heads.append({"pid": pid, "x": cu, "y": cv, "area": math.pi * a * b})
        if res is None:
            continue
        cov, lum = res
        img = img * (1.0 - cov) + tone * lum
        for m in head_maps.values():  # 나중에(더 높게) 그려진 표면에 가려진 만큼 머리 가시 면적 감소
            m *= 1.0 - cov
        if kind == "head":
            head_maps[pid] = cov.copy()
    labels = []
    for h in sorted(heads, key=lambda h: h["pid"]):
        vis = float(head_maps[h["pid"]].sum() / h["area"]) if h["pid"] in head_maps else 0.0
        labels.append({"pid": h["pid"], "x": round(h["x"], 2), "y": round(h["y"], 2), "vis": round(min(vis, 1.0), 2)})
    return img, labels


def _bilinear(grid: np.ndarray) -> np.ndarray:
    gh, gw = grid.shape
    ys = np.linspace(0, gh - 1, HEIGHT)
    xs = np.linspace(0, gw - 1, WIDTH)
    rows = np.array([np.interp(xs, np.arange(gw), grid[i]) for i in range(gh)])
    return np.array([np.interp(ys, np.arange(gh), rows[:, j]) for j in range(WIDTH)]).T


def _grating(rng: np.random.Generator) -> np.ndarray:
    ang, period, phase = rng.uniform(0, math.pi), rng.uniform(3, 16), rng.uniform(0, 2 * math.pi)
    wave = np.sin(2 * math.pi * (_XX * math.cos(ang) + _YY * math.sin(ang)) / period + phase)
    if rng.random() < 0.5:
        wave = np.sign(wave)  # 사각파: 판재 이음매처럼 경계가 선명
    return rng.uniform(0.03, 0.12) * wave


def make_background(rng: np.random.Generator, texture: str, base: float) -> np.ndarray:
    grad = rng.uniform(-0.1, 0.1) * (_XX / WIDTH - 0.5) + rng.uniform(-0.1, 0.1) * (_YY / HEIGHT - 0.5)
    if texture == "plain":
        bg = base + grad
    elif texture == "tiles":
        size = rng.uniform(8, 14)
        ox, oy = rng.uniform(0, size, 2)
        ix = np.floor((_XX + ox) / size).astype(int)
        iy = np.floor((_YY + oy) / size).astype(int)
        jitter = rng.normal(0, 0.03, (iy.max() + 1, ix.max() + 1))
        bg = base + grad + jitter[iy, ix]
        grout = (((_XX + ox) % size) < 1.0) | (((_YY + oy) % size) < 1.0)
        bg = np.where(grout, bg * 0.7, bg)
    elif texture == "noise":
        bg = base + grad + rng.uniform(0.08, 0.15) * _bilinear(rng.normal(0, 1, (10, 13)))
    elif texture == "speckle":
        bg = base + grad + rng.normal(0, 0.03, (HEIGHT, WIDTH))
    elif texture == "grating":  # 마루·줄무늬 매트. 30%는 두 방향 교차
        bg = base + grad + _grating(rng)
        if rng.random() < 0.3:
            bg += _grating(rng)
    elif texture == "grid":
        ang, step = rng.uniform(0, math.pi), rng.uniform(6, 20)
        u = _XX * math.cos(ang) + _YY * math.sin(ang) + rng.uniform(0, step)
        v = -_XX * math.sin(ang) + _YY * math.cos(ang) + rng.uniform(0, step)
        lines = ((u % step) < 1.0) | ((v % step) < 1.0)
        bg = base + grad + np.where(lines, rng.choice([-1, 1]) * rng.uniform(0.05, 0.2), 0.0)
    elif texture == "mats":  # 매트·그림자 같은 큰 사각형
        bg = base + grad
        for _ in range(int(rng.integers(1, 5))):
            cx, cy = rng.uniform(0, WIDTH), rng.uniform(0, HEIGHT)
            hw, hh, ang = rng.uniform(5, 30), rng.uniform(5, 30), rng.uniform(0, math.pi)
            u = (_XX - cx) * math.cos(ang) + (_YY - cy) * math.sin(ang)
            v = -(_XX - cx) * math.sin(ang) + (_YY - cy) * math.cos(ang)
            bg = np.where((np.abs(u) < hw) & (np.abs(v) < hh), bg + rng.uniform(-0.3, 0.3), bg)
    elif texture == "multiscale":
        bg = base + grad
        for gh, gw in ((4, 5), (8, 11), (16, 21), (36, 48)):
            bg += rng.uniform(0.0, 0.08) * _bilinear(rng.normal(0, 1, (gh, gw)))
    elif texture == "terrazzo":  # test 전용
        bg = base + grad + rng.normal(0, 0.015, (HEIGHT, WIDTH))
        for _ in range(int(rng.integers(150, 400))):
            cx, cy, r = rng.uniform(0, WIDTH), rng.uniform(0, HEIGHT), rng.uniform(0.6, 2.0)
            chip = (_XX - cx) ** 2 + (_YY - cy) ** 2 <= r * r
            bg = np.where(chip, base + rng.choice([-1, 1]) * rng.uniform(0.1, 0.35), bg)
    else:
        raise ValueError(f"unknown texture: {texture}")
    return np.clip(bg, 0.0, 1.0)


def render(scn: Scenario) -> tuple[np.ndarray, list[dict]]:
    """프레임(uint8, [n, 72, 96])과 프레임별 머리 라벨 [{idx, heads: [{pid, x, y, vis}]}]."""
    rng = np.random.default_rng([scn.seed, 1])  # 궤적 샘플링과 독립된 렌더링 난수
    bg = make_background(rng, scn.texture, scn.base_tone)
    r2 = ((_XX - WIDTH / 2) ** 2 + (_YY - HEIGHT / 2) ** 2) / ((WIDTH / 2) ** 2 + (HEIGHT / 2) ** 2)
    vignette = 1.0 - scn.vignette * r2
    frames = np.empty((scn.n_frames, HEIGHT, WIDTH), dtype=np.uint8)
    labels = []
    for k in range(scn.n_frames):
        t = k / FPS
        img, heads = _composite(scn, bg, t)
        if scn.blur_s > 0:  # 노출 시간 동안의 이동을 두 시점 평균으로 근사 (배경은 그대로)
            img = 0.5 * (img + _composite(scn, bg, t + scn.blur_s)[0])
        img = np.clip(img * scn.light_gain, 0.0, 1.0) ** scn.light_gamma * vignette
        img = img + rng.normal(0, scn.noise_sigma / 255.0, img.shape)
        frames[k] = np.round(np.clip(img, 0.0, 1.0) * 255).astype(np.uint8)
        labels.append({"idx": k, "heads": heads})
    return frames, labels
