import re
import shutil
import subprocess

import pytest

from app import zones
from app.config import parse
from app.settings import update

TOP = [[0, 0], [1, 0], [1, 0.3], [0, 0.3]]   # top 30 % of the displayed picture


def test_validate_and_settings_roundtrip(tmp_path):
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})
    update(cfg, tmp_path / "s.json", {"cameras": {"cam1": {"zones": [[[0, 0], [1, 0], [0.5, 0.123456]]]}}})
    assert cfg.cameras[0].zones == [{"points": [[0.0, 0.0], [1.0, 0.0], [0.5, 0.1235]], "motion": True, "detect": True}]
    for bad in ([[[0, 0], [1, 1]]], [[[0, 0], [2, 0], [1, 1]]], "x", [TOP] * 9):
        with pytest.raises(ValueError):
            zones.validate(bad)


def test_ground_point_decides():
    # A car on the street: box mostly above y=0.3, bottom edge at 0.28 -> ignored.
    assert zones.ignored((100, 10, 300, 100), 640, 360, [TOP])
    # A person in the yard whose head reaches into the zone: feet at 0.9 -> kept.
    assert not zones.ignored((100, 50, 140, 324), 640, 360, [TOP])
    assert not zones.ignored((100, 10, 300, 100), 640, 360, [])
    assert zones.inside(0.5, 0.5, [[0, 0], [1, 0], [1, 1]]) and not zones.inside(0.2, 0.8, [[0, 0], [1, 0], [1, 1]])


def test_motion_mask_is_drawn_in_display_space(tmp_path):
    cv2 = pytest.importorskip("cv2")
    m = cv2.imread(str(zones.motion_mask([TOP], 0, 320, 180, tmp_path / "a.png")), cv2.IMREAD_GRAYSCALE)
    assert m.shape == (180, 320) and m[10, 160] == 0 and m[170, 160] == 255
    # Rotated 90 clockwise for display: the displayed top is the stream's left side.
    m = cv2.imread(str(zones.motion_mask([TOP], 90, 320, 180, tmp_path / "b.png")), cv2.IMREAD_GRAYSCALE)
    assert m.shape == (180, 320) and m[90, 10] == 0 and m[90, 310] == 255


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_masked_motion_scores_nothing_inside_the_zone(tmp_path, monkeypatch):
    from app import motion
    monkeypatch.setattr(motion, "input_args", lambda url, transport: ["-i", url])
    clip = tmp_path / "top-mover.mp4"   # a white block moving along the top of the picture
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=black:s=640x360:r=10:d=3[bg];color=white:s=40x60:r=10[fg];"
                    "[bg][fg]overlay=x='mod(t*200,600)':y=10:shortest=1",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})

    def peak(mask):
        cmd = motion.motion_cmd(cfg.cameras[0], cfg, str(clip), mask, 180)
        err = subprocess.run(cmd, capture_output=True, text=True).stderr
        return max(float(v) for v in re.findall(r"lavfi\.signalstats\.YAVG=([0-9.]+)", err))
    assert peak(None) > 1.0
    assert peak(zones.motion_mask([TOP], 0, 320, 180, tmp_path / "m.png")) < 0.05
    # A zone elsewhere leaves the score as it was.
    assert peak(zones.motion_mask([[[0, 0.6], [1, 0.6], [1, 1], [0, 1]]], 0, 320, 180, tmp_path / "n.png")) == pytest.approx(peak(None), rel=0.05)


def test_zone_kinds(tmp_path):
    """A street zone that ignores only detections still lets motion through (someone
    on the sidewalk, partly in front of the street, triggers); a BBQ zone that
    ignores only motion still lets a person standing there count."""
    cv2 = pytest.importorskip("cv2")
    street = {"points": TOP, "motion": False, "detect": True}
    bbq = {"points": [[0, 0.6], [1, 0.6], [1, 1], [0, 1]], "motion": True, "detect": False}
    assert zones.validate([street, bbq])[0]["motion"] is False
    with pytest.raises(ValueError):
        zones.validate([{"points": TOP, "motion": False, "detect": False}])
    assert zones.ignored((100, 10, 300, 100), 640, 360, [street])              # car on the street: ignored
    assert not zones.ignored((100, 250, 140, 340), 640, 360, [bbq])           # person at the BBQ: counts
    m = cv2.imread(str(zones.motion_mask([street, bbq], 0, 320, 180, tmp_path / "k.png")), cv2.IMREAD_GRAYSCALE)
    assert m[10, 160] == 255 and m[170, 160] == 0                             # street open, BBQ masked
    # The older form (a bare polygon) still means both.
    assert zones.ignored((100, 10, 300, 100), 640, 360, [TOP]) and zones.motion_polys([TOP]) == [TOP]
