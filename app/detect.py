"""On-server object detection to label motion events as person / vehicle / animal.

YOLOX-tiny (Megvii, Apache-2.0, COCO classes) through OpenCV's DNN module on CPU
(no PyTorch), ~65 ms per frame. On recorded footage it found a person walking
through the frame in ~3x as many frames as MobileNet-SSD (the earlier model:
brief and partial appearances especially), at a similar false alarm rate.

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

# COCO class index -> category (0 person; 1 bicycle, 2 car, 3 motorcycle, 5 bus, 7 truck;
# 14-23 bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe). Animals are
# one category on purpose: what matters is "an animal", not which (COCO has no
# rabbit, fox or deer; those tend to come out as one of these).
COCO_CAT = {0: "person", 1: "vehicle", 2: "vehicle", 3: "vehicle", 5: "vehicle", 7: "vehicle",
            **{i: "animal" for i in range(14, 24)}}
CATS = ("person", "vehicle", "animal")
# Moving areas with no detection are kept as "motion" boxes when at least this
# fraction of the frame (0.05 %: ~115 px at 640x360, a small animal at a distance).
BLOB_MIN_FRAC = 0.0005
MAX_BLOBS = 6
# Animals score lower than people (dogs walking past the driveway: 0.3-0.5), so they
# have their own threshold; the moving / 2-frame / not-static rules still apply.
CONF_BY_CAT = {"animal": 0.3}

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
# Validated on 443 events: a backyard camera's false vehicles 63 -> 0, people 55/56 kept.
# Animals too: the first day of animal labels on that camera were all fixtures with
# shadows moving over them (the covered BBQ, the stair railing); an animal that
# matters moves across the picture.
STATIC_CATS = {"vehicle", "animal"}
STATIC_IOU = 0.7


class ObjectDetector:
    def __init__(self, model_dir: str | Path = MODEL_DIR, confidence: float = 0.45,
                 backend: str = "auto"):
        self.confidence = confidence
        self.ok = False
        self.backend = None
        # One network, called from worker threads for several cameras at once; an
        # OpenCV dnn Net is not thread-safe (interleaved setInput/forward produced
        # garbage: dozens of phantom "vehicles", once an exp() overflow).
        import threading
        self._lock = threading.Lock()
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
        try:
            if backend in ("auto", "yolox") and yolox.is_file():
                self._net, self.backend = cv2.dnn.readNetFromONNX(str(yolox)), "yolox"
                self._grid = self._yolox_grid(np)
            else:
                log.warning("detector: no model files in %s; detection disabled", d)
                return
            self.ok = True
            log.info("detector: %s loaded from %s", self.backend, d)
        except Exception as e:  # build without the DNN module / corrupt model
            log.warning("detector unavailable: %s", e)

    # ---- single image -------------------------------------------------------

    def detect(self, jpeg: bytes, zones=None) -> list[str]:
        """Return the categories present ('person' and/or 'vehicle'), highest-conf first."""
        best = self.scores(jpeg, zones)
        return [c for c in sorted(best, key=best.get, reverse=True) if best[c] > 0]

    def scores(self, jpeg: bytes, zones=None) -> dict[str, float]:
        """Best confidence per category ({'person': 0.82, 'vehicle': 0.0}); 0 = not seen."""
        best = {c: 0.0 for c in CATS}
        img = self._decode(jpeg)
        if img is None:
            return best
        for cat, conf, _box in self.kept_boxes(img, zones):
            best[cat] = max(best[cat], conf)
        return best

    def boxes(self, img) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        """Detections above the confidence threshold: (category, conf, (x1, y1, x2, y2))."""
        if not self.ok or img is None:
            return []
        return self._yolox(img)

    def _conf(self, cat: str) -> float:
        return CONF_BY_CAT.get(cat, self.confidence)

    def kept_boxes(self, img, zones=None) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        """boxes() minus detections standing in one of the camera's ignore zones."""
        found = self.boxes(img)
        if zones and found:
            from .zones import ignored
            h, w = img.shape[:2]
            found = [d for d in found if not ignored(d[2], w, h, zones)]
        return found

    # ---- motion event burst ---------------------------------------------------

    def analyze_burst(self, jpegs: list[bytes], zones=None) -> tuple[list[dict[str, float]], list[str]]:
        scores, labels, _boxes, _fixtures = self.analyze(jpegs, zones)
        return scores, labels

    def analyze(self, jpegs: list[bytes], zones=None, fixtures=None):
        """analyze_burst() plus, per frame, the boxes worth showing: the detections that
        counted ([category, x1, y1, x2, y2], 0-1 of the frame) and the moving areas no
        detection explains (["motion", ...]) — what to look at when judging the event.

        `fixtures`: 0-1 boxes known to flicker in place on this camera (its clock),
        learned from earlier bursts; small moving areas there are dropped. Returns
        (scores, labels, boxes, fixtures found in this burst)."""
        """Per-frame best *moving* confidence per category, plus the event's labels.

        A detection counts only if its box overlaps pixels that changed against the
        frames about 1 s earlier/later (2 frames apart at 2 fps): parked cars and
        furniture never move, people and arriving cars do. A category becomes a
        label only if it counts in at least MIN_FRAMES frames (one-frame blips on
        shadows are the usual false alarms).
        """
        empty = {c: 0.0 for c in CATS}
        if not self.ok:
            return [dict(empty) for _ in jpegs], [], [[] for _ in jpegs], []
        imgs = [self._decode(j) for j in jpegs]
        masks = motion_masks(imgs, self._cv2, self._np)
        dets = [self.kept_boxes(img, zones) for img in imgs]
        scores, counted = [], []
        for k, (mask, frame_dets) in enumerate(zip(masks, dets)):
            best, kept = dict(empty), []
            for cat, conf, box in frame_dets:
                if cat in STATIC_CATS and never_moved(k, box, cat, dets):
                    continue
                if mask is None or moving_fraction(mask, box) >= MOVE_FRAC:
                    best[cat] = max(best[cat], conf)
                    kept.append((cat, box))
            scores.append(best)
            counted.append(kept)
        labels = [c for c in CATS if sum(s[c] > 0 for s in scores) >= MIN_FRAMES]
        # Boxes for unexplained motion use pixels that changed against both neighbours:
        # where something *is*, not also where it was a second ago (ghost boxes).
        now_masks = motion_masks(imgs, self._cv2, self._np, both=True)
        blobs = [moving_blobs(mask, self._cv2, self._np, [b for _c, b in kept], zones)
                 for mask, kept in zip(now_masks, counted)]
        first = next((i for i in imgs if i is not None), None)
        area = first.shape[0] * first.shape[1] if first is not None else None
        kept_blobs = drop_fixtures(blobs, area)
        found = []   # this burst's fixtures, for the caller to remember
        if first is not None:
            fh, fw = first.shape[:2]
            for fb, kb in zip(blobs, kept_blobs):
                for b in fb:
                    nb = _norm(b, fw, fh)
                    if b not in kb and not any(iou(nb, f) >= 0.3 for f in found):
                        found.append(nb)
        boxes = []
        for img, kept, fblobs in zip(imgs, counted, kept_blobs):
            if img is None:
                boxes.append([])
                continue
            h, w = img.shape[:2]
            # Only labels that held up over the burst; a one-frame blip shows as motion.
            fb = [[cat, *_norm(box, w, h)] for cat, box in kept if cat in labels]
            for b in fblobs:
                nb = _norm(b, w, h)
                small = area and (b[2] - b[0]) * (b[3] - b[1]) < FIXTURE_SMALL * area
                if small and any(iou(nb, f) >= 0.3 for f in fixtures or []):
                    continue
                fb.append(["motion", *nb])
            boxes.append(fb)
        return scores, labels, boxes, found

    # ---- backends -------------------------------------------------------------

    def _decode(self, jpeg: bytes):
        if self._cv2 is None:
            return None
        return self._cv2.imdecode(self._np.frombuffer(jpeg, self._np.uint8), self._cv2.IMREAD_COLOR)

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
        blob = cv2.dnn.blobFromImage(canvas, 1.0, (YOLOX_SIZE, YOLOX_SIZE), swapRB=False)
        with self._lock:
            self._net.setInput(blob)
            o = self._net.forward()[0]                  # (N, 85): cx, cy, w, h (raw), obj, 80 classes
        idx = sorted(COCO_CAT)
        cls = o[:, 4:5] * o[:, [5 + i for i in idx]]    # objectness x class score
        best = cls.max(1)
        keep = np.where(best >= min(self.confidence, *CONF_BY_CAT.values()))[0]
        if not len(keep):
            return []
        grid, stride = self._grid
        xy = (o[keep, :2] + grid[keep]) * stride[keep]
        # Clamp the log-size: a garbage output (from concurrent use of the network,
        # before the lock above) overflowed exp() to inf and failed the whole event.
        wh = np.exp(np.clip(o[keep, 2:4], -10, 8)) * stride[keep]
        out = []
        for k, (cx, cy), (bw, bh) in zip(keep, xy, wh):
            if not np.isfinite((cx, cy, bw, bh)).all():
                continue
            c = int(cls[k].argmax())
            if float(cls[k, c]) < self._conf(COCO_CAT[idx[c]]):
                continue
            out.append((COCO_CAT[idx[c]], float(cls[k, c]),
                        (int((cx - bw / 2) / r), int((cy - bh / 2) / r), int((cx + bw / 2) / r), int((cy + bh / 2) / r))))
        return nms(out, cv2)


