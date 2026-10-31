"""Stabilization math (A2) — smoothing + inversion of tracked transforms.

Stabilize = apply the INVERSE of the smoothed camera motion to the
footage.  Both steps are pure functions over the tracker's per-frame
transform entries so the Color page and the Composite page can share
them, and so they are trivially testable.

Transform convention matches ``types.TrackTransform``: translation in
source pixels, rotation in degrees, per-axis multiplicative scale.  The
full affine is  M = T · R · S  (column-vector convention).
"""

from typing import Dict, List, Tuple

import numpy as np

# Scale below this is treated as degenerate and clamped (plan A2: 除零保护).
_MIN_SCALE = 1e-6


def smooth_series(values: List[Tuple[int, float]],
                  window: int) -> List[Tuple[int, float]]:
    """Centered moving average over (frame, value) pairs.

    Frames must be sorted; the window truncates at the ends (partial
    windows are averaged over whatever is available).  A window ≤ 1 or
    an empty input returns the series unchanged.  Window membership is
    by FRAME DISTANCE, not list index: failed frames punch gaps in the
    series and an index window would smear across unrelated times.
    """
    if window <= 1 or not values:
        return list(values)
    half = window // 2
    out: List[Tuple[int, float]] = []
    for i, (frame, value) in enumerate(values):
        lo = i
        while lo - 1 >= 0 and abs(values[lo - 1][0] - frame) <= half:
            lo -= 1
        hi = i + 1
        while hi < len(values) and abs(values[hi][0] - frame) <= half:
            hi += 1
        acc = 0.0
        for j in range(lo, hi):
            acc += values[j][1]
        out.append((frame, acc / (hi - lo)))
    return out


def smooth_entry(entry: Dict[str, float], window: int) -> Dict[str, float]:
    """Smooth one scalar field dict {frame: value} (see smooth_series)."""
    items = sorted(((int(f), float(v)) for f, v in entry.items()))
    smoothed = smooth_series(items, window)
    return {str(f): v for f, v in smoothed}


def unwrap_degrees(values: List[Tuple[int, float]]) -> List[Tuple[int, float]]:
    """Unwrap a degree series across the ±180° seam.

    Tracked rotation comes from atan2, so a shot that keeps rotating past
    180° produces 179°, -179°, -178°… — averaging or interpolating those
    raw values yields ~0° nonsense.  After unwrapping the series is
    continuous (may exceed ±180°, which transform keyframes accept).
    """
    if len(values) < 2:
        return list(values)
    rads = np.deg2rad(np.asarray([v for _, v in values], dtype=np.float64))
    unwrapped = np.rad2deg(np.unwrap(rads))
    return [(f, float(u)) for (f, _), u in zip(values, unwrapped)]


_unwrap_degrees = unwrap_degrees  # private alias used internally


def affine_matrix(tx: float, ty: float, rotation_deg: float,
                  sx: float, sy: float) -> "np.ndarray":
    """2x3 affine M = T · R · S from tracked components."""
    theta = np.deg2rad(rotation_deg)
    cos, sin = float(np.cos(theta)), float(np.sin(theta))
    r = np.array([[cos, -sin], [sin, cos]], dtype=np.float64)
    s = np.array([[max(abs(sx), _MIN_SCALE) * (1 if sx >= 0 else -1), 0.0],
                  [0.0, max(abs(sy), _MIN_SCALE) * (1 if sy >= 0 else -1)]],
                 dtype=np.float64)
    m = r @ s
    return np.array([[m[0, 0], m[0, 1], float(tx)],
                     [m[1, 0], m[1, 1], float(ty)]], dtype=np.float64)


