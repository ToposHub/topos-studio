"""Pure camera-path compensation in source-pixel coordinates.

For an observed reference-to-frame path H and a desired smooth path S, the
source-to-output correction is S @ inverse(H). This deliberately does not change
Composite's older inverse-of-smoothed-track bake helper.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from src.shared.tracking.types import TrackStatus, TrackTransform

MatrixTuple = tuple[float, ...]
_IDENTITY: MatrixTuple = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


@dataclass(frozen=True)
class StabilizationPlan:
    """Derived render matrices; source trajectories remain untouched."""

    matrices: dict[int, MatrixTuple]
    zoom_factor: float
    strength_factor: float
    valid_frames: int
    failed_frames: tuple[int, ...]


def _bounded_scalar(value: float, name: str, lower: float, upper: float) -> float:
    number = float(value)
    if not np.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f'{name} must be finite and between {lower} and {upper}')
    return number


def _normalizer(frame_size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    width, height = frame_size
    if width <= 0 or height <= 0:
        raise ValueError('Stabilization frame dimensions must be positive')
    extent = float(max(width, height))
    matrix = np.array([[extent, 0.0, width / 2.0],
                       [0.0, extent, height / 2.0], [0.0, 0.0, 1.0]])
    corners = np.array([[-width / (2 * extent), -height / (2 * extent), 1.0],
                        [width / (2 * extent), -height / (2 * extent), 1.0],
                        [width / (2 * extent), height / (2 * extent), 1.0],
                        [-width / (2 * extent), height / (2 * extent), 1.0]])
    return matrix, corners


def _safe_matrix(matrix: np.ndarray, corners: np.ndarray) -> np.ndarray | None:
    """Reject singular, reflected or horizon-crossing normalized homographies."""
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        return None
    if abs(matrix[2, 2]) < 1e-10:
        return None
    normalized = matrix / matrix[2, 2]
    try:
        if np.linalg.cond(normalized) > 1e6:
            return None
    except np.linalg.LinAlgError:
        return None
    projected = corners @ normalized.T
    denominators = projected[:, 2]
    if np.any(denominators <= 1e-7):
        return None
    points = projected[:, :2] / denominators[:, None]
    edges = np.roll(points, -1, axis=0) - points
    following = np.roll(edges, -1, axis=0)
    crosses = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    if not np.all(crosses > 1e-10):
        return None
    return normalized


def _pose(matrix: np.ndarray) -> np.ndarray | None:
    """Polar-decompose the local Jacobian, retaining projective components."""
    translation = matrix[:2, 2]
    perspective = matrix[2, :2]
    jacobian = matrix[:2, :2] - np.outer(translation, perspective)
    try:
        left, singular, right = np.linalg.svd(jacobian)
    except np.linalg.LinAlgError:
        return None
    rotation = left @ right
    if np.linalg.det(rotation) <= 0.0 or singular.min() < 1e-7:
        return None
    log_stretch = right.T @ np.diag(np.log(singular)) @ right
    angle = np.arctan2(rotation[1, 0], rotation[0, 0])
    return np.array([translation[0], translation[1], angle,
                     log_stretch[0, 0], log_stretch[0, 1], log_stretch[1, 1],
                     perspective[0], perspective[1]], dtype=np.float64)


def _from_pose(pose: np.ndarray) -> np.ndarray:
    tx, ty, angle, xx, xy, yy, px, py = pose
    values, vectors = np.linalg.eigh(np.array([[xx, xy], [xy, yy]]))
    stretch = vectors @ np.diag(np.exp(np.clip(values, -12.0, 12.0))) @ vectors.T
    cosine, sine = np.cos(angle), np.sin(angle)
    rotation = np.array([[cosine, -sine], [sine, cosine]])
    matrix = np.eye(3)
    translation = np.array([tx, ty])
    perspective = np.array([px, py])
    matrix[:2, :2] = rotation @ stretch + np.outer(translation, perspective)
    matrix[:2, 2] = translation
    matrix[2, :2] = perspective
    return matrix


def _smooth(values: np.ndarray, radius: int) -> np.ndarray:
    """O(n) local-linear smoothing, preserving intentional pans at both ends."""
    if radius <= 0 or len(values) <= 1:
        return values.copy()
    values = values.copy()
    values[:, 2] = np.unwrap(values[:, 2])
    count = len(values)
    low = np.maximum(0, np.arange(count) - radius)
    high = np.minimum(count, np.arange(count) + radius + 1)
    sums = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(values, axis=0)])
    lengths = (high - low).astype(np.float64)
    means = (sums[high] - sums[low]) / lengths[:, None]
    # Centered interior windows already reproduce linear motion. Truncating
    # endpoint windows with a mean would slow the pan down by half, creating
    # a visible ease-in/out that was never present in the camera motion.
    positions = np.arange(count, dtype=np.float64)
    weighted = np.vstack([np.zeros((1, values.shape[1])),
                          np.cumsum(values * positions[:, None], axis=0)])
    x_sums = np.r_[0., np.cumsum(positions)]
    xx_sums = np.r_[0., np.cumsum(positions ** 2)]
    mean_x = (x_sums[high] - x_sums[low]) / lengths
    variance_x = xx_sums[high] - xx_sums[low] - lengths * mean_x ** 2
    covariance = weighted[high] - weighted[low] - lengths[:, None] * mean_x[:, None] * means
    slopes = covariance / np.maximum(variance_x, 1.)[:, None]
    return means + slopes * (positions - mean_x)[:, None]


def _covers(inverses: np.ndarray, corners: np.ndarray, zoom: float) -> bool:
    targets = corners.copy()
    targets[:, :2] /= zoom
    projected = np.einsum('nij,kj->nki', inverses, targets)
    denominator = projected[:, :, 2]
    if not np.isfinite(projected).all() or np.any(denominator <= 1e-8):
        return False
    positions = projected[:, :, :2] / denominator[:, :, None]
    bounds = np.max(np.abs(corners[:, :2]), axis=0)
    return bool(np.all(np.abs(positions) <= bounds + 1e-9))


def compute_stabilization_plan(
    transforms: Mapping[int, TrackTransform], frame_size: tuple[int, int], *,
    smooth: float = .25, strength: float = 1.0, camera_lock: bool = False,
    zoom: bool = True, cropping_ratio: float = .5, fps: float = 25.0,
) -> StabilizationPlan:
    """Derive safe source-to-output matrices without changing source analysis.

    Smooth controls a time-based half-window (seconds). A zero smooth value
    leaves the observed path unchanged; Camera Lock instead targets identity.
    Cropping ratio is the minimum retained source width/height: .5 caps zoom at
    2x. If that cannot cover the output, all corrections are attenuated by one
    common factor. Zoom is constant across the clip, avoiding zoom pumping.
    Missing/failed/invalid frames split paths and are never smoothed across.
    """
    smooth = _bounded_scalar(smooth, 'smooth', 0.0, 1.0)
    strength = _bounded_scalar(strength, 'strength', 0.0, 1.0)
    cropping_ratio = _bounded_scalar(cropping_ratio, 'cropping_ratio', 0.0, 1.0)
    if not np.isfinite(fps) or fps <= 0.0:
        raise ValueError('fps must be positive and finite')
    normalizer, corners = _normalizer(frame_size)
    inverse_normalizer = np.linalg.inv(normalizer)
    frames = sorted(transforms)
    matrices = {frame: _IDENTITY for frame in frames}
    if not frames:
        return StabilizationPlan(matrices, 1.0, 1.0, 0, ())
    radius = int(round(smooth * fps))
    segments: list[list[tuple[int, np.ndarray, np.ndarray]]] = []
    failed: list[int] = []
    previous: int | None = None
    for frame in frames:
        entry = transforms[frame]
        matrix = np.asarray(entry.matrix, dtype=np.float64).reshape(3, 3)
        checked = _safe_matrix(inverse_normalizer @ matrix @ normalizer, corners)
        pose = _pose(checked) if checked is not None else None
        usable = entry.status != TrackStatus.FAILED and pose is not None
        if not usable:
            failed.append(frame)
            previous = None
            continue
        if previous is None or frame != previous + 1:
            segments.append([])
        segments[-1].append((frame, checked, pose))
        previous = frame
    valid_count = len(frames) - len(failed)
    if strength == 0.0 or (not camera_lock and radius == 0):
        return StabilizationPlan(matrices, 1.0, 1.0, valid_count, tuple(failed))

    corrections: dict[int, np.ndarray] = {}
    correction_poses: dict[int, np.ndarray] = {}
    for segment in segments:
        values = np.asarray([pose for _, _, pose in segment])
        desired = _smooth(values, radius)
        for index, (frame, observed, _) in enumerate(segment):
            target = np.eye(3) if camera_lock else _from_pose(desired[index])
            correction = _safe_matrix(target @ np.linalg.inv(observed), corners)
            pose = _pose(correction) if correction is not None else None
            if pose is None:
                failed.append(frame)
                continue
            # Rebuilding only when strength != 1 preserves the exact full
            # projective correction in the common full-strength case.
            correction = correction if strength == 1.0 else _from_pose(pose * strength)
            checked = _safe_matrix(correction, corners)
            checked_pose = _pose(checked) if checked is not None else None
            if checked_pose is None:
                failed.append(frame)
                continue
            corrections[frame] = checked
            correction_poses[frame] = checked_pose

    strength_factor = 1.0
    zoom_factor = 1.0
    if zoom and corrections:
        # A single crop scale over the whole shot prevents breathing. Work in
        # source-centered units so 1080p and 4K choose equivalent crops.
        ordered = sorted(corrections)
        original = np.asarray([corrections[frame] for frame in ordered])
        poses = np.asarray([correction_poses[frame] for frame in ordered])
        max_zoom = min(8.0, 1.0 / max(cropping_ratio, .125))
        inverses = np.linalg.inv(original)
        if not _covers(inverses, corners, max_zoom):
            low, high = 0.0, 1.0
            accepted = np.repeat(np.eye(3)[None], len(ordered), axis=0)
            for _ in range(18):
                middle = (low + high) / 2.0
                candidate = np.asarray([_from_pose(pose * middle) for pose in poses])
                if _covers(np.linalg.inv(candidate), corners, max_zoom):
                    low, accepted = middle, candidate
                else:
                    high = middle
            strength_factor = low
            original = accepted
            inverses = np.linalg.inv(original)
        low_zoom, high_zoom = 1.0, max_zoom
        if not _covers(inverses, corners, 1.0):
            for _ in range(24):
                middle = (low_zoom + high_zoom) / 2.0
                if _covers(inverses, corners, middle):
                    high_zoom = middle
                else:
                    low_zoom = middle
            zoom_factor = high_zoom
        crop = np.diag([zoom_factor, zoom_factor, 1.0])
        corrections = {frame: crop @ original[index] for index, frame in enumerate(ordered)}

    for frame, correction in corrections.items():
        source_matrix = normalizer @ correction @ inverse_normalizer
        source_matrix /= source_matrix[2, 2]
        matrices[frame] = tuple(float(value) for value in source_matrix.ravel())
    failures = tuple(sorted(set(failed)))
    return StabilizationPlan(matrices, zoom_factor, strength_factor,
                             len(frames) - len(failures), failures)


def compute_stabilization(
    transforms: Mapping[int, TrackTransform], frame_size: tuple[int, int], *,
    smooth: float = .25, strength: float = 1.0, camera_lock: bool = False,
    zoom: bool = True, cropping_ratio: float = .5, fps: float = 25.0,
) -> dict[int, MatrixTuple]:
    """Return source-pixel 3x3 render matrices (see compute_stabilization_plan)."""
    return compute_stabilization_plan(
        transforms, frame_size, smooth=smooth, strength=strength,
        camera_lock=camera_lock, zoom=zoom, cropping_ratio=cropping_ratio, fps=fps,
    ).matrices
