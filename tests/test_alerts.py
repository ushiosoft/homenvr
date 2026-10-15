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
