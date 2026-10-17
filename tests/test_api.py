import os

from fastapi.testclient import TestClient

from app.config import parse
from app.main import create_app
from tests.test_storage import make


def client(tmp_path, **ui):
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"), "ui": ui,
                 "cameras": [{"id": "cam1", "record_url": "rtsp://x/1", "live_url": "rtsp://x/2"}]})
    return TestClient(create_app(cfg, run_recorders=False))


def test_segments_and_download(tmp_path):
    base = 1760500000
    with client(tmp_path) as c:
        p = make(tmp_path / "rec", "cam1", base, 600, size=5000)
        r = c.get("/api/cameras/cam1/segments", params={"start": base, "end": base + 86400})
        assert r.status_code == 200 and r.json()[0]["name"] == p.name
        r = c.get(f"/recordings/cam1/{p.name}", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
        assert c.get("/recordings/cam1/..%2F..%2Fetc%2Fpasswd").status_code == 404
        assert c.get("/api/cameras/nope/segments", params={"start": 0, "end": 1}).status_code == 404
        assert c.get("/").status_code == 200


def test_basic_auth(tmp_path):
    with client(tmp_path, username="admin", password="pw") as c:
        assert c.get("/api/cameras").status_code == 401
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/cameras", auth=("admin", "pw")).status_code == 200


def test_saved_thumb_and_delete(tmp_path):
    with client(tmp_path) as c:
        d = tmp_path / "rec" / "saved"
        d.mkdir(parents=True)
        (d / "cam1-20261015-120000.mp4").write_bytes(b"clip")
        (d / "cam1-20261015-120000.jpg").write_bytes(b"\xff\xd8jpeg")
        assert [x["name"] for x in c.get("/api/saved").json()] == ["cam1-20261015-120000.mp4"]
        r = c.get("/api/saved/cam1-20261015-120000.mp4/thumb")
        assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
        assert c.get("/api/saved/..%2Fx.mp4/thumb").status_code == 404
        assert c.delete("/api/saved/cam1-20261015-120000.mp4").status_code == 200
        assert not (d / "cam1-20261015-120000.jpg").exists()


def test_webrtc_signalling(tmp_path, monkeypatch):
    with client(tmp_path) as c:   # webrtc_api not configured -> HLS only
        assert c.get("/api/cameras").json()[0]["webrtc"] is False
        assert c.post("/api/cameras/cam1/webrtc", content="v=0\r\n").status_code == 404
    from app import webrtc
    monkeypatch.setattr(webrtc.Go2rtc, "answer", lambda self, cam, offer: "v=0\r\no=answer-for-" + cam.id)
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"),
                 "webrtc_api": "http://go2rtc:1984/",
                 "cameras": [{"id": "cam1", "record_url": "rtsp://x/1", "live_url": "rtsp://x/2", "live_codec": "h264"}]})
    assert cfg.webrtc_api == "http://go2rtc:1984"
    with TestClient(create_app(cfg, run_recorders=False)) as c:
        assert c.get("/api/cameras").json()[0]["webrtc"] is True
        r = c.post("/api/cameras/cam1/webrtc", content="v=0\r\no=offer", headers={"Content-Type": "application/sdp"})
        assert r.status_code == 200 and r.text.endswith("answer-for-cam1") and r.headers["content-type"].startswith("application/sdp")
        assert c.post("/api/cameras/cam1/webrtc", content="hello").status_code == 400
        assert c.post("/api/cameras/nope/webrtc", content="v=0").status_code == 404
    assert webrtc.stream_source(cfg.cameras[0], "hevc") == "ffmpeg:rtsp://x/2#video=h264#audio=opus"
    assert webrtc.stream_source(cfg.cameras[0], "h264") == "rtsp://x/2"   # passed through untouched


def test_sub_sources_prefer_go2rtc_and_fall_back(monkeypatch):
    import asyncio
    from app import recorder
    cfg = parse({"storage_dir": "/tmp/x", "webrtc_api": "http://go2rtc:1984",
                 "cameras": [{"id": "cam1", "record_url": "rtsp://nvr/101", "live_url": "rtsp://nvr/102"}]})
    assert recorder.sub_sources(cfg.cameras[0], cfg) == ["rtsp://go2rtc:8554/cam1", "rtsp://nvr/102"]
    no_g2r = parse({"storage_dir": "/tmp/x", "cameras": [{"id": "cam1", "record_url": "r", "live_url": "rtsp://nvr/102"}]})
    assert recorder.sub_sources(no_g2r.cameras[0], no_g2r) == ["rtsp://nvr/102"]

    async def fake_probe(url, transport="tcp", timeout=20):
        if "go2rtc" in url:
            raise RuntimeError("connection refused")
        return {"video": "hevc"}
    monkeypatch.setattr(recorder, "probe", fake_probe)
    url, info, fallback = asyncio.run(recorder.pick_source(recorder.sub_sources(cfg.cameras[0], cfg), "tcp"))
    assert (url, fallback) == ("rtsp://nvr/102", True)
