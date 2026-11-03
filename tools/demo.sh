#!/usr/bin/env bash
# 데모를 처음부터 다시 시작한다: 보드 일시정지 → 서버(새 DB) → [재생: 인증 주입] → 보드 리셋.
#   tools/demo.sh replay    # 재생 모드: 합성 시나리오 9개, 인증 대조 켬 (동반입장·미인증·장기·심야 알림)
#   tools/demo.sh camera    # 카메라 모드: 인증 대조 끔
# 펌웨어는 tools/flash.sh로 미리 써 둔다. 종료는 Ctrl+C (서버도 함께 종료). 토큰은 출력하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:-replay}"
PORT="${PORT:-/dev/cu.usbmodem101}"
PY=.venv/bin/python
TOKEN="$(sed -n 's/^CONFIG_DOORKEEPER_DEVICE_TOKEN="\(.*\)"$/\1/p' firmware/sdkconfig.local)"
[ -n "$TOKEN" ] || { echo "firmware/sdkconfig.local에 CONFIG_DOORKEEPER_DEVICE_TOKEN이 없습니다"; exit 1; }
case "$MODE" in
  replay) DB=data/demo.db; AUTH=1 ;;
  camera) DB=data/camera.db; AUTH=0 ;;
  *) echo "사용법: $0 replay|camera"; exit 1 ;;
esac

echo "1/4 보드 일시정지 (이전 실행의 이벤트가 새 DB에 섞이지 않게)"
$PY -m esptool --chip esp32s3 -p "$PORT" --after no_reset chip_id >/dev/null

echo "2/4 서버 시작 (새 DB: $DB, 인증 대조: $AUTH)"
pkill -f "uvicorn server.app:create_app" 2>/dev/null || true
rm -f "$DB" "$DB-wal" "$DB-shm"
DOORKEEPER_AUTH_RULES=$AUTH DOORKEEPER_DEVICE_TOKEN="$TOKEN" DOORKEEPER_DB="$DB" \
  .venv/bin/uvicorn server.app:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log --log-level warning &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
$PY -c "
import time, httpx
for _ in range(80):
    try: httpx.get('http://127.0.0.1:8000/healthz'); break
    except Exception: time.sleep(0.25)
else: raise SystemExit('서버가 뜨지 않았습니다')"

if [ "$MODE" = replay ]; then
  echo "3/4 모의 인증·기준점 주입 (재생 전에 넣어야 함)"
  DOORKEEPER_DEVICE_TOKEN="$TOKEN" $PY -m tools.auth_injector | head -1
else
  echo "3/4 (카메라 모드: 인증 주입 없음)"
fi

echo "4/4 보드 리셋 → 약 10초 뒤 대시보드에 '정상' 표시"
$PY -c "
import time, serial
s = serial.Serial('$PORT'); s.dtr = False; s.rts = True; time.sleep(0.1); s.rts = False; s.close()"
echo "대시보드: http://localhost:8000   (종료: Ctrl+C)"
wait $SERVER
