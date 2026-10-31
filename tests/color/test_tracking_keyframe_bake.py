"""阶段 6 测试：跟踪结果烘焙为关键帧（采样间隔、失败帧跳过、数据保留）。"""

import numpy as np
import pytest

from src.features.color.tracking.tracking_evaluator import (
    BAKE_TRACK_NAMES,
    bake_binding_keyframes,
    capture_base_geometry,
)
from src.features.color.tracking.track_data import TrackingBinding
from src.features.color.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackTransform,
)

FRAME = (1280, 720)


def translation_matrix(dx_px: float, dy_px: float) -> np.ndarray:
    return np.array([[1, 0, dx_px], [0, 1, dy_px], [0, 0, 1]], dtype=np.float64)


def make_binding(frames: dict, failed_frames=()) -> TrackingBinding:
    binding = TrackingBinding(
        binding_id="b1", clip_id="clip1", shape_id="s1",
        source_frame_start=0, source_frame_end=124, reference_frame=62,
    )
    binding.base_geometry = capture_base_geometry({
        "type": "circle", "center": [0.5, 0.5], "size": [0.2, 0.2],
        "rotation": 0.0,
    })
    for frame, matrix in frames.items():
        binding.apply_result(TrackTransform.from_matrix(
            frame, matrix, 0.9, TrackStatus.VALID))
    for frame in failed_frames:
        binding.apply_result(TrackTransform.from_matrix(
            frame, np.eye(3), 0.0, TrackStatus.FAILED,
            failure_reason=FailureReason.OCCLUSION))
    return binding


class TestKeyframeBake:
    def test_bake_produces_all_tracks(self):
        binding = make_binding({f: translation_matrix(f - 62, 0)
                                for f in range(62, 72)})
        tracks = bake_binding_keyframes(binding, FRAME)
        assert set(tracks.keys()) == set(BAKE_TRACK_NAMES)
        assert len(tracks["center_x"].keyframes) == 10

    def test_bake_values_match_evaluated_geometry(self):
        binding = make_binding({70: translation_matrix(64, 36)})
        tracks = bake_binding_keyframes(binding, FRAME)
        kf = tracks["center_x"].get_keyframe_at(70)
        assert kf is not None
        assert kf.value == pytest.approx(0.55, abs=1e-9)
        assert tracks["center_y"].get_keyframe_at(70).value == pytest.approx(
            0.55, abs=1e-9)

    def test_bake_with_interval(self):
        binding = make_binding({f: np.eye(3) for f in range(60, 70)})
        tracks = bake_binding_keyframes(binding, FRAME, interval=2)
        frames = [kf.frame for kf in tracks["center_x"].keyframes]
        # 60, 62, 64, 66, 68 + 末帧 69
        assert frames == [60, 62, 64, 66, 68, 69]

    def test_failed_frames_skipped(self):
        binding = make_binding(
            {68: np.eye(3), 69: np.eye(3), 71: np.eye(3)},
            failed_frames=[70],
        )
        tracks = bake_binding_keyframes(binding, FRAME)
        frames = [kf.frame for kf in tracks["center_x"].keyframes]
        assert 70 not in frames
        assert frames == [68, 69, 71]

    def test_bake_preserves_tracking_data(self):
        binding = make_binding({f: np.eye(3) for f in range(62, 66)})
        version_before = binding.result_version
        transforms_before = dict(binding.transforms)
        bake_binding_keyframes(binding, FRAME)
        assert binding.transforms == transforms_before
        assert binding.result_version == version_before

    def test_empty_binding_returns_empty_tracks(self):
        binding = TrackingBinding(
            binding_id="b1", clip_id="clip1", shape_id="s1")
        tracks = bake_binding_keyframes(binding, FRAME)
        for track in tracks.values():
            assert track.keyframes == []

    def test_invalid_interval_rejected(self):
        binding = make_binding({70: np.eye(3)})
        with pytest.raises(ValueError):
            bake_binding_keyframes(binding, FRAME, interval=0)
