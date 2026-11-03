"""히트맵 모델 학습 → float / int8 TFLite 변환.

    python -m sim.generate --split train --out data/train
    python -m sim.generate --split val --out data/val
    python -m ml.train --epochs 20

산출물: ml/model.tflite(int8, 기기·CI용), ml/model_float.tflite(양자화 손실 비교용).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import tensorflow as tf

from ml.model import GRID_H, GRID_W, INPUT_SHAPE, STRIDE, build_model

VIS_MIN = 0.5  # sim.generate와 같은 기준
SIGMA_CELLS = 0.5  # 정답 가우시안 폭. 작게 잡아 붙은 두 머리의 피크가 번지지 않게 한다
POS_WEIGHT = 15.0  # 108칸 중 양성이 1~3칸뿐인 불균형 보정
IGNORE_RADIUS = 1.0  # 부분 가시 머리 주변 이 반경(칸) 안은 손실에서 제외

_CY, _CX = np.mgrid[0:GRID_H, 0:GRID_W] + 0.5  # 칸 중심 (칸 단위)


def make_target(heads: np.ndarray) -> np.ndarray:
    """heads [K, 3] (x, y, vis, px) → [9, 12, 2] (정답 히트맵, 손실 마스크)."""
    target = np.zeros((GRID_H, GRID_W), np.float32)
    mask = np.ones((GRID_H, GRID_W), np.float32)
    for x, y, vis in heads:
        if np.isnan(x) or vis <= 0:
            continue
        d2 = (_CX - x / STRIDE) ** 2 + (_CY - y / STRIDE) ** 2
        if vis >= VIS_MIN:
            # 가장 가까운 칸이 1이 되도록 정규화. 정규화하지 않으면 머리가 칸 모서리에 있을 때
            # 최대값이 0.37에 그쳐 임계값 0.5를 넘지 못한다. 주변 칸의 상대 비율(칸 내부 위치)은 유지된다
            g = np.exp(-(d2 - d2.min()) / (2 * SIGMA_CELLS**2))
            target = np.maximum(target, g)
        else:
            mask[d2 <= IGNORE_RADIUS**2] = 0.0
    return np.stack([target, mask], axis=-1)


def masked_weighted_bce(y_true, y_pred):
    t, m = y_true[..., 0:1], y_true[..., 1:2]
    p = tf.clip_by_value(y_pred, 1e-6, 1 - 1e-6)
    loss = -(POS_WEIGHT * t * tf.math.log(p) + (1 - t) * tf.math.log(1 - p))
    return tf.reduce_sum(loss * m) / tf.maximum(tf.reduce_sum(m), 1.0)


def load_train(path: Path) -> tuple[np.ndarray, np.ndarray]:
    d = np.load(path)
    x = d["x"].astype(np.float32)[..., None] / 255.0
    y = np.stack([make_target(h) for h in d["heads"]])
    return x, y


def load_val(data_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    metas = json.loads((data_dir / "scenarios.json").read_text(encoding="utf-8"))
    frames = np.load(data_dir / "frames.npz")
    xs, ys = [], []
    for m in metas:
        f = frames[m["scenario_id"]]
        for lab in m["frames"]:
            heads = np.array([(h["x"], h["y"], h["vis"]) for h in lab["heads"]], np.float32).reshape(-1, 3)
            xs.append(f[lab["idx"]])
            ys.append(make_target(heads))
    return np.stack(xs).astype(np.float32)[..., None] / 255.0, np.stack(ys)


def augment(x: np.ndarray, y: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """좌우·상하 반전(천장 수직 시점이라 기하가 대칭)과 밝기·대비 흔들기."""
    x, y = x.copy(), y.copy()
    flip_w = rng.random(len(x)) < 0.5
    flip_h = rng.random(len(x)) < 0.5
    x[flip_w], y[flip_w] = x[flip_w][:, :, ::-1], y[flip_w][:, :, ::-1]
    x[flip_h], y[flip_h] = x[flip_h][:, ::-1], y[flip_h][:, ::-1]
    gain = rng.uniform(0.8, 1.2, (len(x), 1, 1, 1)).astype(np.float32)
    bias = rng.uniform(-0.08, 0.08, (len(x), 1, 1, 1)).astype(np.float32)
    return np.clip(x * gain + bias, 0.0, 1.0), y


def convert(model, rep: np.ndarray, out_dir: Path) -> None:
    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    (out_dir / "model_float.tflite").write_bytes(conv.convert())

    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = lambda: ([r[None]] for r in rep)
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    (out_dir / "model.tflite").write_bytes(conv.convert())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train", type=Path, default=Path("data/train/train.npz"))
    p.add_argument("--val", type=Path, default=Path("data/val"))
    p.add_argument("--out", type=Path, default=Path("ml"))
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--batch", type=int, default=64)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    tf.keras.utils.set_random_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    t0 = time.time()
    x_tr, y_tr = load_train(args.train)
    x_va, y_va = load_val(args.val)
    print(f"학습 {len(x_tr)}장, 검증 {len(x_va)}장, 입력 {INPUT_SHAPE}, 출력 {(GRID_H, GRID_W)}")

    model = build_model()
    steps = args.epochs * int(np.ceil(len(x_tr) / args.batch))
    lr = tf.keras.optimizers.schedules.CosineDecay(2e-3, steps, alpha=0.05)
    model.compile(optimizer=tf.keras.optimizers.Adam(lr), loss=masked_weighted_bce)
    print(f"파라미터 {model.count_params():,}개")

    ckpt_dir = args.out / "checkpoints"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    best = np.inf
    for epoch in range(1, args.epochs + 1):
        xa, ya = augment(x_tr, y_tr, rng)
        hist = model.fit(xa, ya, batch_size=args.batch, epochs=1, shuffle=True, verbose=0)
        val = model.evaluate(x_va, y_va, batch_size=256, verbose=0)
        mark = ""
        if val < best:
            best = val
            model.save(ckpt_dir / "best.keras")
            mark = " *"
        print(f"epoch {epoch:2d}  train {hist.history['loss'][0]:.4f}  val {val:.4f}{mark}  ({time.time() - t0:.0f}s)",
              flush=True)

    model = tf.keras.models.load_model(ckpt_dir / "best.keras", compile=False)
    rep = x_tr[rng.choice(len(x_tr), 200, replace=False)]  # 양자화 범위 추정용 대표 데이터
    convert(model, rep, args.out)
    for name in ("model_float.tflite", "model.tflite"):
        print(f"{name}: {(args.out / name).stat().st_size:,} bytes")
    print(f"완료 ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
