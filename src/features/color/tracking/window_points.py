"""Immutable reference-point selection for asynchronous window tracking."""
from __future__ import annotations

from typing import Optional

import numpy as np

from src.shared.tracking.feature_detector import DetectorConfig, FeatureDetector


class WindowPointDetector(FeatureDetector):
    """Apply user-selected points and exclusion areas in a tracked region.

    Coordinates are region-relative, so redetection follows a moved/scaled
    search window. Each worker receives its own detector and frozen tuples.
    """

    def __init__(self, config: DetectorConfig,
                 seeds: tuple[tuple[float, float], ...] = (),
                 excluded: tuple[tuple[float, float], ...] = (),
                 point_mode: bool = False) -> None:
        super().__init__(config)
        self._seeds = seeds
        self._excluded = excluded
        self._point_mode = point_mode

    def detect(self, gray: np.ndarray, region: tuple[float, float, float, float],
               mask: Optional[np.ndarray] = None) -> np.ndarray:
        x, y, w, h = region
        candidates = (np.empty((0, 2), dtype=np.float32) if self._point_mode and self._seeds
                      else super().detect(gray, region, mask))
        if self._excluded and len(candidates):
            excluded = np.asarray([[x + px * w, y + py * h] for px, py in self._excluded])
            radius = max(3.0, self.config.min_distance)
            keep = np.linalg.norm(candidates[:, None] - excluded[None], axis=2).min(axis=1) > radius
            candidates = candidates[keep]
        if self._seeds:
            seeds = np.asarray([[x + px * w, y + py * h] for px, py in self._seeds], dtype=np.float32)
            inside = ((seeds[:, 0] >= 0) & (seeds[:, 0] < gray.shape[1]) &
                      (seeds[:, 1] >= 0) & (seeds[:, 1] < gray.shape[0]))
            seeds = seeds[inside]
            if len(candidates) and len(seeds):
                distances = np.linalg.norm(candidates[:, None] - seeds[None], axis=2)
                candidates = candidates[distances.min(axis=1) > self.config.min_distance]
            candidates = np.concatenate([seeds, candidates])
        return candidates[:self.config.max_features].astype(np.float32)