def decompose_affine(m: "np.ndarray") -> Tuple[float, float, float, float, float]:
    """Extract (tx, ty, rotation_deg, sx, sy) from a 2x3 affine."""
    tx, ty = float(m[0, 2]), float(m[1, 2])
    a, b = float(m[0, 0]), float(m[1, 0])
    sx = float(np.hypot(a, b))
    rotation = float(np.degrees(np.arctan2(b, a)))
    # second column length minus the rotation-coupled shear component
    c, d = float(m[0, 1]), float(m[1, 1])
    sy = float(c * -np.sin(np.deg2rad(rotation)) + d * np.cos(np.deg2rad(rotation)))
    # Preserve the flip sign via the determinant: abs() here would turn a
    # mirrored matrix into a rotation and silently invert the stabilization
    # direction (affine_matrix composes the sign back into S).
    det = a * d - b * c
    if det < 0:
        sy = -abs(sy)
    else:
        sy = abs(sy)
    return tx, ty, rotation, sx, max(abs(sy), _MIN_SCALE) * (1 if sy >= 0 else -1)


def invert_entry(entry: Dict[str, float]) -> Dict[str, float]:
    """Inverse of one tracked transform (with degenerate-scale clamp).

    Composes M = T·R·S from the entry, returns the same component schema
    for M⁻¹.
    """
    m = affine_matrix(
        float(entry.get("tx", 0.0)),
        float(entry.get("ty", 0.0)),
        float(entry.get("rotation", 0.0)),
        float(entry.get("scale_x", 1.0)),
        float(entry.get("scale_y", 1.0)),
    )
    # Invert the 2x3 affine: [A | t] → [A⁻¹ | -A⁻¹ t]
    a = m[:, :2]
    t = m[:, 2:3]
    a_inv = np.linalg.inv(a)
    m_inv = np.concatenate([a_inv, -a_inv @ t], axis=1)
    tx, ty, rotation, sx, sy = decompose_affine(m_inv)
    return {"tx": tx, "ty": ty, "rotation": rotation,
            "scale_x": sx, "scale_y": sy}


def stabilize_transforms(transforms: Dict[str, Dict[str, float]],
                         window: int = 15,
                         include_rotation_scale: bool = False,
                         ) -> Dict[str, Dict[str, float]]:
    """Produce stabilizing transforms: smoothed motion, inverted.

    Args:
        transforms: tracker schema {frame_str: entry}.
        window: smoothing window in frames (1 disables smoothing).
        include_rotation_scale: also smooth+invert rotation/scale
            (plan A2: "position 为主，可选包含 scale/rotate").

    Returns:
        {frame_str: {tx, ty, rotation?, scale_x?, scale_y?}} — only the
        selected channels; feed these to a transform node's keyframes.
        Frames are re-keyed to integers sorted ascending; invalid frames
        (status != valid) must be filtered by the caller before calling.
    """
    frames = sorted(int(f) for f in transforms.keys())
    if not frames:
        return {}

    def _field(name: str) -> List[Tuple[int, float]]:
        # NaN/inf entries (dropped NCC matches, corrupt JSON) would poison
        # the whole smoothing window and blow up linalg.inv — sanitize.
        return [(f, float(np.nan_to_num(
            float(transforms[str(f)].get(name, 0.0)),
            nan=0.0, posinf=0.0, neginf=0.0))) for f in frames]

    tx = smooth_series(_field("tx"), window)
    ty = smooth_series(_field("ty"), window)
    smoothed = {
        "tx": dict(tx), "ty": dict(ty),
    }
    if include_rotation_scale:
        # atan2-derived rotation wraps at ±180° — smooth across the seam
        # on the unwrapped series or 179°/-179° average into ~0°.
        smoothed["rotation"] = dict(smooth_series(
            _unwrap_degrees(_field("rotation")), window))
        smoothed["scale_x"] = dict(smooth_series(_field("scale_x"), window))
        smoothed["scale_y"] = dict(smooth_series(_field("scale_y"), window))

    out: Dict[str, Dict[str, float]] = {}
    for f in frames:
        entry = {"tx": smoothed["tx"][f], "ty": smoothed["ty"][f]}
        if include_rotation_scale:
            entry["rotation"] = smoothed["rotation"][f]
            entry["scale_x"] = smoothed["scale_x"][f]
            entry["scale_y"] = smoothed["scale_y"][f]
        inv = invert_entry(entry)
        if include_rotation_scale:
            out[str(f)] = inv
        else:
            # translation-only stabilization: don't touch rotation/scale
            out[str(f)] = {"tx": inv["tx"], "ty": inv["ty"]}
    return out
