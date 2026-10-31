"""阶段 6 测试：手动修正优先于自动结果；清除修正后回退自动结果。"""

import numpy as np
import pytest

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


def make_store_with_binding() -> tuple:
    binding = TrackingBinding(
        binding_id="b1", clip_id="clip1", shape_id="s1",
        source_frame_start=0, source_frame_end=124, reference_frame=62,
    )
    binding.base_geometry = capture_base_geometry({
        "type": "circle", "center": [0.5, 0.5], "size": [0.2, 0.2],
        "rotation": 0.0,
    })
    # 自动结果：帧 70 向右 64px
    binding.apply_result(TrackTransform.from_matrix(
        70, translation_matrix(64, 0), 0.9, TrackStatus.VALID))
    store = TrackingStore()
    store.add_binding(binding)
    return store, binding


class TestManualCorrectionPriority:
    def test_manual_overrides_auto(self):
        store, binding = make_store_with_binding()
        # 手动修正：帧 70 向左 128px
        binding.set_manual_correction(TrackTransform.from_matrix(
            70, translation_matrix(-128, 0), 0.5, TrackStatus.VALID))
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 70, FRAME)
        assert geometry["center"][0] == pytest.approx(0.4, abs=1e-9)

    def test_clear_manual_falls_back_to_auto(self):
        store, binding = make_store_with_binding()
        binding.set_manual_correction(TrackTransform.from_matrix(
            70, translation_matrix(-128, 0), 0.5, TrackStatus.VALID))
        binding.clear_manual_correction(70)
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 70, FRAME)
        assert geometry["center"][0] == pytest.approx(0.55, abs=1e-9)

    def test_retrack_does_not_overwrite_manual(self):
        store, binding = make_store_with_binding()
        binding.set_manual_correction(TrackTransform.from_matrix(
            70, translation_matrix(-128, 0), 0.5, TrackStatus.VALID))
        # 模拟重新跟踪写入同帧自动结果：不应覆盖手动修正
        binding.apply_result(TrackTransform.from_matrix(
            70, translation_matrix(200, 0), 0.9, TrackStatus.VALID))
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 70, FRAME)
        assert geometry["center"][0] == pytest.approx(0.4, abs=1e-9)

    def test_manual_status_recorded(self):
        _, binding = make_store_with_binding()
        binding.set_manual_correction(TrackTransform.from_matrix(
            70, translation_matrix(-128, 0), 0.5, TrackStatus.VALID))
        transform = binding.transform_at(70)
        assert transform.status == TrackStatus.MANUAL

    def test_other_frames_still_use_auto(self):
        store, binding = make_store_with_binding()
        binding.apply_result(TrackTransform.from_matrix(
            71, translation_matrix(32, 0), 0.9, TrackStatus.VALID))
        binding.set_manual_correction(TrackTransform.from_matrix(
            70, translation_matrix(-128, 0), 0.5, TrackStatus.VALID))
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 71, FRAME)
        assert geometry["center"][0] == pytest.approx(0.525, abs=1e-9)
