"""ffmpeg process supervision: one recorder and one live-view process per camera."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import Camera, Config
from .storage import FFMPEG_PATTERN, Storage

log = logging.getLogger(__name__)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
RTSP_TIMEOUT_US = "10000000"  # 10 s socket I/O timeout, in microseconds.


def _ffmpeg_major() -> int:
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True).stdout
        m = re.search(r"version\s+n?(\d+)", out)
        return int(m.group(1)) if m else 5
    except Exception:
        return 5


# The RTSP socket I/O timeout flag is version-dependent and the name collides
# with unrelated options, so we pick it once from the ffmpeg version:
#   - ffmpeg 5+ : -timeout   (microseconds; socket I/O timeout for the RTSP demuxer)
#   - ffmpeg 4.x: -stimeout  (-timeout there means *listen* timeout -> server mode,
#                 "Unable to open RTSP for listening"; -rw_timeout isn't accepted
#                 as an RTSP input option)
_TIMEOUT_FLAG = "-timeout" if _ffmpeg_major() >= 5 else "-stimeout"


def socket_timeout_args() -> list[str]:
    return [_TIMEOUT_FLAG, RTSP_TIMEOUT_US]


def input_args(url: str, transport: str) -> list[str]:
    # genpts/discardcorrupt: camera RTSP streams often start with packets that
    # have no PTS or are damaged by WiFi loss.
    return ["-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt",
            "-rtsp_transport", transport, *socket_timeout_args(), "-i", url]


def redact(text: str) -> str:
    """Strip credentials from rtsp://user:pass@host URLs in log text."""
    import re

    return re.sub(r"(rtsp://)[^/@\s]+@", r"\1***@", text)


async def probe(url: str, transport: str = "tcp", timeout: float = 20) -> dict:
    """Return {'video': codec, 'width', 'height', 'audio': codec|None} for an RTSP URL."""
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error", "-rtsp_transport", transport, *socket_timeout_args(),
        "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffprobe timed out")
    if proc.returncode != 0:
        raise RuntimeError(redact(err.decode(errors="replace").strip()) or f"ffprobe exit {proc.returncode}")
    streams = json.loads(out or b"{}").get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise RuntimeError("no video stream")
    return {"video": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
            "audio": a.get("codec_name") if a else None}


def rotation_vf(deg: int) -> str:
    """ffmpeg transpose chain for a 0/90/180/270 rotation (empty for 0)."""
    return {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}.get(int(deg) % 360, "")


async def _ffmpeg_jpeg(args_in: list[str], width: int, timeout: float, skip: int = 0, rot: int = 0) -> bytes:
    sel = f"select='gte(n\\,{skip})'," if skip else ""
    rv = rotation_vf(rot)
    vf = f"{sel}{rv + ',' if rv else ''}scale={width}:-2"
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *args_in,
        "-vf", vf, "-frames:v", "1", "-fps_mode", "passthrough",
        "-q:v", "4", "-f", "image2", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        raise RuntimeError("snapshot timed out")
    if proc.returncode != 0 or not out:
        raise RuntimeError(redact(err.decode(errors="replace").strip()) or "snapshot failed")
    return out


async def snapshot_from_live(live_dir, cam_id: str, width: int = 640, timeout: float = 15, rot: int = 0) -> bytes:
    """Grab a JPEG from the latest on-disk live HLS segment — clean H.264, no extra
    camera connection (a fresh RTSP grab contends with the live worker on the
    wireless sub-stream and returns grey frames). Segments start on a keyframe."""
    import tempfile
    d = Path(live_dir) / cam_id
    init = d / "init.mp4"
    segs = sorted(d.glob("*.m4s"))
    if not init.exists() or not segs:
        raise RuntimeError("no live segments on disk")
    seg = segs[-2] if len(segs) >= 2 else segs[-1]  # prefer a finalized (not in-progress) segment
    fd, tmp = tempfile.mkstemp(suffix=".mp4")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(init.read_bytes()); f.write(seg.read_bytes())
        return await _ffmpeg_jpeg(["-i", tmp], width, timeout, rot=rot)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def snapshot(url: str, transport: str = "tcp", width: int = 640, timeout: float = 15, rot: int = 0) -> bytes:
    """Fallback: grab a frame directly from RTSP (skips HEVC warmup frames)."""
    return await _ffmpeg_jpeg(
        ["-rtsp_transport", transport, *socket_timeout_args(), "-i", url], width, timeout, skip=20, rot=rot)


