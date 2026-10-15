#!/bin/sh
# Feed fake cameras: main = HEVC 1280x720 + G.711 audio, sub = H.264 640x360.
# Each publisher retries forever, like a real camera reconnecting to its NVR.
U='rtsp://admin:S3cret!pw@nvr:8554/Streaming/Channels'
forever() { while true; do "$@"; sleep 2; done; }
for ch in 1 2 3; do
  forever ffmpeg -hide_banner -loglevel error -re -f lavfi -i "testsrc2=size=1280x720:rate=15" \
    -f lavfi -i "sine=frequency=$((300*ch)):sample_rate=8000" \
    -c:v libx265 -preset ultrafast -x265-params keyint=30:log-level=error -c:a pcm_mulaw \
    -f rtsp -rtsp_transport tcp "$U/${ch}01" &
  forever ffmpeg -hide_banner -loglevel error -re -f lavfi -i "testsrc=size=640x360:rate=15" \
    -c:v libx264 -preset ultrafast -g 15 -f rtsp -rtsp_transport tcp "$U/${ch}02" &
done
wait
