"""
跟踪结果存储与版本迁移（阶段 1）。

``TrackingStore`` 管理一个调色栈范围内的全部 ``TrackingBinding``：
- 按 (clip_id, shape_id) 索引，一个形状同时只允许一个绑定。
- 提供 schema_version 迁移：旧工程缺少字段时按默认值补齐。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import TRACKING_SCHEMA_VERSION

logger = logging.getLogger(__name__)


class TrackingStore:
    """跟踪绑定存储（线程约定：只在 UI 线程读写）。"""

    SCHEMA_VERSION = TRACKING_SCHEMA_VERSION

    def __init__(self) -> None:
        self._bindings: Dict[str, TrackingBinding] = {}
        # AUD-24911: (clip_id, shape_id) -> binding_id 二级索引，
        # get_binding 位于渲染每帧每形状的求值热路径，避免 O(n) 线性扫描。
        self._by_shape: Dict[Tuple[str, str], str] = {}

    # ---- 增删改查 ----

    def add_binding(self, binding: TrackingBinding, replace: bool = False) -> TrackingBinding:
        """添加绑定；同一 (clip, shape) 已存在时按 replace 决定替换或复用。"""
        existing = self.get_binding(binding.clip_id, binding.shape_id)
        if existing is not None:
            if not replace:
                raise ValueError(
                    f"绑定已存在: clip={binding.clip_id} shape={binding.shape_id}"
                )
            self.remove_binding(existing.binding_id)
        self._bindings[binding.binding_id] = binding
        self._by_shape[(binding.clip_id, binding.shape_id)] = binding.binding_id
        return binding

    def remove_binding(self, binding_id: str) -> None:
        binding = self._bindings.pop(binding_id, None)
        if binding is not None:
            key = (binding.clip_id, binding.shape_id)
            if self._by_shape.get(key) == binding_id:
                self._by_shape.pop(key, None)

    def remove_bindings_for_shape(self, clip_id: str, shape_id: str) -> None:
        binding = self.get_binding(clip_id, shape_id)
        if binding is not None:
            self.remove_binding(binding.binding_id)

    def get_binding(self, clip_id: str, shape_id: str) -> Optional[TrackingBinding]:
        binding_id = self._by_shape.get((clip_id, shape_id))
        if binding_id is None:
            return None
        return self._bindings.get(binding_id)

    def get_binding_by_id(self, binding_id: str) -> Optional[TrackingBinding]:
        return self._bindings.get(binding_id)

    def bindings_for_clip(self, clip_id: str) -> List[TrackingBinding]:
        return [b for b in self._bindings.values() if b.clip_id == clip_id]

    def all_bindings(self) -> List[TrackingBinding]:
        return list(self._bindings.values())

    def clear(self) -> None:
        self._bindings.clear()
        self._by_shape.clear()

    def __len__(self) -> int:
        return len(self._bindings)

    def is_empty(self) -> bool:
        return not self._bindings

    # ---- 序列化与迁移 ----

    def to_dict(self) -> dict:
        """序列化（写入工程调色栈 payload 的 ``trackings`` 键）。"""
        if self.is_empty():
            return {}
        return {
            "schema_version": self.SCHEMA_VERSION,
            "bindings": [b.to_dict() for b in self._bindings.values()],
        }

    @staticmethod
    def from_dict(data: Optional[dict]) -> "TrackingStore":
        """反序列化，容忍旧工程（无键/空值/缺字段）。"""
        store = TrackingStore()
        if not data:
            return store
        version = int(data.get("schema_version", 0))
        migrated = TrackingStore._migrate(data, version)
        for raw in migrated.get("bindings", []):
            try:
                binding = TrackingBinding.from_dict(raw)
            except Exception:  # noqa: BLE001 - 单条损坏不阻塞整体加载
                logger.warning("跟踪绑定数据损坏，已跳过: %r", raw, exc_info=True)
                continue
            store._bindings[binding.binding_id] = binding
            store._by_shape[(binding.clip_id, binding.shape_id)] = binding.binding_id
        return store

    @staticmethod
    def _migrate(data: dict, version: int) -> dict:
        """schema 迁移链：每个旧版本一步显式迁移，不允许静默猜测。"""
        result = dict(data)
        if version == 0:
            # v0：无 schema_version 的初始格式 → 补齐字段
            result["schema_version"] = 1
            version = 1
        if version == TrackingStore.SCHEMA_VERSION:
            return result
        # 未来版本迁移在此追加：if version == 1: ... version = 2
        logger.warning(
            "未知的跟踪数据 schema_version=%s（当前 %s），按当前版本尽力加载",
            version, TrackingStore.SCHEMA_VERSION,
        )
        result["schema_version"] = TrackingStore.SCHEMA_VERSION
        return result
