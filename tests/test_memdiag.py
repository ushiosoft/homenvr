import asyncio
import logging

import app.memdiag as memdiag


def test_memdiag_logs_periodically_and_traces_once_over_limit(monkeypatch, caplog):
    rss = {"mb": 300.0}
    monkeypatch.setattr(memdiag, "process_rss_mb", lambda: rss["mb"])
    monkeypatch.setattr(memdiag, "TRACE_SECONDS", 0.2)

    async def run():
        d = memdiag.MemoryDiag()
        d.check(1500, now=0)
        d.check(1500, now=60)              # within LOG_EVERY: no second line
        d.check(1500, now=700)
        rss["mb"] = 2000.0
        d.check(1500, now=760)             # over: stacks + tracing starts
        assert d._task is not None
        hold = [bytearray(1 << 20) for _ in range(3)]   # allocations made while tracing
        await d._task
        first = d._task
        d.check(1500, now=820)             # still over: no second capture
        assert d._task is first
        rss["mb"] = 400.0
        d.check(1500, now=880)             # back under: re-armed
        return hold

    with caplog.at_level(logging.INFO, logger="app.memdiag"):
        asyncio.run(run())
    text = caplog.text
    assert text.count("memory: homenvr") == 2      # t=0 and t=700 (LOG_EVERY apart)
    assert "2000 MB is over 1500 MB" in text
    assert "asyncio tasks" in text
    assert "largest allocations made meanwhile" in text and "test_memdiag.py" in text
    assert not memdiag.tracemalloc.is_tracing()


def test_memdiag_off_when_limit_zero(monkeypatch, caplog):
    monkeypatch.setattr(memdiag, "process_rss_mb", lambda: 9000.0)
    d = memdiag.MemoryDiag()
    with caplog.at_level(logging.INFO, logger="app.memdiag"):
        d.check(0, now=0)
    assert d._task is None and "over" not in caplog.text
