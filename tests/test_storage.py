import os
import time

from app.storage import Storage, format_stamp, parse_stamp


def make(storage_root, cam, start, dur, size=1000):
    d = storage_root / cam
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{format_stamp(start)}.mp4"
    p.write_bytes(b"x" * size)
    os.utime(p, (start + dur, start + dur))
    return p


def test_stamp_roundtrip():
    t = 1760500000.0
    assert parse_stamp(format_stamp(t) + ".mp4") == t
    assert parse_stamp("garbage.mp4") is None


def test_segments_filter_and_order(tmp_path):
    s = Storage(tmp_path)
    base = 1760500000
    for i in (2, 0, 1):
        make(tmp_path, "cam1", base + i * 600, 600)
    (tmp_path / "cam1" / "notes.txt").write_text("ignore")
    segs = s.segments("cam1")
    assert [x.start for x in segs] == [base, base + 600, base + 1200]
    assert segs[0].duration == 600
    # overlap query: [base+700, base+800) only hits the second segment
    assert [x.start for x in s.segments("cam1", base + 700, base + 800)] == [base + 600]
    assert s.segments("nope") == []


def test_expire_by_age_protects_newest(tmp_path):
    # Expiry moves to grace (not delete); the newest per camera is protected.
    s = Storage(tmp_path)
    now = time.time()
    old = make(tmp_path, "cam1", now - 10 * 86400, 600)
    newest_but_old = make(tmp_path, "cam2", now - 10 * 86400, 600)  # sole seg -> protected
    keep = make(tmp_path, "cam1", now - 600, 600)
    moved = s.expire(max_gb=0, max_days=5, now=now)
    assert moved == [tmp_path / "grace" / "cam1" / old.name]
    assert not old.exists() and keep.exists() and newest_but_old.exists()


def test_expire_by_size(tmp_path):
    s = Storage(tmp_path)
    now = time.time()
    paths = [make(tmp_path, "cam1", now - (10 - i) * 600, 600, size=400 * 1024**2) for i in range(5)]
    # 5 * 400 MiB = ~1.95 GiB active; cap at 1 GiB -> move the 3 oldest to grace
    moved = s.expire(max_gb=1, max_days=0, now=now)
    assert [p.name for p in moved] == [p.name for p in paths[:3]]
    assert len(s.grace_segments()) == 3


def test_resolve_rejects_traversal(tmp_path):
    s = Storage(tmp_path)
    p = make(tmp_path, "cam1", 1760500000, 600)
    assert s.resolve("cam1", p.name) == p
    assert s.resolve("cam1", "../cam1/" + p.name) is None
    assert s.resolve("cam1", "x.mp4") is None


def test_concat_list_trims(tmp_path):
    s = Storage(tmp_path)
    base = 1760500000
    make(tmp_path, "cam1", base, 600)
    make(tmp_path, "cam1", base + 600, 600)
    script = s.concat_list("cam1", base + 500, base + 700)
    assert script.count("file '") == 2
    assert "inpoint 500.000" in script and "outpoint 100.000" in script


def test_span_fast_first_last(tmp_path):
    s = Storage(tmp_path)
    base = 1760500000
    make(tmp_path, "cam1", base, 600)
    make(tmp_path, "cam1", base + 1200, 600)
    first, last = s.span("cam1")
    assert first == base
    assert last == base + 1200 + 600       # newest start + its duration (from mtime)
    assert s.span("empty") == (None, None)