def nms(dets: list, cv2, iou_max: float = 0.45) -> list:
    """One box per object: YOLOX proposes several overlapping boxes for each (it has
    no suppression built in). Per category, keep the most confident of each cluster."""
    out = []
    for cat in {d[0] for d in dets}:
        group = [d for d in dets if d[0] == cat]
        rects = [[b[0], b[1], b[2] - b[0], b[3] - b[1]] for _c, _s, b in group]
        keep = cv2.dnn.NMSBoxes(rects, [s for _c, s, _b in group], 0.0, iou_max)
        out += [group[int(i)] for i in (keep.flatten() if hasattr(keep, "flatten") else keep)]
    return sorted(out, key=lambda d: -d[1])


def motion_masks(imgs, cv2, np, gap: int = 2, both: bool = False):
    """For each frame, a 0/1 mask of pixels that changed against the frames `gap`
    before and after it: either side, or with `both` the two sides (at the burst's
    ends, the one side there is). None where it can't be computed."""
    grey = [cv2.GaussianBlur(cv2.cvtColor(i, cv2.COLOR_BGR2GRAY), (5, 5), 0) if i is not None else None
            for i in imgs]
    masks = []
    for k, g in enumerate(grey):
        if g is None:
            masks.append(None)
            continue
        sides = [(cv2.absdiff(g, grey[j]) >= DIFF_LEVEL).astype(np.uint8) for j in (k - gap, k + gap)
                 if 0 <= j < len(grey) and grey[j] is not None and grey[j].shape == g.shape]
        m = np.zeros(g.shape, np.uint8)
        if both and len(sides) == 2:
            m = sides[0] & sides[1]
        else:
            for d in sides:
                m |= d
        masks.append(cv2.dilate(m, np.ones((5, 5), np.uint8)) if m.any() or len(grey) > 1 else None)
    return masks


