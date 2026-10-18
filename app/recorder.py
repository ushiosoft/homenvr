"""ffmpeg process supervision: one recorder and one live-view process per camera."""
from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import re
import subprocess
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

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


# How long a live/motion worker stays on the NVR fallback before retrying go2rtc.
FALLBACK_RETRY_SECONDS = 300
# go2rtc starts a stream's producer (and, for H.265, its transcoder) on demand, so
# right after a (re)start its restream can refuse the first probes for a few
# seconds. Retry it this many times, this far apart, before using the fallback.
PREFERRED_ATTEMPTS = 4
PREFERRED_RETRY_PAUSE = 5.0


def sub_sources(cam: Camera, cfg: Config) -> list[str]:
    """Where to read a camera's sub-stream, in order of preference.

    With the go2rtc sidecar configured, its internal RTSP restream comes first: it
    already holds one connection per camera (for WebRTC), so live HLS and motion
    detection share it instead of each opening their own on the NVR (12 -> 6 NVR
    streams for 3 cameras), and its output is H.264 (passed through or converted
    once). The NVR's own URL stays as the fallback.
    """
    out = []
    if cfg.webrtc_api:
        host = urlparse(cfg.webrtc_api).hostname
        out.append(f"rtsp://{host}:8554/{cam.id}")
    out.append(cam.live_url)
    return out


async def pick_source(urls: list[str], transport: str) -> tuple[str, dict, bool]:
    """Probe the URLs in order; return (url, stream info, is_fallback) for the first
    that answers. Raises the last error if none do."""
    err: Exception | None = None
    for i, url in enumerate(urls):
        attempts = PREFERRED_ATTEMPTS if i == 0 and len(urls) > 1 else 1
        for n in range(attempts):
            try:
                return url, await probe(url, transport, timeout=15), i > 0
            except Exception as e:  # noqa: BLE001 - retry, then try the next source
                err = e
                if n + 1 < attempts:
                    await asyncio.sleep(PREFERRED_RETRY_PAUSE)
    raise err or RuntimeError("no source")


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


def _live_segments(live_dir, cam_id: str) -> tuple[Path, list[Path]]:
    """(init.mp4, finished live segments oldest->newest). Only segments listed in the
    playlist are finished; the one being written isn't listed yet."""
    d = Path(live_dir) / cam_id
    init = d / "init.mp4"
    try:
        names = [ln.strip() for ln in (d / "index.m3u8").read_text().splitlines()
                 if ln.strip().endswith(".m4s")]
    except OSError:
        names = []
    segs = [d / n for n in names if (d / n).is_file()]
    if not init.exists() or not segs:
        raise RuntimeError("no live segments on disk")
    return init, segs


def _concat_tmp(init: Path, segs: list[Path]) -> str:
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".mp4")
    with os.fdopen(fd, "wb") as f:
        f.write(init.read_bytes())
        for seg in segs:
            f.write(seg.read_bytes())
    return tmp


async def snapshot_from_live(live_dir, cam_id: str, width: int = 640, timeout: float = 15, rot: int = 0) -> bytes:
    """Grab the newest frame from the live HLS segments on disk — clean H.264, no extra
    camera connection (a fresh RTSP grab contends with the live worker on the
    wireless sub-stream and returns grey frames)."""
    init, segs = _live_segments(live_dir, cam_id)
    tmp = _concat_tmp(init, segs[-1 - LIVE_LEAD_SEGMENTS:])   # from a keyframe (see live_frames)
    try:
        # The END of the newest finished segment: its first frame is ~2 s older.
        return await _ffmpeg_jpeg(["-sseof", "-0.4", "-i", tmp], width, timeout, rot=rot)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def live_frames(live_dir, cam_id: str, since: float, max_segments: int = 3, fps: float = 2,
                      width: int = 640, timeout: float = 20, rot: int = 0, with_start: bool = False):
    """JPEG frames (fps per second) from the live segments finished at or after the
    wall-clock time `since` (at most max_segments of them), oldest first. With
    with_start, returns (frames, wall-clock time of the first frame): when the segment
    before the first one finished, i.e. when the first one began."""
    init, segs = _live_segments(live_dir, cam_id)
    recent = [p for p in segs if p.stat().st_mtime >= since][:max_segments] or segs[-1:]
    i = segs.index(recent[0])
    start = segs[i - 1].stat().st_mtime if i > 0 else recent[0].stat().st_mtime - HLS_SEG_SECONDS
    # Pass-through (H.264) live segments needn't start on a keyframe: decode from a few
    # segments earlier and drop that lead-in, or the burst's first seconds are garbage.
    lead = segs[max(0, i - LIVE_LEAD_SEGMENTS):i]
    j = segs.index(lead[0]) if lead else i
    lead_start = segs[j - 1].stat().st_mtime if j > 0 else (lead[0].stat().st_mtime - HLS_SEG_SECONDS if lead else start)
    skip = max(0.0, start - lead_start)
    tmp = _concat_tmp(init, lead + recent)
    rv = rotation_vf(rot)
    vf = f"fps={fps},{rv + ',' if rv else ''}scale={width}:-2"
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", tmp, "-ss", f"{skip:.2f}", "-vf", vf,
            "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
            raise RuntimeError("frame burst timed out")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    frames = split_jpegs(out)
    if not frames:
        raise RuntimeError(redact(err.decode(errors="replace").strip()) or "no frames")
    return (frames, start) if with_start else frames


