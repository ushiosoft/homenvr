"""Incident clustering + noise folding, on data shaped like a real day: a noisy
backyard camera (tree shadows) logging weak events every ~15 s for hours, while
the other cameras see occasional real activity."""
import random

from fastapi.testclient import TestClient

from app import incidents as inc
from app.config import parse
from app.events import EventStore
from app.main import create_app
from app.storage import Storage
from tests.test_storage import make

THR = {"cam1": 4.0, "cam2": 4.0, "cam3": 4.0}
NAMES = {"cam1": "Garage", "cam2": "Main gate", "cam3": "Backyard"}
T0 = 1792000000.0


class Ev:
    def __init__(self):
        self.rows, self.next_id = [], 1

    def add(self, cam, ts, score=5.0, label=""):
        self.rows.append({"id": self.next_id, "camera": cam, "ts": ts, "score": score, "label": label})
        self.next_id += 1


def noisy_day():
    rnd = random.Random(7)
    ev = Ev()
    # Backyard: 2 h of shadows, an event every 10-25 s, weak scores (4-7 vs threshold 4).
    t = T0
    while t < T0 + 7200:
        ev.add("cam3", t, rnd.uniform(4.0, 7.0))
        t += rnd.uniform(10, 25)
    # Meanwhile a real visitor: Garage then Main gate, labeled person.
    ev.add("cam1", T0 + 3000, 30.0, "person")
    ev.add("cam2", T0 + 3040, 12.0, "person")
    # Later, a car at the gate after the shadows stopped.
    ev.add("cam2", T0 + 9000, 25.0, "vehicle")
    # A lone weak blip on the Garage: probably not an incident.
    ev.add("cam1", T0 + 12000, 4.3)
    return ev.rows


def test_noisy_camera_is_folded_not_chained():
    out = inc.build(noisy_day(), THR, NAMES)
    kinds = [i["kind"] for i in out["items"]]
    assert kinds.count("busy") == 1
    busy = next(i for i in out["items"] if i["kind"] == "busy")
    assert busy["camera"] == "cam3" and busy["n"] > 300
    assert busy["end"] - busy["start"] > 6000
    # Plain 90 s clustering would have made one 2-hour incident; now the visitor is its own.
    visitor = next(i for i in out["items"] if "person" in i["labels"])
    assert visitor["cameras"] == ["cam1", "cam2"]
    assert visitor["path"] == "Garage → Main gate"
    assert visitor["end"] - visitor["start"] < 120
    assert not visitor["likely_noise"]
    # Every item is short except the busy card.
    assert all(i["end"] - i["start"] < 600 for i in out["items"] if i["kind"] == "incident")


def test_newest_first_and_counts():
    out = inc.build(noisy_day(), THR, NAMES)
    starts = [i["start"] for i in out["items"]]
    assert starts == sorted(starts, reverse=True)
    assert out["counts"] == {"all": 3, "person": 1, "vehicle": 1, "animal": 0}
    assert out["events_per_camera"]["cam3"] == out["folded_events"]


def test_likely_noise_flag():
    out = inc.build(noisy_day(), THR, NAMES)
    blip = out["items"][0]  # newest: the lone weak Garage event
    assert blip["cameras"] == ["cam1"] and blip["likely_noise"]
    car = next(i for i in out["items"] if "vehicle" in i["labels"])
    assert not car["likely_noise"]  # labeled events are never noise


def test_filter_hides_busy_and_unlabeled():
    out = inc.build(noisy_day(), THR, NAMES, "person")
    assert [i["labels"] for i in out["items"]] == [["person"]]
    assert out["counts"]["all"] == 3  # counts don't depend on the active filter


def test_short_weak_runs_are_not_folded():
    ev = Ev()
    for k in range(6):  # a minute of weak motion: a normal incident, not a busy period
        ev.add("cam3", T0 + k * 10, 4.5)
    out = inc.build(ev.rows, THR, NAMES)
    assert [i["kind"] for i in out["items"]] == ["incident"]
    assert out["items"][0]["likely_noise"]


def test_thumbnail_prefers_labeled_then_strongest():
    ev = Ev()
    ev.add("cam1", T0, 40.0)
    ev.add("cam1", T0 + 5, 6.0, "person")
    ev.add("cam2", T0 + 8, 9.0)
    ev.add("cam2", T0 + 9, 15.0)
    item = inc.build(ev.rows, THR, NAMES)["items"][0]
    assert {t["camera"]: t["event_id"] for t in item["thumbs"]} == {"cam1": 2, "cam2": 4}


# ---- API ----

