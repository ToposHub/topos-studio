"""阶段 1 验收：跟踪数据模型单元测试。

覆盖：
- TrackTransform / FeatureObservation 不可变类型的校验与序列化。
- TrackingBinding：手动修正优先、自动结果不覆盖手动修正、状态查询。
- TrackingStore：增删改查与同 (clip, shape) 冲突约束。
"""

import math

import numpy as np
import pytest

from src.features.color.tracking.track_data import TrackingBinding
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.types import (
    FeatureObservation,
    FailureReason,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)


def _translation_matrix(tx: float, ty: float) -> np.ndarray:
    return np.array([[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]])


def _rotation_matrix(deg: float) -> np.ndarray:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _make_binding(**kwargs) -> TrackingBinding:
    defaults = dict(
        binding_id="b1",
        clip_id="clip-1",
        shape_id="shape-1",
        source_frame_start=0,
        source_frame_end=124,
        reference_frame=62,
    )
    defaults.update(kwargs)
    return TrackingBinding(**defaults)


# ================================================================
# TrackTransform
# ================================================================


class TestTrackTransform:
    def test_matrix_length_validation(self):
        with pytest.raises(ValueError):
            TrackTransform(
                frame=0,
                matrix=(1.0, 0.0, 0.0),  # 不是 9 个元素
                translation=(0.0, 0.0),
                rotation=0.0,
                scale=(1.0, 1.0),
                shear=(0.0, 0.0),
                confidence=1.0,
                status=TrackStatus.VALID,
            )

    def test_confidence_range_validation(self):
        with pytest.raises(ValueError):
            TrackTransform(
                frame=0,
                matrix=(1, 0, 0, 0, 1, 0, 0, 0, 1),
                translation=(0.0, 0.0),
                rotation=0.0,
                scale=(1.0, 1.0),
                shear=(0.0, 0.0),
                confidence=1.5,
                status=TrackStatus.VALID,
            )

    def test_from_matrix_decomposition_translation(self):
        t = TrackTransform.from_matrix(
            10, _translation_matrix(3.5, -2.0), 0.9, TrackStatus.VALID
        )
        assert t.frame == 10
        assert t.translation == pytest.approx((3.5, -2.0))
        assert t.rotation == pytest.approx(0.0, abs=1e-9)
        assert t.scale == pytest.approx((1.0, 1.0))
        assert t.confidence == pytest.approx(0.9)

    def test_from_matrix_decomposition_rotation_scale(self):
        m = _rotation_matrix(30.0) * 2.0
        m[2, 2] = 1.0
        t = TrackTransform.from_matrix(0, m, 0.8, TrackStatus.VALID)
        assert t.rotation == pytest.approx(30.0, abs=1e-6)
        assert t.scale[0] == pytest.approx(2.0, abs=1e-9)
        assert t.scale[1] == pytest.approx(2.0, abs=1e-9)

    def test_failed_result_identity(self):
        t = TrackTransform.failed(5, FailureReason.OCCLUSION)
        assert t.status == TrackStatus.FAILED
        assert t.failure_reason == FailureReason.OCCLUSION
        assert t.confidence == 0.0
        np.testing.assert_allclose(t.matrix_np, np.eye(3))

    def test_frozen_immutability(self):
        t = TrackTransform.failed(0, FailureReason.NONE)
        with pytest.raises(Exception):
            t.frame = 1  # type: ignore[misc]

    def test_dict_roundtrip(self):
        t = TrackTransform.from_matrix(
            7, _translation_matrix(1.0, 2.0), 0.75, TrackStatus.WEAK,
            FailureReason.BLUR,
        )
        restored = TrackTransform.from_dict(t.to_dict())
        assert restored == t


class TestFeatureObservation:
    def test_dict_roundtrip(self):
        o = FeatureObservation(
            frame=3, feature_id=42, x=100.5, y=200.25,
            error=0.35, confidence=0.9, status=TrackStatus.VALID,
        )
        assert FeatureObservation.from_dict(o.to_dict()) == o


# ================================================================
# TrackingBinding
# ================================================================


