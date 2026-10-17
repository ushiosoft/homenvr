"""Read-only view of the server configuration for the Settings page.

Shows config.yml as written and the environment variables homenvr's setup uses
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


def describe(config_path: str | None = None) -> dict:
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
    return {"config_path": str(path), "config": mask_yaml(raw, secrets), "env": env}
