"""阶段 6 测试：跟踪结果绑定求值（base_geometry + transform，不覆盖原始形状）。"""

import numpy as np
import pytest

from src.features.color.tracking.tracking_evaluator import (
    TrackingEvaluator,
    apply_transform_to_geometry,
    capture_base_geometry,
)
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.track_data import TrackingBinding
from src.features.color.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackTransform,
)

FRAME = (1280, 720)


def translation_matrix(dx_px: float, dy_px: float) -> np.ndarray:
    return np.array([[1, 0, dx_px], [0, 1, dy_px], [0, 0, 1]], dtype=np.float64)


def make_binding(frames: dict, shape_type="circle", center=(0.5, 0.5),
                 size=(0.2, 0.2), reference=62) -> TrackingBinding:
    binding = TrackingBinding(
        binding_id="b1", clip_id="clip1", shape_id="s1",
        source_frame_start=0, source_frame_end=124, reference_frame=reference,
    )
    binding.base_geometry = capture_base_geometry({
        "type": shape_type, "center": list(center), "size": list(size),
        "rotation": 0.0,
    })
    for frame, matrix in frames.items():
        status = TrackStatus.VALID
        binding.apply_result(TrackTransform.from_matrix(
            frame, matrix, 0.9, status))
    return binding


class TestCaptureGeometry:
    def test_capture_circle(self):
        base = capture_base_geometry({
            "type": "circle", "center": [0.4, 0.6], "size": [0.3, 0.2],
            "rotation": 15.0,
        })
        assert base["type"] == "circle"
        assert base["center"] == [0.4, 0.6]
        assert base["rotation"] == 15.0

    def test_capture_curve_deep_copies_points(self):
        shape = {
            "type": "curve", "center": [0.5, 0.5], "size": [0, 0],
            "rotation": 0.0,
            "points": [{"pos": [0.1, 0.1], "c1": [0.05, 0.1], "c2": [0.15, 0.1]}],
        }
        base = capture_base_geometry(shape)
        shape["points"][0]["pos"][0] = 0.9
        assert base["points"][0]["pos"][0] == 0.1  # 快照不受原形状修改影响


class TestEvaluatorBinding:
    def test_no_binding_returns_none(self):
        evaluator = TrackingEvaluator(TrackingStore())
        assert evaluator.evaluate_geometry("clip1", "s1", 62, FRAME) is None

    def test_disabled_binding_returns_none(self):
        store = TrackingStore()
        binding = make_binding({70: translation_matrix(64, 0)})
        store.add_binding(binding)
        evaluator = TrackingEvaluator(store)
        evaluator.set_binding_enabled("b1", False)
        assert evaluator.evaluate_geometry("clip1", "s1", 70, FRAME) is None
        evaluator.set_binding_enabled("b1", True)
        assert evaluator.evaluate_geometry("clip1", "s1", 70, FRAME) is not None

    def test_frame_outside_range_returns_none(self):
        store = TrackingStore()
        store.add_binding(make_binding({70: translation_matrix(64, 0)}))
        evaluator = TrackingEvaluator(store)
        assert evaluator.evaluate_geometry("clip1", "s1", 200, FRAME) is None

    def test_translation_moves_center(self):
        store = TrackingStore()
        # 64px = 0.05 归一化（宽 1280）
        store.add_binding(make_binding({70: translation_matrix(64, 36)}))
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 70, FRAME)
        assert geometry["center"][0] == pytest.approx(0.55, abs=1e-9)
        assert geometry["center"][1] == pytest.approx(0.55, abs=1e-9)
        # 纯平移不改变尺寸与旋转
        assert geometry["size"] == pytest.approx([0.2, 0.2])
        assert geometry["rotation"] == pytest.approx(0.0, abs=1e-9)

    def test_reference_frame_identity_no_double_transform(self):
        store = TrackingStore()
        store.add_binding(make_binding({
            62: np.eye(3),
            70: translation_matrix(64, 0),
        }))
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 62, FRAME)
        # 参考帧结果与基准几何完全一致
        assert geometry["center"] == [0.5, 0.5]
        assert geometry["size"] == [0.2, 0.2]
        assert geometry["rotation"] == 0.0

    def test_failed_frame_falls_back_to_base(self):
        store = TrackingStore()
        binding = make_binding({70: np.eye(3)})
        failed = TrackTransform.from_matrix(
            71, np.eye(3), 0.0, TrackStatus.FAILED,
            failure_reason=FailureReason.OCCLUSION)
        binding.apply_result(failed)
        store.add_binding(binding)
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 71, FRAME)
        assert geometry["center"] == [0.5, 0.5]  # 不产生假有效位移

    def test_frame_without_result_falls_back_to_base(self):
        store = TrackingStore()
        store.add_binding(make_binding({70: translation_matrix(64, 0)}))
        evaluator = TrackingEvaluator(store)
        geometry = evaluator.evaluate_geometry("clip1", "s1", 99, FRAME)
        assert geometry["center"] == [0.5, 0.5]


class TestApplyTransformPure:
    def test_identity_returns_equal_copy(self):
        base = {"type": "rect", "center": [0.3, 0.4], "size": [0.2, 0.1],
                "rotation": 0.0}
        result = apply_transform_to_geometry(base, np.eye(3), FRAME)
        assert result == base
        assert result is not base

    def test_input_not_mutated(self):
        base = {"type": "circle", "center": [0.5, 0.5], "size": [0.2, 0.2],
                "rotation": 0.0}
        apply_transform_to_geometry(base, translation_matrix(100, 100), FRAME)
        assert base["center"] == [0.5, 0.5]
