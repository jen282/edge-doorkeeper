"""모의 인증 주입기: 재생 데모의 인증 이벤트와 시나리오별 인원 기준점을 서버에 넣는다.

    DOORKEEPER_DEVICE_TOKEN=... python -m tools.auth_injector --server http://127.0.0.1:8000

기기 재생을 시작하기 '전에' 실행한다. 인증이 늦게 들어오면 이미 닫힌 출입 세션이 미인증 입장으로 판정되기 때문.
규칙은 이벤트 시각으로 평가하고 아직 오지 않은 시각(ts > now)은 제외하므로, 미리 넣어도 결과는 같다.
토큰은 환경변수로만 받고 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--manifest", type=Path, default=Path("data/frames_manifest.json"))
    p.add_argument("--device-id", default="door-01")
    p.add_argument("--t-long-ms", type=int, default=30_000, help="장기 체류 기준 (데모 30초)")
    args = p.parse_args()

    token = os.environ.get("DOORKEEPER_DEVICE_TOKEN")
    if not token:
        raise SystemExit("환경변수 DOORKEEPER_DEVICE_TOKEN이 필요합니다")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.server, timeout=5) as c:
        c.put("/v1/settings/t_long_ms", json={"value": args.t_long_ms}).raise_for_status()
        auths = []
        for m in manifest:
            # 시나리오는 서로 독립된 합성 장면이라 시작 직전에 인원 0 기준점을 둔다 (docs/07-database.md 4.2절)
            r = c.post("/v1/occupancy/reset",
                       json={"device_id": args.device_id, "ts": m["scenario_start_ms"] - 1, "reason": "test"})
            r.raise_for_status()
            auths += [{"auth_id": a["auth_id"], "door_id": args.device_id,
                       "ts": m["scenario_start_ms"] + a["ts_offset_ms"], "allowed_count": a["allowed_count"]}
                      for a in m["auth"]]
        r = c.post("/v1/auth", json=auths, headers={"X-Device-Token": token})
        r.raise_for_status()
    print(f"기준점 {len(manifest)}개, 인증 {len(auths)}건 → {r.json()}, 장기 체류 기준 {args.t_long_ms}ms")
    for m in manifest:
        print(f"  {m['scenario_id']} {m['name']:12s} 기대 알림 {m['expected_alerts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
