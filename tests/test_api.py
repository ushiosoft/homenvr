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
