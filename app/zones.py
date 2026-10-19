"""Per-camera ignore zones: polygons where motion and/or objects don't count.

Drawn on the Settings page over a snapshot of the camera *as displayed* (after
its rotation), stored as normalized [x, y] points (0-1) in camera.zones.

- Object detection: a person/vehicle is dropped when its ground point (bottom
  centre of the box) lies in a zone — a car driving on the street at the top of
  the picture, or beyond the gate, never becomes a label or a push.
- Motion: the zones are blacked out of the frame difference before it's scored
  (app/motion.py), so movement there makes no events.

Each zone says what it ignores: {"points": [[x, y], ...], "motion": bool, "detect": bool,
"nopush": bool} (a bare list of points is the older form: motion + detect). "nopush"
keeps people/vehicles/animals standing in the zone labelled (boxes, filters) but they
don't make the event push — passers-by on the sidewalk, say; anyone outside such zones
still does (app.recorder.push_labels). Ignoring only
detections suits a street (no vehicle labels or pushes, but someone walking along the
sidewalk, partly in front of it, still triggers); ignoring only motion suits something
that moves where people still stand (a BBQ cover in the wind).
"""
from __future__ import annotations

from pathlib import Path

MAX_ZONES = 8
MAX_POINTS = 32


def points(z) -> list:
    return z["points"] if isinstance(z, dict) else z


def ignores_motion(z) -> bool:
    return z.get("motion", True) if isinstance(z, dict) else True


def ignores_detect(z) -> bool:
    return z.get("detect", True) if isinstance(z, dict) else True


def no_push(z) -> bool:
    return bool(z.get("nopush", False)) if isinstance(z, dict) else False


def nopush_polys(zones) -> list:
    return [points(z) for z in zones or [] if no_push(z)]


def motion_polys(zones) -> list:
    return [points(z) for z in zones or [] if ignores_motion(z)]


def detect_polys(zones) -> list:
    return [points(z) for z in zones or [] if ignores_detect(z)]


def validate(value) -> list[dict]:
    """Coerce/validate a zones value from the settings API (raises ValueError)."""
    if not isinstance(value, list) or len(value) > MAX_ZONES:
        raise ValueError(f"zones must be a list of at most {MAX_ZONES} polygons")
    out = []
    for z in value:
        poly = points(z)
        if not isinstance(poly, list) or not 3 <= len(poly) <= MAX_POINTS:
            raise ValueError(f"a zone needs 3-{MAX_POINTS} points")
        pts = []
        for p in poly:
            x, y = float(p[0]), float(p[1])
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError("zone points must be within 0-1")
            pts.append([round(x, 4), round(y, 4)])
        m, d, n = ignores_motion(z), ignores_detect(z), no_push(z)
        if not (m or d or n):
            raise ValueError("a zone must ignore motion, detections or pushes")
        out.append({"points": pts, "motion": bool(m), "detect": bool(d), "nopush": bool(n)})
    return out


def inside(x: float, y: float, poly) -> bool:
    """Point in polygon (ray casting), normalized coordinates."""
    hit = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def ignored(box, width: int, height: int, zones) -> bool:
    """True if a detection box (pixels, in the displayed frame) stands in a zone."""
    polys = detect_polys(zones)
    if not polys or not width or not height:
        return False
    x1, _y1, x2, y2 = box
    gx, gy = (x1 + x2) / 2 / width, min(y2, height - 1) / height
    return any(inside(gx, gy, z) for z in polys)


def motion_mask(zones, rotation: int, width: int, height: int, path: Path) -> Path:
    """Write a greyscale mask for the motion analyzer: white = analyze, black = a
    zone. `width` x `height` is the analyzed stream (not rotated); zones are in the
    rotated, displayed picture, so they're drawn there and rotated back."""
    import cv2
    import numpy as np
    rot = int(rotation) % 360
    dw, dh = (height, width) if rot in (90, 270) else (width, height)
    m = np.full((dh, dw), 255, np.uint8)
    for z in motion_polys(zones):
        pts = np.array([[round(x * (dw - 1)), round(y * (dh - 1))] for x, y in z], np.int32)
        cv2.fillPoly(m, [pts], 0)
    # Displayed = stream rotated clockwise by `rot`; undo it.
    back = {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_CLOCKWISE}.get(rot)
    if back is not None:
        m = cv2.rotate(m, back)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), m)
    return path
