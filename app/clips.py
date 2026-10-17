"""Saved clips: building them from recordings, and a seekable copy for the browser.

A saved clip is the original footage, stream-copied (the Download button gives
exactly that file). For playing it in the page the browser needs H.264 in an MP4
it can seek in: a file with its index up front (faststart) served with HTTP Range.
H.264 clips of unrotated cameras already are one. Others (H.265, or a rotated
camera) are converted once, in the background and at low priority, into
saved/.play/<name>; until that's done the page plays a live transcode, which
can't seek beyond what has arrived.

saved/.play/<name> is therefore either the converted copy or a hard link to the
original (no conversion needed): if it exists, it's what the page plays.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from .config import Config
from .recorder import FFMPEG, FFPROBE, keyframe_before, rotation_vf
from .storage import Storage

log = logging.getLogger(__name__)

PLAY_DIR = ".play"
PLAY_HEIGHT = 1080   # converted copies: at most this tall (the originals are 2K)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.mp4$")


class ClipError(Exception):
    """A clip could not be built; `status` is the HTTP status that fits."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def clip_start(storage: Storage, cam_id: str, start: float) -> float:
    """`start` moved back to the keyframe at or before it. A stream copy that starts
    between keyframes begins with frames nothing can decode (a frozen or grey
    picture until the next keyframe, seconds later)."""
    path, offset = await asyncio.to_thread(storage.segment_at, cam_id, start)
    if not path:
        return start
    try:
        kf, _codec = await keyframe_before(path, offset)
    except Exception:  # noqa: BLE001 - an unclean first second beats no clip
        return start
    return start - offset + kf


async def build_clip(storage: Storage, cam_id: str, start: float, end: float, out: Path,
                     max_seconds: float) -> None:
    """Stream-copy the [start, end) range (active + grace) into `out` (faststart MP4)."""
    if end <= start or end - start > max_seconds:
        raise ClipError(400, f"range must be 0-{max_seconds:.0f}s")
    start = await clip_start(storage, cam_id, start)
    script = storage.concat_list(cam_id, start, end)
    if script.count("\nfile ") == 0:
        raise ClipError(404, "no recordings in range")
    fd, lst = tempfile.mkstemp(suffix=".ffconcat")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-safe", "0", "-f", "concat",
            "-i", lst, "-map", "0", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(out),
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    finally:
        os.unlink(lst)
    if proc.returncode != 0 or not out.exists():
        raise ClipError(500, err.decode(errors="replace")[-500:])


def camera_of(name: str) -> str:
    return name.split("-", 1)[0]


def probe_codec(path: Path) -> str | None:
    """Video codec of a file (blocking)."""
    import subprocess
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=30).stdout
    streams = (json.loads(out or "{}") or {}).get("streams") or [{}]
    return streams[0].get("codec_name")


class ClipConverter:
    """One background worker that makes each saved clip's playable copy."""

    def __init__(self, cfg: Config, storage: Storage):
        self.cfg, self.storage = cfg, storage
        self._queue: asyncio.Queue[Path] | None = None
        self._pending: set[str] = set()
        self._task: asyncio.Task | None = None

    def play_dir(self) -> Path:
        d = self.storage.saved_dir() / PLAY_DIR
        d.mkdir(exist_ok=True)
        return d

    def play_path(self, clip: Path) -> Path:
        return self.storage.saved_dir() / PLAY_DIR / clip.name

    def ready(self, clip: Path) -> Path | None:
        p = self.play_path(clip)
        return p if p.is_file() else None

    def remove(self, clip: Path) -> None:
        self.play_path(clip).unlink(missing_ok=True)

    def start(self) -> None:
        """Start the worker and queue every saved clip that has no playable copy yet."""
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._run(), name="clip-convert")
        for e in sorted(os.scandir(self.storage.saved_dir()), key=lambda e: e.name):
            if e.is_file() and NAME_RE.match(e.name):
                self.enqueue(Path(e.path))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def enqueue(self, clip: Path) -> None:
        if self._queue is None or clip.name in self._pending or self.ready(clip):
            return
        self._pending.add(clip.name)
        self._queue.put_nowait(clip)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            clip = await self._queue.get()
            try:
                if clip.is_file() and not self.ready(clip):
                    await self.convert(clip)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the live transcode still works
                log.warning("saved clip %s: no seekable copy (%s)", clip.name, e)
            finally:
                self._pending.discard(clip.name)

    async def convert(self, clip: Path) -> Path:
        cam = self.cfg.camera(camera_of(clip.name))
        rot = cam.rotation if cam else 0
        out = self.play_dir() / clip.name
        codec = await asyncio.to_thread(probe_codec, clip)
        if codec == "h264" and not rotation_vf(rot):
            os.link(clip, out)  # already playable and seekable as it is
            return out
        rv = rotation_vf(rot)
        vf = (rv + "," if rv else "") + f"scale=-2:'min({PLAY_HEIGHT},ih)'"
        part = out.with_suffix(".part")
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15", FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(clip),
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "3", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-f", "mp4", str(part),
            stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            part.unlink(missing_ok=True)
            raise
        if proc.returncode != 0:
            part.unlink(missing_ok=True)
            raise RuntimeError(err.decode(errors="replace").strip()[-300:] or f"ffmpeg exit {proc.returncode}")
        part.replace(out)
        log.info("saved clip %s: seekable copy ready", clip.name)
        return out
