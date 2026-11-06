"""Evidence for a memory runaway.

2026-11-04: homenvr went from under 1.5 GB to its 4 GB container cap in about three
minutes, with nothing in the log to say what it was doing. So: log memory every ten
minutes for a timeline, and once memory passes alerts.memory_alert_mb, dump what
every thread and asyncio task is doing, then trace which code allocates over the
next minute (tracemalloc, started only then: it slows allocations while on).
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
import time
import traceback
import tracemalloc

from .alerts import process_rss_mb

log = logging.getLogger(__name__)

LOG_EVERY = 600       # seconds between "memory:" log lines
TRACE_SECONDS = 60    # how long tracemalloc watches once over the limit
TRACE_FRAMES = 8
TOP = 12


def proc_status_mb(field: str) -> float | None:
    """A kB field of /proc/self/status (VmHWM, ...) in MB; None where unavailable."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def dump_stacks(reason: str) -> None:
    """Every thread's Python stack (to stderr, i.e. the container log) and every
    asyncio task's (to the log). Call from the event loop thread."""
    log.warning("memory: %s — thread stacks follow on stderr", reason)
    sys.stderr.flush()
    faulthandler.dump_traceback(sys.stderr, all_threads=True)
    lines = []
    for t in asyncio.all_tasks():
        frames = t.get_stack(limit=6)
        where = " <- ".join(f"{f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno} {f.f_code.co_name}"
                            for f in reversed(frames)) or "(not started)"
        lines.append(f"  {t.get_name()}: {where}")
    log.warning("memory: %d asyncio tasks\n%s", len(lines), "\n".join(sorted(lines)))


async def trace_growth(seconds: float | None = None) -> None:
    """Log the code paths that allocated the most over the next `seconds`."""
    seconds = TRACE_SECONDS if seconds is None else seconds
    if tracemalloc.is_tracing():
        return
    before = process_rss_mb()
    tracemalloc.start(TRACE_FRAMES)
    try:
        await asyncio.sleep(seconds)
        snap = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    stats = snap.filter_traces([tracemalloc.Filter(False, tracemalloc.__file__)]).statistics("traceback")
    out = []
    for s in stats[:TOP]:
        out.append(f"  {s.size / 2**20:.1f} MB in {s.count} blocks")
        out.extend("    " + ln for ln in s.traceback.format(most_recent_first=True)[:2 * TRACE_FRAMES])
    log.warning("memory: %.0f -> %.0f MB over %.0f s; largest allocations made meanwhile (still held):\n%s",
                before or 0, process_rss_mb() or 0, seconds, "\n".join(out) or "  (none)")
    dump_stacks("after tracing")


class MemoryDiag:
    """Call check() from the event loop once per watchdog tick."""

    def __init__(self) -> None:
        self._last_log = float("-inf")
        self._tripped = False
        self._task: asyncio.Task | None = None

    def check(self, limit_mb: int, now: float | None = None) -> None:
        rss = process_rss_mb()
        if rss is None:
            return
        now = time.monotonic() if now is None else now
        if now - self._last_log >= LOG_EVERY:
            self._last_log = now
            log.info("memory: homenvr %.0f MB (peak %.0f MB)", rss, proc_status_mb("VmHWM") or 0)
        if limit_mb > 0 and rss > limit_mb:
            if not self._tripped:
                self._tripped = True
                dump_stacks(f"{rss:.0f} MB is over {limit_mb} MB")
                self._task = asyncio.create_task(trace_growth(), name="memdiag")
        else:
            self._tripped = False
