"""기기 재생용 프레임 묶음(frames.bin)과 매니페스트를 만든다. 형식은 docs/03-hardware.md 1.4절.

    python -m tools.pack_frames --data data/test --out data/frames.bin
    parttool.py -p /dev/cu.usbmodem101 write_partition --partition-name frames --input data/frames.bin

시나리오는 모델 정답 여부와 무관하게 '조건'으로만 고른다(PC↔기기 동치성 확인용, 체리피킹 방지).
시작 시각은 14:00 KST부터 2분 간격으로 다시 배치하고(심야 시나리오만 01:00), 장기 체류 시나리오는
유휴 구간을 늘린다. 인증·정답은 시나리오 시작 기준 오프셋이라 그대로 유효하다.

바이너리 형식 (little-endian)
  헤더 20B : magic "EDFR", version u16, width u16, height u16, fps u16, n_scenarios u32, reserved u32
  항목 40B : id char[16], start_ms i64, n_frames u32, offset u32, idle_gap_s u32, reserved u32
  프레임   : uint8 [n_frames, height, width] (offset은 파티션 시작 기준)
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np

MAGIC = b"EDFR"
VERSION = 1
HEADER = struct.Struct("<4sHHHHII")
ENTRY = struct.Struct("<16sqIIII")
PARTITION_BYTES = 0x500000
TAIL_IDLE_S = 6  # 시나리오 뒤 heartbeat만 보내는 구간. 출입 세션(간격 5초)이 닫히도록
LONG_STAY_IDLE_S = 40  # 대시보드 기준 30초(데모)에서 장기 체류가 확정되도록
FIRST_START_MS = 1_793_682_000_000  # 2026-11-03 14:00 KST
SLOT_MS = 120_000  # 시나리오를 2분 간격으로 순서대로 배치 (대시보드에서 시간순으로 읽히게)
NIGHT_START_MS = FIRST_START_MS + 11 * 3_600_000  # 다음 날 01:00 KST

# (이름, 조건, 덮어쓰기). 조건은 범주·세부유형·인증·기대 알림만 본다
PICKS = [
    ("단독 입장(인증)", lambda m: m["subtype"] == "walk_in" and len(m["auth"]) == 1 and not m["expected_alerts"], {}),
    ("단독 퇴장", lambda m: m["subtype"] == "walk_out", {}),
    ("앞뒤 밀착 동반입장", lambda m: m["subtype"] == "front_back_in" and m["category"] == "front_back"
     and m["expected_alerts"] == ["tailgate"], {}),
    ("나란히 각자 인증", lambda m: m["category"] == "side_by_side" and m["subtype"] == "side_in"
     and len(m["auth"]) == 2, {}),
    ("반대 방향 교차", lambda m: m["category"] == "opposite", {}),
    ("U턴", lambda m: m["subtype"] == "uturn", {}),
    ("미인증 입장", lambda m: m["category"] == "single" and m["expected_alerts"] == ["unauth_entry"], {}),
    ("장기 체류", lambda m: m["subtype"] == "walk_in" and len(m["auth"]) == 1 and not m["expected_alerts"],
     {"idle_gap_s": LONG_STAY_IDLE_S}),
    ("심야 체류", lambda m: m["subtype"] == "walk_in" and len(m["auth"]) == 1 and not m["expected_alerts"],
     {"night": True}),
]


def select(metas: list[dict]) -> list[tuple[str, dict, dict]]:
    used: set[str] = set()
    out = []
    for name, cond, over in PICKS:
        m = next(m for m in metas if m["scenario_id"] not in used and cond(m))
        used.add(m["scenario_id"])
        out.append((name, m, over))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=Path("data/test"))
    p.add_argument("--out", type=Path, default=Path("data/frames.bin"))
    args = p.parse_args()

    metas = json.loads((args.data / "scenarios.json").read_text(encoding="utf-8"))
    frames = np.load(args.data / "frames.npz")
    picks = select(metas)

    first = picks[0][1]
    blobs, entries, manifest = [], [], []
    offset = HEADER.size + ENTRY.size * len(picks)
    for k, (name, m, over) in enumerate(picks):
        f = frames[m["scenario_id"]]
        start = NIGHT_START_MS if over.get("night") else FIRST_START_MS + k * SLOT_MS
        idle = over.get("idle_gap_s", TAIL_IDLE_S)
        entries.append(ENTRY.pack(m["scenario_id"].encode(), start, len(f), offset, idle, 0))
        blobs.append(f.tobytes())
        manifest.append({"name": name, "scenario_id": m["scenario_id"], "category": m["category"],
                         "subtype": m["subtype"], "scenario_start_ms": start, "n_frames": len(f), "idle_gap_s": idle,
                         "fps": m["fps"], "auth": m["auth"], "gt_events": m["gt_events"],
                         "expected_alerts": m["expected_alerts"]})
        offset += len(f) * f.shape[1] * f.shape[2]
    header = HEADER.pack(MAGIC, VERSION, first["width"], first["height"], first["fps"], len(picks), 0)
    data = header + b"".join(entries) + b"".join(blobs)
    if len(data) > PARTITION_BYTES:
        raise SystemExit(f"frames.bin {len(data)} bytes > 파티션 {PARTITION_BYTES} bytes")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    man_path = args.out.with_name(args.out.stem + "_manifest.json")
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    for e in manifest:
        print(f"  {e['scenario_id']}  {e['name']:12s} 프레임 {e['n_frames']:3d}  유휴 {e['idle_gap_s']:2d}s"
              f"  인증 {len(e['auth'])}  기대 알림 {e['expected_alerts']}")
    print(f"→ {args.out} ({len(data):,} bytes / 파티션 {PARTITION_BYTES:,}), {man_path}")


if __name__ == "__main__":
    main()
