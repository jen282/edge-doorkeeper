"""기기 재생 결과(시리얼 로그)와 PC 결과(같은 int8 모델 + Python 후처리)가 같은지 확인한다 (TC-24).

    python -m tools.device_parity --port /dev/cu.usbmodem101

보드를 리셋한 뒤 DONE이 나올 때까지 시리얼을 읽는다. 펌웨어 출력 형식은 firmware/main/main.c 참고.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import serial

from ml.evaluate import predict
from ml.postproc import run_scenario

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# 우리 태그 로그만 보여 준다 (Wi-Fi 라이브러리 로그에는 SSID·IP가 있을 수 있다)
OWN_LOG = re.compile(r"^[IWE] \(\d+\) (model|main|frames|net|uplink):")


def read_device(port: str, timeout_s: float) -> list[str]:
    ser = serial.Serial(port, 115200, timeout=0.2)
    ser.dtr = False
    ser.rts = True  # RTS로 칩 리셋 (USB-Serial-JTAG)
    time.sleep(0.1)
    ser.rts = False
    lines, buf, end = [], b"", time.time() + timeout_s
    while time.time() < end:
        buf += ser.read(4096)
        *done, buf = buf.split(b"\n")
        for raw in done:
            line = ANSI.sub("", raw.decode(errors="replace")).strip()  # ESP_LOG 색상 코드 제거
            lines.append(line)
            if line == "DONE":
                ser.close()
                return lines
    ser.close()
    raise SystemExit("시간 초과: DONE을 받지 못함. 시리얼 로그 끝부분:\n" + "\n".join(lines[-20:]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/cu.usbmodem101")
    p.add_argument("--manifest", type=Path, default=Path("data/frames_manifest.json"))
    p.add_argument("--data", type=Path, default=Path("data/test"))
    p.add_argument("--model", type=Path, default=Path("ml/model.tflite"))
    p.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args()

    lines = read_device(args.port, args.timeout)
    for line in lines:
        if OWN_LOG.match(line):
            print("  기기:", line)
    dev: dict[str, list[tuple]] = {}
    for line in lines:
        if line.startswith("EVT "):
            _, sid, d, ts, fr = line.split()
            dev.setdefault(sid, []).append((d, int(fr), int(ts)))
    perf = next((ln for ln in lines if ln.startswith("PERF ")), "")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    frames = np.load(args.data / "frames.npz")
    mismatch = 0
    for m in manifest:
        sid = m["scenario_id"]
        py = [(e["direction"], e["frame"], e["ts_offset_ms"]) for e in run_scenario(predict(args.model, frames[sid]))]
        d = dev.get(sid, [])
        same = len(py) == len(d) and all(a[:2] == b[:2] and abs(a[2] - b[2]) <= 1 for a, b in zip(py, d, strict=True))
        gt = [(e["direction"], e["ts_offset_ms"]) for e in m["gt_events"]]
        print(f"{'일치' if same else '불일치'}  {sid} {m['name']:12s} 기기={[x[0] for x in d]} PC={[x[0] for x in py]}"
              f" 정답={[x[0] for x in gt]}")
        mismatch += not same
    print(perf)
    print(f"시나리오 {len(manifest)}개 중 불일치 {mismatch}개")
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
