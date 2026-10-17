"""Saved clips: keyframe-aligned building and the seekable playback copy (real ffmpeg)."""
import asyncio
import os
import shutil
import subprocess

import pytest

from app import clips
from app.config import parse
from app.storage import Storage, format_stamp

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
BASE = 1760500000


def recording(root, cam="cam1", gop=50):
    """A 20 s 25 fps H.264 recording with a keyframe every `gop` frames."""
    d = root / cam
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{format_stamp(BASE)}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=20",
                    "-c:v", "libx264", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", str(p)], check=True)
    os.utime(p, (BASE + 20, BASE + 20))
    return p


def first_pts_and_key(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                          "-show_entries", "packet=flags", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return out.strip().splitlines()[0]


def duration(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(path)], capture_output=True, text=True).stdout)


def test_build_clip_starts_on_a_keyframe(tmp_path):
    s = Storage(tmp_path)
    recording(tmp_path)     # keyframes every 2 s
    out = tmp_path / "clip.mp4"
    asyncio.run(clips.build_clip(s, "cam1", BASE + 5.0, BASE + 9.0, out, 3600))
    assert first_pts_and_key(out).startswith("K")             # decodable from the first frame
    assert duration(out) == pytest.approx(5.0, abs=0.25)     # snapped back to the keyframe at 4 s (unsnapped: 4 s)
    with pytest.raises(clips.ClipError) as e:
        asyncio.run(clips.build_clip(s, "cam1", BASE + 100, BASE + 110, out, 3600))
    assert e.value.status == 404


def test_converter_links_h264_and_converts_rotated(tmp_path):
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [
        {"id": "cam1", "record_url": "r", "live_url": "l"},
        {"id": "cam2", "record_url": "r", "live_url": "l", "rotation": 90}]})
    s = Storage(tmp_path)
    saved = s.saved_dir()
    shutil.copy(recording(tmp_path), saved / "cam1-20261017-120000.mp4")
    shutil.copy(recording(tmp_path, "cam2"), saved / "cam2-20261017-120000.mp4")
    conv = clips.ClipConverter(cfg, s)

    async def run():
        conv.start()
        for _ in range(200):
            if all(conv.ready(p) for p in saved.glob("*.mp4")):
                break
            await asyncio.sleep(0.1)
        await conv.stop()
    asyncio.run(run())
    a = conv.ready(saved / "cam1-20261017-120000.mp4")
    b = conv.ready(saved / "cam2-20261017-120000.mp4")
    assert a and os.path.samefile(a, saved / "cam1-20261017-120000.mp4")    # H.264, unrotated: linked
    assert b and b.stat().st_nlink == 1                                    # rotated: converted
    dims = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", str(b)], capture_output=True, text=True).stdout
    assert dims.strip() == "180,320"
    assert s.saved_bytes() == sum(p.stat().st_size for p in saved.glob("*.mp4")) + b.stat().st_size
    conv.remove(saved / "cam2-20261017-120000.mp4")
    assert conv.ready(saved / "cam2-20261017-120000.mp4") is None
