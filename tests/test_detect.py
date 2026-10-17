from app.detect import ObjectDetector


def test_detector_graceful_without_model(tmp_path):
    # No model files present -> detector disabled, detect() returns [] (never crashes).
    det = ObjectDetector(model_dir=tmp_path)
    assert det.ok is False
    assert det.detect(b"not an image") == []


def test_categories_mapping():
    from app.detect import COCO_CAT, VOC_CAT
    assert VOC_CAT["person"] == "person" and VOC_CAT["car"] == VOC_CAT["motorbike"] == "vehicle"
    assert COCO_CAT[0] == "person" and {COCO_CAT[i] for i in (1, 2, 3, 5, 7)} == {"vehicle"}
    # Any animal is one label: bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe.
    assert {COCO_CAT[i] for i in range(14, 24)} == {"animal"} and VOC_CAT["dog"] == VOC_CAT["bird"] == "animal"


def _frames(n=8, w=160, h=90, mover_from=None):
    """Synthetic burst: a static bright block ("parked car") at the left, plus a
    block that moves right across the frame ("person") from frame mover_from on."""
    import cv2
    import numpy as np
    out = []
    for k in range(n):
        img = np.full((h, w, 3), 40, np.uint8)
        img[50:80, 5:40] = 200                                   # static object
        if mover_from is not None and k >= mover_from:
            x = 60 + (k - mover_from) * 10
            img[20:70, x:x + 15] = 230                           # moving object
        out.append(cv2.imencode(".jpg", img)[1].tobytes())
    return out


class FakeDet:
    """ObjectDetector with boxes() stubbed: always 'sees' the static car, and the
    person wherever the bright moving block is."""
    def __new__(cls):
        import cv2
        import numpy as np
        from app.detect import ObjectDetector
        d = ObjectDetector.__new__(ObjectDetector)
        d.ok, d.confidence, d._cv2, d._np, d.backend = True, 0.45, cv2, np, "fake"

        def boxes(img):
            out = [("vehicle", 0.9, (5, 50, 40, 80))]
            cols = np.where(img[30, 45:, 0] > 215)[0]
            if len(cols):
                x = 45 + int(cols[0])
                out.append(("person", 0.8, (x, 20, x + 15, 70)))
            return out
        d.boxes = boxes
        return d


def test_burst_ignores_static_objects():
    det = FakeDet()
    scores, labels = det.analyze_burst(_frames(mover_from=None))
    assert labels == [] and all(s["vehicle"] == 0 for s in scores)   # parked car never moves
    scores, labels = det.analyze_burst(_frames(mover_from=2))
    assert labels == ["person"]                                       # the walker counts, the car still doesn't
    assert sum(s["person"] > 0 for s in scores) >= 2


def test_static_vehicle_ignored_even_when_its_pixels_change():
    """Shadows crossing a parked car change its pixels; it must still not count."""
    import cv2
    import numpy as np
    det = FakeDet()
    frames = []
    for k in range(8):
        img = np.full((90, 160, 3), 40, np.uint8)
        img[50:80, 5:40] = 200 if k % 2 else 120     # "shadow" flicker on the static car
        frames.append(cv2.imencode(".jpg", img)[1].tobytes())
    _scores, labels = det.analyze_burst(frames)
    assert "vehicle" not in labels


def test_burst_needs_two_frames():
    from app import detect
    det = FakeDet()
    scores, labels = det.analyze_burst(_frames(n=8, mover_from=7))   # appears in the last frame only
    assert sum(s["person"] > 0 for s in scores) <= 1 and labels == []
    assert detect.MIN_FRAMES == 2


def test_should_push_policy():
    from app.recorder import should_push
    assert should_push("motion", []) and should_push("motion", ["vehicle"])
    assert should_push("person", ["person"]) and not should_push("person", ["vehicle"]) and not should_push("person", [])
    assert should_push("person_vehicle", ["vehicle"]) and not should_push("person_vehicle", [])
    assert should_push("person,animal", ["animal"]) and not should_push("person,animal", ["vehicle"])
    assert should_push("person,vehicle,animal", ["animal"]) and not should_push("person_vehicle", ["animal"])


def test_split_jpegs():
    from app.recorder import split_jpegs
    a, b = b"\xff\xd8AAA\xff\x00x\xff\xd9", b"\xff\xd8BB\xff\xd9"
    assert split_jpegs(a + b + b"\xff\xd8trunc") == [a, b]
    assert split_jpegs(b"") == []


def test_pick_frame_prefers_person_then_vehicle_then_trigger():
    from app.recorder import pick_frame
    f = [b"0", b"1", b"2", b"3", b"4", b"5"]
    none = {"person": 0.0, "vehicle": 0.0}
    sc = [none, {"person": 0.0, "vehicle": 0.9}, {"person": 0.5, "vehicle": 0.0},
          {"person": 0.8, "vehicle": 0.0}, none, none]
    assert pick_frame(f, sc) == (3, ["person", "vehicle"])          # most confident person
    sc2 = [none, {"person": 0.0, "vehicle": 0.6}, none, none, none, none]
    assert pick_frame(f, sc2) == (1, ["vehicle"])
    assert pick_frame(f, [none] * 6) == (3, [])                      # ~the trigger moment


