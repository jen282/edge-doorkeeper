"""테스트 세트 정답(라벨·이벤트·인증·기대 알림)의 해시. 픽셀은 해시하지 않는다.

    python -m sim.hash_gt data/test > sim/test_set.sha256

생성기가 바뀌어 게이트 기준(정답)이 달라지면 CI가 이 값의 불일치로 알려 준다.
"""

import hashlib
import json
import sys
from pathlib import Path

GT_KEYS = ("scenario_id", "category", "subtype", "n_frames", "scenario_start_ms",
           "frames", "gt_events", "auth", "expected_alerts")


def gt_hash(data_dir: Path) -> tuple[str, int]:
    metas = json.loads((data_dir / "scenarios.json").read_text(encoding="utf-8"))
    canon = [{k: m[k] for k in GT_KEYS} for m in metas]
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), len(metas)


if __name__ == "__main__":
    digest, n = gt_hash(Path(sys.argv[1]))
    print(f"{digest}  {n} scenarios")
