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
THUMBS_DIR = "thumbs"  # per-event JPEGs: thumbs/<camera>/<event id>.jpg
RESERVED_DIRS = {GRACE_DIR, SAVED_DIR, THUMBS_DIR}
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

    def segments(self, camera: str, start: float | None = None, end: float | None = None,
                 root: Path | None = None) -> list[Segment]:
        """Segments for a camera overlapping [start, end), oldest first.

        `root` scans an alternate base (e.g. the grace/ dir) instead of the main root.
        """
        d = (root or self.root) / camera
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

    def segment_at(self, camera: str, ts: float) -> tuple[Path | None, float]:
        """Find the segment (active or grace) covering `ts`; return (path, offset_seconds)."""
        for base in (self.root, self.root / GRACE_DIR):
            for seg in self.segments(camera, ts - 1, ts + 1, root=base):
                if seg.start <= ts < seg.end:
                    return seg.path, max(0.0, ts - seg.start)
        return None, 0.0

    def _stamps(self, camera: str) -> list[float]:
        """Segment start times (active + grace) from filenames only — no stat calls,
        which are slow on bind-mounted Windows drives."""
        out: list[float] = []
        for base in (self.root, self.root / GRACE_DIR):
            d = base / camera
            if d.is_dir():
                out.extend(s for s in (parse_stamp(e.name) for e in os.scandir(d)
                                       if e.name.endswith(".mp4")) if s is not None)
        out.sort()
        return out

    def earliest(self, camera: str) -> float | None:
        st = self._stamps(camera)
        return st[0] if st else None

    def coverage(self, camera: str, start: float, end: float, segment_seconds: float,
                 now: float | None = None) -> list[tuple[float, float]]:
        """Recorded spans overlapping [start, end), from segment names alone.

        A segment is assumed to run until the next one starts or for segment_seconds,
        whichever is first; the newest one runs at most until `now`.
        """
        now = now or time.time()
        stamps = self._stamps(camera)
        spans: list[tuple[float, float]] = []
        for i, a in enumerate(stamps):
            b = a + segment_seconds
            if i + 1 < len(stamps):
                b = min(b, stamps[i + 1])
            else:
                b = min(b, now)
            if b <= start or a >= end:
                continue
            a, b = max(a, start), min(b, end)
            if spans and a - spans[-1][1] <= 2:  # contiguous (allow small gaps)
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
        return spans

    def thumb_path(self, camera: str, event_id: int) -> Path:
        return self.root / THUMBS_DIR / camera / f"{int(event_id)}.jpg"

    def delete_thumbs(self, camera: str, event_ids) -> int:
        n = 0
        for eid in event_ids:
            try:
                self.thumb_path(camera, eid).unlink()
                n += 1
            except FileNotFoundError:
                pass
        return n

    def cameras_on_disk(self) -> list[str]:
        if not self.root.is_dir():
            return []
        # Only real camera dirs — skip the reserved grace/ and saved/ folders.
        return sorted(e.name for e in os.scandir(self.root)
                      if e.is_dir() and e.name not in RESERVED_DIRS)

    def usage(self) -> dict:
        active = sum(s.size for c in self.cameras_on_disk() for s in self.segments(c))
        grace = self._bytes(self.grace_segments())
        saved = self.saved_bytes()
        du = shutil.disk_usage(self.root)
        return {"recordings_bytes": active, "grace_bytes": grace, "saved_bytes": saved,
                "disk_total_bytes": du.total, "disk_free_bytes": du.free}

    def resolve(self, camera: str, name: str) -> Path | None:
        """Safe lookup of a segment file by name (no path traversal)."""
        if parse_stamp(name) is None or "/" in name or "\\" in name:
            return None
        p = self.root / camera / name
        return p if p.is_file() else None

    # ---- grace + saved locations ----------------------------------------

    def grace_dir(self, camera: str) -> Path:
        d = self.root / GRACE_DIR / camera
        d.mkdir(parents=True, exist_ok=True)
        return d

    def saved_dir(self) -> Path:
        d = self.root / SAVED_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def grace_segments(self) -> list[Segment]:
        """All segments currently in grace (pending deletion), oldest first."""
        base = self.root / GRACE_DIR
        if not base.is_dir():
            return []
        out: list[Segment] = []
        for camdir in os.scandir(base):
            if not camdir.is_dir():
                continue
            for seg in self.segments(camdir.name, root=base):
                out.append(seg)
        out.sort(key=lambda s: s.start)
        return out

    def saved_bytes(self) -> int:
        d = self.root / SAVED_DIR
        if not d.is_dir():
            return 0
        total = sum(e.stat().st_size for e in os.scandir(d) if e.is_file())
        play = d / ".play"  # seekable copies (see app/clips.py); hard links cost nothing
        if play.is_dir():
            total += sum(st.st_size for st in (e.stat() for e in os.scandir(play) if e.is_file())
                         if st.st_nlink == 1)
        return total

    def _bytes(self, segs: list[Segment]) -> int:
        return sum(s.size for s in segs)

    # ---- two-stage retention --------------------------------------------
    #
    # Stage 1 "expire": active segments past max_days or max_gb are MOVED to
    # grace/ (cheap rename, reversible, no data loss). Runs continuously.
    # Stage 2 "reclaim": grace is hard-DELETED only under real disk pressure
    # (free < min_free_gb), oldest first, in one batch down to a headroom
    # cushion, then stops. No constant trickle deletion.
    # saved/ is never touched by either stage.

    def expire(self, max_gb: float, max_days: float, now: float | None = None) -> list[Path]:
        now = now or time.time()
        cutoff = now - max_days * 86400 if max_days > 0 else None
        max_bytes = max_gb * 1024**3 if max_gb > 0 else None

        active: list[Segment] = []
        for cam in self.cameras_on_disk():
            active.extend(self.segments(cam)[:-1])  # protect newest (being written)
        active.sort(key=lambda s: s.start)
        total = sum(self._bytes(self.segments(c)) for c in self.cameras_on_disk())

        moved: list[Path] = []
        for seg in active:
            too_old = cutoff is not None and seg.end < cutoff
            too_big = max_bytes is not None and total > max_bytes
            if not (too_old or too_big):
                break
            dst = self.grace_dir(seg.camera) / seg.path.name
            try:
                os.replace(seg.path, dst)  # atomic within the same filesystem
            except OSError:
                continue
            total -= seg.size
            moved.append(dst)
        if moved:
            log.info("retention: moved %d segments to grace", len(moved))
        return moved

    def reclaim(self, min_free_gb: float, headroom_gb: float = 5.0, now: float | None = None) -> list[Path]:
        if min_free_gb <= 0:
            return []
        free = shutil.disk_usage(self.root).free
        min_free = min_free_gb * 1024**3
        if free >= min_free:
            return []  # no pressure -> do nothing (no constant deletion)
        target = min_free + headroom_gb * 1024**3  # hysteresis: recover past the line
        deleted: list[Path] = []
        for seg in self.grace_segments():  # oldest grace first
            if free >= target:
                break
            try:
                seg.path.unlink()
            except FileNotFoundError:
                continue
            free += seg.size
            deleted.append(seg.path)
        if deleted:
            log.warning("retention: disk pressure, deleted %d grace segments", len(deleted))
        return deleted

    def run_retention(self, max_gb: float, max_days: float, min_free_gb: float,
                      headroom_gb: float = 5.0, now: float | None = None) -> dict:
        now = now or time.time()
        moved = self.expire(max_gb, max_days, now)
        deleted = self.reclaim(min_free_gb, headroom_gb, now)
        return {"moved_to_grace": len(moved), "deleted": len(deleted)}

    # ---- export ----------------------------------------------------------

    def concat_list(self, camera: str, start: float, end: float) -> str:
        """ffmpeg concat-demuxer script covering [start, end), active + grace."""
        lines = ["ffconcat version 1.0"]
        segs = self.segments(camera, start, end)
        segs += self.segments(camera, start, end, root=self.root / GRACE_DIR)
        for seg in sorted(segs, key=lambda s: s.start):
            lines.append(f"file '{seg.path.as_posix()}'")
            if start > seg.start:
                lines.append(f"inpoint {start - seg.start:.3f}")
            if end < seg.end:
                lines.append(f"outpoint {end - seg.start:.3f}")
        return "\n".join(lines) + "\n"
