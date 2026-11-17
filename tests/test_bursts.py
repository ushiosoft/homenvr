"""Detection bursts stay small even when a live stream's timestamps jump."""
import asyncio
import logging
import shutil
import subprocess
import time

import pytest

from app import recorder


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_live_burst_capped_when_timestamps_jump(tmp_path):
    """2026-11-09/16: one camera's live stream jumped thousands of seconds back and
    forth; fps=2 filled the "gap" with duplicates, a burst became thousands of frames,
    and decoding them took homenvr to its 4 GB cap. Here 4 s of video with a 600 s
    jump would give ~1200 frames."""
    src = tmp_path / "a.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15", "-t", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    live = tmp_path / "live" / "cam1"
    names = []
    for part, offset in (("a", 0), ("b", 600)):
        d = tmp_path / part
        d.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-c", "copy", "-output_ts_offset", str(offset),
                        "-f", "hls", "-hls_time", "1", "-hls_segment_type", "fmp4",
                        "-hls_fmp4_init_filename", "init.mp4", str(d / "p.m3u8")], check=True)
        live.mkdir(parents=True, exist_ok=True)
        if part == "a":
            shutil.copy(d / "init.mp4", live / "init.mp4")
        for seg in sorted(d.glob("*.m4s")):
            names.append(f"{part}{seg.name}")
            shutil.copy(seg, live / names[-1])
    (live / "index.m3u8").write_text("#EXTM3U\n" + "\n".join(names) + "\n")

    frames = asyncio.run(recorder.live_frames(tmp_path / "live", "cam1", since=0, max_segments=10))
    assert 1 <= len(frames) <= recorder.MAX_BURST_FRAMES


def test_log_limiter_caps_ffmpeg_floods(caplog):
    lim = recorder.LogLimiter("[cam1/live]", per_minute=3)
    with caplog.at_level(logging.WARNING, logger="app.recorder"):
        for i in range(10):
            lim.warning(f"pts has no value {i}", now=100.0 + i)
        lim.warning("next minute", now=161.0)
        lim.flush()
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["[cam1/live] pts has no value 0", "[cam1/live] pts has no value 1",
                     "[cam1/live] pts has no value 2",
                     "[cam1/live] 7 more ffmpeg messages in the last minute not logged",
                     "[cam1/live] next minute"]
