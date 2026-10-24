"""Server-side motion detection on the camera sub-stream (no NVR changes).

Runs a lightweight ffmpeg scene-score analysis on the low-res sub-stream and
records a motion event whenever the share of the picture that changed exceeds the threshold
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
from pathlib import Path

from .config import Camera, Config
from .events import EventStore
from .recorder import FALLBACK_RETRY_SECONDS, FFMPEG, input_args, pick_source, redact, sub_sources

log = logging.getLogger(__name__)

# The score: the share of the picture (%) whose brightness changed by more than
# CHANGE_LEVEL between analysed frames (after a light blur against sensor noise).
# Something that moves changes its own pixels a lot, so it counts in proportion to
# its size wherever it is; a camera's exposure step or a keyframe's quality pulse
# shifts every pixel a little, so it counts for (almost) nothing. The previous score,
# the mean change over the whole picture, was the other way round: a person walking
# two dogs across the street averaged ~1.5 (under the threshold) while exposure
# steps scored 5-9. ffmpeg reports it as signalstats' YAVG of a 0/255 image.
CHANGE_LEVEL = 25
_YAVG_RE = re.compile(rb"lavfi\.signalstats\.YAVG=([0-9.]+)")
ANALYZE_FPS = 5
ANALYZE_WIDTH = 320
# Frames in a row the score must reach the threshold for an event (at ANALYZE_FPS: 0.4 s).
# A single-frame jump is not motion: a camera's exposure steps (whole-picture
# brightness changes, ~200/hour in daylight) and decode glitches make exactly that,
# while anything that really moves changes several frames in a row.
CONFIRM_FRAMES = 2
# No frame score for this long = the analyzer is blind (e.g. the stream's timestamps
# jumped back and the fps filter drops every frame until they catch up): restart it.
STALL_SECONDS = 30
# The output reader's line limit (asyncio's default is 64 KB).
LINE_LIMIT = 1 << 20


def sensitivity_for(cam: Camera, cfg: Config) -> float:
    """Per-camera motion threshold override, else the global default."""
    return cam.motion_sensitivity if getattr(cam, "motion_sensitivity", 0) else cfg.motion.sensitivity


def analyze_height(width: int | None, height: int | None) -> int:
    """Height of the analyzed frames (ANALYZE_WIDTH wide, even, aspect kept)."""
    if not width or not height:
        return 180
    return max(2, round(ANALYZE_WIDTH * height / width / 2) * 2)


def motion_cmd(cam: Camera, cfg: Config, url: str | None = None, mask: Path | None = None,
               height: int = 180) -> list[str]:
    """The analyzer: frame differences at ANALYZE_FPS, thresholded at CHANGE_LEVEL and
    scored by the share of changed pixels (see score()). With a mask (the camera's
    ignore zones, app/zones.py) the difference is multiplied by it first, so changes
    inside a zone score nothing."""
    head = [FFMPEG, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            *input_args(url or cam.live_url, cfg.rtsp_transport)]
    scale = f"fps={ANALYZE_FPS},scale={ANALYZE_WIDTH}:{height},gblur=sigma=1"
    changed = f"lutyuv=y=if(gt(val\\,{CHANGE_LEVEL})\\,255\\,0),signalstats,metadata=print"
    if mask is None:
        return [*head, "-an", "-vf", f"{scale},tblend=all_mode=difference,{changed}",
                "-f", "null", "-"]
    # One still image as the second input: blend keeps applying its last frame. The
    # mask is declared limited-range so its 0/255 survive the conversion to YUV as
    # they are (otherwise white becomes 235 and every score drops ~8 %).
    graph = (f"[0:v]{scale},tblend=all_mode=difference[d];"
             f"[1:v]format=gray,scale=in_range=tv:out_range=tv,format=yuv420p[m];"
             f"[d][m]blend=all_mode=multiply,{changed}")
    return [*head, "-i", str(mask), "-an", "-filter_complex", graph, "-f", "null", "-"]


def score(yavg: float) -> float:
    """signalstats' YAVG of the thresholded difference -> % of the picture changed."""
    return yavg * 100.0 / 255.0


