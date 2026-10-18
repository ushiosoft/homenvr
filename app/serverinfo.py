"""Read-only view of the server configuration for the Settings page.

Shows the effective configuration (config.yml with the Settings page's overlay,
settings.json, applied — each overlaid value marked), config.yml as written, and
the environment variables homenvr's setup uses
(they come from .env through docker compose's env_file), with every secret
masked: values of password/token/key/secret/webhook fields, credentials inside
URLs, and literal values that equal a secret from the environment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .settings import MASK

ENV_KEYS = ["TZ", "RECORDINGS_DIR", "NVR_PASSWORD", "UI_PASSWORD", "TS_AUTHKEY",
            "WEBRTC_LAN_IP", "WEBRTC_TS_IP", "LOG_LEVEL"]
_SECRET_KEY = re.compile(r"(pass|token|secret|key|webhook|ntfy_url|auth)", re.I)
_YAML_FIELD = re.compile(r"(?P<key>[A-Za-z_][\w-]*)(?P<sep>\s*:\s*)(?P<val>\$\{[^}]*\}|\"[^\"]*\"|'[^']*'|[^,{}#\n]+)")
_URL_CRED = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)
_ENV_REF = re.compile(r"^\$\{[A-Za-z_]\w*(:-[^}]*)?\}$")


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def mask_yaml(text: str, secrets: list[str]) -> str:
    """Mask secret values in YAML text (block and {flow: style}); ${VAR} references stay."""
    def field(m: re.Match) -> str:
        val = m.group("val").rstrip()
        if is_secret_key(m.group("key")) and val and not _ENV_REF.match(val.strip("\"'")):
            return f"{m.group('key')}{m.group('sep')}{MASK}" + m.group("val")[len(val):]
        return m.group(0)
    out = []
    for line in text.splitlines():
        code, hash_, comment = line.partition(" #")
        code = _YAML_FIELD.sub(field, code)
        out.append(code + hash_ + _URL_CRED.sub(r"\1***@", comment))
    text = "\n".join(out)
    text = _URL_CRED.sub(r"\1***@", text)
    for s in secrets:
        if len(s) >= 4:
            text = text.replace(s, MASK)
    return text


def _scalar(key: str, v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, (list, dict)):
        import json
        return json.dumps(v, separators=(", ", ": "))
    v = str(v)
    if is_secret_key(key) and v:
        return MASK
    v = _URL_CRED.sub(r"\1***@", v)
    return v if v and re.fullmatch(r"[\w./:@*+-]+", v) else json_quote(v)


def json_quote(v: str) -> str:
    import json
    return json.dumps(v, ensure_ascii=False)


def effective(cfg, overlay: dict) -> str:
    """The configuration homenvr runs with, as YAML: config.yml with the Settings
    page's overlay applied. Values that come from the overlay are marked."""
    lines = ["# What homenvr is running with. '# set in Settings' = changed on the Settings",
             "# page (stored in settings.json), which overrides config.yml.", ""]

    def section(name: str, obj, keys: list[str], from_settings: dict | None = None):
        lines.append(f"{name}:")
        for k in keys:
            v = getattr(obj, k)
            if k == "telegram":
                v = {kk: (MASK if is_secret_key(kk) and vv else vv) for kk, vv in (v or {}).items()}
            if k == "smtp":
                v = {kk: (MASK if is_secret_key(kk) and vv else vv) for kk, vv in (v or {}).items()}
            mark = "  # set in Settings" if from_settings and k in from_settings else ""
            lines.append(f"  {k}: {_scalar(k, v)}{mark}")
        lines.append("")

    a = cfg.alerts
    section("alerts", a, ["notifier", "webhook_url", "ntfy_url", "telegram", "smtp", "motion_push", "push_on",
                          "motion_push_cooldown", "public_url", "stale_segment_seconds", "check_interval"],
            overlay.get("alerts"))
    section("motion", cfg.motion, ["enabled", "sensitivity", "min_gap_seconds", "detect", "detect_confidence"],
            overlay.get("motion"))
    section("retention", cfg.retention, ["max_days", "max_gb", "min_free_gb", "reclaim_headroom_gb"],
            overlay.get("retention"))
    section("autosave", cfg.autosave, ["enabled", "before", "after", "max_days", "max_gb"], overlay.get("autosave"))
    lines.append("cameras:")
    ov_cams = overlay.get("cameras") or {}
    for c in cfg.cameras:
        oc = ov_cams.get(c.id) or {}
        fields = [("id", c.id), ("name", c.name), ("channel", c.channel), ("enabled", c.enabled),
                  ("rotation", c.rotation), ("push_on", c.push_on or "(global)"),
                  ("motion_sensitivity", c.motion_sensitivity or "(global)"), ("zones", len(c.zones)),
                  ("audio", c.audio), ("live_codec", c.live_codec)]
        body = ", ".join(f"{k}: {_scalar(k, v)}" for k, v in fields)
        mark = f"  # set in Settings: {', '.join(sorted(oc))}" if oc else ""
        lines.append(f"  - {{{body}}}{mark}")
    lines.append("")
    src = cfg.source
    if src:
        lines.append(f"nvr: {{host: {src.host}, port: {src.port}, username: {src.username}, password: {MASK}}}")
    lines += [f"segment_seconds: {cfg.segment_seconds}", f"rtsp_transport: {cfg.rtsp_transport}",
              f"webrtc_api: {cfg.webrtc_api or '(off)'}", f"storage_dir: {cfg.storage_dir}",
              f"ui: {{username: {cfg.ui_username or '(no login)'}, password: {MASK if cfg.ui_password else ''}}}"]
    return "\n".join(lines)


def describe(config_path: str | None = None, cfg=None, overlay: dict | None = None) -> dict:
    path = Path(config_path or os.environ.get("HOMENVR_CONFIG", "/config/config.yml"))
    try:
        raw = path.read_text()
    except OSError as e:
        raw = f"# could not read {path}: {e}"
    referenced = re.findall(r"\$\{([A-Za-z_]\w*)", raw)
    keys = list(dict.fromkeys(ENV_KEYS + referenced))
    secrets = [os.environ[k] for k in keys if is_secret_key(k) and os.environ.get(k)]
    env = []
    for k in keys:
        if k not in os.environ:
            continue
        v = os.environ[k]
        env.append({"key": k, "value": MASK if (is_secret_key(k) and v) else _URL_CRED.sub(r"\1***@", v)})
    out = {"config_path": str(path), "config": mask_yaml(raw, secrets), "env": env}
    if cfg is not None:
        out["effective"] = mask_yaml(effective(cfg, overlay or {}), secrets)
    return out
