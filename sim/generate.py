"""합성 데이터셋 생성 CLI.

    python -m sim.generate --split test --out data/test     # scenarios.json + frames.npz
    python -m sim.generate --split train --out data/train   # train.npz (프레임 샘플) + scenarios.json

val/test는 시나리오 전체 프레임을 저장한다(이벤트 단위 평가용).
train은 시나리오마다 일부 프레임만 뽑아 저장한다(프레임 단위 학습용).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from sim.render import render
from sim.scenario import DEFAULT_COUNT, SPLIT_SEED_BASE, assign_categories, build_scenario

MAX_HEADS = 4  # 한 프레임에 등장하는 최대 인원 (group3 = 3)
VIS_MIN = 0.5  # 이 비율 이상 보이는 머리만 양성 (docs/05-data.md 1.3절)


def _one(args: tuple[str, int, str]) -> tuple[dict, np.ndarray, list[dict]]:
    split, index, category = args
    scn = build_scenario(split, index, category)
    frames, labels = render(scn)
    return scn.meta(), frames, labels


def _sample_train_frames(meta: dict, labels: list[dict], k: int) -> list[int]:
    """사람이 보이는 프레임을 우선(특히 2명 이상), 빈 프레임도 일부 섞는다."""
    rng = np.random.default_rng([meta["seed"], 2])
    n_vis = [sum(h["vis"] >= VIS_MIN for h in f["heads"]) for f in labels]
    multi = [i for i, n in enumerate(n_vis) if n >= 2]
    one = [i for i, n in enumerate(n_vis) if n == 1]
    empty = [i for i, n in enumerate(n_vis) if n == 0]

    def take(pool: list[int], m: int) -> list[int]:
        if m <= 0 or not pool:
            return []
        return [int(i) for i in rng.choice(pool, size=min(m, len(pool)), replace=False)]

    want_vis = k - 2
    picked = take(multi, want_vis)
    picked += take(one, want_vis - len(picked))
    picked += take(empty, k - len(picked))
    return sorted(picked)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", choices=sorted(SPLIT_SEED_BASE), required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--n", type=int, help="시나리오 수 (기본: train 2500, val 200, test 300)")
    p.add_argument("--frames-per-scenario", type=int, default=8, help="train에서 시나리오당 저장할 프레임 수")
    p.add_argument("--workers", type=int, default=os.cpu_count())
    args = p.parse_args()

    n = args.n or DEFAULT_COUNT[args.split]
    cats = assign_categories(args.split, n)
    jobs = [(args.split, i, cats[i]) for i in range(n)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(_one, jobs, chunksize=8))

    args.out.mkdir(parents=True, exist_ok=True)
    if args.split == "train":
        xs, heads = [], []
        metas = []
        for meta, frames, labels in results:
            for i in _sample_train_frames(meta, labels, args.frames_per_scenario):
                arr = np.full((MAX_HEADS, 3), np.nan, dtype=np.float32)
                for j, h in enumerate(labels[i]["heads"][:MAX_HEADS]):
                    arr[j] = (h["x"], h["y"], h["vis"])
                xs.append(frames[i])
                heads.append(arr)
            metas.append(meta)
        np.savez_compressed(args.out / "train.npz", x=np.stack(xs), heads=np.stack(heads))
        n_frames = len(xs)
    else:
        metas = [dict(meta, frames=labels) for meta, _, labels in results]
        np.savez_compressed(args.out / "frames.npz", **{m["scenario_id"]: f for m, f, _ in results})
        n_frames = sum(m["n_frames"] for m in metas)
    (args.out / "scenarios.json").write_text(json.dumps(metas, ensure_ascii=False), encoding="utf-8")

    counts: dict[str, int] = {}
    for m in metas:
        counts[m["category"]] = counts.get(m["category"], 0) + 1
    print(f"{args.split}: 시나리오 {n}개, 프레임 {n_frames}장, {time.time() - t0:.1f}초 → {args.out}")
    print("  범주:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
