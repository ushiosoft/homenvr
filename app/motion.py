"""Server-side motion detection on the camera sub-stream (no NVR changes).

Runs a lightweight ffmpeg scene-score analysis on the low-res sub-stream and
records a motion event whenever frame-to-frame change exceeds the sensitivity
threshold. Events are debounced (a burst of motion = one event) and written to
the shared EventStore for the smart timeline.

Detection is on the live sub-stream in real time, so each detection's wall-clock
time ~= now — no PTS/stream-time alignment needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from .config import Camera, Config
from .events import EventStore
from .recorder import FFMPEG, input_args, redact

log = logging.getLogger(__name__)

# Average brightness of the inter-frame difference image (0-255). Static scene -> ~0,
# any real motion -> rises. A far better motion signal than scene-cut detection.
_YAVG_RE = re.compile(rb"lavfi\.signalstats\.YAVG=([0-9.]+)")
ANALYZE_FPS = 5
ANALYZE_WIDTH = 320


def sensitivity_for(cam: Camera, cfg: Config) -> float:
    """Per-camera motion threshold override, else the global default."""
    return cam.motion_sensitivity if getattr(cam, "motion_sensitivity", 0) else cfg.motion.sensitivity


def motion_cmd(cam: Camera, cfg: Config) -> list[str]:
    vf = (f"fps={ANALYZE_FPS},scale={ANALYZE_WIDTH}:-2,"
          f"tblend=all_mode=difference,signalstats,metadata=print")
    return [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info",
        *input_args(cam.live_url, cfg.rtsp_transport),
        "-an", "-vf", vf, "-f", "null", "-",
    ]


class MotionDetector:
    """Supervised per-camera motion analyzer with backoff restart."""

    def __init__(self, cam: Camera, cfg: Config, store: EventStore, on_event=None):
        self.cam, self.cfg, self.store = cam, cfg, store
        self.on_event = on_event  # optional async callback(camera_id, ts, score)
        self._stopping = False
        self._proc: asyncio.subprocess.Process | None = None
        self.last_event_ts = 0.0
        self.events = 0
        self._loop = None

    def _record(self, score: float, now: float | None = None) -> bool:
        """Debounce: only one event per min_gap_seconds window. Returns True if stored."""
        now = now or time.time()
        if now - self.last_event_ts < self.cfg.motion.min_gap_seconds:
            return False
        self.last_event_ts = now
        eid = self.store.add(self.cam.id, now, "motion", score)
        self.events += 1
        if self.on_event and self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.on_event(self.cam.id, now, score, eid)))
        return True

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        backoff = 2.0
        while not self._stopping:
            t0 = time.monotonic()
            try:
                cmd = motion_cmd(self.cam, self.cfg)
                log.info("[%s/motion] start: %s", self.cam.id, redact(" ".join(cmd)))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "TZ": "UTC"},
                )
                assert self._proc.stderr
                async for line in self._proc.stderr:
                    m = _YAVG_RE.search(line)
                    if not m:
                        continue
                    yavg = float(m.group(1))
                    if yavg >= sensitivity_for(self.cam, self.cfg) and self._record(yavg):
                        log.debug("[%s/motion] event YAVG=%.2f", self.cam.id, yavg)
                await self._proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("[%s/motion] %s", self.cam.id, redact(str(e)))
            finally:
                self._proc = None
            if self._stopping:
                break
            if time.monotonic() - t0 > 60:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def stop(self) -> None:
        self._stopping = True
        p = self._proc
        if p and p.returncode is None:
            p.kill()
            await p.wait()
