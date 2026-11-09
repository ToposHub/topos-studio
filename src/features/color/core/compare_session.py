"""CV-B04：多图／多版本比较会话——与 Wipe 隔离的只读后端域。

缺口（计划 CV-B04）：``ImageEngine.set_split_view_mode`` 的 split 是
wipe（单图 vs 参考/original），不是多图网格比较；真正的 Split Screen
需要独立的比较会话：只读快照、可释放、与 wipe 互不污染（切换时可恢复
原参考与分割位置）。

本模块是纯领域层（无 Qt／GL 依赖）：

- ``CompareItem``：比较项的身份与 provenance（project/asset/clip/
  version/revision/source frame/size/color context），缩略图保真度如实
  标注（JPEG 缩略图只用于列表，不冒充 HDR 比较 master）；
- ``ColorCompareSession``：会话状态机——begin/结束、上限（首版 ≤4
  项）、去重（缓存键）、失效（删版本／切工程立即移除对应项，不显示
  旧图）、LRU 字节上限、release 释放一切在途/完成快照；
- 会话**不**触碰 engine 的 split/reference/compare_mode——wipe 与
  A/B 的 API、快捷键、原图回退行为保持不变（宿主在进入比较时自行
  保存/恢复 wipe 状态）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 首版限制：参考项数量与呈现布局（静态 still／同帧版本比较）。
MAX_COMPARE_ITEMS = 4
VALID_LAYOUTS = ("single", "side_by_side", "grid_2x2")

# 缩略图保真度：jpeg_thumb 只可用于列表，不得作为 HDR 比较 master。
FIDELITY_LEVELS = ("jpeg_thumb", "uint8_still", "hdr_exr", "full_float")
_HDR_READY_FIDELITY = ("hdr_exr", "full_float")


@dataclass(frozen=True)
class CompareItem:
    """一个比较项：身份 + provenance + 资源描述（不含像素）。"""

    item_id: str
    label: str
    kind: str                      # gallery_still | clip_version | original
    project_id: Optional[str] = None
    asset_id: Optional[str] = None
    clip_id: Optional[str] = None
    version_id: Optional[str] = None
    revision: int = 0
    source_frame: Optional[int] = None
    width: int = 0
    height: int = 0
    color_context: str = ""        # 输入/工作空间 + 显示输出的稳定标识
    fidelity: str = "uint8_still"
    approx_bytes: int = 0          # 快照内存占用估计（宿主按像素估）

    def __post_init__(self) -> None:
        if self.fidelity not in FIDELITY_LEVELS:
            raise ValueError(f"unknown fidelity: {self.fidelity}")
        if not self.item_id:
            raise ValueError("item_id cannot be empty")

    def cache_key(self) -> str:
        """缓存键：内容身份的稳定摘要（含 revision/frame/尺寸/色彩）。"""
        identity = {
            "kind": self.kind,
            # Gallery stills have their own stable identity; unlike versions,
            # they may have no clip/asset provenance at all.
            "gallery_item_id": self.item_id if self.kind == "gallery_still" else None,
            "project_id": self.project_id,
            "asset_id": self.asset_id,
            "clip_id": self.clip_id,
            "version_id": self.version_id,
            "revision": self.revision,
            "source_frame": self.source_frame,
            "size": [self.width, self.height],
            "color": self.color_context,
            "fidelity": self.fidelity,
        }
        blob = json.dumps(identity, sort_keys=True,
                          ensure_ascii=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def is_hdr_compare_ready(self) -> bool:
        return self.fidelity in _HDR_READY_FIDELITY

    def matches_invalidation(self, *, project_id: Optional[str] = None,
                             asset_id: Optional[str] = None,
                             version_id: Optional[str] = None) -> bool:
        """删除版本／切工程等失效事件是否命中本项。"""
        if project_id is not None and self.project_id == project_id:
            return True
        if asset_id is not None and self.asset_id == asset_id:
            return True
        if version_id is not None and self.version_id == version_id:
            return True
        return False


@dataclass
class CompareSlot:
    """会话内一个已接纳的比较项（含呈现状态）。"""

    item: CompareItem
    cache_key: str
    fit: str = "contain"           # contain | fill（不同尺寸的明确 fit）
    pinned: bool = False           # 锁帧项（同步/锁帧规则：仅静态帧可锁）


@dataclass
class ColorCompareSession:
    """与 wipe 隔离的比较会话（首版：静态参考／同帧版本，≤4 项）。

    只管理身份与呈现状态；像素快照的渲染/释放回调由宿主注入
    （``on_release``），会话保证调用顺序与次数可预测。
    """

    max_items: int = MAX_COMPARE_ITEMS
    max_cache_bytes: int = 512 * 1024 * 1024
    anchor_clip_id: Optional[str] = None
    anchor_source_frame: Optional[int] = None
    color_context: str = ""
    layout: str = "side_by_side"
    # 进入比较时宿主保存的 wipe 状态（退出恢复；会话不直接改引擎）。
    saved_wipe_state: Optional[Dict[str, Any]] = None
    slots: List[CompareSlot] = field(default_factory=list)
    active: bool = False
    released_keys: Tuple[str, ...] = ()
    _cache_bytes: int = 0
    _on_release=None  # callable(cache_key) —— 宿主释放纹理/快照

    def __post_init__(self) -> None:
        if self.max_items < 1 or self.max_items > MAX_COMPARE_ITEMS:
            raise ValueError("first version supports 1-4 compare items")

    # ── 会话生命周期 ──────────────────────────────────────────────────

    def begin(self, items, layout: str = "side_by_side",
              *, anchor_clip_id: Optional[str] = None,
              anchor_source_frame: Optional[int] = None,
              color_context: str = "",
              saved_wipe_state: Optional[Dict[str, Any]] = None,
              on_release=None) -> Tuple[List[str], List[str]]:
        """开启（或重建）比较会话。

        返回 ``(accepted_ids, rejected)``：rejected 是被拒绝的 item_id
        与原因对。全部项被拒时会话不进入 active。重复 cache_key 去重；
        HDR 比较项用 jpeg_thumb 保真度直接拒绝（不冒充 master）。
        """
        self.release()
        if layout not in VALID_LAYOUTS:
            raise ValueError(f"unknown layout: {layout}")
        self.layout = layout
        self.anchor_clip_id = anchor_clip_id
        self.anchor_source_frame = anchor_source_frame
        self.color_context = color_context
        self.saved_wipe_state = dict(saved_wipe_state or {})
        self._on_release = on_release
        accepted: List[str] = []
        rejected: List[str] = []
        seen_keys = set()
        for item in items:
            if not isinstance(item, CompareItem):
                rejected.append((getattr(item, "item_id", "?"),
                                 "not_a_compare_item"))
                continue
            if len(self.slots) >= self.max_items:
                rejected.append((item.item_id, "too_many_items"))
                continue
            key = item.cache_key()
            if key in seen_keys:
                rejected.append((item.item_id, "duplicate"))
                continue
            if item.fidelity == "jpeg_thumb":
                rejected.append((item.item_id,
                                 "jpeg_thumb_not_compare_master"))
                continue
            if item.approx_bytes > self.max_cache_bytes:
                rejected.append((item.item_id, "exceeds_memory_budget"))
                continue
            if self._cache_bytes + item.approx_bytes > self.max_cache_bytes:
                self._evict_until_fits(item.approx_bytes, protect=key)
            if self._cache_bytes + item.approx_bytes > self.max_cache_bytes:
                rejected.append((item.item_id, "memory_budget_exhausted"))
                continue
            seen_keys.add(key)
            self.slots.append(CompareSlot(item=item, cache_key=key))
            self._cache_bytes += item.approx_bytes
            accepted.append(item.item_id)
        self.active = bool(self.slots)
        return accepted, rejected

    def end(self) -> Optional[Dict[str, Any]]:
        """结束会话并返回进入时保存的 wipe 状态（供宿主恢复）。"""
        saved = self.saved_wipe_state
        self.release()
        self.active = False
        self.layout = "side_by_side"
        self.anchor_clip_id = None
        self.anchor_source_frame = None
        self.saved_wipe_state = None
        return saved

    def release(self) -> None:
        """释放全部快照（在途/完成一并），会话回到空。"""
        released = []
        for slot in self.slots:
            self._notify_release(slot.cache_key)
            released.append(slot.cache_key)
        self.released_keys = tuple(released)
        self.slots = []
        self._cache_bytes = 0
        self.active = False

    # ── 失效与诊断 ────────────────────────────────────────────────────

    def invalidate(self, *, project_id: Optional[str] = None,
                   asset_id: Optional[str] = None,
                   version_id: Optional[str] = None) -> int:
        """删除版本／切工程：命中的项立即移除并释放（不显示旧图）。

        全部项被移除时会话自动结束（active=False）。
        """
        keep: List[CompareSlot] = []
        removed = 0
        for slot in self.slots:
            if slot.item.matches_invalidation(
                    project_id=project_id, asset_id=asset_id,
                    version_id=version_id):
                self._cache_bytes -= min(slot.item.approx_bytes,
                                         self._cache_bytes)
                self._notify_release(slot.cache_key)
                removed += 1
            else:
                keep.append(slot)
        self.slots = keep
        if not self.slots:
            self.active = False
        return removed

    def diagnostics(self) -> Dict[str, Any]:
        """JSON 可序列化诊断（缓存上限、命中项、保真度分布）。"""
        return {
            "active": self.active,
            "layout": self.layout,
            "items": [
                {
                    "item_id": slot.item.item_id,
                    "label": slot.item.label,
                    "kind": slot.item.kind,
                    "version_id": slot.item.version_id,
                    "revision": slot.item.revision,
                    "source_frame": slot.item.source_frame,
                    "cache_key": slot.cache_key,
                    "fidelity": slot.item.fidelity,
                    "hdr_ready": slot.item.is_hdr_compare_ready(),
                    "fit": slot.fit,
                }
                for slot in self.slots
            ],
            "cache_bytes": self._cache_bytes,
            "max_cache_bytes": self.max_cache_bytes,
        }

    # ── 内部 ──────────────────────────────────────────────────────────

    def _notify_release(self, cache_key: str) -> None:
        callback = self._on_release
        if callback is None:
            return
        try:
            callback(cache_key)
        except Exception:  # noqa: BLE001 —— 释放失败不阻断会话管理
            import logging

            logging.getLogger(__name__).warning(
                "compare session release callback failed for %s",
                cache_key, exc_info=True)

    def _evict_until_fits(self, incoming_bytes: int, *,
                          protect: str) -> None:
        """LRU 淘汰（slots 顺序即最近使用序）直到能容纳新项。"""
        index = 0
        while (self._cache_bytes + incoming_bytes > self.max_cache_bytes
               and index < len(self.slots)):
            slot = self.slots[index]
            if slot.cache_key == protect:
                index += 1
                continue
            self._cache_bytes -= min(slot.item.approx_bytes,
                                     self._cache_bytes)
            self._notify_release(slot.cache_key)
            self.slots.pop(index)