class TestTrackingBinding:
    def test_manual_correction_takes_priority(self):
        b = _make_binding()
        auto = TrackTransform.from_matrix(
            10, _translation_matrix(5.0, 0.0), 0.9, TrackStatus.VALID
        )
        manual = TrackTransform.from_matrix(
            10, _translation_matrix(7.0, 0.0), 0.5, TrackStatus.VALID
        )
        b.apply_result(auto)
        b.set_manual_correction(manual)
        final = b.transform_at(10)
        assert final is not None
        assert final.status == TrackStatus.MANUAL
        assert final.translation == pytest.approx((7.0, 0.0))
        assert final.confidence == pytest.approx(1.0)
        # 自动结果仍保留在来源字典中（不被删除）
        assert 10 in b.transforms

    def test_apply_result_does_not_overwrite_manual(self):
        b = _make_binding()
        manual = TrackTransform.from_matrix(
            10, _translation_matrix(7.0, 0.0), 0.5, TrackStatus.VALID
        )
        b.set_manual_correction(manual)
        auto = TrackTransform.from_matrix(
            10, _translation_matrix(5.0, 0.0), 0.9, TrackStatus.VALID
        )
        b.apply_result(auto)
        # 手动修正帧的最终结果依然是手动修正
        final = b.transform_at(10)
        assert final.status == TrackStatus.MANUAL
        assert final.translation == pytest.approx((7.0, 0.0))
        # 自动结果没有写入（同一帧只允许一个最终结果）
        assert 10 not in b.transforms

    def test_status_at_without_data_is_failed(self):
        b = _make_binding()
        assert b.status_at(99) == TrackStatus.FAILED

    def test_clear_manual_restores_auto(self):
        b = _make_binding()
        auto = TrackTransform.from_matrix(
            10, _translation_matrix(5.0, 0.0), 0.9, TrackStatus.VALID
        )
        b.apply_result(auto)
        b.set_manual_correction(
            TrackTransform.from_matrix(
                10, _translation_matrix(9.0, 0.0), 0.5, TrackStatus.VALID
            )
        )
        b.clear_manual_correction(10)
        final = b.transform_at(10)
        assert final.status == TrackStatus.VALID
        assert final.translation == pytest.approx((5.0, 0.0))

    def test_clear_results_keeps_manual_by_default(self):
        b = _make_binding()
        b.apply_result(
            TrackTransform.from_matrix(
                10, _translation_matrix(5.0, 0.0), 0.9, TrackStatus.VALID
            )
        )
        b.set_manual_correction(
            TrackTransform.from_matrix(
                11, _translation_matrix(6.0, 0.0), 0.5, TrackStatus.VALID
            )
        )
        b.clear_results()
        assert not b.transforms
        assert 11 in b.manual_corrections
        b.clear_results(keep_manual=False)
        assert not b.manual_corrections

    def test_failed_and_weak_frames(self):
        b = _make_binding()
        b.apply_result(TrackTransform.failed(3, FailureReason.OCCLUSION))
        b.apply_result(
            TrackTransform.from_matrix(
                4, _translation_matrix(1.0, 0.0), 0.4, TrackStatus.WEAK
            )
        )
        b.apply_result(
            TrackTransform.from_matrix(
                5, _translation_matrix(2.0, 0.0), 0.9, TrackStatus.VALID
            )
        )
        assert b.failed_frames() == [3]
        assert b.weak_frames() == [4]

    def test_frame_range_with_results(self):
        b = _make_binding()
        assert b.frame_range_with_results() is None
        b.apply_result(
            TrackTransform.from_matrix(
                20, _translation_matrix(1.0, 0.0), 0.9, TrackStatus.VALID
            )
        )
        b.set_manual_correction(
            TrackTransform.from_matrix(
                5, _translation_matrix(0.0, 1.0), 0.5, TrackStatus.VALID
            )
        )
        assert b.frame_range_with_results() == (5, 20)

    def test_result_version_increments(self):
        b = _make_binding()
        v0 = b.result_version
        b.apply_result(
            TrackTransform.from_matrix(
                1, _translation_matrix(1.0, 0.0), 0.9, TrackStatus.VALID
            )
        )
        assert b.result_version == v0 + 1

    def test_empty_binding_id_auto_generated(self):
        b = TrackingBinding(binding_id="", clip_id="c", shape_id="s")
        assert b.binding_id


# ================================================================
# TrackingStore
# ================================================================


class TestTrackingStore:
    def test_add_and_get(self):
        store = TrackingStore()
        b = _make_binding()
        store.add_binding(b)
        assert store.get_binding("clip-1", "shape-1") is b
        assert store.get_binding_by_id("b1") is b
        assert len(store) == 1
        assert not store.is_empty()

    def test_duplicate_binding_rejected(self):
        store = TrackingStore()
        store.add_binding(_make_binding())
        with pytest.raises(ValueError):
            store.add_binding(_make_binding(binding_id="b2"))

    def test_duplicate_binding_replace(self):
        store = TrackingStore()
        store.add_binding(_make_binding())
        new = _make_binding(binding_id="b2", shape_name="renamed")
        store.add_binding(new, replace=True)
        assert len(store) == 1
        assert store.get_binding("clip-1", "shape-1") is new

    def test_remove_bindings_for_shape(self):
        store = TrackingStore()
        store.add_binding(_make_binding())
        store.add_binding(_make_binding(binding_id="b2", shape_id="shape-2"))
        store.remove_bindings_for_shape("clip-1", "shape-1")
        assert store.get_binding("clip-1", "shape-1") is None
        assert store.get_binding("clip-1", "shape-2") is not None

    def test_bindings_for_clip(self):
        store = TrackingStore()
        store.add_binding(_make_binding())
        store.add_binding(_make_binding(binding_id="b2", clip_id="clip-2"))
        assert len(store.bindings_for_clip("clip-1")) == 1
        assert len(store.bindings_for_clip("clip-2")) == 1
        assert store.bindings_for_clip("nope") == []
