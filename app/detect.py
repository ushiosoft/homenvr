"""On-server object detection to label motion events as person / vehicle.

Uses OpenCV's DNN with MobileNet-SSD (Caffe, Pascal VOC classes) — light enough
to run per motion event on CPU, no GPU or heavy ML stack. Degrades gracefully to
no labels if the model or OpenCV is unavailable.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

VOC = ["background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car",
       "cat", "chair", "cow", "diningtable", "dog", "horse", "motorbike", "person",
       "pottedplant", "sheep", "sofa", "train", "tvmonitor"]
PERSON = {"person"}
VEHICLE = {"car", "bus", "motorbike", "bicycle", "train"}

MODEL_DIR = os.environ.get("HOMENVR_MODEL_DIR", "/models")


class ObjectDetector:
    def __init__(self, model_dir: str | Path = MODEL_DIR, confidence: float = 0.45):
        self.confidence = confidence
        self.ok = False
        self._cv2 = None
        self._np = None
        self._net = None
        d = Path(model_dir)
        proto = d / "MobileNetSSD_deploy.prototxt"
        weights = d / "MobileNetSSD_deploy.caffemodel"
        try:
            import cv2
            import numpy as np
            if not (proto.is_file() and weights.is_file()):
                log.warning("detector: model files not found in %s; detection disabled", d)
                return
            self._cv2, self._np = cv2, np
            self._net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
            self.ok = True
            log.info("detector: loaded MobileNet-SSD from %s", d)
        except Exception as e:  # opencv missing / build without Caffe / corrupt model
            log.warning("detector unavailable: %s", e)

    def detect(self, jpeg: bytes) -> list[str]:
        """Return the categories present ('person' and/or 'vehicle'), highest-conf first."""
        if not self.ok:
            return []
        cv2, np = self._cv2, self._np
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []
        blob = cv2.dnn.blobFromImage(cv2.resize(img, (300, 300)), 0.007843, (300, 300), 127.5)
        self._net.setInput(blob)
        det = self._net.forward()
        best = {"person": 0.0, "vehicle": 0.0}
        for i in range(det.shape[2]):
            conf = float(det[0, 0, i, 2])
            if conf < self.confidence:
                continue
            cls = VOC[int(det[0, 0, i, 1])]
            cat = "person" if cls in PERSON else "vehicle" if cls in VEHICLE else None
            if cat:
                best[cat] = max(best[cat], conf)
        return [c for c in ("person", "vehicle") if best[c] > 0]
