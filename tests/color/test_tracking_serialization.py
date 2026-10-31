"""阶段 1 验收：跟踪数据持久化（序列化/反序列化往返）。

覆盖计划验收标准：
- 空轨迹、完整轨迹、部分失败轨迹、含手动修正轨迹都能保存和加载。
- 旧工程没有 trackings 字段时正常加载。
- 同一帧只允许一个最终结果，但保留自动结果与手动修正的来源。
- ColorStackData 的 with_* 重建方法不丢失 trackings 字段。
"""

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
from src.shared.project.color_stack import ColorStackData


def _translation_matrix(tx: float, ty: float) -> np.ndarray:
    return np.array([[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]])


def _valid_transform(frame: int, tx: float, conf: float = 0.9) -> TrackTransform:
    return TrackTransform.from_matrix(
        frame, _translation_matrix(tx, 0.0), conf, TrackStatus.VALID
    )


def _full_binding() -> TrackingBinding:
    """含自动结果、失败帧、弱帧、观测与手动修正的完整绑定。"""
    b = TrackingBinding(
        binding_id="b-full",
        clip_id="clip-1",
        shape_id="shape-1",
        shape_name="Power Window 1",
        source_frame_start=0,
        source_frame_end=124,
        reference_frame=62,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
        pattern_region=(0.4, 0.4, 0.2, 0.2),
        search_region=(0.3, 0.3, 0.4, 0.4),
        media_fingerprint="fp-abc",
        base_geometry={"kind": "circle", "center": (0.5, 0.5)},
    )
    for f in range(60, 66):
        b.apply_result(
            _valid_transform(f, tx=(f - 62) * 2.5),
            obs=(
                FeatureObservation(
                    frame=f, feature_id=1, x=100.0 + f, y=200.0,
                    error=0.3, confidence=0.9,
                ),
            ),
        )
    # 部分失败轨迹
    b.apply_result(TrackTransform.failed(66, FailureReason.OCCLUSION))
    b.apply_result(_valid_transform(67, tx=12.5, conf=0.45))
    b.transforms[67] = TrackTransform(
        frame=67,
        matrix=b.transforms[67].matrix,
        translation=b.transforms[67].translation,
        rotation=b.transforms[67].rotation,
        scale=b.transforms[67].scale,
        shear=b.transforms[67].shear,
        confidence=0.45,
        status=TrackStatus.WEAK,
        failure_reason=FailureReason.NONE,
    )
    # 手动修正（覆盖自动结果帧 63 的最终结果）
    b.set_manual_correction(_valid_transform(63, tx=99.0))
    b.has_forward = True
    b.has_backward = False
    return b


def _assert_binding_equal(a: TrackingBinding, b: TrackingBinding) -> None:
    assert a.binding_id == b.binding_id
    assert a.clip_id == b.clip_id
    assert a.shape_id == b.shape_id
    assert a.shape_name == b.shape_name
    assert a.source_frame_start == b.source_frame_start
    assert a.source_frame_end == b.source_frame_end
    assert a.reference_frame == b.reference_frame
    assert a.mode == b.mode
    assert a.pattern_region == pytest.approx(b.pattern_region)
    assert a.search_region == pytest.approx(b.search_region)
    assert a.has_forward == b.has_forward
    assert a.has_backward == b.has_backward
    assert a.result_version == b.result_version
    assert a.media_fingerprint == b.media_fingerprint
    assert a.algorithm_version == b.algorithm_version
    assert a.base_geometry == b.base_geometry
    assert set(a.transforms) == set(b.transforms)
    for f in a.transforms:
        assert a.transforms[f] == b.transforms[f]
    assert set(a.manual_corrections) == set(b.manual_corrections)
    for f in a.manual_corrections:
        assert a.manual_corrections[f] == b.manual_corrections[f]
    assert set(a.observations) == set(b.observations)
    for f in a.observations:
        assert a.observations[f] == b.observations[f]


# ================================================================
# TrackingStore 序列化往返
# ================================================================


