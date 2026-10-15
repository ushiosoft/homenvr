import time
from pathlib import Path

from app.storage import GRACE_DIR, SAVED_DIR, Storage
from tests.test_storage import make


def test_expire_moves_old_to_grace_not_deleted(tmp_path):
    s = Storage(tmp_path)
    now = time.time()
    old = make(tmp_path, "cam1", now - 10 * 86400, 600)
    keep = make(tmp_path, "cam1", now - 600, 600)   # newest, protected
    moved = s.expire(max_gb=0, max_days=5, now=now)
    assert len(moved) == 1
    assert not old.exists()                          # moved, not in place
    assert (tmp_path / GRACE_DIR / "cam1" / old.name).exists()  # now in grace
    assert keep.exists()                             # newest protected
    # expired data still counts on disk (grace), and is listed as grace
    assert len(s.grace_segments()) == 1


def test_expire_by_size(tmp_path):
    s = Storage(tmp_path)
    now = time.time()
    segs = [make(tmp_path, "cam1", now - (10 - i) * 600, 600, size=400 * 1024**2) for i in range(5)]
    moved = s.expire(max_gb=1, max_days=0, now=now)   # ~1.95GB active, cap 1GB
    assert len(moved) == 3                            # oldest 3 moved to grace
    assert len(s.grace_segments()) == 3


def test_reclaim_only_under_pressure(tmp_path, monkeypatch):
    s = Storage(tmp_path)
    now = time.time()
    make(tmp_path, "cam1", now - 10 * 86400, 600)
    make(tmp_path, "cam1", now - 600, 600)   # newest, protected from expiry
    s.expire(max_gb=0, max_days=5, now=now)
    assert len(s.grace_segments()) == 1

    import shutil
    # Plenty of free space -> no deletion
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"total": 100e9, "used": 0, "free": 100e9}))
    assert s.reclaim(min_free_gb=10) == []
    assert len(s.grace_segments()) == 1

    # Under pressure -> delete grace
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"total": 100e9, "used": 99e9, "free": 1e9}))
    deleted = s.reclaim(min_free_gb=10, headroom_gb=5)
    assert len(deleted) == 1
    assert len(s.grace_segments()) == 0


def test_saved_never_touched(tmp_path):
    s = Storage(tmp_path)
    saved = s.saved_dir() / "keep-me.mp4"
    saved.write_bytes(b"x" * 1000)
    now = time.time()
    make(tmp_path, "cam1", now - 10 * 86400, 600)
    s.expire(max_gb=0, max_days=5, now=now)
    import shutil
    # Even under extreme pressure, reclaim only deletes grace, never saved/
    deleted = s.reclaim(min_free_gb=10_000_000, headroom_gb=1)  # impossible target
    assert saved.exists()
    assert all(SAVED_DIR not in str(p) for p in deleted)
    assert s.saved_bytes() == 1000


def test_concat_spans_active_and_grace(tmp_path):
    s = Storage(tmp_path)
    base = 1760500000
    make(tmp_path, "cam1", base, 600)                # active
    g = make(tmp_path, "cam1", base + 600, 600)      # will move to grace
    (tmp_path / GRACE_DIR / "cam1").mkdir(parents=True, exist_ok=True)
    import os
    os.replace(g, tmp_path / GRACE_DIR / "cam1" / g.name)
    script = s.concat_list("cam1", base, base + 1200)
    assert script.count("file '") == 2               # both active + grace included
