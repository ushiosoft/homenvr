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


def test_effective_config_shows_settings_overlay(tmp_path):
    from app import settings as s
    from app.serverinfo import effective
    cfg = parse({"storage_dir": str(tmp_path), "alerts": {"notifier": "none"},
                 "cameras": [{"id": "cam1", "record_url": "rtsp://u:pw@nvr/1", "live_url": "l"}]})
    s.update(cfg, tmp_path / "settings.json", {"alerts": {"notifier": "webhook", "webhook_url": "https://discord.com/api/webhooks/1/SECRET"},
                                               "cameras": {"cam1": {"rotation": 90}}})
    out = effective(cfg, s.load_overlay(tmp_path / "settings.json"))
    assert "notifier: webhook  # set in Settings" in out
    assert "SECRET" not in out and "webhook_url: " + MASK in out
    assert "rotation: 90" in out and "# set in Settings: rotation" in out
    assert "pw@" not in out


def _autosave_run(tmp_path, monkeypatch, events, pushes):
    """Run an AutoSaver over pushes (offsets from a minute ago); returns (start, end)
    offsets of each clip built, relative to the same base."""
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [
        {"id": c, "record_url": "r", "live_url": "l"} for c in ("cam1", "cam2")]})
    base = time.time() - 600            # long past: cut at once

    class Events:
        def query_all(self, a, b):
            return [{"camera": c, "ts": base + t, "label": ""} for c, t in events if a <= base + t <= b]
    built = []

    async def fake_build(storage, cam, start, end, out, max_s):
        assert end - start <= max_s
        built.append((cam, round(start - base), round(end - base)))
        out.write_bytes(b"clip")
    monkeypatch.setattr(clips, "build_clip", fake_build)
    monkeypatch.setattr(clips.AutoSaver, "SETTLE", 0)
    a = clips.AutoSaver(cfg, Storage(tmp_path), Events())

    async def run():
        for t in pushes:
            a.pushed("cam1", base + t)
        await asyncio.sleep(0.3)
        await a.stop()
    asyncio.run(run())
    return built


def test_autosave_clip_starts_where_the_incident_does(tmp_path, monkeypatch):
    # Deer: two "animal" events before the third one pushed as "person" (2026-11-17).
    # The event 247 s earlier is a separate incident; cam2's motion is another camera's.
    events = [("cam1", 0), ("cam1", 247), ("cam1", 264), ("cam1", 285), ("cam2", 250)]
    assert _autosave_run(tmp_path, monkeypatch, events, [285]) == [("cam1", 237, 315)]


def test_autosave_clip_follows_motion_after_the_push(tmp_path, monkeypatch):
    events = [("cam1", 100), ("cam1", 125), ("cam1", 180), ("cam1", 400)]
    # Push at 100; motion carries on to 180 (chained), 400 is a later incident.
    assert _autosave_run(tmp_path, monkeypatch, events, [100]) == [("cam1", 90, 210)]


def test_autosave_lead_in_is_bounded(tmp_path, monkeypatch):
    # A camera that fires every 30 s all along: reach back AUTO_MAX_LEAD at most,
    # and never past the length cap.
    events = [("cam1", t) for t in range(0, 601, 30)]
    (cam, s, e), = _autosave_run(tmp_path, monkeypatch, events, [300])
    assert cam == "cam1" and s == 300 - clips.AUTO_MAX_LEAD - 10 and e - s <= clips.AUTO_MAX_SECONDS
