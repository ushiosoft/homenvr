# Roadmap

## Built

| Area | What | Notes |
| --- | --- | --- |
| Recording | Main stream stream-copied into 10-minute MP4 files; SD-card backfill | `app/recorder.py`, `app/importer.py` |
| Retention | Age / size limits move footage to a reversible grace area; deleted only under disk pressure | `app/storage.py` |
| Live view | WebRTC through a go2rtc sidecar (~1 s), low-latency HLS fallback; the live and motion workers read go2rtc's restream, so the NVR serves two streams per camera | `app/webrtc.py` |
| Motion | Server-side frame differencing on the sub-stream (share of the picture that changed, held 0.4 s), per-camera thresholds | `app/motion.py` |
| Labels | YOLOX-tiny person / vehicle / animal on a 10 s burst, then a second pass on the 2K recording that can send a late push | `app/detect.py` |
| Zones | Per-camera polygons: mask motion, drop detections, or keep labels without pushing | `app/zones.py` |
| Incidents | Events of all cameras clustered into incidents; noisy single-camera stretches folded into "busy" periods | `app/incidents.py` |
| Alerts | Health watchdog (recorder down, stale files, disk) and motion photo pushes via ntfy, Discord/Slack, Telegram or email | `app/alerts.py` |
| Clips | Exact-second export, permanent saved clips, auto-saved clips of pushed events | `app/clips.py` |
| Remote | Tailscale sidecar: HTTPS and WebRTC on your tailnet, no open ports | `docs/remote-access.md` |
| UI | One Cameras page for live and review, any number of cameras, phone layout, wall display, Settings page | `app/static/index.html`, `docs/multi-camera-view.md` |

## Open

- **Many cameras:** stream only the tiles on screen (or page the grid) so 9+
  cameras stay light on the browser and the server; collapse quiet timeline lanes;
  drag-to-reorder cameras.
- **Small subjects:** frame differencing misses very small movers (a rabbit at
  night is ~0.06 % of the picture); a blob/track-based trigger would catch them.
- **Settings:** move more `config.yml` options onto the Settings page (those that
  need a restart marked as such).
- **Doorbells:** button-press push and two-way talk for RTSP doorbells.
- **Camera events from the NVR:** read Hikvision's ISAPI alert stream
  (`/ISAPI/Event/notification/alertStream`) as an alternative motion source. It
  costs no server CPU, but needs the NVR's HTTP port, which is often closed.
- **Non-root container.**

Not planned: cloud services of any kind, and anything that modifies the NVR.
