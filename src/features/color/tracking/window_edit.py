"""Power Window editing in displayed coordinates, independent of Qt and GPU.

Optional editor metadata lives in the already extensible ``base_geometry``
payload: ``_motion_channels`` and ``_frame_geometry``. Older bindings need no
migration. Geometry snapshots themselves never contain this metadata.
"""
from __future__ import annotations

import copy
import math
import time
from typing import Mapping

import numpy as np

from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import TrackStatus, TrackTransform


def filter_motion_matrix(matrix: np.ndarray, base: dict,
                         frame_size: tuple[int, int], channels: Mapping[str, bool]) -> np.ndarray:
    """Select motion components around the window pivot, in pixel space.

    Keeping the pivot fixed when disabling translation avoids introducing an
    apparent pan from rotation or zoom about the image origin.
    """
    h = np.asarray(matrix, dtype=np.float64).reshape(3, 3).copy()
    if not isinstance(channels, Mapping) or not channels or all(channels.get(k, True) for k in
                           ("tx", "ty", "scale", "rotation", "perspective")):
        return h
    w, height = frame_size
    pivot = np.asarray(base.get("center", [0.5, 0.5]), dtype=float) * [w, height]
    projected = h @ np.r_[pivot, 1.0]
    denominator = float(projected[2])
    if abs(denominator) < 1e-12:
        raise ValueError("Tracking transform places the window pivot at infinity")
    projected = projected[:2] / denominator
    displacement = projected - pivot
    displacement *= [channels.get("tx", True), channels.get("ty", True)]
    # Polar decomposition keeps anisotropic scale/shear separate from rotation.
    # Decompose the local derivative, not the homogeneous top-left block:
    # perspective changes the derivative at the selected window's pivot.
    derivative = (h[:2, :2] - np.outer(projected, h[2, :2])) / denominator
    u, singular, vt = np.linalg.svd(derivative)
    rotation = u @ vt
    stretch = vt.T @ np.diag(singular) @ vt
    linear = (rotation if channels.get("rotation", True) else np.eye(2)) @ (
        stretch if channels.get("scale", True) else np.eye(2))
    result = np.eye(3)
    if channels.get("perspective", True):
        if abs(h[2, 2]) < 1e-12:
            raise ValueError("Tracking transform has no finite homogeneous normalization")
        result[2, :2] = h[2, :2] / h[2, 2]
    divisor = float(result[2, :2] @ pivot + 1.0)
    target = pivot + displacement
    result[:2, :2] = divisor * linear + np.outer(target, result[2, :2])
    result[:2, 2] = target * divisor - result[:2, :2] @ pivot
    return result


def _geometry_matrix(geometry: dict, frame_size: tuple[int, int]) -> np.ndarray:
    w, h = frame_size
    x, y = geometry.get("center", [0.5, 0.5])
    sx, sy = geometry.get("size", [0.3, 0.3])
    if abs(sx) < 1e-9 or abs(sy) < 1e-9:
        points = [p.get("pos", [x, y]) for p in geometry.get("points", [])]
        if points:
            coords = np.asarray(points)
            sx, sy = np.maximum(np.ptp(coords, axis=0), 1e-6)
        else:
            sx, sy = max(abs(sx), 1e-6), max(abs(sy), 1e-6)
    angle = math.radians(float(geometry.get("rotation", 0.0)))
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c * sx * w, -s * sy * h, x * w],
                     [s * sx * w, c * sy * h, y * h], [0.0, 0.0, 1.0]])


def _transfer_point_delta(target: dict, before: dict, after: dict) -> None:
    """Apply matching point edits without copying an earlier frame's shape."""
    if target.get("type") == "curve":
        target_points = target.get("points", [])
        before_points, after_points = before.get("points", []), after.get("points", [])
        if len(target_points) != len(before_points) or len(before_points) != len(after_points):
            raise ValueError("Clip-wide edits cannot change a manually corrected curve's point count")
        for dest, old, new in zip(target_points, before_points, after_points):
            for key in ("pos", "c1", "c2"):
                dest[key] = (np.asarray(dest[key]) + np.asarray(new[key]) - np.asarray(old[key])).tolist()
            for key in ("type", "fi", "fo"):
                if new.get(key) != old.get(key):
                    if key in new:
                        dest[key] = new[key]
                    else:
                        dest.pop(key, None)
    elif target.get("type") == "gradient":
        target["grad_points"] = (np.asarray(target["grad_points"])
                                  + np.asarray(after["grad_points"])
                                  - np.asarray(before["grad_points"])).tolist()


