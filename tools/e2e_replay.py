"""종단 검증 (CI e2e 잡): 가상 기기(PC의 int8 모델 + Python 후처리) → 실제 서버 코드 → 알림 대조.

    python -m tools.e2e_replay --model ml/model.tflite --data data/test --gates ml/gates.yaml \\
        --report report/e2e.json --summary report/summary.md

기기 대신 PC에서 같은 .tflite와 같은 후처리로 이벤트를 만들고, FastAPI TestClient로 서버에 보낸다.
게이트: 동반입장(tailgate) 재현율·정밀도 (docs/09-ci.md 1.2절). 미인증 입장은 보고만 한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from ml.evaluate import predict_all
from ml.postproc import run_scenario
from server.app import create_app
from server.config import Config

TOKEN = "e2e"
TAIL_MS = 6000  # 시나리오 뒤 heartbeat까지의 간격. 출입 세션(5초)이 닫히도록 (기기 재생과 같은 값)
SLOT_MS = 60_000  # 테스트 세트 시나리오 시작 간격 (sim.scenario.SLOT_MS)
LEAD_MS = 10_000  # 인증은 첫 통과보다 최대 3초 앞선다 (sim.scenario.build_auth). 여유를 두고 10초


def replay(metas: list[dict], probs: dict, client: TestClient) -> None:
    auths = [{"auth_id": a["auth_id"], "door_id": "door-01", "ts": m["scenario_start_ms"] + a["ts_offset_ms"],
              "allowed_count": a["allowed_count"]} for m in metas for a in m["auth"]]
    for i in range(0, len(auths), 100):  # 본문 16KB 한도 안으로 나눠 보낸다
        client.post("/v1/auth", json=auths[i:i + 100], headers={"X-Device-Token": TOKEN}).raise_for_status()
    seq = 0
    for m in sorted(metas, key=lambda m: m["scenario_start_ms"]):
        start = m["scenario_start_ms"]
        # 시나리오는 독립된 합성 장면이라 시작 직전에 인원 기준점을 둔다 (기기 재생 데모와 같은 방식)
        client.post("/v1/occupancy/reset", json={"device_id": "door-01", "ts": start - 1, "reason": "test"})
        events = []
        for e in run_scenario(probs[m["scenario_id"]], fps=m["fps"]):
            events.append({"kind": "passage", "seq": seq, "ts": start + e["ts_offset_ms"],
                           "direction": e["direction"], "conf": 1.0})
            seq += 1
        hb_ts = start + m["n_frames"] * 1000 // m["fps"] + TAIL_MS
        events.append({"kind": "heartbeat", "seq": seq, "ts": hb_ts, "fps": 8.0, "infer_ms_p50": 0, "infer_ms_max": 0,
                       "free_heap": 0, "min_free_heap": 0, "stack_hwm": 0, "arena_used": 0, "dropped": 0,
                       "wifi_rssi": -50, "mode": "replay", "scenario_id": m["scenario_id"]})
        seq += 1
        for i in range(0, len(events), 32):  # 배치 최대 32건 (서버 스키마)
            body = {"device_id": "door-01", "boot_id": 1, "fw_version": "e2e", "model_version": "e2e",
                    "events": events[i:i + 32]}
            client.post("/v1/device/events", json=body, headers={"X-Device-Token": TOKEN}).raise_for_status()


def score(metas: list[dict], alerts: list[dict], alert_type: str, excluded: set[str]) -> dict:
    got = {}
    for m in metas:
        # 알림 시각은 세션 시작(=인증 시각)이라 시나리오 시작보다 최대 3초 앞설 수 있다 → 구간을 앞당긴다
        lo = m["scenario_start_ms"] - LEAD_MS
        hi = lo + SLOT_MS
        got[m["scenario_id"]] = any(a["type"] == alert_type and lo <= a["ts"] < hi for a in alerts)
    tp = fp = fn = 0
    misses, false_alarms = [], []
    for m in metas:
        if m["category"] in excluded:
            continue
        want, have = alert_type in m["expected_alerts"], got[m["scenario_id"]]
        tp += want and have
        if have and not want:
            fp += 1
            false_alarms.append(m["scenario_id"])
        if want and not have:
            fn += 1
            misses.append(m["scenario_id"])
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
            "misses": misses, "false_alarms": false_alarms}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", type=Path, default=Path("ml/model.tflite"))
    p.add_argument("--data", type=Path, default=Path("data/test"))
    p.add_argument("--gates", type=Path, default=Path("ml/gates.yaml"))
    p.add_argument("--report", type=Path)
    p.add_argument("--summary", type=Path)
    args = p.parse_args()

    gates = yaml.safe_load(args.gates.read_text(encoding="utf-8"))
    excluded = set(gates["accuracy"]["excluded_categories"])
    metas = json.loads((args.data / "scenarios.json").read_text(encoding="utf-8"))
    probs = predict_all(args.model, args.data, metas)
    app = create_app(Config(db_path=":memory:", device_token=TOKEN, watchdog_interval_s=0))
    with TestClient(app) as client:
        replay(metas, probs, client)
        alerts = client.get("/v1/alerts", params={"limit": 500}).json()

    tg = score(metas, alerts, "tailgate", excluded)
    un = score(metas, alerts, "unauth_entry", excluded)
    g = gates["e2e"]
    results = [
        {"name": "동반입장 재현율", "value": tg["recall"], "limit": f"≥ {g['tailgate_recall_min']}",
         "passed": tg["recall"] >= g["tailgate_recall_min"]},
        {"name": "동반입장 정밀도", "value": tg["precision"], "limit": f"≥ {g['tailgate_precision_min']}",
         "passed": tg["precision"] >= g["tailgate_precision_min"]},
    ]
    passed = all(r["passed"] for r in results)
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['name']}: {r['value']:.3f} ({r['limit']})")
    print(f"      동반입장 TP={tg['tp']} FP={tg['fp']} FN={tg['fn']}  놓침={tg['misses']}  오탐={tg['false_alarms']}")
    print(f"      미인증 입장(보고만) 재현율={un['recall']:.3f} 정밀도={un['precision']:.3f}"
          f"  TP={un['tp']} FP={un['fp']} FN={un['fn']}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"passed": passed, "gates": results, "tailgate": tg, "unauth_entry": un},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        lines = ["## 종단 게이트 (가상 기기 → 서버 규칙)", "", "| 게이트 | 값 | 기준 | 결과 |", "|---|---|---|---|"]
        for r in results:
            lines.append(f"| {r['name']} | {r['value']:.3f} | {r['limit']} | {'✅' if r['passed'] else '❌'} |")
        lines += ["", f"미인증 입장(보고만): 재현율 {un['recall']:.3f} · 정밀도 {un['precision']:.3f}"]
        with args.summary.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
