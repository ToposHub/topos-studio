"""阶段 6 测试：时间线帧 ↔ source 帧同步求值（复用 ClipFrameMapper）。"""

import numpy as np
import pytest

from src.features.color.tracking.coordinate_map import ClipFrameMapper
from src.features.color.tracking.tracking_evaluator import (
    TrackingEvaluator,
    capture_base_geometry,
)
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.track_data import TrackingBinding
from src.features.color.tracking.types import TrackStatus, TrackTransform

FRAME = (1280, 720)


def translation_matrix(dx_px: float, dy_px: float) -> np.ndarray:
    return np.array([[1, 0, dx_px], [0, 1, dy_px], [0, 0, 1]], dtype=np.float64)


def make_store(mapper: ClipFrameMapper) -> TrackingStore:
    """source 100-199 的素材：每帧向右移动 (frame-100) px。"""
    binding = TrackingBinding(
        binding_id="b1", clip_id="clip1", shape_id="s1",
        source_frame_start=mapper.source_start,
        source_frame_end=mapper.source_end,
        reference_frame=mapper.source_start,
    )
    binding.base_geometry = capture_base_geometry({
        "type": "circle", "center": [0.5, 0.5], "size": [0.2, 0.2],
        "rotation": 0.0,
    })
    for f in range(mapper.source_start, mapper.source_end + 1):
        binding.apply_result(TrackTransform.from_matrix(
            f, translation_matrix(f - mapper.source_start, 0),
            0.9, TrackStatus.VALID))
    store = TrackingStore()
    store.add_binding(binding)
    return store


class TestTimelineFrameSync:
    def test_timeline_frame_maps_to_source_geometry(self):
        mapper = ClipFrameMapper(source_start=100, source_end=199,
                                 timeline_start=10)
        evaluator = TrackingEvaluator(make_store(mapper))
        # timeline 20 → source 110 → 位移 10px = 10/1280
        geometry = evaluator.evaluate_geometry_at_timeline(
            "clip1", "s1", 20, mapper, FRAME)
        assert geometry is not None
        assert geometry["center"][0] == pytest.approx(
            0.5 + 10 / 1280, abs=1e-9)

    def test_timeline_first_frame_is_reference(self):
        mapper = ClipFrameMapper(source_start=100, source_end=199,
                                 timeline_start=10)
        evaluator = TrackingEvaluator(make_store(mapper))
        geometry = evaluator.evaluate_geometry_at_timeline(
            "clip1", "s1", 10, mapper, FRAME)
        # 参考帧恒等：与基准几何一致
        assert geometry["center"] == [0.5, 0.5]

    def test_timeline_outside_clip_returns_none(self):
        mapper = ClipFrameMapper(source_start=100, source_end=199,
                                 timeline_start=10)
        evaluator = TrackingEvaluator(make_store(mapper))
        assert evaluator.evaluate_geometry_at_timeline(
            "clip1", "s1", 5, mapper, FRAME) is None
        assert evaluator.evaluate_geometry_at_timeline(
            "clip1", "s1", 500, mapper, FRAME) is None

    def test_speed_mapping(self):
        # 2x 速度：timeline 5 帧对应 source 10 帧
        mapper = ClipFrameMapper(source_start=100, source_end=199,
                                 timeline_start=0, speed=2.0)
        evaluator = TrackingEvaluator(make_store(mapper))
        geometry = evaluator.evaluate_geometry_at_timeline(
            "clip1", "s1", 5, mapper, FRAME)
        assert geometry is not None
        # source 110 → 位移 10px
        assert geometry["center"][0] == pytest.approx(
            0.5 + 10 / 1280, abs=1e-9)

    def test_consistent_with_direct_source_evaluation(self):
        mapper = ClipFrameMapper(source_start=100, source_end=199,
                                 timeline_start=10)
        evaluator = TrackingEvaluator(make_store(mapper))
        via_timeline = evaluator.evaluate_geometry_at_timeline(
            "clip1", "s1", 42, mapper, FRAME)
        direct = evaluator.evaluate_geometry("clip1", "s1", 132, FRAME)
        assert via_timeline == direct
