# homenvr roadmap

Planned enhancements beyond the core record/playback system.

**Status (Oct 2026):** 1 (motion events, via Option B + object labels), 2 (health
alerts) and 3 (Tailscale sidecar) are built. 4 is the open backlog item.

Context: the core system already replaces the Hik app's playback — recordings
are local MP4s served with HTTP range requests, so "failed to grab recording"
and cloud-relay slowness are gone. These three features make homenvr clearly
better than the app rather than just more reliable.

---

## 1. Motion events + smart timeline

Goal: mark activity on the playback timeline so you jump to events instead of
scrubbing empty footage.

There are two possible event sources. This is the main decision for this feature.

### Option A — the cameras' own events (ISAPI alert stream)  ← recommended
The DS-IPC-K24H cameras already detect motion on-device (and human/vehicle if
AcuSense-equipped). The NVR exposes these as a long-lived HTTP stream:

    GET /ISAPI/Event/notification/alertStream   (multipart, digest auth)

Each part is an XML `<EventNotificationAlert>` with channel, event type
(VMD / linedetection / fielddetection / PIR), and timestamp.

- **Pros:** zero server CPU; uses the camera's real detection (incl. smart
  human/vehicle classification); precise timestamps.
- **Cost:** requires the NVR's **HTTP port (80) enabled** — currently closed.
  Enable it locally (Configuration → Network → Ports, or the on-device menu).
  Bound to the LAN only; not exposed to the internet.

Design: a new `app/events.py` async task holds the alertStream open (auto-reconnect,
same supervision pattern as the recorder), writes events to a small SQLite DB
(`events.db`: ts, camera, type, raw). New API `GET /api/cameras/{id}/events?start&end`
returns markers; the timeline JS draws them and click-to-seek jumps to the event.

### Option B — server-side motion detection (no NVR changes)
Analyze the **sub-stream** (640×360, cheap to decode) with ffmpeg scene-change
detection or frame differencing; optionally a small person/vehicle model later.

- **Pros:** no port changes; fully self-contained.
- **Cons:** server CPU (modest for 3× 640×360); less accurate than AcuSense;
  "motion" only unless we add ML.

Design: an ffmpeg `select='gt(scene,T)'` or `scdet` pass per camera writing
timestamps to the same `events.db`; everything downstream is identical to A.

**Recommendation:** A if you're willing to enable port 80 on the LAN (much better
quality, free CPU); B as the no-touch fallback. The DB schema and timeline UI are
shared, so we can start with B and add A later without rework.

---

## 2. Health alerts

Goal: never have a camera silently stop recording.

The recorder already tracks per-worker state (running, restarts, last_error,
stream info). Add a watchdog loop in the supervisor that checks, each minute:

- recorder process running for each enabled camera;
- the newest segment's mtime is advancing (detects a "connected but no data" hang
  that a bare process check misses);
- disk free above `min_free_gb`;
- (optional) event stream connected, if feature 1/A is on.

Alerts are **edge-triggered**: fire once when a check starts failing, once more on
recovery — no repeat spam. Notification backends via one config block:

- **ntfy** (recommended) — self-hostable, dead-simple HTTP POST, free phone app;
- SMTP email;
- generic webhook (Discord/Slack/Telegram) or Apprise for all-of-the-above.

New config:

    alerts:
      notifier: "ntfy"            # ntfy | smtp | webhook | none
      ntfy_url: "https://ntfy.sh/your-private-topic"
      stale_segment_seconds: 180  # mtime not advancing this long = alert

New files: `app/alerts.py` (notifier abstraction + watchdog). Also surface current
health in the UI header (green/red per camera already exists; add a disk + stale
indicator).

---

## 3. Remote access

Goal: the app's one genuine convenience — watch from anywhere — without the Hik
cloud, ads, or exposing the NVR.

homenvr is a plain HTTP service, so this is a networking layer, not app code.
Three options, in order of recommendation:

### Tailscale  ← recommended for private/family use
WireGuard mesh; install the Tailscale app on your phone and the rack server (or a
compose sidecar). Reach homenvr at its Tailscale IP / MagicDNS name. No ports
forwarded, works behind CGNAT, per-device ACLs. Nothing is public.

- Add a `tailscale` sidecar service to compose, or run it on the host.
- Keep homenvr's Basic auth on as defense-in-depth.

### Cloudflare Tunnel
Gives a clean public HTTPS hostname with no open ports; put Cloudflare Access in
front for auth. Best if you want a shareable URL for family members who won't
install Tailscale.

### Caddy/nginx reverse proxy + port-forward + Let's Encrypt
Most control, most exposure — only with strong auth and ideally IP allowlisting.
Not recommended unless the other two don't fit.

**Recommendation:** Tailscale. ~15 min to set up, nothing exposed, and it also
lets you reach the NVR itself remotely for maintenance.

---

## 4. Sub-second live view (WebRTC streaming service)  — backlog

Goal: a "doorbell-style" live picture with < 1 s delay, instead of HLS.

Where the delay is today (Oct 2026, after moving to 1 s HLS segments): the
browser player sits 2 segments (≈ 2 s) behind the newest video, segments are
1 s long, plus camera/NVR RTSP, the H.264 transcode and browser buffering.
Measured: hls.js latency 4.1 s → 2.1 s; camera clock vs. display 8–9 s → 6 s
(that figure includes the camera clock's offset). HLS can't go much lower
without stalling on the cameras' Wi-Fi.

Design: add a small streaming server as a compose sidecar —
**go2rtc** (recommended: one binary, RTSP in → WebRTC/MSE out, can transcode
HEVC → H.264 with ffmpeg, handles G.711 audio) or **MediaMTX**.

- It pulls each camera's sub-stream from the NVR once; homenvr's live worker can
  then read from go2rtc instead of the NVR, so the cameras' Wi-Fi isn't carrying
  an extra copy. (The recorder keeps its own main-stream connection.)
- The Cameras page plays WebRTC in the tiles (go2rtc ships a small JS player /
  `video-rtc` element), with the current hls.js path as the fallback.
- Remote: WebRTC over Tailscale works peer-to-peer on the tailnet (UDP); expose
  go2rtc's WebRTC port only on the tailnet, or use its TCP fallback through the
  same HTTPS proxy.
- Auth: keep go2rtc unexposed and proxy its signalling through homenvr
  (`/live/rtc/...`) so the existing login still applies.
- Keep: snapshots / push frames read from homenvr's own live segments, so the
  motion → push path doesn't depend on the new service.

Open questions: whether the NVR allows a second sub-stream reader alongside the
motion detector (or move motion detection to read from go2rtc too); CPU of
go2rtc's own transcode vs. today's live ffmpeg.

---

## Suggested build order

1. **Health alerts** — smallest, protects everything else; builds on existing state.
2. **Remote access (Tailscale)** — config/infra only, no app code; unlocks daily use.
3. **Motion events** — biggest feature; decide Option A vs B first (the port-80 call).

Backfilling the SD card (the existing `app/importer.py`) is independent and can run
anytime before the 128 GB card rotates away.