def split_jpegs(data: bytes) -> list[bytes]:
    """Split concatenated JPEGs (ffmpeg image2pipe/mjpeg). Inside JPEG entropy data
    0xFF is always byte-stuffed, so FFD9 only occurs as the end-of-image marker."""
    out, i = [], 0
    while True:
        a = data.find(b"\xff\xd8", i)
        if a < 0:
            break
        b = data.find(b"\xff\xd9", a + 2)
        if b < 0:
            break
        out.append(data[a:b + 2])
        i = b + 2
    return out


def pick_frame(frames: list[bytes], scores: list[dict[str, float]],
               event_labels: list[str] | None = None) -> tuple[int, list[str]]:
    """Choose the frame to push/keep for an event: the most confident person, else the
    most confident vehicle, else animal, else the frame nearest the trigger (~60% of
    the way in: the burst spans ~6 s before to ~4 s after it). With event_labels (the
    event's labels from ObjectDetector.analyze) only those categories are considered:
    a one-frame blip of another category must not pick a frame that doesn't show what
    the event is labelled with. Returns (index, labels seen)."""
    from .detect import CATS
    labels = [c for c in CATS if any(sc.get(c, 0) > 0 for sc in scores)]
    for cat in (c for c in CATS if event_labels is None or c in event_labels):
        best = max(range(len(frames)), key=lambda i: scores[i].get(cat, 0) if i < len(scores) else 0)
        if best < len(scores) and scores[best].get(cat, 0) > 0:
            return best, labels
    return trigger_index(len(frames)), labels


def push_categories(policy: str) -> set[str]:
    """alerts.push_on as the set of labels that push: "motion" (every event) or a
    comma list of person / vehicle / animal ("person_vehicle" is the older spelling
    of "person,vehicle")."""
    return set(policy.replace("person_vehicle", "person,vehicle").split(",")) - {""}


def should_push(policy: str, labels: list[str]) -> bool:
    """Whether an event with these labels (moving, per analyze_burst) is pushed."""
    if policy == "motion":
        return True
    return bool(push_categories(policy or "person") & set(labels))


def trigger_index(n: int) -> int:
    return min(n - 1, int(n * 0.6))


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


_codec_cache: dict[str, str | None] = {}


async def probe_file_codec(path: str) -> str | None:
    """Video codec of a recording file (cached per camera directory: a camera's
    recordings share one codec)."""
    key = str(Path(path).parent)
    if key not in _codec_cache:
        proc = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
            "-of", "csv=p=0", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
        _codec_cache[key] = out.decode().strip() or None
    return _codec_cache[key]


async def keyframe_before(path, offset: float) -> tuple[float, str | None]:
    """(time of the last video keyframe at or before `offset` in the file, codec).
    Reads only a few seconds of packets around the offset."""
    lo = max(0.0, offset - 10)
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{lo:.3f}%{offset + 0.5:.3f}",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 15)
    best = 0.0
    for line in out.decode().splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                t = float(parts[0])
            except ValueError:
                continue
            if t <= offset + 0.05:
                best = max(best, t)
    return best, await probe_file_codec(str(path))


