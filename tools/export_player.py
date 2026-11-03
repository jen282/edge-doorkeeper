"""카메라 시연용 합성 영상 재생 페이지(HTML 한 장)를 만든다. 화면에 띄우고 보드 카메라로 촬영한다.

    python -m tools.export_player                       # 기기 시나리오 9개 (data/frames_manifest.json)
    python -m tools.export_player --ids sg_0014,fb_0001 # 직접 고르기
    open data/player/index.html

조작: Space 재생/일시정지 · C 정렬 테두리 · F 전체 화면 · H 상태 글자 숨김
촬영 시: 영상 영역이 카메라 화면을 꽉 채우고, 영상의 위쪽(바깥)이 카메라 화면의 위쪽이 되게 맞춘다.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import numpy as np

TEMPLATE = Path(__file__).with_name("player_template.html")  # __DATA__ 자리에 프레임 JSON을 넣는다


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=Path("data/test"))
    p.add_argument("--manifest", type=Path, default=Path("data/frames_manifest.json"))
    p.add_argument("--ids", help="쉼표로 구분한 시나리오 ID (없으면 매니페스트 순서)")
    p.add_argument("--idle-s", type=float, default=3.0, help="시나리오 사이 빈 바닥 시간(초)")
    p.add_argument("--out", type=Path, default=Path("data/player/index.html"))
    args = p.parse_args()

    if args.ids:
        ids = args.ids.split(",")
    else:
        ids = [m["scenario_id"] for m in json.loads(args.manifest.read_text(encoding="utf-8"))]
    frames = np.load(args.data / "frames.npz")
    clips = [{"id": i, "n": len(frames[i]), "b64": base64.b64encode(frames[i].tobytes()).decode()} for i in ids]
    first = frames[ids[0]]
    data = {"width": first.shape[2], "height": first.shape[1], "fps": 8, "idle_s": args.idle_s, "scenarios": clips}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    page = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", json.dumps(data))
    args.out.write_text(page, encoding="utf-8")
    total_s = sum(c["n"] for c in clips) / 8 + args.idle_s * len(clips)
    print(f"→ {args.out} (시나리오 {len(clips)}개, 한 바퀴 약 {total_s:.0f}초, {args.out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
