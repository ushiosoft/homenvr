"""Low-latency live view through a go2rtc sidecar (WebRTC).

go2rtc pulls each camera's sub-stream, transcodes HEVC -> H.264 (+ Opus audio,
which WebRTC requires) and serves it over WebRTC, well under a second behind live
versus several seconds for HLS. homenvr stays the only thing the browser talks
to: the WebRTC offer goes to /api/cameras/{id}/webrtc (behind homenvr's login)
and is forwarded here to go2rtc's API, which is never exposed. Media then flows
directly between the browser and go2rtc's WebRTC port.

Streams are registered at runtime from homenvr's own camera config, so the NVR
credentials live in one place. go2rtc's API also tries to save each stream into
its config file; that file is mounted read-only (it must not collect secrets), so
the save fails while the stream is still created in memory — registration is
therefore confirmed by reading the stream list back, and redone whenever go2rtc
has restarted and lost it.

Each stream is also *preloaded* (kept running with no viewer): go2rtc otherwise
starts the camera connection + transcoder only when someone opens the page, which
made the first picture take 6-12 s. keep_warm() re-applies both every minute.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request

from .config import Camera, Config
from .recorder import redact

log = logging.getLogger(__name__)


def stream_source(cam: Camera) -> str:
    """go2rtc source for a camera: its live (sub) stream, transcoded to H.264 when the
    camera sends HEVC (live_codec: h264), audio to Opus for WebRTC."""
    if cam.live_codec == "h264":
        return f"ffmpeg:{cam.live_url}#video=h264#audio=opus"
    return f"ffmpeg:{cam.live_url}#video=copy#audio=opus"


class Go2rtc:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.webrtc_api

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    def _streams(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/streams", timeout=5) as r:
            return json.load(r) or {}

    def ensure_stream(self, cam: Camera) -> None:
        """Make sure go2rtc has a stream named after the camera (idempotent)."""
        if cam.id in self._streams():
            return
        q = urllib.parse.urlencode({"name": cam.id, "src": stream_source(cam)})
        try:
            urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/streams?{q}", method="PUT"), timeout=5)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if "read-only" not in body:  # anything but the expected "can't save to config"
                raise RuntimeError(f"go2rtc rejected stream {cam.id}: {e.code} {redact(body)[:200]}")
        if cam.id not in self._streams():
            raise RuntimeError(f"go2rtc did not create stream {cam.id}")
        log.info("[%s] webrtc stream registered with go2rtc", cam.id)

    def ensure_preload(self, cam: Camera) -> None:
        """Keep the camera's stream running with no viewer, so live view starts at once."""
        with urllib.request.urlopen(f"{self.base}/api/preload", timeout=5) as r:
            if cam.id in (json.load(r) or {}):
                return
        q = urllib.parse.urlencode({"src": cam.id})
        try:  # go2rtc waits for the stream to start before answering, and (like streams)
            # tries to save to its read-only config; both are fine — verified below.
            urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/preload?{q}", method="PUT"), timeout=20)
        except (urllib.error.HTTPError, socket.timeout, TimeoutError):
            pass
        with urllib.request.urlopen(f"{self.base}/api/preload", timeout=5) as r:
            if cam.id not in (json.load(r) or {}):
                raise RuntimeError(f"go2rtc did not preload {cam.id}")
        log.info("[%s] webrtc stream preloaded", cam.id)

    async def keep_warm(self, interval: float = 60) -> None:
        """Register + preload every enabled camera, and repeat (go2rtc may restart)."""
        failing: set[str] = set()
        while True:
            for cam in self.cfg.cameras:
                if not cam.enabled:
                    continue
                try:
                    await asyncio.to_thread(self.ensure_stream, cam)
                    await asyncio.to_thread(self.ensure_preload, cam)
                    failing.discard(cam.id)
                except Exception as e:
                    if cam.id not in failing:  # log once per outage, not every minute
                        log.warning("[%s] webrtc: go2rtc not ready (%s); live view uses HLS meanwhile",
                                    cam.id, redact(str(e)))
                    failing.add(cam.id)
            await asyncio.sleep(interval)

    def answer(self, cam: Camera, offer_sdp: str) -> str:
        """Exchange the browser's SDP offer for go2rtc's answer (WHEP-style, no trickle)."""
        self.ensure_stream(cam)
        q = urllib.parse.urlencode({"src": cam.id})
        req = urllib.request.Request(f"{self.base}/api/webrtc?{q}", data=offer_sdp.encode(), method="POST",
                                     headers={"Content-Type": "application/sdp"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode()
