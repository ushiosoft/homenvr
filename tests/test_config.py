import pytest

from app.config import parse


def test_nvr_urls_and_password_escaping():
    cfg = parse({"nvr": {"host": "10.0.0.5", "password": "p@ss:w/rd"},
                 "cameras": [{"id": "front", "channel": 2}]})
    cam = cfg.cameras[0]
    assert cam.record_url == "rtsp://admin:p%40ss%3Aw%2Frd@10.0.0.5:554/Streaming/Channels/201"
    assert cam.live_url.endswith("/Streaming/Channels/202")


def test_override_urls_without_nvr():
    cfg = parse({"cameras": [{"id": "a", "record_url": "rtsp://x/1", "live_url": "rtsp://x/2"}]})
    assert cfg.cameras[0].record_url == "rtsp://x/1"


def test_bad_ids():
    with pytest.raises(ValueError):
        parse({"cameras": [{"id": "../x", "record_url": "r", "live_url": "l"}]})
    with pytest.raises(ValueError):
        parse({"nvr": {"host": "h"}, "cameras": [{"id": "a"}, {"id": "a"}]})


def test_env_secrets_with_yaml_syntax_are_literal(tmp_path, monkeypatch):
    from app.config import load
    p = tmp_path / "config.yml"
    p.write_text("storage_dir: /tmp/x\n"
                 "nvr:\n  host: 10.0.0.1\n  password: ${NVR_PASSWORD}\n"
                 "ui:\n  username: user\n  password: ${UI_PASSWORD}  # trailing comment\n"
                 "cameras: [{id: cam1, channel: 1}]\n")
    monkeypatch.setenv("UI_PASSWORD", "!123456#")
    monkeypatch.setenv("NVR_PASSWORD", "a: b # c")
    cfg = load(p)
    assert cfg.ui_password == "!123456#"


def test_config_file_needs_at_least_one_camera(tmp_path):
    import pytest
    from app.config import load
    p = tmp_path / "config.yml"
    p.write_text("storage_dir: /tmp/x\ncameras: []\n")
    with pytest.raises(ValueError, match="at least one camera"):
        load(p)
    p.write_text("storage_dir: /tmp/x\ncameras: [{id: front, record_url: rtsp://x/1, live_url: rtsp://x/2}]\n")
    assert [c.id for c in load(p).cameras] == ["front"]


def test_any_number_of_nvr_channels(tmp_path):
    """Not tied to a 4-channel NVR: channel 10+ gives Hikvision's <channel><stream> path."""
    cfg = parse({"nvr": {"host": "10.0.0.5", "password": "p"},
                 "cameras": [{"channel": ch} for ch in range(1, 17)]})
    assert len(cfg.cameras) == 16 and cfg.cameras[-1].id == "cam16"
    assert cfg.cameras[9].record_url.endswith("/Streaming/Channels/1001")
    assert cfg.cameras[15].live_url.endswith("/Streaming/Channels/1602")


def test_config_example_loads(monkeypatch):
    """config.example.yml is what new users copy: it must stay loadable and current."""
    from pathlib import Path
    from app.config import load
    monkeypatch.setenv("NVR_PASSWORD", "x")
    monkeypatch.setenv("UI_PASSWORD", "y")
    cfg = load(Path(__file__).parent.parent / "config.example.yml")
    assert len(cfg.cameras) >= 1 and cfg.ui_password == "y" and cfg.webrtc_api
