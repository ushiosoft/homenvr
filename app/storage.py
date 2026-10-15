"""Recording storage: segment index, retention and clip export.

Layout: <storage_dir>/<camera_id>/<YYYYmmddTHHMMSSZ>.mp4

The filename is the segment's start time in UTC. Invariant: a segment file's
mtime is its media end time. The recorder writes in real time, so this holds
naturally; the importer sets it explicitly with os.utime.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STAMP_FMT = "%Y%m%dT%H%M%SZ"
# Reserved top-level folders under storage_dir that are not cameras.
GRACE_DIR = "grace"
SAVED_DIR = "saved"
RESERVED_DIRS = {GRACE_DIR, SAVED_DIR}
# Same thing in ffmpeg strftime syntax (ffmpeg runs with TZ=UTC).
FFMPEG_PATTERN = "%Y%m%dT%H%M%SZ.mp4"


@dataclass(frozen=True)
class Segment:
    camera: str
    path: Path
    start: float
    end: float
    size: int

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def parse_stamp(name: str) -> float | None:
    stem = name[:-4] if name.endswith(".mp4") else name
    try:
        return datetime.strptime(stem, STAMP_FMT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def format_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(STAMP_FMT)


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def camera_dir(self, camera: str) -> Path:
        d = self.root / camera
        d.mkdir(parents=True, exist_ok=True)
        return d

    def segments(self, camera: str, start: float | None = None, end: float | None = None) -> list[Segment]:
        """Segments for a camera overlapping [start, end), oldest first."""
        d = self.root / camera
        if not d.is_dir():
            return []
        out: list[Segment] = []
        for entry in os.scandir(d):
            if not entry.name.endswith(".mp4"):
                continue
            s = parse_stamp(entry.name)
            if s is None:
                continue
            try:
                st = entry.stat()
            except FileNotFoundError:  # deleted by retention mid-scan
                continue
            if st.st_size == 0:
                continue
            seg = Segment(camera, Path(entry.path), s, max(s, st.st_mtime), st.st_size)
            if end is not None and seg.start >= end:
                continue
            if start is not None and seg.end <= start:
                continue
            out.append(seg)
        out.sort(key=lambda x: x.start)
        return out

    def span(self, camera: str) -> tuple[float | None, float | None]:
        """(first_start, last_end) for a camera without statting every file.

        Filenames are start timestamps, so first/last come from the name min/max;
        only the newest file is statted (its mtime = media end). O(1) stats.
        """
        d = self.root / camera
        if not d.is_dir():
            return (None, None)
        stamps = sorted(
            s for s in (parse_stamp(e.name) for e in os.scandir(d) if e.name.endswith(".mp4"))
            if s is not None
        )
        if not stamps:
            return (None, None)
        last_name = format_stamp(stamps[-1]) + ".mp4"
        try:
            last = max(stamps[-1], (d / last_name).stat().st_mtime)
        except OSError:
            last = stamps[-1]
        return (stamps[0], last)

    def cameras_on_disk(self) -> list[str]:
        if not self.root.is_dir():
            return []
        # Only real camera dirs — skip the reserved grace/ and saved/ folders.
        return sorted(e.name for e in os.scandir(self.root)
                      if e.is_dir() and e.name not in RESERVED_DIRS)

    def usage(self) -> dict:
        total = sum(s.size for c in self.cameras_on_disk() for s in self.segments(c))
        du = shutil.disk_usage(self.root)
        return {"recordings_bytes": total, "disk_total_bytes": du.total, "disk_free_bytes": du.free}

    def resolve(self, camera: str, name: str) -> Path | None:
        """Safe lookup of a segment file by name (no path traversal)."""
        if parse_stamp(name) is None or "/" in name or "\\" in name:
            return None
        p = self.root / camera / name
        return p if p.is_file() else None

    # ---- retention -------------------------------------------------------

    def enforce_retention(self, max_gb: float, max_days: float, min_free_gb: float, now: float | None = None) -> list[Path]:
        """Delete oldest segments until all limits hold. Returns deleted paths.

        The newest segment of each camera is never deleted because the
        recorder may still be writing it.
        """
        now = now or time.time()
        all_segs: list[Segment] = []
        for cam in self.cameras_on_disk():
            segs = self.segments(cam)
            all_segs.extend(segs[:-1])  # protect newest
        all_segs.sort(key=lambda s: s.start)

        total = sum(s.size for c in self.cameras_on_disk() for s in self.segments(c))
        free = shutil.disk_usage(self.root).free
        max_bytes = max_gb * 1024**3 if max_gb > 0 else None
        min_free = min_free_gb * 1024**3 if min_free_gb > 0 else None
        cutoff = now - max_days * 86400 if max_days > 0 else None

        deleted: list[Path] = []
        for seg in all_segs:
            too_old = cutoff is not None and seg.end < cutoff
            too_big = max_bytes is not None and total > max_bytes
            too_full = min_free is not None and free < min_free
            if not (too_old or too_big or too_full):
                break  # sorted oldest-first, so nothing later qualifies by age either
            try:
                seg.path.unlink()
            except FileNotFoundError:
                continue
            total -= seg.size
            free += seg.size
            deleted.append(seg.path)
        if deleted:
            log.info("retention: deleted %d segments", len(deleted))
        return deleted

    # ---- export ----------------------------------------------------------

    def concat_list(self, camera: str, start: float, end: float) -> str:
        """ffmpeg concat-demuxer script covering [start, end)."""
        lines = ["ffconcat version 1.0"]
        for seg in self.segments(camera, start, end):
            lines.append(f"file '{seg.path.as_posix()}'")
            if start > seg.start:
                lines.append(f"inpoint {start - seg.start:.3f}")
            if end < seg.end:
                lines.append(f"outpoint {end - seg.start:.3f}")
        return "\n".join(lines) + "\n"
