import time

from app.alerts import Check, Watchdog, make_notifier
from app.config import Alerts, parse
from app.recorder import Supervisor
from app.storage import Storage
from tests.test_storage import make


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, message, *, failing):
        self.sent.append((title, failing, message))


def test_watchdog_edge_triggered():
    state = {"ok": True}
    n = RecordingNotifier()
    wd = Watchdog(lambda now: [Check("cam A", state["ok"], "msg")], n)

    assert wd.tick() == []            # healthy, no notification
    assert wd.tick() == []            # still healthy, still nothing
    state["ok"] = False
    assert wd.tick() == [("cam A", True)]   # transition to failing -> one alert
    assert wd.tick() == []            # still failing -> no repeat
    state["ok"] = True
    assert wd.tick() == [("cam A", False)]  # recovery -> one alert
    assert [s[1] for s in n.sent] == [True, False]


def test_notifier_failure_does_not_crash_tick():
    class Boom:
        def send(self, *a, **k):
            raise RuntimeError("down")
    wd = Watchdog(lambda now: [Check("x", False, "m")], Boom())
    assert wd.tick() == [("x", True)]  # still records the transition despite notifier error


def test_make_notifier_defaults_to_null():
    from app.alerts import NullNotifier, NtfyNotifier
    assert isinstance(make_notifier(Alerts()), NullNotifier)
    assert isinstance(make_notifier(Alerts(notifier="ntfy", ntfy_url="https://ntfy.sh/x")), NtfyNotifier)
    # misconfigured (no url) falls back to null
    assert isinstance(make_notifier(Alerts(notifier="ntfy")), NullNotifier)


def test_observer_flags_down_stale_and_disk(tmp_path):
    cfg = parse({
        "storage_dir": str(tmp_path),
        "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}],
        "alerts": {"stale_segment_seconds": 100},
        "retention": {"min_free_gb": 0},  # don't trip disk in this test
    })
    storage = Storage(tmp_path)
    sup = Supervisor(cfg, storage)

    from app.alerts import build_observer
    observe = build_observer(cfg, sup, storage)
    now = time.time()

    # No worker registered -> recorder "not running" -> failing
    checks = {c.key: c for c in observe(now)}
    assert checks["camera cam1"].ok is False

    # Simulate a running recorder with fresh data
    class FakeWorker:
        class state:
            @staticmethod
            def as_dict():
                return {"running": True, "last_error": ""}
    sup.workers[("cam1", "record")] = FakeWorker()
    make(tmp_path, "cam1", now - 30, 20)  # newest segment ends ~10s ago -> fresh
    checks = {c.key: c for c in observe(now)}
    assert checks["camera cam1"].ok is True

    # Make the newest segment old -> stale
    checks = {c.key: c for c in observe(now + 500)}
    assert checks["camera cam1"].ok is False
    assert "no new data" in checks["camera cam1"].message


def test_send_image_multipart_and_ntfy():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from app.alerts import WebhookNotifier, NtfyNotifier
    cap = {}
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get('Content-Length', 0))
            cap['body'] = self.rfile.read(n)
            cap['ct'] = self.headers.get('Content-Type')
            cap['ua'] = self.headers.get('User-Agent')
            cap['title'] = self.headers.get('Title')
            cap['click'] = self.headers.get('Click')
            self.send_response(204); self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    img = b"\xff\xd8\xff" + b"JPEGDATA" + b"\xff\xd9"

    WebhookNotifier(f"http://127.0.0.1:{port}/wh").send_image("Motion", "cam1", img, "s.jpg")
    assert cap['ct'].startswith("multipart/form-data; boundary=")
    assert b'name="payload_json"' in cap['body'] and b'name="files[0]"' in cap['body']
    assert img in cap['body'] and cap['ua'].startswith("homenvr/")

    WebhookNotifier(f"http://127.0.0.1:{port}/wh")  # reset not needed
    NtfyNotifier(f"http://127.0.0.1:{port}/topic").send_image("Motion", "cam1", img, "s.jpg")
    assert cap['body'] == img and cap['title'] == "Motion"  # ntfy sends raw image body
    assert cap['click'] is None
    NtfyNotifier(f"http://127.0.0.1:{port}/topic").send_image("Motion", "cam1", img, "s.jpg", "https://h/?play=1&cam=cam1")
    assert cap['click'] == "https://h/?play=1&cam=cam1"   # tapping the push opens the event
    WebhookNotifier(f"http://127.0.0.1:{port}/wh").send_image("Motion", "cam1", img, "s.jpg", "https://h/?play=1&cam=cam1")
    assert b"cam1\\nhttps://h/?play=1&cam=cam1" in cap['body']
    srv.shutdown()


def test_observer_flags_homenvr_memory(tmp_path, monkeypatch):
    """2026-10-27: homenvr grew to 9.8 GB and stalled the whole server before the kernel
    killed it. The watchdog now alerts once its own memory passes alerts.memory_alert_mb."""
    import app.alerts as alerts
    def observe_with(rss_mb, limit=None):
        raw = {"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}],
               "retention": {"min_free_gb": 0}}
        if limit is not None:
            raw["alerts"] = {"memory_alert_mb": limit}
        cfg = parse(raw)
        monkeypatch.setattr(alerts, "process_rss_mb", lambda: rss_mb)
        storage = Storage(tmp_path)
        return {c.key: c for c in alerts.build_observer(cfg, Supervisor(cfg, storage), storage)(time.time())}

    assert parse({"storage_dir": str(tmp_path)}).alerts.memory_alert_mb == 1500
    checks = observe_with(400)
    assert checks["homenvr memory"].ok is True
    checks = observe_with(2100)
    assert checks["homenvr memory"].ok is False and "2100 MB" in checks["homenvr memory"].message
    assert "homenvr memory" not in observe_with(2100, limit=0)     # 0 = off
    assert "homenvr memory" not in observe_with(None)              # not measurable (not Linux)


def test_process_rss_mb_reads_this_process():
    from app.alerts import process_rss_mb
    mb = process_rss_mb()
    assert mb is None or 5 < mb < 100000
