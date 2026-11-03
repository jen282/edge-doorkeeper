"""C 후처리(firmware/components/postproc)와 Python 후처리(ml/postproc.py)의 결과가 같은지 PC에서 확인한다.

    python -m tools.postproc_parity --model ml/model.tflite --data data/test

같은 int8 모델 히트맵을 두 구현에 넣고 시나리오별 이벤트(방향, 프레임, 시각)를 비교한다.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from ml.evaluate import predict_all
from ml.postproc import run_scenario

PP_DIR = Path("firmware/components/postproc")


def build_host(out_dir: Path) -> Path:
    exe = out_dir / "postproc_host"
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{PP_DIR / 'include'}",
                    str(PP_DIR / "postproc.c"), str(PP_DIR / "test" / "host_main.c"), "-lm", "-o", str(exe)],
                   check=True)
    return exe


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", type=Path, default=Path("ml/model.tflite"))
    p.add_argument("--data", type=Path, default=Path("data/test"))
    args = p.parse_args()

    metas = json.loads((args.data / "scenarios.json").read_text(encoding="utf-8"))
    probs = predict_all(args.model, args.data, metas)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        exe = build_host(tmp)
        blob = [struct.pack("<i", len(metas))]
        for m in metas:
            hm = probs[m["scenario_id"]].astype(np.float32)
            blob += [struct.pack("<i", len(hm)), hm.tobytes()]
        (tmp / "hm.bin").write_bytes(b"".join(blob))
        out = subprocess.run([str(exe), str(tmp / "hm.bin")], check=True, capture_output=True, text=True).stdout

    c_events: dict[int, list[tuple]] = {}
    for line in out.splitlines():
        _, s, d, ts, fr = line.split()
        c_events.setdefault(int(s), []).append((d, int(fr), int(ts)))

    mismatch = 0
    n_events = 0
    for s, m in enumerate(metas):
        py = [(e["direction"], e["frame"], e["ts_offset_ms"]) for e in run_scenario(probs[m["scenario_id"]])]
        c = c_events.get(s, [])
        n_events += len(py)
        same = len(py) == len(c) and all(a[:2] == b[:2] and abs(a[2] - b[2]) <= 1 for a, b in zip(py, c, strict=True))
        if not same:
            mismatch += 1
            print(f"불일치 {m['scenario_id']}: python={py} c={c}")
    print(f"시나리오 {len(metas)}개, 이벤트 {n_events}건, 불일치 시나리오 {mismatch}개")
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
