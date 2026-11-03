#!/usr/bin/env bash
# 보드에 펌웨어를 쓴다 (모드를 바꿀 때만). 쓴 뒤 보드는 부트로더에서 대기하고, tools/demo.sh가 리셋한다.
#   tools/flash.sh replay    # 재생 펌웨어 + 프레임 묶음(data/frames.bin)
#   tools/flash.sh camera    # 카메라 펌웨어
# 빌드는 미리 해 둔다 (firmware/build, firmware/build_camera). 비밀값은 출력하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-replay}"
PORT="${PORT:-/dev/cu.usbmodem101}"
# shellcheck disable=SC1090
. ~/esp/esp-idf/export.sh >/dev/null 2>&1

case "$MODE" in
  replay)
    [ -f data/frames.bin ] || .venv/bin/python -m tools.pack_frames
    parttool.py -p "$PORT" --partition-table-file firmware/build/partition_table/partition-table.bin \
      write_partition --partition-name frames --input data/frames.bin | grep -E "Wrote|Hash" || true
    BUILD=firmware/build ;;
  camera) BUILD=firmware/build_camera ;;
  *) echo "사용법: $0 replay|camera"; exit 1 ;;
esac
(cd "$BUILD" && python -m esptool --chip esp32s3 -p "$PORT" -b 460800 --before default_reset --after no_reset \
  write_flash @flash_args | grep -E "Hash|Staying")
echo "완료: $MODE 펌웨어. 실행은 tools/demo.sh $MODE"
