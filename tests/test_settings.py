import pytest

from app import settings as s
from app.config import parse


def mkcfg(tmp_path):
    return parse({"storage_dir": str(tmp_path),
                  "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}]})


def test_update_applies_and_persists(tmp_path):
    cfg = mkcfg(tmp_path)
    path = s.settings_path(cfg)
    out = s.update(cfg, path, {
        "alerts": {"notifier": "telegram",
                   "telegram": {"bot_token": "123:ABC", "chat_id": "999"},
                   "stale_segment_seconds": 120},
        "motion": {"sensitivity": 6.5},
        "retention": {"max_days": 14},
    })
    # applied to live cfg
    assert cfg.alerts.notifier == "telegram"
    assert cfg.alerts.telegram == {"bot_token": "123:ABC", "chat_id": "999"}
    assert cfg.motion.sensitivity == 6.5
    assert cfg.retention.max_days == 14
    # secrets masked in the returned view
    assert out["alerts"]["telegram"]["bot_token"] == s.MASK
    assert out["alerts"]["telegram"]["chat_id"] == "999"
    # persisted overlay survives a reload into a fresh cfg
    cfg2 = mkcfg(tmp_path)
    s.apply(cfg2, s.load_overlay(path))
    assert cfg2.alerts.notifier == "telegram"
    assert cfg2.alerts.telegram["bot_token"] == "123:ABC"
    assert cfg2.motion.sensitivity == 6.5


def test_masked_secret_is_not_overwritten(tmp_path):
    cfg = mkcfg(tmp_path)
    path = s.settings_path(cfg)
    s.update(cfg, path, {"alerts": {"notifier": "ntfy", "ntfy_url": "https://ntfy.sh/secret"}})
    # Sending the mask back (as the UI would when the field is untouched) keeps it
    s.update(cfg, path, {"alerts": {"notifier": "ntfy", "ntfy_url": s.MASK}})
    assert cfg.alerts.ntfy_url == "https://ntfy.sh/secret"


def test_validation_rejects_bad_values(tmp_path):
    cfg = mkcfg(tmp_path)
    path = s.settings_path(cfg)
    with pytest.raises(ValueError):
        s.update(cfg, path, {"alerts": {"notifier": "pager"}})          # not an enum member
    with pytest.raises(ValueError):
        s.update(cfg, path, {"motion": {"sensitivity": 0}})             # below min
    with pytest.raises(ValueError):
        s.update(cfg, path, {"retention": {"max_days": -1}})            # below min


def test_unknown_fields_ignored(tmp_path):
    cfg = mkcfg(tmp_path)
    before = cfg.rtsp_transport
    s.update(cfg, s.settings_path(cfg), {"rtsp_transport": "udp", "alerts": {"bogus": 1}})
    assert cfg.rtsp_transport == before  # not a managed field, untouched


def test_current_masks_only_set_secrets(tmp_path):
    cfg = mkcfg(tmp_path)
    cur = s.current(cfg)
    assert cur["alerts"]["ntfy_url"] == ""         # unset -> empty, not masked
    cfg.alerts.ntfy_url = "x"
    assert s.current(cfg)["alerts"]["ntfy_url"] == s.MASK
