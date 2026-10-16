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
