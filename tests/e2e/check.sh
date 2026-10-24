#!/bin/sh
# End-to-end check: fake NVR (MediaMTX) + three fake cameras + homenvr, then
# record -> live -> playback -> export through the API. Run from the repo root:
#   sh tests/e2e/check.sh            (KEEP=1 leaves the stack running)
set -u
cd "$(dirname "$0")"
API=http://127.0.0.1:18080
AUTH=admin:test
fail=0
ok()  { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; fail=1; }

docker compose up -d --build --quiet-pull >/dev/null 2>&1 || { echo "compose up failed"; exit 1; }
trap '[ "${KEEP:-}" = 1 ] || docker compose down -v >/dev/null 2>&1' EXIT

echo "waiting for homenvr..."
for i in $(seq 1 60); do curl -fs "$API/healthz" >/dev/null && break; sleep 2; done
curl -fs "$API/healthz" >/dev/null && ok "healthz" || { bad "healthz"; exit 1; }

# segment_seconds is 20 in config.yml: wait for a couple of finished files per camera.
echo "recording for 70 s..."
sleep 70
now=$(date +%s)
for cam in cam1 cam2 cam3; do
  n=$(curl -fs -u $AUTH "$API/api/cameras/$cam/segments?start=$((now - 600))&end=$now" | grep -o '"name"' | wc -l)
  [ "$n" -ge 2 ] && ok "$cam: $n segments recorded" || bad "$cam: $n segments recorded (want >= 2)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -u $AUTH "$API/live/$cam/index.m3u8")
  [ "$code" = 200 ] && ok "$cam: live HLS playlist" || bad "$cam: live HLS playlist ($code)"
done

t=$((now - 50))
size=$(curl -fs -u $AUTH "$API/api/cameras/cam1/playback.mp4?start=$t&dur=10&height=480" | wc -c)
[ "$size" -gt 20000 ] && ok "playback (transcoded): $size bytes" || bad "playback (transcoded): $size bytes"
size=$(curl -fs -u $AUTH "$API/api/cameras/cam1/export?start=$t&end=$((t + 10))" | wc -c)
[ "$size" -gt 20000 ] && ok "export: $size bytes" || bad "export: $size bytes"

[ $fail = 0 ] && echo "e2e: all checks passed" || { echo "e2e: FAILED"; docker compose logs --tail 30 homenvr; }
exit $fail