# The event's window on the recording, around the trigger time. Longer than the live
# burst (no push waits for it): people and animals often stay in view well after the
# trigger, and the recording's timeline can run a few seconds from the live clock.
BURST_BEFORE, BURST_AFTER = 6.0, 10.0
REFINE_AFTER = 20.0   # seconds after the trigger: by then the recording holds the window


def window_inputs(parts: list, audio: bool = False) -> tuple[list[str], str | None]:
    """ffmpeg inputs that decode exactly the recording window `parts`
    (Storage.window_parts), each file opened with an input seek, plus the filtergraph
    joining several (None for one file; its output is then [v] / [a]).

    Input seeking, not the concat demuxer's inpoint: in the first file after a
    recorder restart (which starts with a stale keyframe and a gap) an inpoint lands
    ~4 s early, while input seeking matches what review playback shows."""
    args: list[str] = []
    for path, off, length in parts:
        args += ["-ss", f"{off:.3f}", "-t", f"{length:.3f}", "-i", str(path)]
    if len(parts) == 1:
        return args, None
    pads = "".join(f"[{i}:v:0]" + (f"[{i}:a:0]" if audio else "") for i in range(len(parts)))
    return args, f"{pads}concat=n={len(parts)}:v=1:a={1 if audio else 0}[v]" + ("[a]" if audio else "")


async def recording_frames(storage: Storage, cam: Camera, start: float, end: float, fps: float = 2,
                           width: int = 640, timeout: float = 60) -> list[bytes]:
    """JPEG frames (fps per second) of the recording from exactly `start` to `end`, rotated
    like the camera: the same frames, at the same times, as review playback shows."""
    parts = await asyncio.to_thread(storage.window_parts, cam.id, start, end)
    if not parts:
        return []
    inputs, joined = window_inputs(parts)
    rv = rotation_vf(cam.rotation)
    vf = f"fps={fps},{rv + ',' if rv else ''}scale={width}:-2"
    graph = ["-filter_complex", f"{joined};[v]{vf}[o]", "-map", "[o]"] if joined else ["-vf", vf]
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *inputs, *graph, "-q:v", "4",
        "-f", "image2pipe", "-c:v", "mjpeg", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        return []
    return split_jpegs(out)


STAMP_CHECK_AFTER = 25.0   # seconds into a recorder run before its first file's name is checked


async def probe_duration(path) -> float:
    """Media duration of a (possibly still growing, fragmented) MP4 file."""
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    return float(out.decode().strip())


def save_thumb(path: Path, img: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(img)
    os.replace(tmp, path)


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
        # Split by stream time, NOT -segment_atclocktime: that mode re-reads the wall
        # clock per packet and cuts whenever it appears to go backwards. On WSL2 /
        # Docker Desktop the clock is stepped back ~60 ms every ~30 s by time sync,
        # which produced irregular ~30 s files instead of segment_seconds.
        "-f", "segment", "-segment_time", str(cfg.segment_seconds),
        "-reset_timestamps", "1", "-strftime", "1",
        "-segment_format", "mp4",
        # Fragmented MP4: the in-progress segment is playable and a crash loses
        # at most one fragment instead of the whole file.
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        str(out_dir / FFMPEG_PATTERN),
    ]


# Live HLS segment length, also the forced-keyframe interval. 1 s keeps the live
# picture ~2-3 s behind (the player sits 2 segments back from the newest).
HLS_SEG_SECONDS = 1
HLS_LIST_SIZE = 20   # playlist window (s at 1 s/segment): the ~10 s burst below + its keyframe lead-in
CAPTURE_AFTER = 4.5  # seconds after a motion trigger to wait before picking its frame
LIVE_LEAD_SEGMENTS = 4  # extra live segments decoded first so decoding starts at a keyframe


