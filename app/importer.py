"""Backfill recordings from the NVR's SD card via Hikvision RTSP playback.

    python -m app.importer --camera cam1 --from "2026-10-01 00:00" --to "2026-10-02 00:00"

Hikvision serves stored footage at
    rtsp://<nvr>/Streaming/tracks/<ch>01?starttime=YYYYMMDDTHHMMSSZ&endtime=...
Despite the trailing Z, the times are interpreted as the NVR's *local* time
(a long-standing Hikvision quirk), so --from/--to are taken in the NVR's
timezone (default: this process's TZ).

The NVR streams playback at roughly 1x speed, so importing a day takes about
a day. Chunks that already exist on disk are skipped, so it can be resumed.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

from . import config as config_mod
from .recorder import FFMPEG, FFPROBE, redact, socket_timeout_args
from .storage import Storage, format_stamp

log = logging.getLogger("homenvr.importer")


def playback_url(cfg: config_mod.Config, channel: int, start: datetime, end: datetime) -> str:
    if cfg.source is None:
        raise SystemExit("importer needs the 'nvr' section in config")
    fmt = "%Y%m%dT%H%M%SZ"
    return cfg.source.url(f"/Streaming/tracks/{channel}01?starttime={start.strftime(fmt)}&endtime={end.strftime(fmt)}")


def duration(path: str) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


async def import_chunk(cfg, storage: Storage, cam, start: datetime, end: datetime) -> bool:
    start_ts = start.timestamp()  # naive local datetime -> epoch via process TZ
    dst = storage.camera_dir(cam.id) / f"{format_stamp(start_ts)}.mp4"
    if dst.exists():
        log.info("skip %s (exists)", dst.name)
        return True
    tmp = dst.with_suffix(".part")
    url = playback_url(cfg, cam.channel, start, end)
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-rtsp_transport", "tcp",
           *socket_timeout_args(), "-i", url, "-map", "0:v:0", "-c:v", "copy",
           "-t", str((end - start).total_seconds()), "-f", "mp4",
           "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(tmp)]
    log.info("import %s %s → %s", cam.id, start, redact(url))
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    d = duration(str(tmp)) if tmp.exists() else 0
    if d < 1:
        tmp.unlink(missing_ok=True)
        log.info("no footage for %s (%s)", start, redact(err.decode(errors="replace").strip()[-200:]))
        return False
    tmp.rename(dst)
    os.utime(dst, (start_ts + d, start_ts + d))  # storage invariant: mtime = media end
    log.info("saved %s (%.0fs)", dst.name, d)
    return True


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", required=True, help="camera id from config")
    ap.add_argument("--from", dest="start", required=True, help='"YYYY-MM-DD HH:MM" NVR local time')
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--chunk-minutes", type=int, default=60)
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(asctime)s %(message)s")

    cfg = config_mod.load(a.config)
    cam = cfg.camera(a.camera) or sys.exit(f"unknown camera {a.camera}")
    storage = Storage(cfg.storage_dir)
    t, end = datetime.fromisoformat(a.start), datetime.fromisoformat(a.end)
    step = timedelta(minutes=a.chunk_minutes)
    while t < end:
        await import_chunk(cfg, storage, cam, t, min(t + step, end))
        t += step


if __name__ == "__main__":
    asyncio.run(main())
