"""Configuration loading.

The config file is YAML with ${ENV_VAR} interpolation so secrets (the NVR
password) can live in .env instead of the file itself.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate(text: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)


@dataclass
class Source:
    """An RTSP source that the cameras are reachable through (normally the NVR)."""

    host: str
    port: int = 554
    username: str = "admin"
    password: str = ""

    def url(self, path: str) -> str:
        cred = ""
        if self.username:
            cred = quote(self.username, safe="") + ":" + quote(self.password, safe="") + "@"
        return f"rtsp://{cred}{self.host}:{self.port}{path}"


@dataclass
class Camera:
    id: str
    name: str
    channel: int
    record_url: str
    live_url: str
    # "copy" keeps the camera's codec; "h264" transcodes live view for browsers
    # without HEVC support (costs CPU).
    live_codec: str = "copy"
    # "aac" transcodes camera audio (often G.711, which MP4 cannot carry),
    # "none" drops it, "copy" passes it through.
    audio: str = "aac"
    enabled: bool = True


@dataclass
class Retention:
    max_gb: float = 0.0  # 0 = unlimited
    max_days: float = 30.0  # 0 = unlimited
    min_free_gb: float = 5.0


@dataclass
class Config:
    storage_dir: Path
    live_dir: Path
    segment_seconds: int
    retention: Retention
    cameras: list[Camera] = field(default_factory=list)
    source: Source | None = None
    ui_username: str = ""
    ui_password: str = ""
    rtsp_transport: str = "tcp"

    def camera(self, cam_id: str) -> Camera | None:
        return next((c for c in self.cameras if c.id == cam_id), None)


def _hik_path(channel: int, stream: int) -> str:
    # Hikvision stream ids: <channel><stream>, stream 01 = main, 02 = sub.
    return f"/Streaming/Channels/{channel}{stream:02d}"


def parse(raw: dict) -> Config:
    src = None
    if raw.get("nvr"):
        n = raw["nvr"]
        src = Source(
            host=str(n["host"]),
            port=int(n.get("port", 554)),
            username=str(n.get("username", "admin")),
            password=str(n.get("password", "")),
        )

    cams: list[Camera] = []
    seen: set[str] = set()
    for i, c in enumerate(raw.get("cameras") or []):
        channel = int(c.get("channel", i + 1))
        cam_id = str(c.get("id") or f"cam{channel}")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", cam_id):
            raise ValueError(f"camera id {cam_id!r} must be alphanumeric/underscore/dash")
        if cam_id in seen:
            raise ValueError(f"duplicate camera id {cam_id!r}")
        seen.add(cam_id)
        record_url = c.get("record_url")
        live_url = c.get("live_url")
        if not record_url or not live_url:
            if src is None:
                raise ValueError(f"camera {cam_id}: set record_url/live_url or configure 'nvr'")
            record_url = record_url or src.url(_hik_path(channel, 1))
            live_url = live_url or src.url(_hik_path(channel, 2))
        cams.append(
            Camera(
                id=cam_id,
                name=str(c.get("name") or cam_id),
                channel=channel,
                record_url=str(record_url),
                live_url=str(live_url),
                live_codec=str(c.get("live_codec", "copy")),
                audio=str(c.get("audio", "aac")),
                enabled=bool(c.get("enabled", True)),
            )
        )

    r = raw.get("retention") or {}
    ui = raw.get("ui") or {}
    return Config(
        storage_dir=Path(raw.get("storage_dir", "/recordings")),
        live_dir=Path(raw.get("live_dir", "/tmp/homenvr-live")),
        segment_seconds=int(raw.get("segment_seconds", 600)),
        retention=Retention(
            max_gb=float(r.get("max_gb", 0) or 0),
            max_days=float(r.get("max_days", 30) or 0),
            min_free_gb=float(r.get("min_free_gb", 5) or 0),
        ),
        cameras=cams,
        source=src,
        ui_username=str(ui.get("username", "")),
        ui_password=str(ui.get("password", "")),
        rtsp_transport=str(raw.get("rtsp_transport", "tcp")),
    )


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("HOMENVR_CONFIG", "/config/config.yml"))
    return parse(yaml.safe_load(_interpolate(path.read_text())) or {})
