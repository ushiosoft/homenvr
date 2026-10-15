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