def app_client(tmp_path):
    cfg = parse({"storage_dir": str(tmp_path / "rec"), "live_dir": str(tmp_path / "live"),
                 "segment_seconds": 600,
                 "cameras": [{"id": "cam1", "name": "Garage", "record_url": "r", "live_url": "l"},
                             {"id": "cam3", "name": "Backyard", "record_url": "r", "live_url": "l"}]})
    return cfg, TestClient(create_app(cfg, run_recorders=False))


def test_incidents_and_dismiss_api(tmp_path):
    cfg, c = app_client(tmp_path)
    store = EventStore(cfg.events_db)
    a = store.add("cam1", T0, "motion", 30.0)
    store.add("cam1", T0 + 20, "motion", 30.0)
    b = store.add("cam3", T0 + 1000, "motion", 4.2)
    with c:
        r = c.get("/api/incidents", params={"start": T0 - 10, "end": T0 + 2000}).json()
        assert [i["n"] for i in r["items"]] == [1, 2]
        assert r["items"][0]["likely_noise"] and r["items"][0]["path"] == "Backyard"
        assert r["thresholds"] == {"cam1": 4.0, "cam3": 4.0}
        assert c.post("/api/incidents/dismiss", json={"event_ids": [b]}).json() == {"updated": 1}
        r = c.get("/api/incidents", params={"start": T0 - 10, "end": T0 + 2000}).json()
        assert [i["id"] for i in r["items"]] == [f"i{a}"]
        assert c.get("/api/incidents", params={"start": 0, "end": 1, "filter": "cats"}).status_code == 422


def test_timeline_api(tmp_path):
    cfg, c = app_client(tmp_path)
    day = T0 - (T0 % 86400)
    make(tmp_path / "rec", "cam1", day + 600, 600)
    make(tmp_path / "rec", "cam1", day + 1200, 600)   # contiguous with the first
    make(tmp_path / "rec", "cam1", day + 7200, 600)   # after a gap
    store = EventStore(cfg.events_db)
    store.add("cam1", day + 700, "motion", 9.0)
    eid = store.add("cam1", day + 1900, "motion", 9.0)
    store.set_label(eid, "person")
    with c:
        r = c.get("/api/timeline", params={"start": day, "end": day + 86400}).json()
        lane = r["cameras"][0]
        assert len(lane["counts"]) == 48 and lane["counts"][0] == 1 and lane["counts"][1] == 1
        assert lane["person"][1] == 1 and lane["total"] == 2
        assert lane["coverage"] == [[day + 600, day + 1800], [day + 7200, day + 7800]]
        assert [m[1] for m in lane["events"]] == ["motion", "person"]
        assert r["cameras"][1]["coverage"] == []
        assert c.get("/api/timeline", params={"start": day, "end": day}).status_code == 400


def test_event_thumb_served_from_disk(tmp_path):
    cfg, c = app_client(tmp_path)
    store = EventStore(cfg.events_db)
    eid = store.add("cam1", T0, "motion", 9.0)
    p = Storage(cfg.storage_dir).thumb_path("cam1", eid)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xd8jpeg")
    with c:
        r = c.get(f"/api/events/{eid}/thumb")
        assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
        assert c.get("/api/events/999/thumb").status_code == 404
        # thumbs/ is not mistaken for a camera directory
        assert "thumbs" not in Storage(cfg.storage_dir).cameras_on_disk()


def test_long_chain_is_split_at_pauses():
    """Real shape from 19:26-19:32: steady activity 2-66 s apart chained into one
    6-minute incident, hiding the 19:30:30 activity that triggered a push."""
    ev = Ev()
    offsets = [0, 45, 80, 86, 103, 141, 176, 188, 254, 256, 291, 300, 359]
    for k, off in enumerate(offsets):
        ev.add(["cam1", "cam3", "cam2"][k % 3], T0 + off, 30.0)
    items = inc.build(ev.rows, THR, NAMES)["items"]
    assert all(i["end"] - i["start"] <= inc.MAX_SPAN for i in items)
    assert sum(i["n"] for i in items) == len(offsets)
    pushed = T0 + 254  # the moment the notification was about starts its own card
    assert any(i["start"] == pushed for i in items)


def test_continuous_activity_stays_one_incident():
    ev = Ev()
    for k in range(40):  # every 5 s for 200 s: no pause worth splitting at
        ev.add("cam1", T0 + k * 5, 30.0, "person")
    items = inc.build(ev.rows, THR, NAMES)["items"]
    assert len(items) == 1 and items[0]["n"] == 40
