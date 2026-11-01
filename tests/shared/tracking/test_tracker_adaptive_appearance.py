"""Window opt-in appearance adaptation: complete motion without hiding losses."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from src.shared.tracking.feature_detector import DetectorConfig, FeatureDetector
from src.shared.tracking.frame_sampler import AnalysisQuality, SampledFrame
from src.shared.tracking.tracker_2d import Tracker2D, TrackerConfig, TrackDirection, TrackRequest
from src.shared.tracking.types import FailurePolicy, TrackStatus, TrackingMode
from tests.shared.tracking import tracker_support as ts


class _ChangingCardSampler:
    """Independent card translation and gradual appearance change, with known GT."""

    width, height, count = 256, 160, 64

    def __init__(self) -> None:
        rng = np.random.default_rng(20261102)
        first = rng.integers(35, 195, (80, 96), dtype=np.uint8)
        last = rng.integers(35, 195, (80, 96), dtype=np.uint8)
        self.frames: list[np.ndarray] = []
        for frame in range(self.count):
            mix = frame / (self.count - 1)
            card = ((1.0 - mix) * first + mix * last).astype(np.uint8)
            for y in (10, 30, 50, 70):
                for x in (10, 30, 50, 70, 90):
                    cv2.rectangle(card, (x - 2, y - 2), (x + 2, y + 2), 0, -1)
                    card[y, x] = 255
            image = np.full((self.height, self.width), 15, dtype=np.uint8)
            image[40:120, 48 + frame:144 + frame] = card
            self.frames.append(image)

    def read_gray(self, frame: int, quality: AnalysisQuality) -> SampledFrame:
        return SampledFrame(frame, self.frames[frame], self.width, self.height,
                            1.0, self.width, self.height)

    def request(self) -> TrackRequest:
        return TrackRequest(
            media_path='changing-card-in-memory',
            pattern_region=(48 / self.width, 40 / self.height,
                            96 / self.width, 80 / self.height),
            search_region=(.1, .1, .8, .8),
            reference_frame=0, frame_start=0, frame_end=self.count - 1,
            mode=TrackingMode.TRANSLATION, quality=AnalysisQuality.HIGH,
        )


def test_gradual_appearance_completes_without_weakening_original_gate() -> None:
    sampler = _ChangingCardSampler()
    detector = lambda: FeatureDetector(DetectorConfig(max_features=100))
    original_config = TrackerConfig()
    original = Tracker2D(original_config, detector()).track(sampler.request(), sampler=sampler)
    tracker = Tracker2D(replace(original_config, adaptive_appearance=True), detector())
    adapted = tracker.track(sampler.request(), sampler=sampler)

    assert not original_config.adaptive_appearance  # Legacy consumers opt in explicitly.
    assert any(result.status == TrackStatus.FAILED for result in original.transforms.values())
    assert tracker.config.appearance_min_similarity == original_config.appearance_min_similarity
    assert set(adapted.transforms) == set(range(sampler.count))
    assert all(result.status == TrackStatus.VALID for result in adapted.transforms.values())
    for frame, result in adapted.transforms.items():
        np.testing.assert_allclose(result.translation, (frame, 0.0), atol=.02)


@pytest.mark.parametrize('change', [
    {'status': TrackStatus.WEAK}, {'status': TrackStatus.FAILED},
    {'local': .79}, {'fb': .79}, {'inliers': .59}, {'count': 5}, {'sharpness': 0.0},
])
def test_unverified_or_weak_frames_cannot_adapt_template(change: dict[str, Any]) -> None:
    tracker = Tracker2D(TrackerConfig(adaptive_appearance=True))
    step = SimpleNamespace(
        transform=SimpleNamespace(status=change.get('status', TrackStatus.VALID)),
        local_appearance_similarity=change.get('local', .95),
        fb_pass_ratio=change.get('fb', .95), inlier_ratio=change.get('inliers', .90),
        inlier_count=change.get('count', 12),
    )
    assert not tracker._can_update_appearance(
        step, change.get('sharpness', 100.0), TrackingMode.TRANSLATION_ROTATION_SCALE,
    )


def test_cancel_still_stops_at_the_next_frame_boundary() -> None:
    sampler = _ChangingCardSampler()
    cancel = Event()
    progress: list[tuple[int, int]] = []

    def on_progress(done: int, total: int) -> None:
        progress.append((done, total))
        if done == 5:
            cancel.set()

    result = Tracker2D(TrackerConfig(adaptive_appearance=True)).track(
        sampler.request(), sampler=sampler, cancel_token=cancel, progress_cb=on_progress,
    )
    assert result.cancelled
    assert set(result.transforms) == set(range(6))
    assert progress[-1] == (5, sampler.count - 1)


@pytest.mark.parametrize('name', ['occlusion', 'scene_cut'])
def test_adaptation_preserves_loss_detection_and_geometric_truth(name: str) -> None:
    directory = ts.fixture_dir()
    manifest = ts.load_manifest(directory, name)
    corners = ts.reference_corners(manifest)
    seen_losses = 0
    for direction in (TrackDirection.FORWARD, TrackDirection.BACKWARD):
        tracker = Tracker2D(TrackerConfig(adaptive_appearance=True))
        result = tracker.track(ts.build_request(directory, name, TrackingMode.TRANSLATION, direction))
        for frame, transform in result.transforms.items():
            meta = manifest['frames'][frame]
            if meta.get('occluded', False) or (name == 'scene_cut' and frame >= 70):
                assert transform.status == TrackStatus.FAILED
                assert transform.confidence == 0.0
                np.testing.assert_array_equal(transform.matrix_np, np.eye(3))
                seen_losses += 1
            elif transform.status != TrackStatus.FAILED:
                expected = np.asarray(meta['homography'], dtype=np.float64).reshape(3, 3)
                assert ts.corner_error(transform.matrix_np, expected, corners) < 3.0
    assert seen_losses > 0


@pytest.mark.real_media
@pytest.mark.parametrize('features', [12, 64])
def test_user_window_clip_tracks_every_frame(features: int) -> None:
    configured = os.environ.get('TOPOS_TRACKING_REAL_VIDEO')
    if not configured:
        pytest.skip('Set TOPOS_TRACKING_REAL_VIDEO to opt into the local 124-frame user clip')
    path = Path(configured)
    if not path.is_file():
        pytest.skip(f'Local tracking clip is unavailable: {path.name}')
    request = TrackRequest(
        media_path=str(path), pattern_region=(.35, .2333333333, .3, .5333333334),
        search_region=(.2, 0.0, .6, 1.0), reference_frame=0, frame_start=0, frame_end=123,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE, quality=AnalysisQuality.NORMAL,
        shape_geometry={'type': 'circle', 'center': [.5, .5],
                        'size': [.15, .2666666667], 'rotation': 0.0},
    )
    result = Tracker2D(
        TrackerConfig(adaptive_appearance=True, failure_policy=FailurePolicy.RECAPTURE),
        FeatureDetector(DetectorConfig(max_features=features)),
    ).track(request)
    assert not result.cancelled
    assert set(result.transforms) == set(range(124))
    assert all(item.status == TrackStatus.VALID for item in result.transforms.values())
    assert all(np.isfinite(item.matrix_np).all() for item in result.transforms.values())