async def snapshot_camera(cam, cfg, width: int = 640) -> bytes:
    """Prefer the on-disk live segment (clean, no camera contention); fall back to RTSP."""
    rot = getattr(cam, "rotation", 0)
    try:
        return await snapshot_from_live(cfg.live_dir, cam.id, width, rot=rot)
    except Exception as e:
        log.debug("[%s] live snapshot failed (%s), trying RTSP", cam.id, e)
        return await snapshot(cam.live_url, cfg.rtsp_transport, width, rot=rot)


async def thumbnail(path, offset: float, width: int = 320, timeout: float = 15, rot: int = 0) -> bytes:
    """Extract a JPEG from a recording file at `offset` seconds (input-seek to the
    nearest keyframe — fast). Used for playback event thumbnails."""
    return await _ffmpeg_jpeg(["-ss", f"{max(0.0, offset):.3f}", "-i", str(path)], width, timeout, rot=rot)


def _video_tag(codec: str | None) -> list[str]:
    # hvc1 (parameter sets in the sample entry) is what Safari/Chrome expect for HEVC in MP4.
    return ["-tag:v", "hvc1"] if codec == "hevc" else []


def _audio_args(mode: str, audio_codec: str | None) -> list[str]:
    if not audio_codec or mode == "none":
        return []
    if mode == "copy" or (mode == "aac" and audio_codec == "aac"):
        return ["-map", "0:a:0", "-c:a", "copy"]
    return ["-map", "0:a:0", "-c:a", "aac", "-b:a", "64k", "-ar", "16000"]


def record_cmd(cam: Camera, cfg: Config, info: dict, out_dir: Path) -> list[str]:
    return [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning",
        *input_args(cam.record_url, cfg.rtsp_transport),
        "-map", "0:v:0", "-c:v", "copy", *_video_tag(info["video"]),
        *_audio_args(cam.audio, info.get("audio")),
        "-f", "segment", "-segment_time", str(cfg.segment_seconds),
        "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
        "-segment_format", "mp4",
        # Fragmented MP4: the in-progress segment is playable and a crash loses
        # at most one fragment instead of the whole file.
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        str(out_dir / FFMPEG_PATTERN),
    ]


HLS_SEG_SECONDS = 2  # segment length; also the forced-keyframe interval


def live_cmd(cam: Camera, cfg: Config, info: dict, out_dir: Path) -> list[str]:
    # Force a keyframe exactly at every HLS segment boundary so each segment starts
    # with an IDR. Without this, a viewer (or a refresh) that starts mid-GOP shows
    # black until the next keyframe, and can stall there.
    keyframes = ["-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEG_SECONDS})"]
    if cam.live_codec == "h264" and info["video"] != "h264":
        video = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                 "-sc_threshold", "0", "-b:v", "1500k", *keyframes]
    else:
        # copy: can't insert keyframes, but still align segments to existing IDRs
        video = ["-c:v", "copy", *_video_tag(info["video"])]
    return [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning",
        *input_args(cam.live_url, cfg.rtsp_transport),
        "-map", "0:v:0", *video, "-an",
        "-f", "hls", "-hls_time", str(HLS_SEG_SECONDS), "-hls_list_size", "8",
        # split_by_time: don't wait past hls_time for a keyframe on copy streams.
        "-hls_flags", "delete_segments+omit_endlist+independent_segments+split_by_time",
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
        str(out_dir / "index.m3u8"),
    ]


