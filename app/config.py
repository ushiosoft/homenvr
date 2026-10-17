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


def _interpolate(value):
    """Expand ${VAR} / ${VAR:-default} inside the *parsed* config's strings.

    Done after YAML parsing, not on the raw text, so a secret containing YAML
    syntax (a password like "!123456#" — '!' is a tag, '#' a comment) is taken
    literally instead of breaking the file or being silently truncated.
    """
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


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
    rotation: int = 0  # display rotation in degrees: 0/90/180/270
    motion_sensitivity: float = 0.0  # per-camera override of motion.sensitivity (0 = use global)
    push_on: str = ""  # per-camera override of alerts.push_on ("" = use global)
    zones: list = field(default_factory=list)  # ignore zones: normalized polygons (app/zones.py)


@dataclass
class Retention:
    max_gb: float = 0.0  # 0 = unlimited; active footage past this moves to grace
    max_days: float = 30.0  # 0 = unlimited; active footage older than this moves to grace
    min_free_gb: float = 5.0  # grace is hard-deleted only when free disk drops below this
    reclaim_headroom_gb: float = 5.0  # when reclaiming, free this much past min_free, then stop


@dataclass
class Alerts:
    notifier: str = "none"  # none | ntfy | webhook | telegram | smtp
    ntfy_url: str = ""
    webhook_url: str = ""  # Discord/Slack/Mattermost incoming webhook
    telegram: dict = field(default_factory=dict)  # {bot_token, chat_id}
    smtp: dict = field(default_factory=dict)
    stale_segment_seconds: int = 180  # recorder "running" but no new data this long
    check_interval: int = 60
    motion_push: bool = False  # push a snapshot notification on motion
    motion_push_cooldown: int = 0  # min seconds between motion pushes per camera (0 = every event)
    # Which motion events are pushed: "person" (default), "person_vehicle", or
    # "motion" (every event). Needs object detection; without it, every event is.
    push_on: str = "person"
    # Address the UI is reachable at from a phone (e.g. https://homenvr.<tailnet>.ts.net);
    # motion pushes then link to the event. "" = no link.
    public_url: str = ""


@dataclass
class AutoSave:
    """Clips of pushed events, kept in Saved with their own limits (app/clips.py)."""
    enabled: bool = True
    before: float = 10.0   # seconds of footage before the pushed event
    after: float = 30.0    # ... and after the last push of the incident
    max_days: float = 90.0  # 0 = no limit
    max_gb: float = 50.0    # 0 = no limit


@dataclass
class Motion:
    enabled: bool = False
    # "scene" score threshold (0-1); higher = less sensitive. ffmpeg scdet.
    sensitivity: float = 4.0  # frame-difference brightness threshold (0-255); higher = less sensitive
    min_gap_seconds: float = 8.0  # merge detections closer than this into one event
    cooldown_seconds: float = 3.0
    detect: bool = True  # label events person/vehicle via on-server object detection
    detect_confidence: float = 0.45


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
    alerts: Alerts = field(default_factory=Alerts)
    motion: Motion = field(default_factory=Motion)
    autosave: AutoSave = field(default_factory=AutoSave)
    events_db: Path = Path("/recordings/events.db")
    # go2rtc sidecar API for low-latency WebRTC live view ("" = off: HLS only).
    webrtc_api: str = ""

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
                rotation=int(c.get("rotation", 0)),
                motion_sensitivity=float(c.get("motion_sensitivity", 0) or 0),
                push_on=str(c.get("push_on", "") or ""),
                zones=list(c.get("zones") or []),
            )
        )

    r = raw.get("retention") or {}
    ui = raw.get("ui") or {}
    a = raw.get("alerts") or {}
    m = raw.get("motion") or {}
    au = raw.get("autosave") or {}
    storage_dir = Path(raw.get("storage_dir", "/recordings"))
    return Config(
        storage_dir=storage_dir,
        live_dir=Path(raw.get("live_dir", "/tmp/homenvr-live")),
        segment_seconds=int(raw.get("segment_seconds", 600)),
        retention=Retention(
            max_gb=float(r.get("max_gb", 0) or 0),
            max_days=float(r.get("max_days", 30) or 0),
            min_free_gb=float(r.get("min_free_gb", 5) or 0),
            reclaim_headroom_gb=float(r.get("reclaim_headroom_gb", 5) or 0),
        ),
        cameras=cams,
        source=src,
        ui_username=str(ui.get("username", "")),
        ui_password=str(ui.get("password", "")),
        rtsp_transport=str(raw.get("rtsp_transport", "tcp")),
        alerts=Alerts(
            notifier=str(a.get("notifier", "none")),
            ntfy_url=str(a.get("ntfy_url", "")),
            webhook_url=str(a.get("webhook_url", "")),
            telegram=dict(a.get("telegram") or {}),
            smtp=dict(a.get("smtp") or {}),
            stale_segment_seconds=int(a.get("stale_segment_seconds", 180)),
            check_interval=int(a.get("check_interval", 60)),
            motion_push=bool(a.get("motion_push", False)),
            motion_push_cooldown=int(a.get("motion_push_cooldown", 0)),
            push_on=str(a.get("push_on", "person")),
            public_url=str(a.get("public_url", "") or ""),
        ),
        motion=Motion(
            enabled=bool(m.get("enabled", False)),
            sensitivity=float(m.get("sensitivity", 4.0)),
            min_gap_seconds=float(m.get("min_gap_seconds", 8.0)),
            cooldown_seconds=float(m.get("cooldown_seconds", 3.0)),
            detect=bool(m.get("detect", True)),
            detect_confidence=float(m.get("detect_confidence", 0.45)),
        ),
        autosave=AutoSave(
            enabled=bool(au.get("enabled", True)),
            before=float(au.get("before", 10)),
            after=float(au.get("after", 30)),
            max_days=float(au.get("max_days", 90) or 0),
            max_gb=float(au.get("max_gb", 50) or 0),
        ),
        events_db=Path(raw.get("events_db", str(storage_dir / "events.db"))),
        webrtc_api=str(raw.get("webrtc_api", "") or "").rstrip("/"),
    )


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("HOMENVR_CONFIG", "/config/config.yml"))
    return parse(_interpolate(yaml.safe_load(path.read_text()) or {}))