def edit_tracked_geometry(binding: TrackingBinding, desired: dict, frame: int,
                          frame_size: tuple[int, int], mode: str) -> None:
    """Commit a displayed window edit to the clip or just the current frame.

    Frame overrides preserve arbitrary curve control-point edits exactly;
    automatic tracking can subsequently be rerun without losing them.
    """
    from .tracking_evaluator import apply_transform_to_geometry, capture_base_geometry

    base = binding.base_geometry
    if base is None:
        raise ValueError("A tracked window needs reference geometry")
    if mode not in ("frame", "clip"):
        raise ValueError(f"Unknown window edit mode: {mode}")
    if not binding.contains_frame(frame):
        raise ValueError("Window edits must be inside the binding's source range")
    transform = binding.transform_at(frame)
    h = transform.matrix_np if transform is not None and transform.status != TrackStatus.FAILED else np.eye(3)
    h = filter_motion_matrix(h, base, frame_size, base.get("_motion_channels", {}))
    overrides = base.get("_frame_geometry", {})
    override = overrides.get(str(frame)) if isinstance(overrides, dict) else None
    manual = binding.manual_corrections.get(frame)
    has_override = isinstance(override, dict) and manual is not None and manual.status != TrackStatus.FAILED
    display = override if has_override else apply_transform_to_geometry(base, h, frame_size)
    # Parameter edits may supply only one field. Merge them into the geometry
    # actually shown, including the previous exact override, before capturing.
    clean = capture_base_geometry({**display, **desired})
    delta = _geometry_matrix(clean, frame_size) @ np.linalg.inv(_geometry_matrix(display, frame_size))
    if mode == "frame":
        # Retain a real manual marker and useful transform curves. Arbitrary
        # non-affine point edits use the exact geometry override at evaluation.
        binding.set_manual_correction(TrackTransform.from_matrix(
            frame, delta @ h, 1.0, TrackStatus.MANUAL))
        if not isinstance(base.get("_frame_geometry"), dict):
            base["_frame_geometry"] = {}
        base.setdefault("_frame_geometry", {})[str(frame)] = clean
    elif mode == "clip":
        topology_changed = base.get("type") == "curve" and (
            len(clean.get("points", [])) != len(display.get("points", []))
            or (has_override and len(base.get("points", [])) != len(display.get("points", []))))
        # An existing frame override remains a local correction. Apply only the
        # new clip-wide delta to the reference, rather than baking that local
        # correction into every frame a second time.
        rebased = (apply_transform_to_geometry(base, np.linalg.inv(h) @ delta @ h, frame_size)
                   if has_override else apply_transform_to_geometry(clean, np.linalg.inv(h), frame_size))
        if topology_changed:
            # A clip-wide topology edit establishes the new automatic shape.
            # Other exact manual frame shapes retain their original topology;
            # no node correspondences are guessed and no user work is removed.
            rebased = apply_transform_to_geometry(clean, np.linalg.inv(h), frame_size)
        elif has_override and base.get("type") in ("curve", "gradient"):
            rebased = apply_transform_to_geometry(base, np.eye(3), frame_size)
            _transfer_point_delta(rebased,
                                  apply_transform_to_geometry(display, np.linalg.inv(h), frame_size),
                                  apply_transform_to_geometry(clean, np.linalg.inv(h), frame_size))
        metadata = {k: copy.deepcopy(v) for k, v in base.items() if k.startswith("_")}
        if not isinstance(metadata.get("_frame_geometry", {}), dict):
            metadata.pop("_frame_geometry", None)
        # Move explicit frame shapes with a clip-wide edit as well.
        for key, geometry in metadata.get("_frame_geometry", {}).items():
            if topology_changed:
                continue
            if has_override and base.get("type") in ("curve", "gradient"):
                _transfer_point_delta(geometry, display, clean)
            else:
                metadata["_frame_geometry"][key] = apply_transform_to_geometry(geometry, delta, frame_size)
        if topology_changed and has_override:
            metadata["_frame_geometry"][str(frame)] = clean
        binding.base_geometry = {**rebased, **metadata}
        binding.result_version += 1
        binding.modified_at = time.time()
