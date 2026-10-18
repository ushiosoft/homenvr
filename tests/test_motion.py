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
    assert s.prune(1050) == [("cam1", 1)]                  # removes the ts=1000 event
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


def test_event_during_analysis_is_queued_not_dropped(tmp_path):
    """While one event's burst is analysed, the newest following event runs next
    (intermediate ones coalesce into it) instead of being skipped."""
    import asyncio
    from app.recorder import Supervisor
    from app.storage import Storage
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}],
                 "motion": {"enabled": True}})
    sup = Supervisor(cfg, Storage(tmp_path))
    seen = []

    async def fake_capture(cam, ts, detect):
        seen.append(ts)
        await asyncio.sleep(0.05)
        return b"\xff\xd8x\xff\xd9", [], None
    sup._capture_event = fake_capture

    async def run():
        first = asyncio.create_task(sup._motion_alert("cam1", 100.0, 5.0, 1))
        await asyncio.sleep(0.01)
        await sup._motion_alert("cam1", 105.0, 5.0, 2)   # busy -> queued
        await sup._motion_alert("cam1", 110.0, 5.0, 3)   # replaces the queued one
        await first
        for _ in range(20):                              # let the follow-up run
            await asyncio.sleep(0.02)
    asyncio.run(run())
    assert seen == [100.0, 110.0]
    assert (tmp_path / "thumbs" / "cam1" / "3.jpg").exists()


def test_single_frame_spike_is_not_motion(tmp_path):
    """Main gate's exposure steps: one frame at 5.2-8.7 between near-zero frames, ~200
    an hour. Only CONFIRM_FRAMES frames in a row make an event."""
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}],
                 "motion": {"enabled": True, "sensitivity": 4.0, "min_gap_seconds": 8}})
    md = MotionDetector(cfg.cameras[0], cfg, EventStore(tmp_path / "e.db"))
    t = 1000.0
    for v in (0.02, 5.21, 0.03, 6.94, 0.01, 8.68, 0.02):     # isolated spikes
        assert md._sample(v, now=t) is False
        t += 0.2
    assert md.events == 0
    assert md._sample(4.5, now=t) is False                    # motion: first frame...
    assert md._sample(5.0, now=t + 0.2) is True               # ...second in a row -> event
    assert md._sample(6.0, now=t + 0.4) is False              # same burst: merged (min gap)


def test_analyzer_output_lines_make_events(tmp_path, caplog):
    """Regression: the line handler referenced a removed variable, so every event
    crashed the analyzer (it restarted, blind for a few seconds, ~125 times a night)."""
    import logging
    caplog.set_level(logging.DEBUG)
    cfg = parse({"storage_dir": str(tmp_path), "cameras": [{"id": "cam1", "record_url": "r", "live_url": "l"}],
                 "motion": {"enabled": True, "sensitivity": 1.0, "min_gap_seconds": 8}})
    md = MotionDetector(cfg.cameras[0], cfg, EventStore(tmp_path / "e.db"))
    line = lambda pct: f"[Parsed_metadata_6 @ 0x1] lavfi.signalstats.YAVG={pct * 255 / 100:.4f}\n".encode()
    assert md._line(b"frame=  10 fps=5.0\n", now=1000) is False
    assert md._line(line(2.0), now=1000) is False          # first frame over 1 %
    assert md._line(line(3.0), now=1000.2) is True         # second in a row -> event
    assert md.events == 1 and "3.00 % of the picture" in caplog.text