def test_yolox_garbage_output_does_not_fail():
    """Regression: a garbage network output overflowed exp() -> inf -> int() raised
    and the whole motion event lost its label, thumbnail and push."""
    import numpy as np
    import pytest
    cv2 = pytest.importorskip("cv2")
    from app import detect
    d = object.__new__(detect.ObjectDetector)
    import threading
    d._cv2, d._np, d.confidence, d._lock = cv2, np, 0.45, threading.Lock()
    d._grid = detect.ObjectDetector._yolox_grid(np)
    n = len(d._grid[0])
    o = np.zeros((n, 85), np.float32)
    o[0, 4], o[0, 5], o[0, 2:4] = 1.0, 0.9, 500.0        # person with an absurd size
    o[1, 4], o[1, 5], o[1, 0] = 1.0, 0.9, np.nan        # person at NaN

    class Net:
        def setInput(self, _): pass
        def forward(self): return o[None]
    d._net = Net()
    out = d._yolox(np.zeros((360, 640, 3), np.uint8))
    assert [c for c, _s, _b in out] == ["person"]


def test_burst_boxes_show_labels_and_unexplained_motion():
    det = FakeDet()
    scores, labels, boxes = det.analyze(_frames(mover_from=2))
    assert labels == ["person"] and len(boxes) == 8
    kinds = [{b[0] for b in fb} for fb in boxes]
    assert any("person" in k for k in kinds)
    assert not any("vehicle" in k for k in kinds)            # the parked car never shows
    for fb in boxes:
        for kind, x1, y1, x2, y2 in fb:
            assert 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1
    # Motion that no detection explains is kept as a "motion" box.
    import cv2
    import numpy as np
    from app.detect import ObjectDetector
    d = ObjectDetector.__new__(ObjectDetector)
    d.ok, d.confidence, d._cv2, d._np, d.backend = True, 0.45, cv2, np, "fake"
    d.boxes = lambda img: []
    _s, labels, boxes = d.analyze(_frames(mover_from=2))
    assert labels == [] and any(fb and fb[0][0] == "motion" for fb in boxes)


def test_draw_boxes_changes_the_image():
    import cv2
    import numpy as np
    from app.detect import draw_boxes
    jpg = cv2.imencode(".jpg", np.full((90, 160, 3), 40, np.uint8))[1].tobytes()
    assert draw_boxes(jpg, []) == jpg
    out = cv2.imdecode(np.frombuffer(draw_boxes(jpg, [["animal", 0.2, 0.2, 0.6, 0.8]]), np.uint8), 1)
    assert out[int(0.2 * 90) + 1, int(0.4 * 160)].tolist() != [40, 40, 40]


def test_fixed_flicker_is_not_motion():
    """The camera's clock overlay changes every second at the same place: it must not
    show as a motion box in every frame, while a mover still does."""
    from app.detect import drop_fixtures
    clock = (220, 20, 240, 35)
    blobs = [[clock] + ([(10 * k, 100, 10 * k + 12, 112)] if k in (3, 5) else []) for k in range(10)]
    out = drop_fixtures(blobs)
    assert all(clock not in fb for fb in out)
    assert out[3] == [(30, 100, 42, 112)] and out[5] == [(50, 100, 62, 112)]
    # A small fixture showing in only ~2/3 of the frames (the clock's digits) goes too;
    # a large area at the same place that often (swaying foliage) is kept.
    leaf = (0, 0, 100, 80)
    blobs = [([clock] if k % 3 else []) + ([leaf] if k % 3 else []) for k in range(12)]
    out = drop_fixtures(blobs, frame_area=640 * 360)
    assert all(clock not in fb for fb in out) and sum(leaf in fb for fb in out) == 8


def test_nms_one_box_per_object():
    import cv2
    from app.detect import nms
    dets = [("person", 0.9, (10, 10, 50, 100)), ("person", 0.7, (12, 12, 52, 98)),
            ("person", 0.6, (200, 10, 240, 100)), ("vehicle", 0.8, (11, 11, 51, 99))]
    out = nms(dets, cv2)
    assert sorted(out) == sorted([("person", 0.9, (10, 10, 50, 100)), ("person", 0.6, (200, 10, 240, 100)),
                                  ("vehicle", 0.8, (11, 11, 51, 99))])
    assert nms([], cv2) == []


def test_detector_serializes_network_calls():
    """Several cameras analyse bursts in parallel threads on one network: calls must
    not overlap (an OpenCV Net is not thread-safe)."""
    import threading, time
    import numpy as np
    from app import detect
    d = object.__new__(detect.ObjectDetector)
    import cv2
    d._cv2, d._np, d.confidence, d._lock = cv2, np, 0.45, threading.Lock()
    d._grid = detect.ObjectDetector._yolox_grid(np)
    state = {"in": 0, "overlap": False}

    class Net:
        def setInput(self, _):
            state["in"] += 1
            state["overlap"] |= state["in"] > 1
            time.sleep(0.01)
        def forward(self):
            state["in"] -= 1
            return np.zeros((1, len(d._grid[0]), 85), np.float32)
    d._net = Net()
    ts = [threading.Thread(target=d._yolox, args=(np.zeros((360, 640, 3), np.uint8),)) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not state["overlap"]
