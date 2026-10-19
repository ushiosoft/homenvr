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
    monkeypatch.setattr(recorder, "PREFERRED_RETRY_PAUSE", 0)
    url, info, fallback = asyncio.run(recorder.pick_source(recorder.sub_sources(cfg.cameras[0], cfg), "tcp"))
    assert (url, fallback) == ("rtsp://nvr/102", True)


def test_pick_source_waits_for_go2rtc_to_start(monkeypatch):
    """Regression: right after a restart go2rtc's restream refused the first probe
    (its transcoder was still starting) and the worker sat on the NVR for 5 min."""
    import asyncio
    from app import recorder
    tries = []

    async def fake_probe(url, transport="tcp", timeout=20):
        tries.append(url)
        if "go2rtc" in url and len(tries) < 3:
            raise RuntimeError("404 not found")
        return {"video": "h264"}
    monkeypatch.setattr(recorder, "probe", fake_probe)
    monkeypatch.setattr(recorder, "PREFERRED_RETRY_PAUSE", 0)
    url, _info, fallback = asyncio.run(recorder.pick_source(["rtsp://go2rtc:8554/cam1", "rtsp://nvr/102"], "tcp"))
    assert (url, fallback, len(tries)) == ("rtsp://go2rtc:8554/cam1", False, 3)


def test_go2rtc_registration_is_stable(monkeypatch):
    """Regression: go2rtc lists producer URLs without our "ffmpeg:...#video=" source
    options, which made every keep-warm pass delete + re-create the stream (and a
    read-only 400 on DELETE left it deleted). Registration must happen once, and only
    be redone when the camera's codec changes or go2rtc lost the stream."""
    import io, json, urllib.error
    from app import webrtc
    cfg = parse({"storage_dir": "/tmp/x", "webrtc_api": "http://go2rtc:1984",
                 "cameras": [{"id": "cam1", "record_url": "r", "live_url": "rtsp://nvr/102"}]})
    streams, calls = {}, []

    def fake_urlopen(req, timeout=5):
        url = req if isinstance(req, str) else req.full_url
        method = "GET" if isinstance(req, str) else req.get_method()
        calls.append(method)
        if method == "GET":
            return io.BytesIO(json.dumps({k: {"producers": [{"url": "rtsp://nvr/102"}]} for k in streams}).encode())
        if method == "PUT":
            streams["cam1"] = 1
        if method == "DELETE":
            streams.pop("cam1", None)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b"open /config/go2rtc.yaml: read-only file system"))
    monkeypatch.setattr(webrtc.urllib.request, "urlopen", fake_urlopen)
    g = webrtc.Go2rtc(cfg)
    codec = {"v": "hevc"}
    monkeypatch.setattr(webrtc, "probe_codec", lambda url, timeout=20: codec["v"])
    cam = cfg.cameras[0]
    g.ensure_stream(cam)
    assert "cam1" in streams and calls.count("PUT") == 1
    for _ in range(3):
        g.ensure_stream(cam)                       # keep-warm passes: no churn
    assert calls.count("PUT") == 1 and calls.count("DELETE") == 0
    streams.clear(); g.ensure_stream(cam)          # go2rtc restarted and lost it
    assert calls.count("PUT") == 2 and calls.count("DELETE") == 0
    codec["v"] = "h264"; g._codec.clear(); g.ensure_stream(cam)   # camera switched codec
    assert calls.count("DELETE") == 1 and calls.count("PUT") == 3 and g._registered["cam1"] == "rtsp://nvr/102"


def test_go2rtc_delete_of_unsaved_stream_is_success(monkeypatch):
    """go2rtc answers "yaml: path not exist" when deleting a stream that was never in
    its config file (all of ours) — after deleting it in memory. Not a failure."""
    import io, urllib.error
    from app import webrtc
    cfg = parse({"storage_dir": "/tmp/x", "webrtc_api": "http://go2rtc:1984", "cameras": []})

    def fake_urlopen(req, timeout=5):
        raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"yaml: path not exist"))
    monkeypatch.setattr(webrtc.urllib.request, "urlopen", fake_urlopen)
    webrtc.Go2rtc(cfg)._call("DELETE", "/api/streams?src=cam1")   # no raise


def test_box_frames_api(tmp_path):
    from app.events import EventStore
    with client(tmp_path) as c:
        st = EventStore(tmp_path / "rec" / "events.db")
        eid = st.add("cam1", 1000.0, "motion", 5.0)
        st.set_boxes(eid, {"t0": 994.0, "dt": 0.5, "pick": 2, "f": [[], [["animal", 0.1, 0.2, 0.3, 0.4]], [["motion", 0, 0, 0.1, 0.1]]]})
        r = c.get("/api/cameras/cam1/boxes", params={"start": 990, "end": 1010}).json()
        assert r == [{"t": 994.5, "b": [["animal", 0.1, 0.2, 0.3, 0.4]]}, {"t": 995.0, "b": [["motion", 0, 0, 0.1, 0.1]]}]
        assert c.get("/api/cameras/cam1/boxes", params={"start": 995.2, "end": 1010}).json() == []
        assert c.get("/api/incidents", params={"start": 0, "end": 5000, "filter": "animal"}).status_code == 200


def test_site_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/Edmonton")
    with client(tmp_path) as c:
        assert c.get("/api/site").json() == {"tz": "America/Edmonton"}
    monkeypatch.setenv("TZ", "x; rm -rf")
    with client(tmp_path) as c:
        assert c.get("/api/site").json() == {"tz": None}
