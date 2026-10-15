"""Health alerts: a watchdog that notifies when a camera stops recording,
a stream goes silent, or the disk fills.

Alerts are edge-triggered: one notification when a check starts failing, one
when it recovers. No repeat spam while a condition persists.
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

from .config import Alerts

log = logging.getLogger(__name__)

TIMEOUT = 10
# Discord/Cloudflare reject the default "Python-urllib/*" UA with HTTP 403 (error
# 1010), so every notifier HTTP request sends a real User-Agent.
USER_AGENT = "homenvr/1.0 (+https://github.com/morluto/rea)"


def _post(url: str, data: bytes, extra: dict | None = None):
    headers = {"User-Agent": USER_AGENT}
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, data=data, headers=headers)
    urllib.request.urlopen(req, timeout=TIMEOUT).close()


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """Build a multipart/form-data body. files: {name: (filename, bytes, content_type)}."""
    import uuid
    boundary = "----homenvr" + uuid.uuid4().hex
    buf = bytearray()
    for k, v in fields.items():
        buf += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for name, (fn, content, ctype) in files.items():
        buf += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fn}\"\r\n".encode()
        buf += f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


class Notifier:
    def send(self, title: str, message: str, *, failing: bool) -> None:
        raise NotImplementedError

    def send_image(self, title: str, message: str, image: bytes, filename: str = "snapshot.jpg") -> None:
        """Send with an image attachment. Default: fall back to text-only."""
        self.send(title, message, failing=False)


class NullNotifier(Notifier):
    """Logs only — used when no notifier is configured."""

    def send(self, title: str, message: str, *, failing: bool) -> None:
        log.warning("ALERT %s: %s — %s", "DOWN" if failing else "OK", title, message)


class NtfyNotifier(Notifier):
    def __init__(self, url: str):
        self.url = url

    def send(self, title: str, message: str, *, failing: bool) -> None:
        _post(self.url, message.encode(), {
            "Title": title, "Priority": "high" if failing else "default",
            "Tags": "rotating_light" if failing else "white_check_mark"})

    def send_image(self, title: str, message: str, image: bytes, filename: str = "snapshot.jpg") -> None:
        # ntfy: raw image body with Title/Message/Filename headers.
        _post(self.url, image, {"Title": title, "Message": message,
                                "Filename": filename, "Content-Type": "image/jpeg"})


class WebhookNotifier(Notifier):
    """Posts JSON {text: ...} — compatible with Slack/Discord/Mattermost webhooks."""

    def __init__(self, url: str):
        self.url = url

    def send(self, title: str, message: str, *, failing: bool) -> None:
        icon = "🔴" if failing else "🟢"
        body = json.dumps({"text": f"{icon} *{title}*\n{message}",
                           "content": f"{icon} **{title}**\n{message}"}).encode()
        _post(self.url, body, {"Content-Type": "application/json"})

    def send_image(self, title: str, message: str, image: bytes, filename: str = "snapshot.jpg") -> None:
        # Discord webhook: multipart with payload_json + file attachment.
        payload = json.dumps({"content": f"📸 **{title}**\n{message}"})
        body, ctype = _multipart({"payload_json": payload},
                                 {"files[0]": (filename, image, "image/jpeg")})
        _post(self.url, body, {"Content-Type": ctype})


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str):
        self.token, self.chat_id = bot_token, chat_id

    def send(self, title: str, message: str, *, failing: bool) -> None:
        icon = "🔴" if failing else "🟢"
        data = json.dumps({"chat_id": self.chat_id,
                           "text": f"{icon} *{title}*\n{message}",
                           "parse_mode": "Markdown"}).encode()
        _post(f"https://api.telegram.org/bot{self.token}/sendMessage",
              data, {"Content-Type": "application/json"})

    def send_image(self, title: str, message: str, image: bytes, filename: str = "snapshot.jpg") -> None:
        # Telegram sendPhoto: multipart with photo file + caption.
        body, ctype = _multipart({"chat_id": self.chat_id, "caption": f"📸 {title}\n{message}"},
                                 {"photo": (filename, image, "image/jpeg")})
        _post(f"https://api.telegram.org/bot{self.token}/sendPhoto", body, {"Content-Type": ctype})


class SmtpNotifier(Notifier):
    def __init__(self, smtp: dict):
        self.s = smtp

    def send(self, title: str, message: str, *, failing: bool) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[homenvr] {title}"
        msg["From"] = self.s.get("from", self.s.get("user", "homenvr@localhost"))
        msg["To"] = self.s["to"]
        msg.set_content(message)
        host, port = self.s.get("host", "localhost"), int(self.s.get("port", 587))
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as srv:
            if self.s.get("starttls", True):
                srv.starttls()
            if self.s.get("user"):
                srv.login(self.s["user"], self.s.get("password", ""))
            srv.send_message(msg)


def make_notifier(a: Alerts) -> Notifier:
    if a.notifier == "ntfy" and a.ntfy_url:
        return NtfyNotifier(a.ntfy_url)
    if a.notifier == "webhook" and a.webhook_url:  # Discord/Slack
        return WebhookNotifier(a.webhook_url)
    if a.notifier == "telegram" and a.telegram.get("bot_token") and a.telegram.get("chat_id"):
        return TelegramNotifier(a.telegram["bot_token"], a.telegram["chat_id"])
    if a.notifier == "smtp" and a.smtp.get("to"):
        return SmtpNotifier(a.smtp)
    return NullNotifier()


@dataclass
class Check:
    key: str
    ok: bool
    message: str


class Watchdog:
    """Evaluates health and fires edge-triggered notifications.

    `observe` is a callable returning a list[Check] for the current moment; it is
    injected so the watchdog is trivially testable without a live system.
    """

    def __init__(self, observe, notifier: Notifier):
        self.observe = observe
        self.notifier = notifier
        self._failing: dict[str, bool] = {}  # key -> currently-failing?

    def tick(self, now: float | None = None) -> list[tuple[str, bool]]:
        """Run one evaluation; notify on transitions. Returns (key, failing) fired."""
        fired: list[tuple[str, bool]] = []
        for c in self.observe(now or time.time()):
            was_failing = self._failing.get(c.key, False)
            now_failing = not c.ok
            if now_failing != was_failing:
                self._failing[c.key] = now_failing
                try:
                    self.notifier.send(c.key, c.message, failing=now_failing)
                except Exception:
                    log.exception("notifier failed for %s", c.key)
                fired.append((c.key, now_failing))
        return fired


def build_observer(cfg, supervisor, storage):
    """Returns an observe(now) -> list[Check] closure over the live system."""
    import shutil

    def observe(now: float) -> list[Check]:
        checks: list[Check] = []
        for cam in cfg.cameras:
            if not cam.enabled:
                continue
            st = supervisor.status(cam.id).get("record", {})
            running = bool(st.get("running"))
            if not running:
                checks.append(Check(f"camera {cam.name}", False,
                                    f"recorder not running: {st.get('last_error', 'unknown')[:200]}"))
                continue
            # running but is data actually arriving? newest segment mtime should advance.
            segs = storage.segments(cam.id)
            newest = segs[-1].end if segs else 0
            stale = now - newest > cfg.alerts.stale_segment_seconds if newest else False
            if stale:
                age = int(now - newest)
                checks.append(Check(f"camera {cam.name}", False,
                                    f"recording but no new data for {age}s (stream stalled?)"))
            else:
                checks.append(Check(f"camera {cam.name}", True, "recording normally"))
        free = shutil.disk_usage(storage.root).free
        min_free = cfg.retention.min_free_gb * 1024**3
        if min_free > 0 and free < min_free:
            msg = f"only {free / 1024**3:.1f} GB free (min {cfg.retention.min_free_gb})"
            # If there's nothing left in grace to auto-delete, saved clips may be the cause.
            if not storage.grace_segments():
                saved_gb = storage.saved_bytes() / 1024**3
                if saved_gb > 1:
                    msg += f"; grace empty, {saved_gb:.1f} GB in saved/ (never auto-deleted) — free space manually"
            checks.append(Check("disk space", False, msg))
        else:
            checks.append(Check("disk space", True, f"{free / 1024**3:.1f} GB free"))
        return checks

    return observe
