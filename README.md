# homenvr

A self-hosted replacement for the Hikvision phone app. homenvr records your
cameras straight to your own server over RTSP, so you get **unlimited storage**
(NAS / rack disk), **fast local playback**, a **motion timeline**, **push alerts
with snapshots**, and **remote access** — with no cloud relay, no ads, and no
upselling.

Built and tested against a **DS-7804N-S1/W(B)** NVR with three
**DS-IPC-K24H-LWT** cameras, but works with any Hikvision (or ONVIF/RTSP) NVR or
IP camera. See [PROTOCOL.md](PROTOCOL.md) for how the device was investigated and
how the camera↔NVR communication works.

> Not affiliated with or endorsed by Hikvision. homenvr only **reads** streams
> from your NVR using credentials you supply; it never modifies the device or its
> firmware.

## Why

The stock app routes video through the Hik-Connect cloud even on your own LAN
(the NVR's web/SDK ports are typically closed), which makes playback slow and
flaky, and the SD card fills in days. homenvr connects **directly over LAN RTSP**
and stores recordings on hardware you control.

## Features

- **Live view** — multi-camera grid, low-latency HLS. Per-camera **rotate**
  (for sideways-mounted cameras), **digital zoom + pan** (scroll/pinch/buttons),
  fullscreen.
- **Playback** — timeline scrubber with instant local seeking (no "failed to
  grab recording"), day navigation, **motion-event markers** with jump-to-motion.
- **Motion detection** — server-side (ffmpeg frame-differencing on the sub-stream,
  no NVR changes); marks the timeline and can trigger alerts.
- **Push alerts** — ntfy, Discord, Telegram, or email, when a camera stops
  recording / the disk fills, and **snapshot photo pushes on motion**.
- **Clip export** — download any time range as an MP4.
- **Permanent "saved" clips** — keep important footage (by time range or by
  jumping to a motion event); never auto-deleted.
- **Two-stage retention** — footage past your day/size limits moves to a **grace**
  area (reversible), and is hard-deleted only under real disk pressure, in
  batches — no constant trickle deletion.
- **Remote access** — watch from anywhere via Tailscale or Cloudflare Tunnel; no
  cloud, no open ports. See [docs/remote-access.md](docs/remote-access.md).
- **Settings page** — configure alerts (incl. Telegram/Discord), motion, and
  retention from the browser; changes apply live.
- **SD-card backfill** — import footage already on the NVR before it rotates away.

## Requirements

- A Linux host with Docker + Docker Compose (a NAS, a rack server, a mini PC…).
- Network reachability to the NVR/camera.
- The NVR **admin password** (RTSP credentials). On modern Hikvision devices the
  password was set at activation — there is no factory default.

## Quick start

```bash
git clone https://github.com/ushiosoft/hikvision-homenvr.git
cd hikvision-homenvr
cp .env.example .env              # set NVR_PASSWORD, UI_PASSWORD, TZ, RECORDINGS_DIR
cp config.example.yml config.yml  # set NVR host + cameras
docker compose up -d --build
```

Open `http://<host>:8971` (or from your phone on the same network). Log in with
the UI credentials from `.env`. Point `RECORDINGS_DIR` at your NAS/disk for
unlimited storage.

## Configuration

`config.yml` is the base config (secrets come from `.env` via `${VAR}`). The
**Settings** page writes a runtime overlay for alerts/motion/retention, so you
rarely need to edit the file after setup. Key sections:

```yaml
nvr: { host: 192.168.1.64, port: 554, username: admin, password: ${NVR_PASSWORD} }
cameras:
  - { id: cam1, name: Front, channel: 1, live_codec: h264 }   # one per NVR channel
segment_seconds: 600
retention: { max_days: 60, max_gb: 0, min_free_gb: 20 }        # whichever limit hits first
motion:    { enabled: true, sensitivity: 4.0 }
alerts:    { notifier: none }                                  # ntfy | webhook | telegram | smtp
ui:        { username: admin, password: ${UI_PASSWORD} }
```

See `config.example.yml` for all options (per-camera `live_codec`/`audio`,
custom RTSP URLs, SMTP, motion-snapshot push, etc.).

### Backfill the SD card

```bash
docker compose exec homenvr python -m app.importer \
  --camera cam1 --from "2026-10-01 00:00" --to "2026-10-02 00:00"
```

## How it works

Your agent connects to the NVR's RTSP endpoints (`/Streaming/Channels/<ch>01`
live, `/Streaming/tracks/<ch>01?starttime=…` playback), records the main stream
to fragmented MP4 segments, and remuxes the sub-stream to HLS for live view.
Details and the discovery process are in [PROTOCOL.md](PROTOCOL.md);
planned work is in [ROADMAP.md](ROADMAP.md).

| Component | Role |
| --- | --- |
| `app/recorder.py` | ffmpeg supervision (record + live), snapshots, watchdog |
| `app/storage.py` | segment index, two-stage retention, grace/saved, export |
| `app/motion.py` / `app/events.py` | motion detection + event store |
| `app/alerts.py` | notifiers (ntfy/Discord/Telegram/SMTP) + health watchdog |
| `app/settings.py` | runtime-editable config overlay |
| `app/main.py` | HTTP API + web UI |
| `app/static/` | single-page UI |

## Development

```bash
pip install -r requirements-dev.txt
pytest                       # unit tests
tests/e2e/                   # docker-compose stack with a simulated NVR + cameras
```

The e2e stack runs MediaMTX as a fake Hikvision NVR with synthetic cameras and
exercises record → playback → export end to end.

## Security

- Set a strong `UI_PASSWORD` before exposing homenvr beyond your LAN; prefer
  Tailscale/Cloudflare Access over a bare port-forward.
- Keep the NVR off the public internet — homenvr is the only thing that needs to
  be reachable, and only to you.
- `.env`, `config.yml`, `recordings/`, and the settings overlay are gitignored.

## License

MIT
