import asyncio
import os
import time

from app import clips
from app.config import parse
from app.serverinfo import mask_yaml, describe
from app.settings import MASK
from app.storage import Storage


def test_mask_yaml_hides_secrets_keeps_structure():
    text = """nvr: {host: 192.168.1.64, username: admin, password: ${NVR_PASSWORD}}
alerts:
  webhook_url: https://discord.com/api/webhooks/123/abc   # private
  telegram: {bot_token: "123:ABC", chat_id: "42"}
  smtp: {host: smtp.x, password: hunter22}
cameras:
  - {id: cam1, record_url: rtsp://admin:pw@10.0.0.2/1}
ui:
  username: user
  password: literal-secret
extra: has literal-secret inside
"""
    out = mask_yaml(text, ["literal-secret"])
    assert "${NVR_PASSWORD}" in out                    # references are not secrets
    for leak in ("123/abc", "123:ABC", "hunter22", "admin:pw", "literal-secret"):
        assert leak not in out, leak
    assert 'chat_id: "42"' in out and "username: user" in out and "rtsp://***@10.0.0.2/1" in out
    assert out.count(MASK) >= 5


def test_describe_masks_env(tmp_path, monkeypatch):
    cfgp = tmp_path / "config.yml"
    cfgp.write_text("ui: {password: ${UI_PASSWORD}}\n")
    monkeypatch.setenv("UI_PASSWORD", "s3cret!")
    monkeypatch.setenv("TZ", "America/Edmonton")
    d = describe(str(cfgp))
    env = {e["key"]: e["value"] for e in d["env"]}
    assert env["UI_PASSWORD"] == MASK and env["TZ"] == "America/Edmonton"
    assert "s3cret" not in str(d)


def _clip(d, name, age_days, size):
    p = d / name
    p.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_prune_auto_by_age_then_size(tmp_path):
    d = tmp_path
    (d / ".play").mkdir()
    _clip(d, "cam1-20260101-000000-auto.mp4", 100, 10)          # too old
    _clip(d, "cam1-20260901-000000-auto.mp4", 10, 600)
    _clip(d, "cam1-20260902-000000-auto.mp4", 9, 600)
    _clip(d, "cam1-20250101-000000.mp4", 400, 10)               # manual: never touched
    (d / ".play" / "cam1-20260901-000000-auto.mp4").write_bytes(b"y" * 100)
    gone = clips.prune_auto(d, 90, 1e-6, time.time())           # 1000 bytes allowed
    assert gone == ["cam1-20260101-000000-auto.mp4", "cam1-20260901-000000-auto.mp4"]
    assert not (d / ".play" / "cam1-20260901-000000-auto.mp4").exists()
    assert (d / "cam1-20250101-000000.mp4").exists() and (d / "cam1-20260902-000000-auto.mp4").exists()


def test_autosaver_merges_pushes_into_one_incident(tmp_path, monkeypatch):
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [
        {"id": c, "record_url": "r", "live_url": "l"} for c in ("cam1", "cam2", "cam3")]})

    class Events:
        def query_all(self, a, b):
            return [{"camera": "cam3", "ts": a + 20, "label": "person"},
                    {"camera": "cam2", "ts": a + 20, "label": ""}]   # unlabelled motion: not "saw it"
    built = []

    async def fake_build(storage, cam, start, end, out, max_s):
        built.append((cam, round(end - start)))
        out.write_bytes(b"clip")
    monkeypatch.setattr(clips, "build_clip", fake_build)
    monkeypatch.setattr(clips.AutoSaver, "SETTLE", 0)
    a = clips.AutoSaver(cfg, Storage(tmp_path), Events())

    async def run():
        now = time.time() - 60          # long past: cut at once
        a.pushed("cam1", now)
        a.pushed("cam1", now + 20)      # same incident: extends it
        await asyncio.sleep(0.2)
        await a.stop()
    asyncio.run(run())
    assert sorted(built) == [("cam1", 60), ("cam3", 60)]
    names = sorted(p.name for p in (tmp_path / "saved").glob("*.mp4"))
    assert len(names) == 2 and all(clips.is_auto(n) for n in names)


def test_keep_and_list_auto(tmp_path):
    from tests.test_api import client
    with client(tmp_path) as c:
        d = tmp_path / "rec" / "saved"
        d.mkdir(parents=True)
        (d / "cam1-20261015-120000-auto.mp4").write_bytes(b"clip")
        (d / "cam1-20261015-120000-auto.jpg").write_bytes(b"jpg")
        assert c.get("/api/saved").json()[0]["auto"] is True
        r = c.post("/api/saved/cam1-20261015-120000-auto.mp4/keep")
        assert r.json() == {"kept": "cam1-20261015-120000-kept.mp4"}
        assert (d / "cam1-20261015-120000-kept.jpg").exists()
        assert c.get("/api/saved").json()[0]["auto"] is False
        assert c.post("/api/saved/cam1-20261015-120000-kept.mp4/keep").status_code == 404
        assert c.get("/api/server-config").status_code == 200
