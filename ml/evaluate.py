"""커밋된 모델을 테스트 세트에서 평가하고 게이트를 판정한다 (CI model-gate 잡).

    python -m ml.evaluate --model ml/model.tflite --float-model ml/model_float.tflite \\
        --data data/test --gates ml/gates.yaml --report report/model.json --summary report/summary.md
    python -m ml.evaluate --model ml/model.tflite --data data/val --sweep 0.3,0.4,0.5,0.6   # 임계값 조정

정확도는 시나리오 단위다: 예측 (IN 수, OUT 수)가 정답과 완전히 같으면 정답.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import tensorflow as tf
import yaml

from ml.postproc import PostprocConfig, run_scenario

MATCH_MS = 500  # 이벤트 매칭 허용 시각 차이 (docs/05-data.md 1.3절)
BATCH = 256
_REF = tf.lite.experimental.OpResolverType.BUILTIN_REF  # 기기(ESP-NN, 참조 커널과 비트 동일)와 같은 결과
_NO_DELEGATE = tf.lite.experimental.OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES


def predict(model_path: Path, frames: np.ndarray) -> np.ndarray:
    """uint8 [N, 72, 96] → 확률 히트맵 [N, 9, 12]."""
    it = tf.lite.Interpreter(model_path=str(model_path), experimental_op_resolver_type=_REF)
    idx = it.get_input_details()[0]["index"]
    it.resize_tensor_input(idx, [BATCH, *frames.shape[1:], 1])
    it.allocate_tensors()
    inp, out = it.get_input_details()[0], it.get_output_details()[0]
    probs = []
    for s in range(0, len(frames), BATCH):
        chunk = frames[s:s + BATCH]
        n = len(chunk)
        chunk = np.concatenate([chunk, np.zeros((BATCH - n, *chunk.shape[1:]), np.uint8)])[..., None]
        x = chunk.astype(np.float32) / 255.0
        if inp["dtype"] == np.int8:
            scale, zp = inp["quantization"]
            x = np.clip(np.round(x / scale + zp), -128, 127).astype(np.int8)
        it.set_tensor(inp["index"], x)
        it.invoke()
        y = it.get_tensor(out["index"])[:n, ..., 0]
        if out["dtype"] == np.int8:
            scale, zp = out["quantization"]
            y = (y.astype(np.float32) - zp) * scale
        probs.append(y)
    return np.concatenate(probs)


def predict_all(model_path: Path, data_dir: Path, metas: list[dict]) -> dict[str, np.ndarray]:
    frames = np.load(data_dir / "frames.npz")
    ids = [m["scenario_id"] for m in metas]
    stacked = np.concatenate([frames[i] for i in ids])
    probs = predict(model_path, stacked)
    out, k = {}, 0
    for i in ids:
        n = len(frames[i])
        out[i] = probs[k:k + n]
        k += n
    return out


def _count(events: list[dict], d: str) -> int:
    return sum(e["direction"] == d for e in events)


def _match(pred: list[dict], gt: list[dict]) -> int:
    pairs = sorted(
        (abs(p["ts_offset_ms"] - g["ts_offset_ms"]), i, j)
        for i, p in enumerate(pred) for j, g in enumerate(gt)
        if p["direction"] == g["direction"] and abs(p["ts_offset_ms"] - g["ts_offset_ms"]) <= MATCH_MS
    )
    used_p, used_g = set(), set()
    for _, i, j in pairs:
        if i not in used_p and j not in used_g:
            used_p.add(i)
            used_g.add(j)
    return len(used_p)


def score(metas: list[dict], probs: dict[str, np.ndarray], cfg: PostprocConfig) -> list[dict]:
    rows = []
    for m in metas:
        pred = run_scenario(probs[m["scenario_id"]], cfg, m["fps"])
        gt = m["gt_events"]
        p_io, g_io = (_count(pred, "in"), _count(pred, "out")), (_count(gt, "in"), _count(gt, "out"))
        rows.append({"scenario_id": m["scenario_id"], "category": m["category"], "subtype": m["subtype"],
                     "ok": p_io == g_io, "pred": p_io, "gt": g_io,
                     "tp": _match(pred, gt), "n_pred": len(pred), "n_gt": len(gt)})
    return rows


def summarize(rows: list[dict], acc_cfg: dict) -> dict:
    def acc(rs: list[dict]) -> float | None:
        return round(sum(r["ok"] for r in rs) / len(rs), 4) if rs else None

    excluded = set(acc_cfg["excluded_categories"])
    gated = [r for r in rows if r["category"] not in excluded]
    tp, n_pred, n_gt = (sum(r[k] for r in gated) for k in ("tp", "n_pred", "n_gt"))
    prec = tp / n_pred if n_pred else 1.0
    rec = tp / n_gt if n_gt else 1.0
    cats = sorted({r["category"] for r in rows})
    return {
        "single": acc([r for r in rows if r["category"] == "single"]),
        "adjacent": acc([r for r in rows if r["category"] in acc_cfg["adjacent_categories"]]),
        "overall": acc(gated),
        "per_category": {c: {"acc": acc([r for r in rows if r["category"] == c]),
                             "n": sum(r["category"] == c for r in rows)} for c in cats},
        "event": {"precision": round(prec, 4), "recall": round(rec, 4),
                  "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0},
    }


def check_model(model_path: Path, mcfg: dict) -> list[dict]:
    it = tf.lite.Interpreter(model_path=str(model_path), experimental_op_resolver_type=_NO_DELEGATE)
    it.allocate_tensors()
    inp, out = it.get_input_details()[0], it.get_output_details()[0]
    ops = sorted({d["op_name"] for d in it._get_ops_details()})
    size = model_path.stat().st_size
    scale, zp = inp["quantization"]
    want_in, want_out = mcfg["input"], mcfg["output"]
    return [
        {"name": "모델 크기", "value": size, "limit": f"≤ {mcfg['max_size_bytes']}",
         "passed": size <= mcfg["max_size_bytes"]},
        {"name": "연산자", "value": ops, "limit": f"⊆ {mcfg['allowed_ops']}",
         "passed": set(ops) <= set(mcfg["allowed_ops"])},
        {"name": "입력 형식", "value": f"{list(inp['shape'])} {inp['dtype'].__name__} scale={scale:.8f} zp={zp}",
         "limit": f"{want_in['shape']} {want_in['dtype']} scale={want_in['scale']} zp={want_in['zero_point']}",
         "passed": list(inp["shape"]) == want_in["shape"] and inp["dtype"].__name__ == want_in["dtype"]
         and abs(scale - want_in["scale"]) < 1e-6 and zp == want_in["zero_point"]},
        {"name": "출력 형식", "value": f"{list(out['shape'])} {out['dtype'].__name__}",
         "limit": f"{want_out['shape']} {want_out['dtype']}",
         "passed": list(out["shape"]) == want_out["shape"] and out["dtype"].__name__ == want_out["dtype"]},
    ]


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def write_summary(path: Path, gates: list[dict], summ: dict, float_summ: dict | None) -> None:
    lines = ["## 모델 게이트", "", "| 게이트 | 값 | 기준 | 결과 |", "|---|---|---|---|"]
    lines += [f"| {g['name']} | {_fmt(g['value'])} | {g['limit']} | {'✅' if g['passed'] else '❌'} |" for g in gates]
    lines += ["", "| 범주 | 시나리오 수 | 정확도 |", "|---|---|---|"]
    lines += [f"| {c} | {v['n']} | {_fmt(v['acc'])} |" for c, v in summ["per_category"].items()]
    e = summ["event"]
    lines += ["", f"이벤트 정밀도 {e['precision']:.3f} · 재현율 {e['recall']:.3f} · F1 {e['f1']:.3f}"]
    if float_summ:
        lines += [f"float 모델 전체 정확도 {_fmt(float_summ['overall'])}"]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--float-model", type=Path)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--gates", type=Path, default=Path("ml/gates.yaml"))
    p.add_argument("--report", type=Path)
    p.add_argument("--summary", type=Path, help="마크다운 요약을 덧붙일 파일 (CI: $GITHUB_STEP_SUMMARY)")
    p.add_argument("--threshold", type=float, help="피크 임계값 덮어쓰기")
    p.add_argument("--sweep", help="쉼표로 구분한 임계값 목록. 게이트 없이 정확도만 비교")
    args = p.parse_args()

    gates_cfg = yaml.safe_load(args.gates.read_text(encoding="utf-8"))
    acc_cfg = gates_cfg["accuracy"]
    metas = json.loads((args.data / "scenarios.json").read_text(encoding="utf-8"))
    cfg = PostprocConfig() if args.threshold is None else replace(PostprocConfig(), threshold=args.threshold)
    probs = predict_all(args.model, args.data, metas)

    if args.sweep:
        print("임계값  단독   밀착   전체   이벤트F1")
        for th in (float(t) for t in args.sweep.split(",")):
            s = summarize(score(metas, probs, replace(cfg, threshold=th)), acc_cfg)
            accs = "  ".join(_fmt(s[k]) for k in ("single", "adjacent", "overall"))
            print(f"{th:5.2f}  {accs}  {s['event']['f1']:.3f}")
        return 0

    rows = score(metas, probs, cfg)
    summ = summarize(rows, acc_cfg)
    gates = check_model(args.model, gates_cfg["model"])
    gates += [
        {"name": "단독 통과 정확도", "value": summ["single"], "limit": f"≥ {acc_cfg['single_min']}",
         "passed": summ["single"] is not None and summ["single"] >= acc_cfg["single_min"]},
        {"name": "밀착 통과 정확도", "value": summ["adjacent"], "limit": f"≥ {acc_cfg['adjacent_min']}",
         "passed": summ["adjacent"] is not None and summ["adjacent"] >= acc_cfg["adjacent_min"]},
    ]
    float_summ = None
    if args.float_model:
        float_summ = summarize(score(metas, predict_all(args.float_model, args.data, metas), cfg), acc_cfg)
        drop = round((float_summ["overall"] - summ["overall"]) * 100, 2)
        gates.append({"name": "양자화 손실 (%p)", "value": drop, "limit": f"≤ {acc_cfg['quant_drop_max_pp']}",
                      "passed": drop <= acc_cfg["quant_drop_max_pp"]})

    passed = all(g["passed"] for g in gates)
    failures = [r for r in rows if not r["ok"]]
    report = {"passed": passed, "gates": gates, "int8": summ, "float": float_summ,
              "postproc": asdict(cfg), "failures": failures}
    for g in gates:
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {g['name']}: {_fmt(g['value'])} ({g['limit']})")
    for c, v in summ["per_category"].items():
        print(f"      {c:14s} n={v['n']:3d}  acc={_fmt(v['acc'])}")
    e = summ["event"]
    print(f"      이벤트 P={e['precision']:.3f} R={e['recall']:.3f} F1={e['f1']:.3f}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        write_summary(args.summary, gates, summ, float_summ)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
