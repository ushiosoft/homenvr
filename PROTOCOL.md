# Camera / NVR protocol notes

How a Hikvision wireless NVR kit communicates, and the small part of it homenvr
uses. Established by probing a **DS-7804N-S1/W(B)** NVR with three
**DS-IPC-K24H-LWT** Wi-Fi cameras using passive discovery and standard RTSP
handshakes only; no credentials are needed for any of the probing below. Other
Hikvision models expose the same RTSP scheme, though their open ports may differ.

## The device

SADP discovery (UDP multicast `239.255.255.250:37020`) reports the model, serial
number, firmware version, MAC address, channel count (4 digital channels here)
and whether the device is **activated**. Activated devices have a password chosen
at activation; there is no factory-default password to fall back on, so homenvr
needs the NVR's admin password from you.

## Open ports

A full TCP scan of this NVR found only these services:

| Port | Service | Role |
| --- | --- | --- |
| 554 | **RTSP** | Live and recorded video/audio. The one homenvr uses. |
| 9010 | EZVIZ / Hik-Connect | Cloud command channel (used by the phone app) |
| 9020 | EZVIZ / Hik-Connect | Cloud live-view channel |
| 22 | SSH (dropbear) | Only after it is enabled in the app |

Port 80 (web UI) and port 8000 (SDK/ISAPI) were closed. With those shut, the
phone app routes everything — even at home on the same Wi-Fi — through the
EZVIZ cloud relay on 9010/9020, which is a large part of why it feels slow.
homenvr talks RTSP to the NVR directly on the LAN and never touches the cloud.

## How the pieces talk

```
                          NVR's own Wi-Fi AP
  [cam1] [cam2] [cam3]  ───────────────────►  NVR (DS-7804N-S1/W(B))
  (wireless kit)                                 │  SD card (small: rotates in days)
                                                 │
                      ┌──────────────────────────┼───────────────────────────┐
                      │ RTSP :554 (digest auth)   │ EZVIZ :9010/:9020          │
                      ▼                           ▼                            │
                 homenvr                      Hik-Connect cloud ──► phone app  │
                 records to your disk         (slow relay)                     │
```

In the `/W` wireless kits the cameras join the **NVR's own Wi-Fi**, not your home
network, so they are not individually reachable: all video is pulled **through
the NVR**. One RTSP endpoint (`<nvr>:554`) with the admin credential gives access
to every channel — that is the whole protocol surface homenvr needs. (Cameras on
your own network can instead be given direct `record_url` / `live_url`s.)

### RTSP endpoints (Hikvision scheme)

`OPTIONS` succeeds without credentials and `DESCRIBE` answers `401 Unauthorized`
with `WWW-Authenticate: Digest realm="...", algorithm=MD5` — standard RTSP
**digest** auth, which ffmpeg handles natively.

| Purpose | URL |
| --- | --- |
| Live, main stream (channel N) | `rtsp://admin:PW@<nvr>:554/Streaming/Channels/N01` |
| Live, sub stream (channel N) | `rtsp://admin:PW@<nvr>:554/Streaming/Channels/N02` |
| Recorded playback (channel N) | `rtsp://admin:PW@<nvr>:554/Streaming/tracks/N01?starttime=YYYYMMDDTHHMMSSZ&endtime=YYYYMMDDTHHMMSSZ` |

- `N` is the channel number (1, 2, … 10, …): `01` = main stream, `02` = sub stream,
  so channel 12's main stream is `1201`.
- Codecs are whatever the cameras are set to (H.264 or H.265 per stream). The
  README's *Camera settings* recommends H.264 for both: browsers play it
  directly, and the sub-stream needs no transcode.
- Playback `starttime`/`endtime` carry a `Z` suffix but are interpreted in the
  **NVR's local timezone**, not UTC — a long-standing Hikvision quirk the
  importer accounts for.

## What homenvr does with this

1. **Records** each camera's main stream into 10-minute fragmented-MP4 files on
   your server (stream copy, no re-encoding: near-zero CPU, original quality).
2. **Live view** from the sub-stream: WebRTC through the go2rtc sidecar, with
   low-latency HLS as the fallback.
3. **Playback, scrubbing and export** from the files on disk — local, no cloud.
4. **Backfill** of footage still on the NVR's SD card via the `tracks` URL
   (`app/importer.py`).
5. **Retention** by age, total size and free space, so storage is bounded by your
   disk instead of the SD card.

No firmware modification, no cloud account, no app.