class TestStoreSerialization:
    def test_empty_store_roundtrip(self):
        store = TrackingStore()
        assert store.to_dict() == {}  # 空存储不写入冗余键
        restored = TrackingStore.from_dict({})
        assert restored.is_empty()

    def test_full_binding_roundtrip(self):
        store = TrackingStore()
        binding = _full_binding()
        store.add_binding(binding)

        payload = store.to_dict()
        restored = TrackingStore.from_dict(payload)

        assert len(restored) == 1
        got = restored.get_binding("clip-1", "shape-1")
        assert got is not None
        _assert_binding_equal(binding, got)

    def test_one_final_result_per_frame_but_sources_kept(self):
        store = TrackingStore()
        binding = _full_binding()
        store.add_binding(binding)
        restored = TrackingStore.from_dict(store.to_dict())
        got = restored.get_binding("clip-1", "shape-1")
        # 帧 63：手动修正是最终结果，自动结果仍保留在来源字典
        final = got.transform_at(63)
        assert final.status == TrackStatus.MANUAL
        assert final.translation == pytest.approx((99.0, 0.0))
        assert got.transforms[63].translation == pytest.approx((2.5, 0.0))

    def test_partial_failure_trajectory_roundtrip(self):
        store = TrackingStore()
        store.add_binding(_full_binding())
        got = TrackingStore.from_dict(store.to_dict()).get_binding("clip-1", "shape-1")
        assert got.failed_frames() == [66]
        assert got.weak_frames() == [67]
        assert got.transforms[66].failure_reason == FailureReason.OCCLUSION

    def test_manual_correction_only_binding_roundtrip(self):
        b = TrackingBinding(
            binding_id="b-manual", clip_id="c", shape_id="s",
            source_frame_start=0, source_frame_end=10, reference_frame=5,
        )
        b.set_manual_correction(_valid_transform(5, tx=3.0))
        store = TrackingStore()
        store.add_binding(b)
        got = TrackingStore.from_dict(store.to_dict()).get_binding("c", "s")
        assert got.transform_at(5).status == TrackStatus.MANUAL
        assert not got.transforms

    def test_json_serializable(self):
        """to_dict 结果必须可直接 json.dump（工程文件是 JSON）。"""
        import json

        store = TrackingStore()
        store.add_binding(_full_binding())
        text = json.dumps(store.to_dict())
        restored = TrackingStore.from_dict(json.loads(text))
        assert len(restored) == 1


# ================================================================
# ColorStackData 集成
# ================================================================


class TestColorStackIntegration:
    def test_stack_roundtrip_with_trackings(self):
        store = TrackingStore()
        store.add_binding(_full_binding())
        stack = ColorStackData(stack_id="s1", name="Primary", trackings=store.to_dict())

        restored = ColorStackData.from_dict(stack.to_dict())
        assert restored.trackings == stack.trackings

        got_store = TrackingStore.from_dict(restored.trackings)
        assert len(got_store) == 1
        _assert_binding_equal(
            store.get_binding("clip-1", "shape-1"),
            got_store.get_binding("clip-1", "shape-1"),
        )

    def test_legacy_stack_without_trackings_key(self):
        """旧工程 payload 没有 trackings 键时必须正常加载为空。"""
        legacy = {
            "stack_id": "s1",
            "name": "Primary",
            "grading_params": {},
            "curves_data": {},
        }
        stack = ColorStackData.from_dict(legacy)
        assert stack.trackings == {}
        assert TrackingStore.from_dict(stack.trackings).is_empty()

    def test_with_methods_preserve_trackings(self):
        """所有 with_* 重建方法都必须携带 trackings（防字段遗漏回归）。"""
        store = TrackingStore()
        store.add_binding(_full_binding())
        stack = ColorStackData(stack_id="s1", name="Primary", trackings=store.to_dict())

        rebuilt = [
            stack.with_grading_params({"exposure": 0.5}),
            stack.with_curves({"rgb": []}),
            stack.with_hsl_curves({}),
            stack.with_mixer_params({}),
            stack.with_soft_clip_params({}),
            stack.with_lut(None, 1.0),
            stack.with_name("renamed"),
            stack.with_trackings({}),
        ]
        for s in rebuilt[:-1]:
            assert s.trackings == stack.trackings, f"{type(s).__name__} 丢失 trackings"
        # with_trackings 显式替换
        assert rebuilt[-1].trackings == {}
        assert rebuilt[-1].grading_params == stack.grading_params

    def test_hydrator_roundtrip_via_fake_engine(self):
        """hydrator 收集 → 恢复的完整闭环（不依赖 Qt）。"""
        from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator

        class FakeLayer:
            grading_params = {}
            curves_data = {}
            hsl_curve_data = {}
            mixer_params = {}
            soft_clip_params = {}
            active_lut_path = None
            lut_intensity = 1.0

        class FakeEngine:
            def __init__(self):
                self.layers = [FakeLayer()]
                self.tracking_store = TrackingStore()

        hydrator = ColorStackHydrator()
        engine = FakeEngine()
        engine.tracking_store.add_binding(_full_binding())

        stack = hydrator.collect_stack_from_engine_and_panels("s1", engine, {})
        assert stack.trackings

        # 模拟切换片段后恢复：新引擎 + 同一份栈数据
        engine2 = FakeEngine()
        hydrator.apply_stack_to_engine(stack, engine2)
        got = engine2.tracking_store.get_binding("clip-1", "shape-1")
        assert got is not None
        _assert_binding_equal(
            engine.tracking_store.get_binding("clip-1", "shape-1"), got
        )

        # 恢复到空栈时必须清空旧绑定（不泄漏上一片段数据）
        empty_stack = ColorStackData(stack_id="s2", name="Empty")
        hydrator.apply_stack_to_engine(empty_stack, engine2)
        assert engine2.tracking_store.is_empty()
