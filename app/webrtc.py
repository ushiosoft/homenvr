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


CODEC_RECHECK_SECONDS = 600  # cameras can be switched H.265 <-> H.264 in the Hik app


def stream_source(cam: Camera, codec: str | None) -> str:
    """go2rtc source for a camera's sub-stream.

    H.264 is passed through untouched (go2rtc's native RTSP client: no ffmpeg, no
    CPU; G.711 audio is WebRTC-native too). Anything else (the cameras' default
    H.265) is converted once by go2rtc's ffmpeg to H.264 + Opus, which is what
    browsers, the live HLS worker and motion detection all read from then on.
    """
    if codec == "h264":
        return cam.live_url
    return f"ffmpeg:{cam.live_url}#video=h264#audio=opus"


def probe_codec(url: str, timeout: float = 20) -> str | None:
    """Video codec of an RTSP stream (blocking; run in a thread)."""
    import subprocess
    from .recorder import FFPROBE
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0", url],
                             capture_output=True, text=True, timeout=timeout).stdout.strip()
        return out or None
    except Exception:  # noqa: BLE001 - unknown -> treat as needing conversion
        return None


class Go2rtc:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.webrtc_api
        self._codec: dict[str, tuple[float, str | None]] = {}  # cam id -> (checked at, codec)
        # The source we registered per camera. go2rtc reports a stream's producer URL
        # without the "ffmpeg:" prefix and "#video=..." options, so it can't tell us.
        self._registered: dict[str, str] = {}
        # Cameras whose stream was just (re)created: go2rtc keeps the preload *entry*
        # when a stream is deleted, but its connection dies, so it must be redone.
        self._repreload: set[str] = set()

    def _call(self, method: str, path: str, timeout: float = 5) -> None:
        """go2rtc API write. go2rtc applies the change, then tries to save it to its
        (deliberately read-only) config and answers 400 "read-only" - that's success."""
        try:
            urllib.request.urlopen(urllib.request.Request(f"{self.base}{path}", method=method), timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if "read-only" not in body:
                raise RuntimeError(f"go2rtc {method} failed: {e.code} {redact(body)[:200]}")

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    def _streams(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/streams", timeout=5) as r:
            return json.load(r) or {}

    def codec(self, cam: Camera) -> str | None:
        import time
        at, codec = self._codec.get(cam.id, (0.0, None))
        if time.monotonic() - at > CODEC_RECHECK_SECONDS or codec is None:
            codec = probe_codec(cam.live_url)
            self._codec[cam.id] = (time.monotonic(), codec)
        return codec

    def ensure_stream(self, cam: Camera) -> None:
        """Make sure go2rtc has a stream named after the camera with the right source
        for its current codec (idempotent; re-creates it if the codec changed)."""
        want = stream_source(cam, self.codec(cam))
        exists = cam.id in self._streams()
        if exists and self._registered.get(cam.id) == want:
            return
        if exists:
            # Ours from before a homenvr restart (source unknown), or the camera switched
            # codec: replace it so the source is right.
            self._call("DELETE", "/api/streams?" + urllib.parse.urlencode({"src": cam.id}))
            if self._registered.get(cam.id):
                log.info("[%s] camera codec changed; re-registering its go2rtc stream", cam.id)
        self._call("PUT", "/api/streams?" + urllib.parse.urlencode({"name": cam.id, "src": want}))
        self._repreload.add(cam.id)
        if cam.id not in self._streams():
            raise RuntimeError(f"go2rtc did not create stream {cam.id}")
        self._registered[cam.id] = want
        log.info("[%s] webrtc stream registered with go2rtc (%s)", cam.id,
                 "pass-through" if want == cam.live_url else "converted to H.264")

    def ensure_preload(self, cam: Camera) -> None:
        """Keep the camera's stream running with no viewer, so live view starts at once."""
        with urllib.request.urlopen(f"{self.base}/api/preload", timeout=5) as r:
            listed = cam.id in (json.load(r) or {})
        if listed and cam.id not in self._repreload:
            return
        q = urllib.parse.urlencode({"src": cam.id})
        if listed:  # stale entry from before the stream was re-created
            self._call("DELETE", f"/api/preload?{q}")
        try:  # go2rtc waits for the stream to start before answering, and (like streams)
            # tries to save to its read-only config; both are fine — verified below.
            urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/preload?{q}", method="PUT"), timeout=20)
        except (urllib.error.HTTPError, socket.timeout, TimeoutError):
            pass
        with urllib.request.urlopen(f"{self.base}/api/preload", timeout=5) as r:
            if cam.id not in (json.load(r) or {}):
                raise RuntimeError(f"go2rtc did not preload {cam.id}")
        self._repreload.discard(cam.id)
        log.info("[%s] webrtc stream preloaded", cam.id)

    async def register_all(self, timeout: float = 45) -> None:
        """Best-effort initial registration before the live/motion workers start, so
        they find go2rtc's restreams right away (they fall back to the NVR if not)."""
        async def one(cam):
            try:
                await asyncio.to_thread(self.ensure_stream, cam)
                await asyncio.to_thread(self.ensure_preload, cam)
            except Exception as e:  # noqa: BLE001 - keep_warm keeps retrying
                log.info("[%s] webrtc: initial registration failed (%s)", cam.id, redact(str(e)))
        try:
            await asyncio.wait_for(asyncio.gather(*(one(c) for c in self.cfg.cameras if c.enabled)), timeout)
        except asyncio.TimeoutError:
            log.warning("webrtc: go2rtc registration still pending after %ss; continuing", timeout)

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
