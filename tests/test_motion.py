from app.config import parse
from app.events import EventStore
from app.motion import MotionDetector, motion_cmd


def test_event_store_add_query_prune(tmp_path):
    s = EventStore(tmp_path / "events.db")
    s.add("cam1", 1000.0, "motion", 0.3)
    s.add("cam1", 1100.0, "motion", 0.5)
    s.add("cam2", 1050.0, "motion", 0.2)
    assert len(s.query("cam1", 0, 2000)) == 2
    assert len(s.query("cam1", 1050, 2000)) == 1          # start inclusive-ish window
    assert len(s.query("cam2", 0, 2000)) == 1
    assert s.latest("cam1") == 1100.0
    assert s.prune(1050) == 1                              # removes the ts=1000 event
    assert len(s.query("cam1", 0, 2000)) == 1


def test_motion_debounce(tmp_path):
    cfg = parse({
        "storage_dir": str(tmp_path),
        "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}],
        "motion": {"enabled": True, "min_gap_seconds": 10},
    })
    store = EventStore(tmp_path / "e.db")
    md = MotionDetector(cfg.cameras[0], cfg, store)

    assert md._record(0.4, now=1000) is True     # first detection -> event
    assert md._record(0.9, now=1003) is False    # within 10s gap -> merged, no new event
    assert md._record(0.9, now=1008) is False
    assert md._record(0.5, now=1011) is True     # >10s later -> new event
    assert md.events == 2
    assert len(store.query("cam1", 0, 9999)) == 2


def test_motion_cmd_uses_sub_stream_and_diff_filter(tmp_path):
    cfg = parse({
        "nvr": {"host": "10.0.0.5", "password": "p"},
        "cameras": [{"id": "cam1", "channel": 1}],
        "motion": {"enabled": True, "sensitivity": 3.0},
    })
    cmd = motion_cmd(cfg.cameras[0], cfg)
    joined = " ".join(cmd)
    assert "Channels/102" in joined                  # sub stream
    assert "tblend=all_mode=difference" in joined     # frame differencing
    assert "signalstats" in joined and "metadata=print" in joined
