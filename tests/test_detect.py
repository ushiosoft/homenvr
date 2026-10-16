from app.detect import ObjectDetector


def test_detector_graceful_without_model(tmp_path):
    # No model files present -> detector disabled, detect() returns [] (never crashes).
    det = ObjectDetector(model_dir=tmp_path)
    assert det.ok is False
    assert det.detect(b"not an image") == []


def test_categories_mapping():
    from app.detect import PERSON, VEHICLE
    assert "person" in PERSON
    assert {"car", "bus", "bicycle", "motorbike"} <= VEHICLE


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
