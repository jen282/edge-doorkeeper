"""시나리오 프레임을 머리 라벨·가상 라인과 함께 PNG 한 장으로 확인한다 (외부 의존성 없음).

    python -m sim.preview data/test fb_0007 sb_0012 --out preview.png
"""

from __future__ import annotations

import argparse
import json
import struct
import zlib
from pathlib import Path

import numpy as np

from sim.camera import LINE_A, LINE_C
from sim.generate import VIS_MIN

SCALE = 3
COLS = 8


def write_png(path: Path, gray: np.ndarray) -> None:
    h, w = gray.shape
    raw = b"".join(b"\x00" + gray[i].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def annotate(frame: np.ndarray, heads: list[dict]) -> np.ndarray:
    img = np.kron(frame, np.ones((SCALE, SCALE), dtype=np.uint8))
    for y in (LINE_A, LINE_C):  # 가상 라인 점선
        img[int(y * SCALE), ::4] = 128
    for h in heads:
        u, v = int(h["x"] * SCALE), int(h["y"] * SCALE)
        val = 255 if h["vis"] >= VIS_MIN else 64
        for d in range(-4, 5):
            for yy, xx in ((v, u + d), (v + d, u)):
                if 0 <= yy < img.shape[0] and 0 <= xx < img.shape[1]:
                    img[yy, xx] = val
    return img


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("data_dir", type=Path)
    p.add_argument("ids", nargs="+")
    p.add_argument("--out", type=Path, default=Path("preview.png"))
    args = p.parse_args()

    metas = {m["scenario_id"]: m for m in json.loads((args.data_dir / "scenarios.json").read_text(encoding="utf-8"))}
    frames = np.load(args.data_dir / "frames.npz")
    rows = []
    for sid in args.ids:
        m = metas[sid]
        tiles = [annotate(frames[sid][f["idx"]], f["heads"]) for f in m["frames"]][:COLS * 3]
        while len(tiles) % COLS:
            tiles.append(np.zeros_like(tiles[0]))
        for r in range(0, len(tiles), COLS):
            rows.append(np.hstack([np.pad(t, 1, constant_values=0) for t in tiles[r:r + COLS]]))
        events = [(e["direction"], e["ts_offset_ms"]) for e in m["gt_events"]]
        print(f"{sid}: {m['category']}/{m['subtype']} 이벤트={events}"
              f" 인증={len(m['auth'])} 기대 알림={m['expected_alerts']}")
    write_png(args.out, np.vstack(rows))
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
