"""Patch-scale consensus rejects missing estimates and preserves real motion."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from src.shared.tracking.tracker_2d import Tracker2D, TrackerConfig


def test_legacy_flow_uses_one_original_window() -> None:
    tracker = Tracker2D()
    image = np.zeros((40, 40), dtype=np.uint8)
    points = np.array([[10, 10]], dtype=np.float32)
    status = np.ones((1, 1), dtype=np.uint8)
    with patch('src.shared.tracking.tracker_2d.cv2.calcOpticalFlowPyrLK',
               return_value=(points, status, None)) as flow:
        found, valid = tracker._track_flow(image, image, points, (21, 21), (3, 30, .03))
    assert found is points and valid is status
    flow.assert_called_once()
    assert flow.call_args.kwargs['winSize'] == (21, 21)


def test_consensus_requires_majority_and_never_counts_nonfinite_or_missing() -> None:
    tracker = Tracker2D(TrackerConfig(lk_consensus_windows=(15, 21, 31)))
    image = np.zeros((40, 40), dtype=np.uint8)
    points = np.zeros((4, 2), dtype=np.float32)
    responses = [
        (np.array([[1, 1], [9, 9], [0, 0], [2, 2]], np.float32),
         np.array([[1], [1], [0], [1]], np.uint8), None),
        (np.array([[2, 2], [0, 0], [0, 0], [np.nan, np.nan]], np.float32),
         np.array([[1], [0], [0], [1]], np.uint8), None),
        (np.array([[100, 100], [0, 0], [0, 0], [4, 4]], np.float32),
         np.array([[1], [0], [0], [1]], np.uint8), None),
    ]
    with patch('src.shared.tracking.tracker_2d.cv2.calcOpticalFlowPyrLK',
               side_effect=responses):
        found, valid = tracker._track_flow(image, image, points, (21, 21), (3, 30, .03))
    np.testing.assert_allclose(found, [[2, 2], [9, 9], [0, 0], [3, 3]])
    assert valid.reshape(-1).tolist() == [1, 0, 0, 1]
    assert np.isfinite(found).all()


def test_no_scale_estimate_is_an_explicit_failed_status_without_nan_warning() -> None:
    tracker = Tracker2D(TrackerConfig(lk_consensus_windows=(15, 21, 31)))
    image = np.zeros((40, 40), dtype=np.uint8)
    points = np.array([[10, 10], [20, 20]], dtype=np.float32)
    with patch('src.shared.tracking.tracker_2d.cv2.calcOpticalFlowPyrLK',
               return_value=(None, None, None)):
        with np.errstate(all='raise'):
            found, valid = tracker._track_flow(image, image, points, (21, 21), (3, 30, .03))
    np.testing.assert_array_equal(valid, np.zeros((2, 1), dtype=np.uint8))
    assert np.isfinite(found).all()
