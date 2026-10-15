# Camera / NVR protocol findings

How your Hikvision kit actually communicates, established by probing the live
device at `192.168.1.64` (passive discovery + standard RTSP handshakes only —
no credentials required for any of this).

## The device

SADP discovery (UDP multicast `239.255.255.250:37020`) returned:

| Field | Value |
| --- | --- |
| Model | `DS-7804N-S1/W(B)` |
| Serial | `DS-7804N-S1/W(B)<serial>` |
| Firmware | `V4.x` |
| Channels | 4 digital (3 cameras connected) |
| MAC | `xx-xx-xx-xx-xx-xx` |
| Activated | **true** (custom password set at activation; no factory default) |

## Open ports

A full TCP scan of the NVR found exactly three services:

| Port | Service | Role |
| --- | --- | --- |
| 554 | **RTSP** | Live + recorded video/audio. This is the one that matters. |
| 9010 | EZVIZ/Hik-Connect | Cloud command channel (used by the phone app) |
| 9020 | EZVIZ/Hik-Connect | Cloud live-view channel |
| 22 | SSH (dropbear) | Appears only after you enable it in the app |

Note what is **closed**: port 80 (web UI) and port 8000 (SDK/ISAPI service
port) are both disabled in your configuration. That is why there is no web
interface to log into, and it is a large part of *why the app is slow* — with
80/8000 shut, the phone app has no choice but to route everything (even when
you are at home on the same WiFi) through the EZVIZ cloud relay on 9010/9020.
Your video is making a round trip to a cloud server and back, plus carrying the
app's ads. **homenvr talks RTSP directly to the device on your LAN and skips the
cloud entirely.**

## How the pieces talk

```
                          NVR's own WiFi AP
  [cam1] [cam2] [cam3]  ───────────────────►  DS-7804N-S1/W(B)
  DS-IPC-K24H-LWT        (wireless kit)        192.168.1.64
                                                 │  SD card (128 GB, the bottleneck)
                                                 │
                      ┌──────────────────────────┼───────────────────────────┐
                      │ RTSP :554 (digest auth)   │ EZVIZ :9010/:9020          │
                      ▼                           ▼                            
                 homenvr (your rack)         Hik-Connect cloud ──► phone app   
                 records to NAS/disk          (slow, ad-laden)                  
```

The `/W` wireless kit means the three cameras are bridged over the **NVR's own
WiFi**, not your home LAN. They are therefore not individually reachable at a
`192.168.1.x` address — all camera video must be pulled **through the NVR**.
A single RTSP endpoint (`192.168.1.64:554`) with one admin credential gives
access to every channel. That is the whole protocol surface homenvr needs.

### RTSP endpoints (Hikvision scheme)

Confirmed against the device: `OPTIONS` succeeds and `DESCRIBE` returns
`401 Unauthorized` with `WWW-Authenticate: Digest realm="...", algorithm=MD5`,
i.e. standard RTSP **digest** auth (ffmpeg handles this natively).

| Purpose | URL |
| --- | --- |
| Live, main stream (ch N) | `rtsp://admin:PW@192.168.1.64:554/Streaming/Channels/N01` |
| Live, sub stream (ch N) | `rtsp://admin:PW@192.168.1.64:554/Streaming/Channels/N02` |
| Recorded playback (ch N) | `rtsp://admin:PW@192.168.1.64:554/Streaming/tracks/N01?starttime=YYYYMMDDTHHMMSSZ&endtime=YYYYMMDDTHHMMSSZ` |

- `N` is the channel number (1, 2, 3). `01` = main stream, `02` = sub stream.
- **Main** stream is full-res HEVC (H.265); **sub** is a lower-res H.264 stream
  that browsers can play without transcoding. homenvr records main and uses sub
  for live preview.
- Playback `starttime`/`endtime` carry a `Z` suffix but are interpreted in the
  **NVR's local timezone**, not UTC — a long-standing Hikvision quirk the
  importer accounts for.

## What homenvr does with this

1. **Record** each camera's main stream to 10-minute fragmented-MP4 segments on
   your server (stream-copy, no re-encoding — near-zero CPU, original quality).
2. **Live view** by remuxing the sub stream to low-latency HLS for the browser.
3. **Playback / scrub / export** from the segments on disk — instant, local, no
   cloud.
4. **Backfill** footage already on the NVR's SD card via the `tracks` playback
   URL (`app/importer.py`).
5. **Retention** by age / total size / free-space, so storage is bounded by your
   disk, not a 128 GB card.

No firmware modification, no cloud account, no app.
