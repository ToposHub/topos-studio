"""ML-assisted Roto service (A5) — mask → bezier roto keyframes.

Turns a segmentation mask (from ``ai_mask_engine`` or any injectable
detector) into ordinary RotoShape keyframe data:

1. ``cv2.findContours`` → largest external contour
2. Douglas-Peucker simplification with an ADAPTIVE tolerance: the
   tolerance ramps up until the path fits ``max_points`` (16, the RotoShape
   channel cap); if even the coarsest step overshoots, the path is
   decimated uniformly and the caller is told the shape is complex
3. smooth auto-handles (c1/c2) from the finite-difference tangent, so the
   product is an editable ordinary roto shape — no AI metadata survives
   serialization (产物即普通 roto 关键帧)

Temporal propagation walks outward from an anchor frame in ``step``
increments; the injected ``detector(frame, previous_mask)`` supplies each
new mask and an IoA (Intersection-over-Area) confidence against the
previous mask gates the walk: below the threshold the direction stops and
the last trusted frame is reported.  The whole generated range packs into
ONE undoable unit at the call site (the caller wraps the keyframe writes
in its undo stack).

Pure numpy + cv2 — no GL, no network, no model downloads here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

logger = logging.getLogger(__name__)

MAX_POINTS = 16  # RotoShape channel cap

# Detector signature: (frame: int, previous_mask or None) -> mask or None.
# None from the detector means "no usable segmentation this frame".
Detector = Callable[[int, Optional["np.ndarray"]],
                    Optional["np.ndarray"]]


@dataclass
class RotoKeyframe:
    """One frame's roto path (normalized 0..1 coordinates)."""

    frame: int
    points: List[Dict[str, List[float]]]
    ioa: float = 1.0  # confidence vs the previous propagated frame
    simplified_for_point_cap: bool = False

    def to_points_json(self) -> str:
        import json
        return json.dumps(self.points, ensure_ascii=False)


@dataclass
class PropagationResult:
    anchor_frame: int
    keyframes: Dict[int, RotoKeyframe] = field(default_factory=dict)
    last_trusted_forward: int = -1
    last_trusted_backward: int = -1
    stopped_reason: str = ""

    def trusted_range(self) -> Optional[Tuple[int, int]]:
        frames = sorted(self.keyframes)
        if not frames:
            return None
        return frames[0], frames[-1]


def _largest_contour(mask: "np.ndarray") -> Optional["np.ndarray"]:
    if cv2 is None:
        return None
    binary = (np.asarray(mask) > 0.5).astype(np.uint8) * 255
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    return max(contours, key=cv2.contourArea)


def _normalize(contour: "np.ndarray", width: int, height: int
               ) -> List[Tuple[float, float]]:
    pts = [(float(p[0][0]) / max(1, width - 1),
            float(p[0][1]) / max(1, height - 1))
           for p in contour]
    return pts


def _simplify(points: Sequence[Tuple[float, float]],
              tolerance: float) -> List[Tuple[float, float]]:
    if cv2 is None or len(points) < 3:
        return list(points)
    contour = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    approx = cv2.approxPolyDP(contour, tolerance, True)
    return [(float(p[0][0]), float(p[0][1])) for p in approx]


def contour_to_bezier(points: Sequence[Tuple[float, float]],
                      tension: float = 1.0) -> List[Dict[str, List[float]]]:
    """Auto smooth handles from the central-difference tangent.

    c1 is the incoming (previous-side) handle, c2 the outgoing one; both
    sit at one third of the neighbor distance times ``tension``.
    """
    count = len(points)
    result: List[Dict[str, List[float]]] = []
    for index, (x, y) in enumerate(points):
        px, py = points[(index - 1) % count]
        nx, ny = points[(index + 1) % count]
        tx = (nx - px) * (tension / 3.0)
        ty = (ny - py) * (tension / 3.0)
        result.append({
            "pos": [float(x), float(y)],
            "c1": [float(x - tx), float(y - ty)],
            "c2": [float(x + tx), float(y + ty)],
        })
    return result


