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
