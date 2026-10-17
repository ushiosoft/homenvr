"""Incidents: cluster raw motion events (all cameras) into what a person would call
"something happened", and fold camera noise so it can't swallow the day.

Pure functions over plain event dicts ({id, camera, ts, score, label}) so they are
easy to test against real data shapes.

Why folding matters: a noisy camera (tree shadows) logs an event every ~10-20 s for
hours. Plain gap clustering chains all of that — plus anything any other camera
saw meanwhile — into one multi-hour "incident". So before clustering we pull out
long runs of weak, unlabeled, single-camera motion into "busy period" cards, and
cluster only what remains.
"""
from __future__ import annotations

GAP = 90.0             # events closer than this (any camera) belong to one incident
WEAK_FACTOR = 2.0      # unlabeled event with score < WEAK_FACTOR * threshold is "weak"
MAX_SPAN = 120         # an incident longer than this is split at its largest pauses
MIN_SPLIT_GAP = 10     # ... but never at a pause shorter than this
BUSY_CHAIN_GAP = 300   # weak events of one camera this close form a candidate busy run
BUSY_MIN_EVENTS = 12   # a weak single-camera run this long ...
BUSY_MIN_SECONDS = 600  # ... over at least this long is folded as a busy period
BUSY_MERGE_GAP = 1200  # busy runs of one camera closer than this merge into one card


def labels_of(e: dict) -> list[str]:
    return [x for x in (e.get("label") or "").split(",") if x]


def is_weak(e: dict, thresholds: dict[str, float]) -> bool:
    if labels_of(e):
        return False
    thr = thresholds.get(e["camera"], 4.0) or 4.0
    return float(e.get("score") or 0) < WEAK_FACTOR * thr


def _chains(events: list[dict], gap: float) -> list[list[dict]]:
    """Split time-sorted events into runs where consecutive events are <= gap apart."""
    out: list[list[dict]] = []
    for e in events:
        if out and e["ts"] - out[-1][-1]["ts"] <= gap:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def split_long(chain: list[dict]) -> list[list[dict]]:
    """Split a chain longer than MAX_SPAN at its largest internal pause, recursively.

    Gap clustering alone chains steady activity (a car arriving, then someone
    walking around, then a camera blinking) into one multi-minute incident whose
    card shows only its start time — a push at 19:31 then lands inside a
    "19:26" card and looks missing. Splitting at the natural pauses keeps each
    card short enough that its time range says when things happened.
    """
    if len(chain) < 2 or chain[-1]["ts"] - chain[0]["ts"] <= MAX_SPAN:
        return [chain]
    gaps = [(chain[i + 1]["ts"] - chain[i]["ts"], i) for i in range(len(chain) - 1)]
    gap, i = max(gaps)
    if gap < MIN_SPLIT_GAP:
        return [chain]  # continuous activity: genuinely one long incident
    return split_long(chain[:i + 1]) + split_long(chain[i + 1:])


def find_busy(events: list[dict], thresholds: dict[str, float]) -> list[list[dict]]:
    """Per camera, long runs of weak motion (merged when close). Returns event lists."""
    by_cam: dict[str, list[dict]] = {}
    for e in events:
        if is_weak(e, thresholds):
            by_cam.setdefault(e["camera"], []).append(e)
    periods: list[list[dict]] = []
    for cam_events in by_cam.values():
        runs = [r for r in _chains(cam_events, BUSY_CHAIN_GAP)
                if len(r) >= BUSY_MIN_EVENTS and r[-1]["ts"] - r[0]["ts"] >= BUSY_MIN_SECONDS]
        merged: list[list[dict]] = []
        for r in runs:
            if merged and r[0]["ts"] - merged[-1][-1]["ts"] <= BUSY_MERGE_GAP:
                # Absorb the weak events of this camera that fell between the two runs.
                lo, hi = merged[-1][-1]["ts"], r[0]["ts"]
                merged[-1].extend(e for e in cam_events if lo < e["ts"] < hi)
                merged[-1].extend(r)
            else:
                merged.append(list(r))
        periods.extend(merged)
    return periods


def _summary(events: list[dict], names: dict[str, str]) -> dict:
    cams: list[str] = []
    counts: dict[str, int] = {}
    best: dict[str, dict] = {}
    labels: list[str] = []
    for e in events:
        c = e["camera"]
        if c not in counts:
            cams.append(c)
            counts[c] = 0
        counts[c] += 1
        # Thumbnail per camera: prefer a labeled event, then the strongest motion.
        key = (bool(labels_of(e)), float(e.get("score") or 0))
        b = best.get(c)
        if b is None or key > (bool(labels_of(b)), float(b.get("score") or 0)):
            best[c] = e
        for lb in labels_of(e):
            if lb not in labels:
                labels.append(lb)
    return {
        "start": events[0]["ts"],
        "end": events[-1]["ts"],
        "n": len(events),
        "cameras": cams,
        "counts": counts,
        "path": " → ".join(names.get(c, c) for c in cams),
        "labels": labels,
        "thumbs": [{"camera": c, "event_id": best[c]["id"], "ts": best[c]["ts"]} for c in cams],
        "max_score": max(float(e.get("score") or 0) for e in events),
    }


def build(events: list[dict], thresholds: dict[str, float], names: dict[str, str] | None = None,
          label_filter: str = "all") -> dict:
    """Cluster events into incidents + busy periods, newest first.

    label_filter: "all", "person", "vehicle" or "animal". Busy periods only appear under "all"
    (they are unlabeled by definition). Counts are always for all three filters.
    """
    names = names or {}
    events = sorted(events, key=lambda e: e["ts"])
    busy = find_busy(events, thresholds)
    folded = {e["id"] for p in busy for e in p}
    rest = [e for e in events if e["id"] not in folded]

    incidents: list[dict] = []
    for chain in (part for c in _chains(rest, GAP) for part in split_long(c)):
        s = _summary(chain, names)
        s["kind"] = "incident"
        s["id"] = f"i{chain[0]['id']}"
        s["likely_noise"] = len(s["cameras"]) == 1 and all(is_weak(e, thresholds) for e in chain)
        s["events"] = chain
        incidents.append(s)

    busy_items: list[dict] = []
    for p in busy:
        p.sort(key=lambda e: e["ts"])
        s = _summary(p, names)
        s["kind"] = "busy"
        s["id"] = f"b{p[0]['id']}"
        s["camera"] = p[0]["camera"]
        s["likely_noise"] = True
        s["events"] = p
        # The burst structure inside the period, for "Expand N events".
        s["bursts"] = [{"start": c[0]["ts"], "end": c[-1]["ts"], "n": len(c),
                        "event_id": max(c, key=lambda e: float(e.get("score") or 0))["id"]}
                       for c in _chains(p, GAP)]
        busy_items.append(s)

    counts = {"all": len(incidents),
              "person": sum("person" in i["labels"] for i in incidents),
              "vehicle": sum("vehicle" in i["labels"] for i in incidents),
              "animal": sum("animal" in i["labels"] for i in incidents)}
    if label_filter in ("person", "vehicle", "animal"):
        items = [i for i in incidents if label_filter in i["labels"]]
    else:
        items = incidents + busy_items
    items.sort(key=lambda i: i["start"], reverse=True)
    per_cam: dict[str, int] = {}
    for e in events:
        per_cam[e["camera"]] = per_cam.get(e["camera"], 0) + 1
    return {"items": items, "counts": counts, "total_events": len(events),
            "events_per_camera": per_cam, "folded_events": len(folded)}
