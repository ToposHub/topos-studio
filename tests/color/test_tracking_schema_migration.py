"""阶段 1 验收：跟踪数据 schema 迁移与损坏容忍。

覆盖：
- 无 schema_version 的初始格式（v0）自动迁移到当前版本。
- 高于当前版本的未来数据：警告但尽力加载，不崩溃。
- 单条绑定损坏时跳过该条，其余正常加载。
- 旧工程完全没有 trackings 键时各入口正常返回空存储。
"""

import logging

import numpy as np

from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.types import (
    TrackStatus,
    TrackTransform,
    TrackingMode,
)


def _translation_matrix(tx: float, ty: float) -> np.ndarray:
    return np.array([[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]])


def _transform_dict(frame: int, tx: float) -> dict:
    t = TrackTransform.from_matrix(
        frame, _translation_matrix(tx, 0.0), 0.9, TrackStatus.VALID
    )
    return t.to_dict()


def _binding_dict(binding_id: str = "b1") -> dict:
    return {
        "binding_id": binding_id,
        "clip_id": "clip-1",
        "shape_id": "shape-1",
        "source_frame_start": 0,
        "source_frame_end": 100,
        "reference_frame": 50,
        "mode": "trs",
        "pattern_region": [0.4, 0.4, 0.2, 0.2],
        "search_region": [0.3, 0.3, 0.4, 0.4],
        "transforms": {"50": _transform_dict(50, 1.0)},
        "manual_corrections": {},
        "has_forward": True,
        "has_backward": False,
    }


class TestSchemaMigration:
    def test_current_version_roundtrip(self):
        payload = {
            "schema_version": TrackingStore.SCHEMA_VERSION,
            "bindings": [_binding_dict()],
        }
        store = TrackingStore.from_dict(payload)
        assert len(store) == 1
        b = store.get_binding("clip-1", "shape-1")
        assert b.mode == TrackingMode.TRANSLATION_ROTATION_SCALE
        assert b.transform_at(50).status == TrackStatus.VALID

    def test_v0_payload_without_schema_version_migrates(self):
        """初始格式没有 schema_version 键 → 按 v0 迁移补齐。"""
        payload = {"bindings": [_binding_dict()]}
        store = TrackingStore.from_dict(payload)
        assert len(store) == 1
        assert store.get_binding("clip-1", "shape-1") is not None
        # 迁移不改变原始输入对象
        assert "schema_version" not in payload

    def test_future_version_loads_best_effort_with_warning(self, caplog):
        payload = {
            "schema_version": TrackingStore.SCHEMA_VERSION + 99,
            "bindings": [_binding_dict()],
        }
        with caplog.at_level(logging.WARNING):
            store = TrackingStore.from_dict(payload)
        assert len(store) == 1
        assert "schema_version" in caplog.text

    def test_corrupted_binding_skipped_but_others_load(self, caplog):
        good = _binding_dict("b-good")
        corrupted = _binding_dict("b-bad")
        corrupted["transforms"] = "not-a-dict"  # 非法结构 → from_dict 抛异常
        payload = {
            "schema_version": TrackingStore.SCHEMA_VERSION,
            "bindings": [corrupted, good],
        }
        with caplog.at_level(logging.WARNING):
            store = TrackingStore.from_dict(payload)
        assert len(store) == 1
        assert store.get_binding_by_id("b-good") is not None
        assert "损坏" in caplog.text

    def test_binding_missing_fields_get_defaults(self):
        """早期写入的绑定缺少部分字段时按默认值补齐。"""
        minimal = {"binding_id": "b-min", "clip_id": "c", "shape_id": "s"}
        store = TrackingStore.from_dict(
            {"schema_version": TrackingStore.SCHEMA_VERSION, "bindings": [minimal]}
        )
        b = store.get_binding("c", "s")
        assert b is not None
        assert b.reference_frame == 0
        assert b.transforms == {}
        assert b.manual_corrections == {}


class TestLegacyProjectCompatibility:
    def test_none_payload(self):
        assert TrackingStore.from_dict(None).is_empty()

    def test_empty_payload(self):
        assert TrackingStore.from_dict({}).is_empty()

    def test_payload_without_bindings_key(self):
        store = TrackingStore.from_dict({"schema_version": 1})
        assert store.is_empty()

    def test_color_stack_without_trackings_key(self):
        """旧工程 ColorStackData payload 无 trackings → 空字典，不抛异常。"""
        from src.shared.project.color_stack import ColorStackData

        stack = ColorStackData.from_dict({"stack_id": "s1", "name": "old"})
        assert stack.trackings == {}
        assert TrackingStore.from_dict(stack.trackings).is_empty()

    def test_save_payload_omits_trackings_when_empty(self):
        """空存储序列化返回 {}，保存路径不应写入 trackings 键。

        直接验证 TrackingStore 契约；保存路径按 truthiness 条件写入。
        """
        assert not TrackingStore().to_dict()
