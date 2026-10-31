"""Face workflow geometry core (C3) — landmarks → skin mask.

Pure geometry, no model inference: landmark detection itself stays in the
detector (model files are NOT bundled and are never downloaded silently).
This module turns a landmark point set into a feathered SKIN MASK using
preset polygon index tables over a 468-point face-mesh topology
(MediaPipe FaceMesh compatible; PFLD 98-point sets map through the same
tables via the coarse subset indices).

Conventions:
* landmarks are NORMALIZED uv (x, y in 0..1), one tuple per point
* polygon indices reference the landmark list; CHEEK/FOREHEAD/NOSE
  polygons form the skin region, EYE/BROW/LIP polygons are EXCLUDED
* the mask is polygon-rasterized then Gaussian-feathered
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# ── Preset polygon index tables (468-point face mesh subsets) ──────────
# Coarse, editable tables: enough coverage for skin masking while staying
# legible.  Indices reference the normalized landmark list supplied by the
# detector; missing indices are skipped defensively.
SKIN_POLYGONS: Dict[str, List[int]] = {
    # Left cheek: eye-bottom to jaw, in front of the ear.
    "cheek_left": [116, 117, 118, 119, 120, 121, 128, 123, 116],
    # Right cheek (mirrored).
    "cheek_right": [345, 346, 347, 348, 349, 350, 357, 352, 345],
    # Forehead band between brows and hairline estimate.
    "forehead": [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361,
                 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149,
                 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103,
                 67, 109, 10],
    # Nose bridge/tip (skin, between the eyes and above the lips).
    "nose": [168, 6, 197, 195, 5, 4, 45, 220, 115, 131, 49, 102, 168],
}
EXCLUSION_POLYGONS: Dict[str, List[int]] = {
    "eye_left": [33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154,
                 153, 145, 144, 163, 7, 33],
    "eye_right": [362, 398, 384, 385, 386, 387, 388, 466, 263, 249, 390,
                  373, 374, 380, 381, 382, 362],
    "brow_left": [70, 63, 105, 66, 107, 55, 65, 52, 53, 46, 70],
    "brow_right": [300, 293, 334, 296, 336, 285, 295, 282, 283, 276,
                   300],
    "lips_outer": [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291, 375,
                   321, 405, 314, 17, 84, 181, 91, 146, 61],
}


def _polygon_mask(polygons: Sequence[Sequence[int]],
                  landmarks: Sequence[Tuple[float, float]],
                  width: int, height: int) -> "np.ndarray":
    if cv2 is None:
        raise RuntimeError("OpenCV required for polygon rasterization")
    mask = np.zeros((int(height), int(width)), dtype=np.uint8)
    points: List["np.ndarray"] = []
    for indices in polygons:
        pts = []
        for index in indices:
            if 0 <= int(index) < len(landmarks):
                x, y = landmarks[int(index)]
                pts.append([float(x) * width, float(y) * height])
        if len(pts) >= 3:
            points.append(np.asarray(pts, dtype=np.int32))
    if points:
        cv2.fillPoly(mask, points, 255)
    return mask


def build_skin_mask(landmarks: Sequence[Tuple[float, float]],
                    width: int, height: int,
                    feather_px: float = 6.0) -> "np.ndarray":
    """Feathered float32 skin mask (1.0 = skin, 0.0 = excluded)."""
    if cv2 is None:
        raise RuntimeError("OpenCV required for skin mask rasterization")
    skin = _polygon_mask(SKIN_POLYGONS.values(), landmarks, width,
                         height).astype(np.float32) / 255.0
    exclusions = _polygon_mask(EXCLUSION_POLYGONS.values(), landmarks,
                               width, height).astype(np.float32) / 255.0
    if feather_px > 0.0:
        ksize = int(max(3, round(float(feather_px) * 2) | 1))
        # Feather BOTH masks, then carve: blurring only the skin mask
        # would let skin values bleed back across the hard exclusion and
        # re-activate eyes/lips inside their own polygon centres.
        skin = cv2.GaussianBlur(skin, (ksize, ksize), 0)
        exclusions = cv2.GaussianBlur(exclusions, (ksize, ksize), 0)
    return np.clip(skin * (1.0 - exclusions), 0.0, 1.0).astype(np.float32)


def serialize_landmarks(
        tracks: Dict[int, Sequence[Tuple[float, float]]],
        confidences: Optional[Dict[int, float]] = None) -> str:
    """JSON: {frame: [[x, y, confidence], ...]} — savable, no re-run."""
    import json
    payload: Dict[str, List[List[float]]] = {}
    for frame, points in sorted(tracks.items()):
        conf = (confidences or {}).get(frame, 1.0)
        payload[str(int(frame))] = [
            [float(x), float(y), float(conf)] for x, y in points]
    return json.dumps(payload, ensure_ascii=False)


def deserialize_landmarks(data: str) -> Dict[int, List[Tuple[float, float]]]:
    """Inverse of :func:`serialize_landmarks` (tolerant of bad JSON)."""
    import json
    try:
        payload = json.loads(data or "{}")
    except (TypeError, ValueError):
        return {}
    tracks: Dict[int, List[Tuple[float, float]]] = {}
    if not isinstance(payload, dict):
        return tracks
    for key, points in payload.items():
        try:
            frame = int(key)
        except (TypeError, ValueError):
            continue
        parsed = []
        for point in points or []:
            try:
                parsed.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError, IndexError):
                continue
        tracks[frame] = parsed
    return tracks


def landmarks_at(tracks: Dict[int, List[Tuple[float, float]]],
                 frame: int) -> Optional[List[Tuple[float, float]]]:
    """Exact-frame lookup, else nearest stored frame (deterministic)."""
    if frame in tracks:
        return tracks[int(frame)]
    if not tracks:
        return None
    nearest = min(tracks, key=lambda stored: abs(stored - int(frame)))
    return tracks[nearest]