def _norm(box, w: int, h: int) -> list[float]:
    x1, y1, x2, y2 = box
    return [round(min(max(x1 / w, 0.0), 1.0), 3), round(min(max(y1 / h, 0.0), 1.0), 3),
            round(min(max(x2 / w, 0.0), 1.0), 3), round(min(max(y2 / h, 0.0), 1.0), 3)]


def moving_blobs(mask, cv2, np, explained, zones=None) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of the largest changed areas in a frame's motion mask, minus those
    whose centre is inside a detection (already shown) or in an ignore zone."""
    if mask is None:
        return []
    h, w = mask.shape
    n, _lab, st, _cen = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in sorted(range(1, n), key=lambda i: -st[i, 4]):
        x, y, bw, bh, area = (int(v) for v in st[i])
        if area < BLOB_MIN_FRAC * w * h or len(out) >= MAX_BLOBS:
            break
        cx, cy = x + bw / 2, y + bh / 2
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in explained):
            continue
        if zones:
            from .zones import inside, motion_polys
            if any(inside(cx / w, cy / h, z) for z in motion_polys(zones)):
                continue
        out.append((x, y, x + bw, y + bh))
    return out


# A moving area at the same place in this share of the burst's frames is a fixture;
# small ones (the clock's digits: under FIXTURE_SMALL of the frame) already at half.
FIXTURE_FRAC = 0.8
FIXTURE_FRAC_SMALL = 0.5
FIXTURE_SMALL = 0.005


def drop_fixtures(blobs: list[list], frame_area: float | None = None) -> list[list]:
    """Remove moving areas that sit at the same place in much of the burst: the
    camera's own clock overlay (its seconds change every second, but a small change
    doesn't always make a big enough area, so it shows in ~60-70 % of frames) and
    similar flicker. A passing animal or person moves on, so it never qualifies."""
    n = len(blobs)
    if n < 4:
        return blobs
    out = []
    for k, fb in enumerate(blobs):
        keep = []
        for b in fb:
            same = sum(any(iou(b, o) >= 0.5 for o in other) for j, other in enumerate(blobs) if j != k)
            small = frame_area and (b[2] - b[0]) * (b[3] - b[1]) < FIXTURE_SMALL * frame_area
            if same < (FIXTURE_FRAC_SMALL if small else FIXTURE_FRAC) * (n - 1):
                keep.append(b)
        out.append(keep)
    return out


# BGR colours matching the UI (--person, --vehicle, --animal, --motion).
BOX_COLOURS = {"person": (128, 222, 74), "vehicle": (36, 191, 251), "animal": (250, 139, 192),
               "motion": (184, 163, 148)}


def draw_boxes(jpeg: bytes, boxes: list) -> bytes:
    """The frame with its boxes drawn in (for thumbnails and push photos)."""
    if not boxes:
        return jpeg
    try:
        import cv2
        import numpy as np
    except ImportError:
        return jpeg
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    h, w = img.shape[:2]
    t = max(1, round(w / 320))
    for kind, x1, y1, x2, y2 in sorted(boxes, key=lambda b: b[0] != "motion"):   # labels on top
        p1, p2 = (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h))
        colour = BOX_COLOURS.get(kind, BOX_COLOURS["motion"])
        cv2.rectangle(img, p1, p2, colour, t if kind == "motion" else t + 1)
        if kind != "motion":
            cv2.putText(img, kind, (p1[0], max(12, p1[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4 * t, colour, t)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else jpeg


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
