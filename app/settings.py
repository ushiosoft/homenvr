"""Runtime-editable settings overlay.

config.yml is the base (read-only). The settings page writes a small JSON overlay
(settings.json, in the writable recordings dir) for the fields it manages, which
is layered on top at startup and whenever the page saves. Changes to alerts,
motion sensitivity and retention take effect live because the running loops read
these fields from the shared Config object each iteration.

Secrets (bot tokens, passwords, webhook URLs) are never returned verbatim by the
API — they come back masked, and a masked value sent back on save is ignored
(the stored secret is kept).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .config import Config

MASK = "••••••••"

PUSH_ON = ["person", "person_vehicle", "motion"]

# Which config fields the settings page may change, and their types.
SCHEMA = {
    "alerts": {
        "notifier": ("enum", ["none", "ntfy", "webhook", "telegram", "smtp"]),
        "ntfy_url": ("secret_str", None),
        "webhook_url": ("secret_str", None),
        "telegram": ("telegram", None),
        "stale_segment_seconds": ("int", (10, 86400)),
        "motion_push": ("bool", None),
        "motion_push_cooldown": ("int", (10, 86400)),
        "push_on": ("enum", PUSH_ON),
    },
    "motion": {
        "sensitivity": ("float", (0.1, 255.0)),
        "min_gap_seconds": ("float", (1.0, 3600.0)),
        "detect": ("bool", None),
    },
    "retention": {
        "max_days": ("float", (0.0, 3650.0)),
        "max_gb": ("float", (0.0, 1_000_000.0)),
        "min_free_gb": ("float", (0.0, 1_000_000.0)),
    },
}
# Per-camera editable fields (overlay key "cameras": {cam_id: {field: value}}).
CAMERA_SCHEMA = {
    "name": ("str", None),
    "rotation": ("enum_int", [0, 90, 180, 270]),
    "motion_sensitivity": ("float", (0.0, 255.0)),
    "push_on": ("enum", [""] + PUSH_ON),  # "" = use the global setting
}
_SECRET_FIELDS = {"ntfy_url", "webhook_url"}  # telegram.bot_token handled separately


def _coerce(kind, bounds, value):
    if kind == "enum":
        if value not in bounds:
            raise ValueError(f"must be one of {bounds}")
        return value
    if kind == "bool":
        return bool(value)
    if kind == "str":
        return str(value)[:64]
    if kind == "enum_int":
        v = int(value)
        if v not in bounds:
            raise ValueError(f"must be one of {bounds}")
        return v
    if kind == "int":
        v = int(value)
    elif kind == "float":
        v = float(value)
    else:
        return value
    if bounds and not (bounds[0] <= v <= bounds[1]):
        raise ValueError(f"must be between {bounds[0]} and {bounds[1]}")
    return v


def load_overlay(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_overlay(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)  # atomic


def apply(cfg: Config, overlay: dict) -> None:
    """Mutate cfg in place from a (trusted, already-validated) overlay."""
    for section, fields in overlay.items():
        if section == "cameras":
            for cam_id, cfields in (fields or {}).items():
                cam = cfg.camera(cam_id)
                if cam:
                    for k, v in cfields.items():
                        if hasattr(cam, k):
                            setattr(cam, k, v)
            continue
        target = getattr(cfg, section, None)
        if target is None:
            continue
        for k, v in fields.items():
            if hasattr(target, k):
                setattr(target, k, v)


def current(cfg: Config) -> dict:
    """Managed settings for the API, with secrets masked."""
    a, m, r = cfg.alerts, cfg.motion, cfg.retention
    tg = dict(a.telegram or {})
    return {
        "alerts": {
            "notifier": a.notifier,
            "ntfy_url": MASK if a.ntfy_url else "",
            "webhook_url": MASK if a.webhook_url else "",
            "telegram": {"bot_token": MASK if tg.get("bot_token") else "",
                         "chat_id": tg.get("chat_id", "")},
            "stale_segment_seconds": a.stale_segment_seconds,
            "motion_push": a.motion_push,
            "motion_push_cooldown": a.motion_push_cooldown,
            "push_on": a.push_on,
        },
        "motion": {"enabled": m.enabled, "sensitivity": m.sensitivity,
                   "min_gap_seconds": m.min_gap_seconds, "detect": m.detect},
        "retention": {"max_days": r.max_days, "max_gb": r.max_gb, "min_free_gb": r.min_free_gb},
        "cameras": [{"id": c.id, "name": c.name, "rotation": c.rotation,
                     "motion_sensitivity": c.motion_sensitivity, "push_on": c.push_on} for c in cfg.cameras],
    }


def _validate_patch(cfg: Config, patch: dict) -> dict:
    """Validate an incoming patch; return the overlay fields to persist/apply.

    Masked secrets are dropped (keep the existing value). Returns {section: {k: v}}.
    """
    out: dict = {}
    for section, fields in patch.items():
        if section == "cameras" and isinstance(fields, dict):
            cams_out: dict = {}
            for cam_id, cfields in fields.items():
                if cfg.camera(cam_id) is None or not isinstance(cfields, dict):
                    continue
                co: dict = {}
                for k, v in cfields.items():
                    if k in CAMERA_SCHEMA:
                        kind, bounds = CAMERA_SCHEMA[k]
                        co[k] = _coerce(kind, bounds, v)
                if co:
                    cams_out[cam_id] = co
            if cams_out:
                out["cameras"] = cams_out
            continue
        if section not in SCHEMA or not isinstance(fields, dict):
            continue
        sec_out: dict = {}
        for k, v in fields.items():
            if k not in SCHEMA[section]:
                continue
            kind, bounds = SCHEMA[section][k]
            if kind == "telegram":
                tg = {}
                bt = (v or {}).get("bot_token", "")
                if bt and bt != MASK:
                    tg["bot_token"] = str(bt)
                elif cfg.alerts.telegram.get("bot_token"):
                    tg["bot_token"] = cfg.alerts.telegram["bot_token"]  # keep existing
                tg["chat_id"] = str((v or {}).get("chat_id", "") or "")
                sec_out[k] = tg
            elif kind == "secret_str":
                if v == MASK:
                    continue  # unchanged
                sec_out[k] = str(v or "")
            else:
                sec_out[k] = _coerce(kind, bounds, v)
        if sec_out:
            out[section] = sec_out
    return out


def update(cfg: Config, path: str | Path, patch: dict) -> dict:
    """Validate + apply a patch to cfg, merge into the persisted overlay. Returns current()."""
    validated = _validate_patch(cfg, patch)
    apply(cfg, validated)
    overlay = load_overlay(path)
    for section, fields in validated.items():
        if section == "cameras":  # merge per-camera so other saved fields survive
            dst = overlay.setdefault("cameras", {})
            for cam_id, cfields in fields.items():
                dst.setdefault(cam_id, {}).update(cfields)
        else:
            overlay.setdefault(section, {}).update(fields)
    save_overlay(path, overlay)
    return current(cfg)


def settings_path(cfg: Config) -> Path:
    return cfg.storage_dir / "settings.json"
