"""Saved clips: keyframe-aligned building and the seekable playback copy (real ffmpeg)."""
import asyncio
import os
import shutil
import subprocess
import time

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


def test_ranged_remux_for_webkit(tmp_path):
    """Safari/iOS only plays MP4 from Range-capable responses: file=1 cuts the window
    to a real file and answers Range requests with 206 (the stream answers 200)."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    recording(tmp_path / "rec")
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"),
                 "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    with TestClient(create_app(cfg, run_recorders=False)) as c:
        q = {"start": BASE + 4, "dur": 10}
        r = c.get("/api/cameras/cam1/remux.mp4", params={**q, "file": 1}, headers={"Range": "bytes=0-1"})
        assert r.status_code == 206 and r.headers["content-range"].startswith("bytes 0-1/")
        total = int(r.headers["content-range"].rsplit("/", 1)[1])
        r = c.get("/api/cameras/cam1/remux.mp4", params={**q, "file": 1})
        assert r.status_code == 200 and len(r.content) == total            # same cached file
        out = tmp_path / "w.mp4"; out.write_bytes(r.content)
        assert duration(out) == pytest.approx(10, abs=0.3) and first_pts_and_key(out).startswith("K")
        r = c.get("/api/cameras/cam1/playback.mp4", params={**q, "file": 1, "height": 240}, headers={"Range": "bytes=0-1"})
        assert r.status_code == 206


def test_transcoded_playback_starts_exactly(tmp_path):
    """Regression: the transcode started at the keyframe before `start` (up to a GOP
    early) while the client labelled it `start`."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    recording(tmp_path / "rec")                       # keyframes every 2 s
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"),
                 "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    with TestClient(create_app(cfg, run_recorders=False)) as c:
        r = c.get("/api/cameras/cam1/playback.mp4", params={"start": BASE + 5.0, "dur": 3, "height": 240, "file": 1})
        out = tmp_path / "p.mp4"; out.write_bytes(r.content)
        assert duration(out) == pytest.approx(3.0, abs=0.15)   # was 4 s (from the keyframe at 4 s)


def test_recording_frames_cover_exactly_the_window(tmp_path):
    from app.recorder import recording_frames
    recording(tmp_path)
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    frames = asyncio.run(recording_frames(Storage(tmp_path), cfg.cameras[0], BASE + 5.0, BASE + 9.0))
    assert len(frames) == 8 and all(f[:2] == b"\xff\xd8" for f in frames)
    assert asyncio.run(recording_frames(Storage(tmp_path), cfg.cameras[0], BASE + 100, BASE + 104)) == []


def test_first_file_renamed_to_when_its_footage_starts(tmp_path, monkeypatch):
    """The NVR's pre-buffer makes a restart's first file hold footage from before its
    name: it's renamed to start that much earlier."""
    from app import recorder
    from app.storage import parse_stamp
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    d = tmp_path / "cam1"; d.mkdir()
    now = time.time()
    stamp = int(now) - 30
    f = d / f"{format_stamp(stamp)}.mp4"; f.write_bytes(b"x")
    os.utime(f, (now, now))                     # written until now: 30 s of wall time
    async def held(_p): return 34.0             # ... but it holds 34 s of footage
    monkeypatch.setattr(recorder, "probe_duration", held)
    monkeypatch.setattr(recorder, "STAMP_CHECK_AFTER", 0)
    w = recorder.Worker(cfg.cameras[0], cfg, "record", d)
    asyncio.run(w._fix_first_stamp(stamp))
    names = [p.name for p in d.glob("*.mp4")]
    assert len(names) == 1 and parse_stamp(names[0]) == pytest.approx(stamp - 4, abs=1)
    async def ok(_p): return 30.2               # a right name stays
    monkeypatch.setattr(recorder, "probe_duration", ok)
    g = d / names[0]; os.utime(g, (now, now))
    asyncio.run(w._fix_first_stamp(parse_stamp(names[0])))
    assert [p.name for p in d.glob("*.mp4")] == names


def test_recording_frames_across_two_files(tmp_path):
    from app.recorder import recording_frames
    p = recording(tmp_path)                                  # BASE .. BASE+20
    q = p.with_name(f"{format_stamp(BASE + 20)}.mp4")        # BASE+20 .. BASE+40
    shutil.copy(p, q); os.utime(q, (BASE + 40, BASE + 40))
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    frames = asyncio.run(recording_frames(Storage(tmp_path), cfg.cameras[0], BASE + 16, BASE + 24))
    assert len(frames) == 16


def test_direct_play_window_stops_at_its_file(tmp_path):
    """Direct play opens one file with an input seek (exact on its keyframe) and ends
    at that file's end; the client continues with the next."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    p = recording(tmp_path / "rec")
    q = p.with_name(f"{format_stamp(BASE + 20)}.mp4"); shutil.copy(p, q); os.utime(q, (BASE + 40, BASE + 40))
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"),
                 "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    with TestClient(create_app(cfg, run_recorders=False)) as c:
        r = c.get("/api/cameras/cam1/remux.mp4", params={"start": BASE + 14, "dur": 30, "file": 1})
        out = tmp_path / "r.mp4"; out.write_bytes(r.content)
        assert duration(out) == pytest.approx(6.0, abs=0.3) and first_pts_and_key(out).startswith("K")


def test_direct_play_starts_on_the_asked_keyframe(tmp_path):
    """Regression: the copy began at the previous keyframe (negative timestamps hidden
    only by an edit list, which browsers ignore): the picture ran seconds behind."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    recording(tmp_path / "rec")                       # keyframes every 2 s
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"),
                 "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    with TestClient(create_app(cfg, run_recorders=False)) as c:
        for k in (4.0, 8.0, 12.0):
            r = c.get("/api/cameras/cam1/remux.mp4", params={"start": BASE + k - 0.003, "dur": 4, "file": 1})
            out = tmp_path / "k.mp4"; out.write_bytes(r.content)
            pts = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                                  "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
            assert float(pts.split()[0]) >= -0.06, (k, pts)
            assert duration(out) == pytest.approx(4.0, abs=0.3)