@dataclass
class ProcState:
    kind: str
    running: bool = False
    started_at: float | None = None
    restarts: int = 0
    last_error: str = ""
    stream: dict | None = None
    log_tail: deque = field(default_factory=lambda: deque(maxlen=20))

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "stream": self.stream,
        }


class Worker:
    """Keeps one ffmpeg process alive with exponential backoff."""

    def __init__(self, cam: Camera, cfg: Config, kind: str, out_dir: Path):
        self.cam, self.cfg, self.kind, self.out_dir = cam, cfg, kind, out_dir
        self.state = ProcState(kind)
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

    async def run(self) -> None:
        backoff = 2.0
        while not self._stopping:
            url = self.cam.record_url if self.kind == "record" else self.cam.live_url
            t0 = time.monotonic()
            try:
                info = await probe(url, self.cfg.rtsp_transport)
                self.state.stream = info
                self.out_dir.mkdir(parents=True, exist_ok=True)
                if self.kind == "live":
                    for f in self.out_dir.iterdir():  # stale playlist from a previous run
                        f.unlink(missing_ok=True)
                cmd = (record_cmd if self.kind == "record" else live_cmd)(self.cam, self.cfg, info, self.out_dir)
                log.info("[%s/%s] start: %s", self.cam.id, self.kind, redact(" ".join(cmd)))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE, env={**os.environ, "TZ": "UTC"},
                )
                self.state.running, self.state.started_at = True, time.time()
                assert self._proc.stderr
                async for line in self._proc.stderr:
                    msg = redact(line.decode(errors="replace").rstrip())
                    if msg:
                        self.state.log_tail.append(msg)
                        log.warning("[%s/%s] %s", self.cam.id, self.kind, msg)
                rc = await self._proc.wait()
                self.state.last_error = self.state.log_tail[-1] if self.state.log_tail else f"ffmpeg exited {rc}"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # probe failure, spawn failure
                self.state.last_error = redact(str(e))
                log.error("[%s/%s] %s", self.cam.id, self.kind, self.state.last_error)
            finally:
                self.state.running = False
                self._proc = None
            if self._stopping:
                break
            if time.monotonic() - t0 > 60:
                backoff = 2.0  # it ran for a while; this is a fresh failure
            self.state.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def stop(self) -> None:
        self._stopping = True
        p = self._proc
        if p and p.returncode is None:
            # 'q' on stdin isn't available (DEVNULL); SIGINT lets ffmpeg finalize the segment.
            p.send_signal(2)
            try:
                await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError:
                p.kill()


