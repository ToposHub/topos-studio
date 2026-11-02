"""CPU camera motion analysis; no mask, GUI, renderer or fixed-first-frame NCC."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import cv2
import numpy as np

from src.shared.tracking.feature_detector import DetectorConfig, FeatureDetector
from src.shared.tracking.frame_sampler import FrameSampler
from src.shared.tracking.tracker_2d import TrackRequest, TrackResult
from src.shared.tracking.types import FailureReason, TrackStatus, TrackTransform, TrackingMode


class CameraMotionAnalyzer:
    """Estimate globally distributed image motion on an asynchronous worker."""

    def track(self, request: TrackRequest, sampler: Any = None,
              cancel_token: Any = None,
              progress_cb: Callable[[int, int], None] | None = None) -> TrackResult:
        owns_sampler = sampler is None
        sampler = sampler or FrameSampler(request.media_path)
        detector = FeatureDetector(DetectorConfig(max_features=480, min_features=12,
                                                  grid_cols=8, grid_rows=6, per_bucket_limit=12))
        transforms: dict[int, TrackTransform] = {}
        cancelled = False
        previous: np.ndarray | None = None
        points = np.empty((0, 2), np.float32)
        cumulative = np.eye(3)
        total = request.frame_end - request.frame_start + 1
        try:
            for index, frame in enumerate(range(request.frame_start, request.frame_end + 1)):
                if cancel_token is not None and cancel_token.is_set():
                    cancelled = True
                    break
                sample = sampler.read_gray(frame, request.quality)
                gray = sample.gray
                height, width = gray.shape
                region = (0., 0., float(width), float(height))
                if previous is None:
                    transforms[frame] = TrackTransform.from_matrix(frame, np.eye(3), 1., TrackStatus.VALID)
                    points = detector.detect(gray, region)
                    previous = gray
                else:
                    step, confidence, reason = self._estimate(previous, gray, points, request.mode)
                    if step is None:
                        transforms[frame] = TrackTransform.failed(frame, reason)
                        cumulative = np.eye(3)
                    else:
                        cumulative = step @ cumulative
                        scale = sample.scale_matrix()
                        transforms[frame] = TrackTransform.from_matrix(
                            frame, scale @ cumulative @ np.linalg.inv(scale),
                            confidence, TrackStatus.VALID)
                    # Refill a spatially balanced cloud on every frame. This
                    # tracks neighboring frames, not a fixed foreground target.
                    points = detector.detect(gray, region)
                    previous = gray
                if progress_cb is not None:
                    progress_cb(index + 1, total)
        finally:
            if owns_sampler:
                sampler.close()
        return TrackResult(generation=request.generation, cancelled=cancelled,
                           transforms=transforms, observations={}, diagnostics={})

    @staticmethod
    def _estimate(previous: np.ndarray, current: np.ndarray, points: np.ndarray,
                  mode: TrackingMode) -> tuple[np.ndarray | None, float, FailureReason]:
        if len(points) < 12:
            return None, 0., FailureReason.INSUFFICIENT_FEATURES
        small_previous = cv2.resize(previous, (64, 36)).astype(np.float32)
        small_current = cv2.resize(current, (64, 36)).astype(np.float32)
        if float(np.median(np.abs(small_current - small_previous))) > 55.:
            return None, 0., FailureReason.OCCLUSION
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, .01)
        found, status, _ = cv2.calcOpticalFlowPyrLK(
            previous, current, points, None, winSize=(31, 31), maxLevel=3, criteria=criteria)
        if found is None or status is None:
            return None, 0., FailureReason.SOLVE_FAILED
        back, back_status, _ = cv2.calcOpticalFlowPyrLK(
            current, previous, found, None, winSize=(31, 31), maxLevel=3, criteria=criteria)
        if back is None or back_status is None:
            return None, 0., FailureReason.SOLVE_FAILED
        height, width = current.shape
        valid = ((status.reshape(-1) == 1) & (back_status.reshape(-1) == 1)
                 & np.isfinite(found).all(axis=1) & np.isfinite(back).all(axis=1)
                 & (np.linalg.norm(back - points, axis=1) <= 1.5)
                 & (found[:, 0] >= 0) & (found[:, 0] < width)
                 & (found[:, 1] >= 0) & (found[:, 1] < height))
        source, target = points[valid], found[valid]
        if len(source) < 12:
            return None, 0., FailureReason.INSUFFICIENT_FEATURES
        if mode == TrackingMode.TRANSLATION:
            delta = np.median(target - source, axis=0)
            mask = np.linalg.norm(target - source - delta, axis=1) < 2.0
            matrix = np.eye(3)
            if mask.sum() >= 12:
                matrix[:2, 2] = np.median((target - source)[mask], axis=0)
        elif mode == TrackingMode.TRANSLATION_ROTATION_SCALE:
            affine, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC,
                                                        ransacReprojThreshold=2., confidence=.999)
            if affine is None or inliers is None:
                return None, 0., FailureReason.SOLVE_FAILED
            matrix = np.eye(3)
            matrix[:2] = affine
            mask = inliers.reshape(-1).astype(bool)
        else:
            matrix, inliers = cv2.findHomography(source, target, cv2.RANSAC, 2., confidence=.999)
            if matrix is None or inliers is None:
                return None, 0., FailureReason.SOLVE_FAILED
            mask = inliers.reshape(-1).astype(bool)
        if mask.sum() < 12 or mask.mean() < .3:
            return None, 0., FailureReason.SOLVE_FAILED
        inlier_points = source[mask]
        cells = np.floor(inlier_points / [width / 4., height / 3.]).astype(int)
        if len(np.unique(cells, axis=0)) < 4:
            return None, 0., FailureReason.DEGENERATE
        # Evaluate conditioning around the image center in dimensionless
        # coordinates. Raw pixel translations alone must not trigger failure.
        normalizer = np.array([[width, 0., width / 2.], [0., width, height / 2.], [0., 0., 1.]])
        normalized = np.linalg.inv(normalizer) @ matrix @ normalizer
        corners = np.array([[0., 0.], [width, 0.], [width, height], [0., height]])
        homogeneous = np.c_[corners, np.ones(4)] @ matrix.T
        if (not np.isfinite(matrix).all() or np.linalg.cond(normalized) > 100.
                or np.any(homogeneous[:, 2] <= 1e-7)):
            return None, 0., FailureReason.DEGENERATE
        projected = homogeneous[:, :2] / homogeneous[:, 2, None]
        area = cv2.contourArea(projected.astype(np.float32), oriented=True) / (width * height)
        if not .65 <= area <= 1.5 or not cv2.isContourConvex(projected.astype(np.float32)):
            return None, 0., FailureReason.DEGENERATE
        confidence = min(1., float(mask.mean()) * .7 + min(1., mask.sum() / 100.) * .3)
        return matrix / matrix[2, 2], confidence, FailureReason.NONE
