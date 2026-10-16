"""On-server object detection to label motion events as person / vehicle.

Two backends, both run through OpenCV's DNN module on CPU (no PyTorch):
- YOLOX-tiny (Megvii, Apache-2.0, COCO classes) — the default. On recorded
  footage it found a person walking through the frame in ~3x as many frames as
  MobileNet-SSD (brief and partial appearances especially), at a similar false
  alarm rate, for ~65 ms per frame.
- MobileNet-SSD (Caffe, Pascal VOC classes) — fallback when the YOLOX model
  file is missing.

Static objects are the main source of wrong labels (parked cars beyond a gate,
covered patio furniture read as a "car"), so for a motion event's burst of
frames a detection only counts if its box overlaps pixels that actually changed
— see analyze_burst(). Degrades gracefully to no labels if OpenCV or the model
files are unavailable.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

VOC = ["background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car",
       "cat", "chair", "cow", "diningtable", "dog", "horse", "motorbike", "person",
       "pottedplant", "sheep", "sofa", "train", "tvmonitor"]
VOC_CAT = {"person": "person", "car": "vehicle", "bus": "vehicle", "motorbike": "vehicle",
           "bicycle": "vehicle", "train": "vehicle"}
# COCO class index -> category (0 person; 1 bicycle, 2 car, 3 motorcycle, 5 bus, 7 truck)
COCO_CAT = {0: "person", 1: "vehicle", 2: "vehicle", 3: "vehicle", 5: "vehicle", 7: "vehicle"}
CATS = ("person", "vehicle")

MODEL_DIR = os.environ.get("HOMENVR_MODEL_DIR", "/models")
YOLOX_FILE = "yolox_tiny.onnx"
YOLOX_SIZE = 416

# Burst analysis (frames at 2 fps): a detection counts only if at least MOVE_FRAC of
# its box changed against the frames ~1 s before/after, and a category is only a
# label if it counts in at least MIN_FRAMES frames. Tuned on a day of real events.
MOVE_FRAC = 0.12
DIFF_LEVEL = 25      # grey-level change that counts as "changed" (after a light blur)
MIN_FRAMES = 2
# Vehicles additionally must have moved: if every other detection of it in the
# burst sits at the same place (IoU >= STATIC_IOU), it's a parked car or a
# look-alike (covered patio furniture read as a "car"). Tree shadows crossing it
# change its pixels, so the moving-pixel test alone can't reject it, and such
# detections flicker on and off, so "present in most frames" doesn't catch it
# either. Not applied to people: someone standing at the door should still count.
# Validated on 443 events: Backyard false vehicles 63 -> 0, people 55/56 kept.
STATIC_CATS = {"vehicle"}
STATIC_IOU = 0.7


class ObjectDetector:
    def __init__(self, model_dir: str | Path = MODEL_DIR, confidence: float = 0.45,
                 backend: str = "auto"):
        self.confidence = confidence
        self.ok = False
        self.backend = None
        self._cv2 = self._np = self._net = None
        d = Path(model_dir)
        try:
            import cv2
            import numpy as np
        except Exception as e:
            log.warning("detector unavailable (OpenCV missing): %s", e)
            return
        self._cv2, self._np = cv2, np
        yolox = d / YOLOX_FILE
        proto, weights = d / "MobileNetSSD_deploy.prototxt", d / "MobileNetSSD_deploy.caffemodel"
        try:
            if backend in ("auto", "yolox") and yolox.is_file():
                self._net, self.backend = cv2.dnn.readNetFromONNX(str(yolox)), "yolox"
                self._grid = self._yolox_grid(np)
            elif backend in ("auto", "mobilenet") and proto.is_file() and weights.is_file():
                self._net, self.backend = cv2.dnn.readNetFromCaffe(str(proto), str(weights)), "mobilenet"
            else:
                log.warning("detector: no model files in %s; detection disabled", d)
                return
            self.ok = True
            log.info("detector: %s loaded from %s", self.backend, d)
        except Exception as e:  # build without the DNN module / corrupt model
            log.warning("detector unavailable: %s", e)

    # ---- single image -------------------------------------------------------

    def detect(self, jpeg: bytes) -> list[str]:
        """Return the categories present ('person' and/or 'vehicle'), highest-conf first."""
        best = self.scores(jpeg)
        return [c for c in sorted(best, key=best.get, reverse=True) if best[c] > 0]

    def scores(self, jpeg: bytes) -> dict[str, float]:
        """Best confidence per category ({'person': 0.82, 'vehicle': 0.0}); 0 = not seen."""
        best = {c: 0.0 for c in CATS}
        img = self._decode(jpeg)
        if img is None:
            return best
        for cat, conf, _box in self.boxes(img):
            best[cat] = max(best[cat], conf)
        return best

    def boxes(self, img) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        """Detections above the confidence threshold: (category, conf, (x1, y1, x2, y2))."""
        if not self.ok or img is None:
            return []
        return self._yolox(img) if self.backend == "yolox" else self._mobilenet(img)

    # ---- motion event burst ---------------------------------------------------

    def analyze_burst(self, jpegs: list[bytes]) -> tuple[list[dict[str, float]], list[str]]:
        """Per-frame best *moving* confidence per category, plus the event's labels.

        A detection counts only if its box overlaps pixels that changed against the
        frames about 1 s earlier/later (2 frames apart at 2 fps): parked cars and
        furniture never move, people and arriving cars do. A category becomes a
        label only if it counts in at least MIN_FRAMES frames (one-frame blips on
        shadows are the usual false alarms).
        """
        empty = {c: 0.0 for c in CATS}
        if not self.ok:
            return [dict(empty) for _ in jpegs], []
        imgs = [self._decode(j) for j in jpegs]
        masks = motion_masks(imgs, self._cv2, self._np)
        dets = [self.boxes(img) for img in imgs]
        scores = []
        for k, (mask, frame_dets) in enumerate(zip(masks, dets)):
            best = dict(empty)
            for cat, conf, box in frame_dets:
                if cat in STATIC_CATS and never_moved(k, box, cat, dets):
                    continue
                if mask is None or moving_fraction(mask, box) >= MOVE_FRAC:
                    best[cat] = max(best[cat], conf)
            scores.append(best)
        labels = [c for c in CATS if sum(s[c] > 0 for s in scores) >= MIN_FRAMES]
        return scores, labels

    # ---- backends -------------------------------------------------------------

    def _decode(self, jpeg: bytes):
        if self._cv2 is None:
            return None
        return self._cv2.imdecode(self._np.frombuffer(jpeg, self._np.uint8), self._cv2.IMREAD_COLOR)

    def _mobilenet(self, img):
        cv2 = self._cv2
        h, w = img.shape[:2]
        self._net.setInput(cv2.dnn.blobFromImage(cv2.resize(img, (300, 300)), 0.007843, (300, 300), 127.5))
        det = self._net.forward()
        out = []
        for i in range(det.shape[2]):
            conf = float(det[0, 0, i, 2])
            cat = VOC_CAT.get(VOC[int(det[0, 0, i, 1])]) if conf >= self.confidence else None
            if cat:
                x1, y1, x2, y2 = det[0, 0, i, 3:7]
                out.append((cat, conf, (int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h))))
        return out

    @staticmethod
    def _yolox_grid(np):
        grids, strides = [], []
        for s in (8, 16, 32):
            n = YOLOX_SIZE // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)

    def _yolox(self, img):
        cv2, np = self._cv2, self._np
        h, w = img.shape[:2]
        r = min(YOLOX_SIZE / h, YOLOX_SIZE / w)
        canvas = np.full((YOLOX_SIZE, YOLOX_SIZE, 3), 114, np.uint8)  # YOLOX preprocessing:
        canvas[:int(h * r), :int(w * r)] = cv2.resize(img, (int(w * r), int(h * r)))  # BGR 0-255, top-left pad
        self._net.setInput(cv2.dnn.blobFromImage(canvas, 1.0, (YOLOX_SIZE, YOLOX_SIZE), swapRB=False))
        o = self._net.forward()[0]                      # (N, 85): cx, cy, w, h (raw), obj, 80 classes
        idx = sorted(COCO_CAT)
        cls = o[:, 4:5] * o[:, [5 + i for i in idx]]    # objectness x class score
        best = cls.max(1)
        keep = np.where(best >= self.confidence)[0]
        if not len(keep):
            return []
        grid, stride = self._grid
        xy = (o[keep, :2] + grid[keep]) * stride[keep]
        wh = np.exp(o[keep, 2:4]) * stride[keep]
        out = []
        for k, (cx, cy), (bw, bh) in zip(keep, xy, wh):
            c = int(cls[k].argmax())
            out.append((COCO_CAT[idx[c]], float(cls[k, c]),
                        (int((cx - bw / 2) / r), int((cy - bh / 2) / r), int((cx + bw / 2) / r), int((cy + bh / 2) / r))))
        return out


def motion_masks(imgs, cv2, np, gap: int = 2):
    """For each frame, a 0/1 mask of pixels that changed against the frames `gap`
    before and after it (either side). None where it can't be computed."""
    grey = [cv2.GaussianBlur(cv2.cvtColor(i, cv2.COLOR_BGR2GRAY), (5, 5), 0) if i is not None else None
            for i in imgs]
    masks = []
    for k, g in enumerate(grey):
        if g is None:
            masks.append(None)
            continue
        m = np.zeros(g.shape, np.uint8)
        for j in (k - gap, k + gap):
            if 0 <= j < len(grey) and grey[j] is not None and grey[j].shape == g.shape:
                m |= (cv2.absdiff(g, grey[j]) >= DIFF_LEVEL).astype(np.uint8)
        masks.append(cv2.dilate(m, np.ones((5, 5), np.uint8)) if m.any() or len(grey) > 1 else None)
    return masks


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def never_moved(k: int, box, cat, dets) -> bool:
    """True if the object at `box` in frame k is at the same place in every other
    frame where it was detected (and it was detected in at least one other)."""
    others = [b for j, fd in enumerate(dets) if j != k for c, _conf, b in fd if c == cat and iou(box, b) > 0]
    return bool(others) and all(iou(box, b) >= STATIC_IOU for b in others)


def moving_fraction(mask, box) -> float:
    h, w = mask.shape
    x1, y1, x2, y2 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return float(mask[y1:y2, x1:x2].mean())