class MotionDetector:
    """Supervised per-camera motion analyzer with backoff restart."""

    def __init__(self, cam: Camera, cfg: Config, store: EventStore, on_event=None,
                 ready: asyncio.Event | None = None):
        self.cam, self.cfg, self.store = cam, cfg, store
        self.ready = ready  # wait for this (go2rtc registered) before reading the sub-stream
        self.on_event = on_event  # optional async callback(camera_id, ts, score)
        self._stopping = False
        self._proc: asyncio.subprocess.Process | None = None
        self.last_event_ts = 0.0
        self._above = 0  # consecutive frames at/above the threshold
        self.events = 0
        self._loop = None

    def _line(self, line: bytes, now: float | None = None) -> bool:
        """One line of the analyzer's output; True if it made an event."""
        m = _YAVG_RE.search(line)
        if not m:
            return False
        s = score(float(m.group(1)))
        if self._sample(s, now):
            log.debug("[%s/motion] event: %.2f %% of the picture changed", self.cam.id, s)
            return True
        return False

    async def _read(self, stream: asyncio.StreamReader) -> bool:
        """Feed the analyzer's output to _line until it ends (True) or no frame score
        has come for STALL_SECONDS (False)."""
        last_score = time.monotonic()
        while True:
            left = STALL_SECONDS - (time.monotonic() - last_score)
            if left <= 0:
                return False
            try:
                line = await asyncio.wait_for(stream.readline(), left)
            except asyncio.TimeoutError:
                return False
            if not line:
                return True
            if _YAVG_RE.search(line):
                last_score = time.monotonic()
            self._line(line)

    def _sample(self, score: float, now: float | None = None) -> bool:
        """Feed one frame's score; records an event (see _record) once CONFIRM_FRAMES
        frames in a row reach the threshold. Returns True if an event was stored."""
        if score < sensitivity_for(self.cam, self.cfg):
            self._above = 0
            return False
        self._above += 1
        return self._above >= CONFIRM_FRAMES and self._record(score, now)

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
            fallback_timer = None
            try:
                # Same source as the live view: go2rtc's restream first, NVR as fallback.
                if self.ready is not None:
                    await self.ready.wait()
                url, info, fallback = await pick_source(sub_sources(self.cam, self.cfg), self.cfg.rtsp_transport)
                if fallback:
                    log.warning("[%s/motion] go2rtc unavailable; reading the NVR directly for now", self.cam.id)
                    fallback_timer = asyncio.get_running_loop().call_later(
                        FALLBACK_RETRY_SECONDS, lambda: self._proc and self._proc.returncode is None and self._proc.kill())
                height = analyze_height(info.get("width"), info.get("height"))
                mask = None
                from .zones import motion_mask, motion_polys
                if motion_polys(self.cam.zones):
                    mask = await asyncio.to_thread(
                        motion_mask, self.cam.zones, self.cam.rotation, ANALYZE_WIDTH, height,
                        Path(self.cfg.live_dir) / f"{self.cam.id}-motion-mask.png")
                cmd = motion_cmd(self.cam, self.cfg, url, mask, height)
                log.info("[%s/motion] start: %s", self.cam.id, redact(" ".join(cmd)))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "TZ": "UTC"}, limit=LINE_LIMIT,
                )
                assert self._proc.stderr
                if not await self._read(self._proc.stderr):
                    log.warning("[%s/motion] no frames analysed for %d s; restarting",
                                self.cam.id, STALL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("[%s/motion] %s", self.cam.id, redact(str(e)))
            finally:
                # Never leave it running: blocked on a stderr pipe nobody reads, it would
                # keep holding a go2rtc stream next to its replacement.
                p, self._proc = self._proc, None
                if p and p.returncode is None:
                    p.kill()
                    await p.wait()
                if fallback_timer:
                    fallback_timer.cancel()
            if self._stopping:
                break
            if time.monotonic() - t0 > 60:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def restart(self) -> None:
        """Restart the analyzer (e.g. its zones changed); the run loop starts it again."""
        p = self._proc
        if p and p.returncode is None:
            p.kill()

    async def stop(self) -> None:
        self._stopping = True
        p = self._proc
        if p and p.returncode is None:
            p.kill()
            await p.wait()