def mask_to_path(mask: "np.ndarray", max_points: int = MAX_POINTS,
                 start_tolerance: float = 0.004,
                 max_tolerance: float = 0.12
                 ) -> Optional[Tuple[List[Dict[str, List[float]]], bool]]:
    """Mask → bezier path (≤ ``max_points``), or None for empty masks.

    Returns ``(path, decimated)``; ``decimated`` marks shapes where even
    the coarsest simplification overshot the cap and a uniform decimation
    was applied (the caller should suggest splitting the shape).
    """
    if mask is None or cv2 is None:
        return None
    height, width = mask.shape[:2]
    contour = _largest_contour(mask)
    if contour is None:
        return None
    points = _normalize(contour, width, height)
    if len(points) < 3:
        return None

    decimated = False
    # Douglas-Peucker in normalized space: ramp the tolerance until the
    # path fits the point cap.
    tolerance = start_tolerance
    simplified = points
    while True:
        simplified = _simplify(points, tolerance)
        if len(simplified) <= max_points:
            break
        tolerance *= 1.6
        if tolerance > max_tolerance:
            # Coarsest step still overshoots: uniform decimation, bumping
            # the stride until the slice is genuinely within the cap
            # (ceil(len/step) can exceed it by one on awkward lengths).
            step = max(1, len(simplified) // max_points)
            while -(-len(simplified) // step) > max_points:
                step += 1
            simplified = simplified[::step]
            decimated = True
            break

    path = contour_to_bezier(simplified)
    for point in path:
        point["feather"] = 0.0
    return path, decimated


def mask_to_keyframe(mask: "np.ndarray", frame: int,
                     max_points: int = MAX_POINTS,
                     ioa: float = 1.0) -> Optional[RotoKeyframe]:
    built = mask_to_path(mask, max_points)
    if built is None:
        return None
    path, decimated = built
    return RotoKeyframe(frame=int(frame), points=path,
                        ioa=float(ioa), simplified_for_point_cap=decimated)


def intersection_over_area(previous: "np.ndarray",
                           current: "np.ndarray") -> float:
    """IoA: |prev ∩ cur| / |prev| — area-normalized confidence."""
    prev_area = float((np.asarray(previous) > 0.5).sum())
    if prev_area <= 0.0:
        return 0.0
    both = ((np.asarray(previous) > 0.5)
            & (np.asarray(current) > 0.5)).sum()
    return float(both) / prev_area


def propagate(anchor_mask: "np.ndarray", anchor_frame: int,
              detector: Detector, frames: Sequence[int],
              confidence_threshold: float = 0.6,
              max_points: int = MAX_POINTS) -> PropagationResult:
    """Walk outward from the anchor, gating on IoA confidence.

    ``frames`` is the full ordered candidate frame list; frames beyond a
    failed/low-confidence detection stop that direction and are reported
    via ``last_trusted_forward`` / ``last_trusted_backward``.
    """
    result = PropagationResult(anchor_frame=int(anchor_frame))
    ordered = sorted(int(f) for f in frames)
    anchor_index = min(
        range(len(ordered)),
        key=lambda i: abs(ordered[i] - int(anchor_frame))) \
        if ordered else 0

    previous_mask = anchor_mask
    previous_path = mask_to_path(anchor_mask, max_points)
    if previous_path is not None:
        result.keyframes[int(anchor_frame)] = RotoKeyframe(
            frame=int(anchor_frame), points=previous_path[0])
    else:
        result.stopped_reason = "anchor mask produced no path"
        return result

    for index in range(anchor_index + 1, len(ordered)):
        frame = ordered[index]
        mask = detector(frame, previous_mask)
        if mask is None:
            result.stopped_reason = f"no detection at frame {frame}"
            break
        ioa = intersection_over_area(previous_mask, mask)
        if ioa < confidence_threshold:
            result.stopped_reason = (
                f"confidence {ioa:.2f} below {confidence_threshold:.2f} "
                f"at frame {frame}")
            break
        keyframe = mask_to_keyframe(mask, frame, max_points, ioa=ioa)
        if keyframe is None:
            break
        result.keyframes[frame] = keyframe
        result.last_trusted_forward = frame
        previous_mask = mask

    previous_mask = anchor_mask
    for index in range(anchor_index - 1, -1, -1):
        frame = ordered[index]
        mask = detector(frame, previous_mask)
        if mask is None:
            result.stopped_reason = f"no detection at frame {frame}"
            break
        ioa = intersection_over_area(previous_mask, mask)
        if ioa < confidence_threshold:
            result.stopped_reason = (
                f"confidence {ioa:.2f} below {confidence_threshold:.2f} "
                f"at frame {frame}")
            break
        keyframe = mask_to_keyframe(mask, frame, max_points, ioa=ioa)
        if keyframe is None:
            break
        result.keyframes[frame] = keyframe
        result.last_trusted_backward = frame
        previous_mask = mask

    if result.last_trusted_forward < 0:
        result.last_trusted_forward = int(anchor_frame)
    if result.last_trusted_backward < 0:
        result.last_trusted_backward = int(anchor_frame)
    return result


def apply_keyframes(roto_node: Any, keyframes: Sequence[RotoKeyframe],
                    feather: float = 0.0) -> int:
    """Write generated keyframes onto a RotoShapeNode as ORDINARY roto
    data (points JSON + animated point channels) — fully hand-editable
    afterwards.  Returns the number of frames written.

    The caller wraps this in ONE undo action at the command layer; the
    service itself performs plain parameter writes.
    """
    if not keyframes:
        return 0
    ordered = sorted(keyframes, key=lambda kf: kf.frame)
    tracks: Dict[str, List[Tuple[int, float]]] = {}
    for keyframe in ordered:
        roto_node.set_parameter(
            "points", keyframe.to_points_json())
        roto_node._sync_point_parameters(keyframe.points)
        for index, point in enumerate(keyframe.points):
            names = type(roto_node)._point_param_names(index)
            # AUD-6207: animate ALL six geometry channels, not just pos.
            # c1/c2 are stored as absolute coordinates (mirrored verbatim
            # by RotoShapeNode._sync_point_parameters and re-applied by
            # apply_point_parameter_overrides), so tracks carrying each
            # frame's handle coordinates interpolate the handles between
            # keyframes.  Animating only x/y froze the handles at the last
            # keyframe's position, distorting mid-frame shapes.
            values = (
                (names["x"], point["pos"][0]),
                (names["y"], point["pos"][1]),
                (names["c1_x"], point["c1"][0]),
                (names["c1_y"], point["c1"][1]),
                (names["c2_x"], point["c2"][0]),
                (names["c2_y"], point["c2"][1]),
            )
            for name, value in values:
                tracks.setdefault(name, []).append(
                    (keyframe.frame, float(value)))
    # AUD-6211: count 死变量已删除（函数实际返回 len(ordered)）。
    # 其余两项（冗余同步、轨道追加去重）属行为变更，挂账暂缓。
    for name, samples in tracks.items():
        track = roto_node.keyframe_tracks.get(name)
        if track is None:
            roto_node.keyframe_tracks[name] = {
                "param_name": name,
                "value_type": "float",
                "min_val": 0.0,
                "max_val": 1.0,
                "keyframes": [],
            }
            track = roto_node.keyframe_tracks[name]
        for frame, value in samples:
            track["keyframes"].append(
                {"frame": int(frame), "value": float(value)})
        track["keyframes"].sort(key=lambda kf: int(kf["frame"]))
    if feather:
        roto_node.set_parameter("feather", float(feather))
    # Raw track writes bypass set_keyframe_track's invalidation; mirror it
    # so downstream caches cannot serve results computed before the
    # generated animation existed.
    roto_node._dirty = True
    roto_node.param_revision += 1
    roto_node.bump_output_revision()
    return len(ordered)