def live_cmd(cam: Camera, cfg: Config, info: dict, out_dir: Path, url: str | None = None) -> list[str]:
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
        *input_args(url or cam.live_url, cfg.rtsp_transport),
        # Audio (G.711 on these cameras) is transcoded to AAC so the browser can
        # play it; the UI keeps every tile muted until the user turns sound on.
        "-map", "0:v:0", *video, *(_audio_args(cam.audio, info.get("audio")) or ["-an"]),
        "-f", "hls", "-hls_time", str(HLS_SEG_SECONDS), "-hls_list_size", str(HLS_LIST_SIZE),
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

    def __init__(self, cam: Camera, cfg: Config, kind: str, out_dir: Path, ready: asyncio.Event | None = None):
        self.cam, self.cfg, self.kind, self.out_dir = cam, cfg, kind, out_dir
        self.ready = ready  # live: wait for this (go2rtc registered) before the first start
        self.state = ProcState(kind)
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

    async def run(self) -> None:
        backoff = 2.0
        while not self._stopping:
            t0 = time.monotonic()
            fallback_timer = stamp_fix = None
            try:
                if self.kind == "record":
                    url = self.cam.record_url  # recording keeps its own direct NVR connection
                    info = await probe(url, self.cfg.rtsp_transport)
                else:
                    if self.ready is not None:
                        await self.ready.wait()
                    url, info, fallback = await pick_source(sub_sources(self.cam, self.cfg), self.cfg.rtsp_transport)
                    if fallback:
                        log.warning("[%s/live] go2rtc unavailable; reading the NVR directly for now", self.cam.id)
                        fallback_timer = asyncio.get_running_loop().call_later(
                            FALLBACK_RETRY_SECONDS, lambda: self._proc and self._proc.returncode is None and self._proc.kill())
                self.state.stream = info
                self.out_dir.mkdir(parents=True, exist_ok=True)
                if self.kind == "live":
                    for f in self.out_dir.iterdir():  # stale playlist from a previous run
                        f.unlink(missing_ok=True)
                cmd = (record_cmd(self.cam, self.cfg, info, self.out_dir) if self.kind == "record"
                       else live_cmd(self.cam, self.cfg, info, self.out_dir, url))
                log.info("[%s/%s] start: %s", self.cam.id, self.kind, redact(" ".join(cmd)))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE, env={**os.environ, "TZ": "UTC"},
                )
                self.state.running, self.state.started_at = True, time.time()
                if self.kind == "record":
                    stamp_fix = asyncio.ensure_future(self._fix_first_stamp(self.state.started_at))
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
                if stamp_fix:
                    stamp_fix.cancel()
                if fallback_timer:
                    fallback_timer.cancel()
            if self._stopping:
                break
            if time.monotonic() - t0 > 60:
                backoff = 2.0  # it ran for a while; this is a fresh failure
            self.state.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _fix_first_stamp(self, since: float) -> None:
        """Name the first file of a recorder (re)start after when its footage begins.

        Files are named by the wall clock when ffmpeg opens them, but on connect the NVR
        first sends a few seconds of buffered video, so the first file's footage starts
        that much earlier than its name (seen: 0-6 s, per camera and restart), and the
        review timeline, boxes and incidents are off by it for those 10 minutes. After
        a while, compare how much footage the file holds with how long it has been
        written; if it holds more, rename it to start that much earlier. (Later files
        are split by stream time while live, so their names are right.)
        """
        from .storage import format_stamp, parse_stamp
        await asyncio.sleep(STAMP_CHECK_AFTER)
        files = [p for p in self.out_dir.glob("*.mp4") if (parse_stamp(p.name) or 0) >= since - 5]
        if not files:
            return
        p = min(files, key=lambda p: parse_stamp(p.name))
        stamp = parse_stamp(p.name)
        try:
            held = await probe_duration(p)
            lead = held - (p.stat().st_mtime - stamp)
        except Exception as e:  # noqa: BLE001
            log.debug("[%s] stamp check failed: %s", self.cam.id, e)
            return
        if 1.0 < lead < 60:
            new = p.with_name(format_stamp(stamp - lead) + ".mp4")
            if not new.exists():
                p.rename(new)   # ffmpeg keeps writing through its open file
                log.info("[%s] %s starts %.1f s before its name (NVR pre-buffer); renamed to %s",
                         self.cam.id, p.name, lead, new.name)

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
        self._thumb_floor: dict[str, float] = {}  # per camera: thumbs before this are swept
        self._motion_pending: dict[str, tuple] = {}  # per camera: newest event waiting its turn
        # Per camera: places that flicker in place every burst (the clock overlay), as
        # [0-1 box, bursts seen] — boxes there aren't shown (ObjectDetector.analyze).
        self._fixtures: dict[str, list] = {}
        self._refine_sem = asyncio.Semaphore(1)   # one recording re-analysis at a time
        self._pushed_eids: deque = deque(maxlen=500)   # events already pushed (no late re-push)
        self._refining: set[asyncio.Task] = set()
        self.converter = None  # clips.ClipConverter, set by the app: auto-saved clips get a seekable copy
        self.autosaver = None  # clips.AutoSaver, set in start()
        self._tasks: list[asyncio.Task] = []
        # Set once go2rtc's streams are registered (or there's no go2rtc): the live and
        # motion workers wait for it, recording doesn't. Set by the app.
        self.sub_ready = asyncio.Event()

    def reload_notifier(self) -> None:
        """Rebuild the alert notifier from the (possibly updated) config."""
        from .alerts import make_notifier
        if self.watchdog is not None:
            self.watchdog.notifier = make_notifier(self.cfg.alerts)

    async def _motion_alert(self, cam_id: str, ts: float, score: float, eid: int | None = None) -> None:
        """On every motion event: run object detection to label it, and (if enabled)
        push a snapshot notification. One snapshot is captured and reused for both.

        Which events are pushed follows alerts.push_on (per-camera override
        camera.push_on): "person", "person_vehicle" or "motion". Every event is still
        recorded and labelled either way; only the notification is filtered.

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
        want_thumb = eid is not None
        push_ready = a.motion_push and a.notifier != "none"
        push_now = push_ready and not (a.motion_push_cooldown and ts - self._motion_push_at.get(cam_id, 0.0) < a.motion_push_cooldown)
        # Without a working detector there are no labels to filter on: push every event.
        policy = (cam.push_on or a.push_on) if want_detect else "motion"
        if not (want_detect or want_thumb or push_now):
            return
        if cam_id in self._motion_pushing:
            # Busy with this camera's previous event (~6 s incl. waiting for frames).
            # Keep the newest event and run it next instead of dropping it: on a camera
            # that fires on shadows every 10-20 s, a person arriving mid-analysis would
            # otherwise never be looked at.
            self._motion_pending[cam_id] = (ts, score, eid)
            return
        self._motion_pushing.add(cam_id)
        try:
            img, labels, boxes = await self._capture_event(cam, ts, want_detect)
            if boxes and eid is not None and self.events is not None:
                await asyncio.to_thread(self.events.set_boxes, eid, boxes)
            if want_detect and eid is not None and self.events is not None:
                task = asyncio.ensure_future(self._refine_from_recording(cam, eid, ts))
                self._refining.add(task)
                task.add_done_callback(self._refining.discard)
            if boxes:
                from .detect import draw_boxes
                shown = boxes["f"][boxes["pick"]]
                img = await asyncio.to_thread(draw_boxes, img, shown)
            if want_thumb:
                # Persist the frame as the event's thumbnail: the incident list and
                # filmstrips then load instantly instead of running ffmpeg per image.
                await asyncio.to_thread(save_thumb, self.storage.thumb_path(cam_id, eid), img)
            if want_detect:
                if self.events is not None:
                    await asyncio.to_thread(self.events.set_label, eid, ",".join(labels))
            if push_ready and should_push(policy, labels) and self.autosaver is not None:
                self.autosaver.pushed(cam_id, ts)  # keep a clip of it (also inside the push cooldown)
            if push_now and not should_push(policy, labels):
                log.debug("[%s] not pushed (push_on=%s, labels=%s)", cam_id, policy, labels or "none")
            elif push_now:
                await self._send_push(cam, ts, labels, img, eid)
        except Exception as e:
            log.warning("[%s] motion handling failed: %s", cam_id, redact(str(e)))
        finally:
            self._motion_pushing.discard(cam_id)
            nxt = self._motion_pending.pop(cam_id, None)
            if nxt is not None:
                asyncio.ensure_future(self._motion_alert(cam_id, *nxt))

    async def _capture_event(self, cam, ts: float, detect: bool) -> tuple[bytes, list[str], dict | None]:
        """The frame that best shows what triggered motion at `ts`, its labels, and the
        burst's boxes ({"t0": first frame's time, "dt": seconds per frame, "pick": index
        of the returned frame, "f": per-frame [[kind, x1, y1, x2, y2], ...]}).

        Measured on real pushes, the old single frame (start of the previous live
        segment) showed the person in 4 of 18 cases: the trigger lags the action
        (people were often visible ~2 s *before* it and gone right after), and that
        frame was itself ~4 s old. Instead wait a few seconds past the trigger,
        decode a ~10 s burst at 2 fps (≈6 s before to 4 s after) from the live
        segments, and keep the frame with the most confident person (else vehicle,
        else animal). Only moving objects seen in 2+ frames count
        (ObjectDetector.analyze), so parked cars and furniture don't become labels.
        Pushes arrive ~5 s later than before, but show the subject.
        """
        await asyncio.sleep(max(0.0, ts + CAPTURE_AFTER - time.time()))
        rot = getattr(cam, "rotation", 0)
        try:
            frames, t0 = await live_frames(self.cfg.live_dir, cam.id, since=ts - 4.5,
                                           max_segments=math.ceil(10 / HLS_SEG_SECONDS), rot=rot,
                                           with_start=True)
        except Exception as e:
            log.debug("[%s] frame burst failed (%s); single snapshot", cam.id, e)
            img = await snapshot_camera(cam, self.cfg)
            return img, (await asyncio.to_thread(self.detector.detect, img, cam.zones) if detect else []), None
        if not detect:
            return frames[trigger_index(len(frames))], [], None
        known = [f[0] for f in self._fixtures.get(cam.id, []) if f[1] >= 2]
        scores, labels, boxes, found = await asyncio.to_thread(self.detector.analyze, frames, cam.zones, known)
        self._remember_fixtures(cam.id, found)
        i, _ = pick_frame(frames, scores, labels)
        return frames[i], labels, {"t0": round(t0, 2), "dt": 0.5, "pick": i, "f": boxes}

    async def _refine_from_recording(self, cam, eid: int, ts: float) -> None:
        """Re-analyse the event on the recording once it's there, and keep that result.

        The push goes out from the live burst (fast), but live frames are a poorer
        source: their times are estimated from segment file times (seconds off from
        the recording the review player shows), and pass-through segments can decode
        badly. The recording gives clean frames on exactly the review timeline, so its
        boxes line up with playback and its labels and thumbnail replace the live ones.
        """
        await asyncio.sleep(max(0.0, ts + REFINE_AFTER - time.time()))
        try:
            async with self._refine_sem:
                a, b = ts - BURST_BEFORE, ts + BURST_AFTER
                frames = await recording_frames(self.storage, cam, a, b)
                if len(frames) < 6:
                    return
                known = [f[0] for f in self._fixtures.get(cam.id, []) if f[1] >= 2]
                scores, labels, boxes, found = await asyncio.to_thread(self.detector.analyze, frames, cam.zones, known)
                self._remember_fixtures(cam.id, found)
                i, _ = pick_frame(frames, scores, labels)
                await asyncio.to_thread(self.events.set_boxes, eid,
                                        {"t0": round(a, 2), "dt": 0.5, "pick": i, "f": boxes, "src": "rec"})
                await asyncio.to_thread(self.events.set_label, eid, ",".join(labels))
                from .detect import draw_boxes
                img = await asyncio.to_thread(draw_boxes, frames[i], boxes[i])
                await asyncio.to_thread(save_thumb, self.storage.thumb_path(cam.id, eid), img)
            await self._late_push(cam, eid, ts, labels, img)
        except Exception as e:  # noqa: BLE001 - the live result stays
            log.debug("[%s] recording re-analysis of event %s failed: %s", cam.id, eid, e)

    async def _send_push(self, cam, ts: float, labels: list[str], img: bytes, eid: int | None,
                         note: str = "") -> None:
        a = self.cfg.alerts
        self._motion_push_at[cam.id] = ts
        if eid is not None:
            self._pushed_eids.append(eid)
        from .alerts import make_notifier, review_link
        when = time.strftime("%H:%M:%S", time.localtime(ts))
        what = " + ".join(labels).title() if labels else "Motion"
        await asyncio.to_thread(make_notifier(a).send_image, f"{what} — {cam.name}",
                                f"Detected at {when}{note}", img, f"{cam.id}-{int(ts)}.jpg",
                                review_link(a.public_url, cam.id, ts))
        log.info("[%s] motion push sent (%s%s)", cam.id, what, note)

    async def _late_push(self, cam, eid: int, ts: float, labels: list[str], img: bytes) -> None:
        """The recording pass found what the push rule asks for (a person, say) and the
        live burst had missed it — a driver behind a windscreen at night, seen in the 2K
        recording but not in the live sub-stream. Push now, unless this event was pushed
        already or the camera's push cooldown says no."""
        a = self.cfg.alerts
        if not (a.motion_push and a.notifier != "none") or eid in self._pushed_eids:
            return
        if not should_push(cam.push_on or a.push_on, labels):
            return
        if a.motion_push_cooldown and ts - self._motion_push_at.get(cam.id, 0.0) < a.motion_push_cooldown:
            return
        await self._send_push(cam, ts, labels, img, eid, " (confirmed on the recording)")
        if self.autosaver is not None:
            self.autosaver.pushed(cam.id, ts)

    def _remember_fixtures(self, cam_id: str, found: list) -> None:
        from .detect import iou
        mem = self._fixtures.setdefault(cam_id, [])
        for box in found:
            hit = next((f for f in mem if iou(f[0], box) >= 0.3), None)
            if hit:
                hit[1] += 1
            else:
                mem.append([box, 1])
        mem.sort(key=lambda f: -f[1])
        del mem[12:]

    async def start(self) -> None:
        if not shutil.which(FFMPEG):
            raise RuntimeError("ffmpeg not found")
        if self.cfg.motion.enabled:
            from .events import EventStore
            from .motion import MotionDetector
            self.events = EventStore(self.cfg.events_db)
        from .clips import AutoSaver
        self.autosaver = AutoSaver(self.cfg, self.storage, self.events, self.converter)
        if self.cfg.motion.enabled:
            if self.cfg.motion.detect:
                from .detect import ObjectDetector
                self.detector = ObjectDetector(confidence=self.cfg.motion.detect_confidence)
        for cam in self.cfg.cameras:
            if not cam.enabled:
                continue
            for kind, out in (("record", self.storage.camera_dir(cam.id)), ("live", self.cfg.live_dir / cam.id)):
                w = Worker(cam, self.cfg, kind, out, ready=self.sub_ready if kind == "live" else None)
                self.workers[(cam.id, kind)] = w
                self._tasks.append(asyncio.create_task(w.run(), name=f"{cam.id}-{kind}"))
            if self.cfg.motion.enabled:
                md = MotionDetector(cam, self.cfg, self.events, on_event=self._motion_alert, ready=self.sub_ready)
                self.motion[cam.id] = md
                self._tasks.append(asyncio.create_task(md.run(), name=f"{cam.id}-motion"))
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))
        self._tasks.append(asyncio.create_task(self._watchdog_loop(), name="watchdog"))

    async def _retention_loop(self) -> None:
        last_sweep = 0.0
        while True:
            try:
                r = self.cfg.retention  # re-read each cycle so settings changes apply live
                await asyncio.to_thread(self.storage.run_retention, r.max_gb, r.max_days,
                                        r.min_free_gb, r.reclaim_headroom_gb)
                if self.events is not None and r.max_days > 0:
                    gone = await asyncio.to_thread(self.events.prune, time.time() - r.max_days * 86400)
                    for cam_id, eid in gone:
                        self.storage.delete_thumbs(cam_id, [eid])
                au = self.cfg.autosave
                from .clips import prune_auto
                for name in await asyncio.to_thread(prune_auto, self.storage.saved_dir(), au.max_days,
                                                    au.max_gb, time.time()):
                    log.info("auto-saved clip %s removed (autosave limits)", name)
                if self.events is not None and time.monotonic() - last_sweep > 600:
                    last_sweep = time.monotonic()
                    await asyncio.to_thread(self._sweep_thumbs)
            except Exception:
                log.exception("retention failed")
            await asyncio.sleep(60)

    def _sweep_thumbs(self) -> None:
        """Drop thumbnails of events whose footage is gone (deleted from grace).
        Only the events between the previous and the current oldest recording are
        visited, so each sweep is cheap."""
        for cam in self.cfg.cameras:
            first = self.storage.earliest(cam.id)
            if first is None:
                continue
            lo = self._thumb_floor.get(cam.id, 0.0)
            if first > lo:
                self.storage.delete_thumbs(cam.id, self.events.ids_between(cam.id, lo, first))
                self._thumb_floor[cam.id] = first

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
        if self.autosaver is not None:
            await self.autosaver.stop()
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def status(self, cam_id: str) -> dict:
        return {k: w.state.as_dict() for (c, k), w in self.workers.items() if c == cam_id}
