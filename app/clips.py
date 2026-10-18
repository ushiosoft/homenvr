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
    """Stream-copy the [start, end) range (active + grace) into `out` (faststart MP4).

    The first file is cut with an input seek just past its keyframe (exact; a concat
    inpoint lands seconds early in a restart's first file); later files start at their
    beginning, so they're joined without inpoints."""
    if end <= start or end - start > max_seconds:
        raise ClipError(400, f"range must be 0-{max_seconds:.0f}s")
    start = await clip_start(storage, cam_id, start)
    parts = await asyncio.to_thread(storage.window_parts, cam_id, start, end)
    if not parts:
        raise ClipError(404, "no recordings in range")
    tmp = Path(tempfile.mkdtemp(prefix="clip-"))
    try:
        path, off, length = parts[0]
        first = out if len(parts) == 1 else tmp / "first.mp4"
        await _ffmpeg(["-ss", f"{off + 0.05:.3f}", "-i", str(path), "-t", f"{length:.3f}", "-map", "0",
                       "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", "-f", "mp4", str(first)])
        if len(parts) > 1:
            lines = ["ffconcat version 1.0", f"file '{first.as_posix()}'"]
            for i, (p, _off, n) in enumerate(parts[1:], 1):
                lines.append(f"file '{p.as_posix()}'")
                if i == len(parts) - 1:
                    lines.append(f"outpoint {n:.3f}")
            (tmp / "list.ffconcat").write_text("\n".join(lines) + "\n")
            await _ffmpeg(["-safe", "0", "-f", "concat", "-i", str(tmp / "list.ffconcat"), "-map", "0", "-c", "copy",
                           "-movflags", "+faststart", "-f", "mp4", str(out)])
    finally:
        for f in tmp.iterdir():
            f.unlink(missing_ok=True)
        tmp.rmdir()


async def _ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
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


# ---- auto-saved clips of pushed events ------------------------------------------
#
# Every pushed event also keeps a clip, so what the phone was told about can be
# watched later even after the recordings rotate away. Clips are named
# "<cam>-<stamp>-auto.mp4" and have their own limits (autosave.max_days / max_gb);
# "Keep" on the Saved page renames one to a normal (permanent) saved clip.

AUTO_SUFFIX = "-auto.mp4"
AUTO_MAX_SECONDS = 300   # one incident's clip is at most this long; later pushes start a new one


def is_auto(name: str) -> bool:
    return name.endswith(AUTO_SUFFIX)


def auto_name(cam_id: str, start: float) -> str:
    import time
    return f"{cam_id}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(start))}{AUTO_SUFFIX}"


def kept_name(name: str) -> str:
    """The permanent name for an auto clip ("...-auto.mp4" -> "...-kept.mp4")."""
    return name[: -len(AUTO_SUFFIX)] + "-kept.mp4"


def delete_clip(saved_dir: Path, name: str) -> None:
    """A saved clip with its thumbnail and seekable copy."""
    p = saved_dir / name
    p.unlink(missing_ok=True)
    p.with_suffix(".jpg").unlink(missing_ok=True)
    (saved_dir / PLAY_DIR / name).unlink(missing_ok=True)


def prune_auto(saved_dir: Path, max_days: float, max_gb: float, now: float) -> list[str]:
    """Delete auto clips older than max_days, then the oldest until they fit max_gb
    (counting their thumbnails and converted copies). 0 = no limit. Returns names."""
    clips = []
    for e in os.scandir(saved_dir):
        if e.is_file() and is_auto(e.name):
            p = Path(e.path)
            size = e.stat().st_size
            for extra in (p.with_suffix(".jpg"), saved_dir / PLAY_DIR / e.name):
                try:
                    st = extra.stat()
                    size += st.st_size if st.st_nlink == 1 else 0
                except OSError:
                    pass
            clips.append((e.stat().st_mtime, e.name, size))
    clips.sort()
    total = sum(c[2] for c in clips)
    gone = []
    for mtime, name, size in clips:
        too_old = max_days > 0 and now - mtime > max_days * 86400
        too_big = max_gb > 0 and total > max_gb * 1e9
        if not (too_old or too_big):
            break
        delete_clip(saved_dir, name)
        total -= size
        gone.append(name)
    return gone


class AutoSaver:
    """Collects pushed events into incidents and saves a clip per camera for each.

    A push at ts opens (or extends) the current incident [ts - before, ts + after].
    Once its end has passed and the recorder has written it (+ a margin), one clip
    is cut per camera that saw it: the pushing camera(s), plus any camera with a
    person/vehicle-labelled event in the window. Pushes landing inside the open
    incident extend it (up to AUTO_MAX_SECONDS) instead of making overlapping clips.
    """

    SETTLE = 15  # seconds after the incident's end before cutting (recorder flush)

    def __init__(self, cfg: Config, storage: Storage, events=None, converter: ClipConverter | None = None):
        self.cfg, self.storage, self.events, self.converter = cfg, storage, events, converter
        self.job: dict | None = None
        self._tasks: set[asyncio.Task] = set()

    def pushed(self, cam_id: str, ts: float) -> None:
        a = self.cfg.autosave
        if not a.enabled:
            return
        job = self.job
        if job and ts - a.before <= job["end"] and ts + a.after - job["start"] <= AUTO_MAX_SECONDS:
            job["end"] = max(job["end"], ts + a.after)
            job["cams"].add(cam_id)
            return
        self.job = {"start": ts - a.before, "end": ts + a.after, "cams": {cam_id}}
        task = asyncio.ensure_future(self._run(self.job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: dict) -> None:
        import time
        while (wait := job["end"] + self.SETTLE - time.time()) > 0:
            await asyncio.sleep(wait)   # the end may have moved meanwhile
        if self.job is job:
            self.job = None
        try:
            await self.save(job)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-save failed: %s", e)

    def cameras_for(self, job: dict) -> list[str]:
        cams = set(job["cams"])
        if self.events is not None:
            for e in self.events.query_all(job["start"], job["end"]):
                if e.get("label"):
                    cams.add(e["camera"])
        return sorted(c for c in cams if self.cfg.camera(c))

    async def save(self, job: dict) -> list[Path]:
        out = []
        saved = self.storage.saved_dir()
        for cam_id in await asyncio.to_thread(self.cameras_for, job):
            dst = saved / auto_name(cam_id, job["start"])
            if dst.exists():
                continue
            part = dst.with_suffix(".part")
            try:
                await build_clip(self.storage, cam_id, job["start"], job["end"], part, AUTO_MAX_SECONDS + 60)
                part.replace(dst)
            except ClipError as e:
                part.unlink(missing_ok=True)
                log.info("[%s] auto-save skipped: %s", cam_id, str(e)[:200])
                continue
            log.info("[%s] auto-saved %s", cam_id, dst.name)
            if self.converter is not None:
                self.converter.enqueue(dst)
            out.append(dst)
        return out

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