class Supervisor:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg, self.storage = cfg, storage
        self.workers: dict[tuple[str, str], Worker] = {}
        self.motion: dict[str, object] = {}
        self.events = None  # EventStore, set in start() if motion enabled
        self.detector = None  # ObjectDetector, set in start() if detection enabled
        self.watchdog = None  # set in _watchdog_loop
        self._motion_push_at: dict[str, float] = {}
        self._motion_pushing: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    def reload_notifier(self) -> None:
        """Rebuild the alert notifier from the (possibly updated) config."""
        from .alerts import make_notifier
        if self.watchdog is not None:
            self.watchdog.notifier = make_notifier(self.cfg.alerts)

    async def _motion_alert(self, cam_id: str, ts: float, score: float, eid: int | None = None) -> None:
        """On every motion event: run object detection to label it, and (if enabled)
        push a snapshot notification. One snapshot is captured and reused for both.

        Event cadence is bounded by motion.min_gap_seconds; motion_push_cooldown
        (default 0 = every event) can throttle pushes; an in-flight guard per camera
        prevents pile-ups.
        """
        a = self.cfg.alerts
        cam = self.cfg.camera(cam_id)
        if cam is None:
            return
        want_detect = (self.cfg.motion.detect and self.detector is not None
                       and self.detector.ok and eid is not None)
        push_ready = a.motion_push and a.notifier != "none"
        push_now = push_ready and not (a.motion_push_cooldown and ts - self._motion_push_at.get(cam_id, 0.0) < a.motion_push_cooldown)
        if not (want_detect or push_now):
            return
        if cam_id in self._motion_pushing:
            return
        self._motion_pushing.add(cam_id)
        try:
            img = await snapshot_camera(cam, self.cfg)
            labels: list[str] = []
            if want_detect:
                labels = await asyncio.to_thread(self.detector.detect, img)
                if self.events is not None:
                    await asyncio.to_thread(self.events.set_label, eid, ",".join(labels))
            if push_now:
                self._motion_push_at[cam_id] = ts
                from .alerts import make_notifier
                when = time.strftime("%H:%M:%S", time.localtime(ts))
                what = " + ".join(labels).title() if labels else "Motion"
                await asyncio.to_thread(make_notifier(a).send_image, f"{what} — {cam.name}",
                                        f"Detected at {when}", img, f"{cam_id}-{int(ts)}.jpg")
                log.info("[%s] motion push sent (%s)", cam_id, what)
        except Exception as e:
            log.warning("[%s] motion handling failed: %s", cam_id, redact(str(e)))
        finally:
            self._motion_pushing.discard(cam_id)

    async def start(self) -> None:
        if not shutil.which(FFMPEG):
            raise RuntimeError("ffmpeg not found")
        if self.cfg.motion.enabled:
            from .events import EventStore
            from .motion import MotionDetector
            self.events = EventStore(self.cfg.events_db)
            if self.cfg.motion.detect:
                from .detect import ObjectDetector
                self.detector = ObjectDetector(confidence=self.cfg.motion.detect_confidence)
        for cam in self.cfg.cameras:
            if not cam.enabled:
                continue
            for kind, out in (("record", self.storage.camera_dir(cam.id)), ("live", self.cfg.live_dir / cam.id)):
                w = Worker(cam, self.cfg, kind, out)
                self.workers[(cam.id, kind)] = w
                self._tasks.append(asyncio.create_task(w.run(), name=f"{cam.id}-{kind}"))
            if self.cfg.motion.enabled:
                md = MotionDetector(cam, self.cfg, self.events, on_event=self._motion_alert)
                self.motion[cam.id] = md
                self._tasks.append(asyncio.create_task(md.run(), name=f"{cam.id}-motion"))
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))
        self._tasks.append(asyncio.create_task(self._watchdog_loop(), name="watchdog"))

    async def _retention_loop(self) -> None:
        while True:
            try:
                r = self.cfg.retention  # re-read each cycle so settings changes apply live
                await asyncio.to_thread(self.storage.run_retention, r.max_gb, r.max_days,
                                        r.min_free_gb, r.reclaim_headroom_gb)
                if self.events is not None and r.max_days > 0:
                    await asyncio.to_thread(self.events.prune, time.time() - r.max_days * 86400)
            except Exception:
                log.exception("retention failed")
            await asyncio.sleep(60)

    async def _watchdog_loop(self) -> None:
        from .alerts import Watchdog, build_observer, make_notifier

        notifier = make_notifier(self.cfg.alerts)
        self.watchdog = Watchdog(build_observer(self.cfg, self, self.storage), notifier)
        # Grace period so cameras can connect before the first evaluation.
        await asyncio.sleep(max(20, self.cfg.alerts.check_interval))
        while True:
            try:
                await asyncio.to_thread(self.watchdog.tick)
            except Exception:
                log.exception("watchdog failed")
            await asyncio.sleep(self.cfg.alerts.check_interval)

    async def stop(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def status(self, cam_id: str) -> dict:
        return {k: w.state.as_dict() for (c, k), w in self.workers.items() if c == cam_id}
