# homenvr

A self-hosted replacement for the Hikvision phone app. It records your cameras
straight to your own server over RTSP, so you get unlimited storage (NAS / rack
disk), fast local playback, and no cloud relay, ads, or upselling.

Built for a **DS-7804N-S1/W(B)** NVR with three **DS-IPC-K24H-LWT** cameras, but
works with any Hikvision (or ONVIF/RTSP) NVR. See [PROTOCOL.md](PROTOCOL.md) for
how the device was investigated and how the pieces communicate.

## Why it's faster than the app

Your NVR has ports 80 and 8000 closed, so the phone app routes every stream
through the Hik-Connect cloud (ports 9010/9020) even when you're at home.
homenvr connects to RTSP (port 554) **directly on your LAN** — no round trip to
a cloud server, no ads.

## What you need

- A Linux box with Docker (your rack server).
- Network reachability to the NVR (`192.168.1.64`).
- The NVR **admin password**. The device is "activated", so there is no factory
  default — if you don't have it, recover it via the Hik-Connect app's *Forgot
  password*, the NVR's local HDMI/monitor menu, or Hikvision's SADP reset. See
  "Getting the password" below.

## Quick start

```bash
cd homenvr
cp .env.example .env            # set NVR_PASSWORD, UI_PASSWORD, TZ, RECORDINGS_DIR
cp config.example.yml config.yml # adjust cameras / retention if needed
docker compose up -d --build
```

Open `http://<server-ip>:8080` (log in with `admin` + your `UI_PASSWORD`).
Point `RECORDINGS_DIR` in `.env` at your NAS mount or a big disk.

## Configuration

Everything lives in `config.yml` (secrets come from `.env` via `${VAR}`):

- **cameras** — one entry per NVR channel. `live_codec: h264` transcodes live
  view if your browser can't play H.265; `audio: none|aac|copy`.
- **retention** — `max_days`, `max_gb`, `min_free_gb`. Oldest segments are
  deleted first; the newest (being written) is always kept.
- **segment_seconds** — recording file length (default 600 = 10 min).

## Importing footage already on the SD card

Pulls existing recordings off the NVR via its playback API (runs at ~1x speed,
resumable, skips chunks already on disk):

```bash
docker compose exec homenvr python -m app.importer \
  --camera cam1 --from "2026-10-01 00:00" --to "2026-10-02 00:00"
```

Times are in the NVR's local timezone.

## Getting the password

The NVR reports `Activated: true`, meaning a password was set at setup — the old
`12345` / `12345678` defaults do **not** apply. Recover it through one of:

1. **Hik-Connect app** → device settings / *Forgot password* (uses your account).
2. **Local menu** — plug a monitor + USB mouse into the NVR; set/reset there and
   enable RTSP while you're in.
3. **SADP tool** — official Hikvision owner-reset flow (`PasswordResetAbility`
   is true on this unit).

## Enhancements

- **Motion events + smart timeline** — server-side motion detection marks activity
  on the playback timeline; click a marker or use ◀/▶ motion to jump between events.
  Tune in `config.yml` under `motion:`.
- **Health alerts** — get notified (ntfy / webhook / email) if a camera stops
  recording or the disk fills. Configure under `alerts:`; test via `POST /api/alerts/test`.
- **Remote access** — watch from anywhere via Tailscale or Cloudflare Tunnel, no
  cloud and no open ports. See [docs/remote-access.md](docs/remote-access.md).

## Architecture

| File | Role |
| --- | --- |
| `app/config.py` | Config + `.env` interpolation; builds RTSP URLs |
| `app/storage.py` | Segment index, retention, clip export |
| `app/recorder.py` | ffmpeg supervision (record + live), auto-restart |
| `app/importer.py` | Backfill from the NVR's SD card |
| `app/main.py` | HTTP API + web UI |
| `app/static/` | Single-page UI (live grid, timeline scrubber, export) |

## Development

```bash
pip install -r requirements-dev.txt
pytest                 # unit tests (config, storage, API)
tests/e2e/             # docker-compose stack with a simulated NVR + cameras
```

The e2e stack (`tests/e2e/docker-compose.yml`) runs MediaMTX as a fake Hikvision
NVR with synthetic HEVC/H.264 cameras and exercises the full record → playback →
export path. It was used to validate this build end to end.

## Scope and safety

homenvr only **reads** streams from the NVR using credentials you supply. It does
not modify the device, its firmware, or its configuration. Keep `.env` and
`config.yml` out of version control (they're gitignored).
