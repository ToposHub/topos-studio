"""Shared merge contract for composition records (审计 U-01/U-11/U-12).

工程里的 composition 记录同时承载两类字段：

- graph-owned 字段（nodes/connections/layers/...）：由合成页面序列化，
  每次保存/同步都应该用最新 payload 覆盖；
- 来源/身份/扩展字段（comp_clip_id、comp_asset_id、source_kind、
  timeline_id/clip_id/source_clip_id、width/height、未知扩展字段...）：
  页面序列化不包含它们，整记录替换会把它们清空。

live sync（CompositeController）与显式保存（SaveRouter）必须共用同一
merge 契约，避免两处白名单漂移。viewer_state 内部同样按键合并：图编辑器
键（selected_node_id、output_target_* 等）来自 graph payload，显示查看器
键（zoom_level、exposure 等）来自显示状态，未知键保留既值。
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, Optional

# Graph-owned payload fields. ``layers``/``layer_stacks`` 必须在内（U-01）：
# 图层顺序/可见性/父子级只改这两个字段时也要触发同步与落盘。
GRAPH_OWNED_KEYS: tuple[str, ...] = (
    "composite_schema_version",
    "nodes",
    "connections",
    "viewer_state",
    "viewport_state",
    "backdrops",
    "sticky_notes",
    "groups",
    "fps",
    "duration_frames",
    "layers",
    "layer_stacks",
)


def merge_viewer_state(
    existing: Optional[dict],
    graph_state: Optional[dict],
    display_state: Optional[dict],
) -> Dict[str, Any]:
    """Merge the two viewer_state sources plus preserved unknown keys.

    冲突键所有权：显示查看器键（zoom_level、exposure...）与图编辑器键
    （selected_node_id、output_target_*...）命名不相交，按
    ``existing → graph → display`` 顺序覆盖即可表达所有权；任何一方不
    提供的未知键保留既值，不被整字典替换清空（U-12）。
    """
    merged: Dict[str, Any] = {}
    if isinstance(existing, dict):
        merged.update(copy.deepcopy(existing))
    if isinstance(graph_state, dict):
        merged.update(copy.deepcopy(graph_state))
    if isinstance(display_state, dict):
        merged.update(copy.deepcopy(display_state))
    return merged


def _sync_output_dimensions(merged: Dict[str, Any]) -> None:
    """Sync graph-level width/height metadata from the Output node.

    权威节点分辨率与 composition 元数据保持一致（导出/渲染 fallback 读取
    graph_data["width"]）。
    """
    for node in (merged.get("nodes") or []):
        if isinstance(node, dict) and node.get("type") == "output":
            params = node.get("parameters") or {}
            width = params.get("width")
            height = params.get("height")
            if isinstance(width, (int, float)) and not isinstance(width, bool):
                merged["width"] = int(width)
            if isinstance(height, (int, float)) and not isinstance(height, bool):
                merged["height"] = int(height)
            break


def merge_composition_record(
    existing: Optional[dict],
    graph_data: dict,
    composite_id: str,
    display_viewer_state: Optional[dict] = None,
    modified_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Merge a live graph payload into an existing composition record.

    Args:
        existing: 当前工程里已有的 composition 记录（可能为 None/非 dict）。
        graph_data: 页面/图序列化 payload（graph-owned 字段的来源）。
        composite_id: 目标 composition key；强制写回记录，防止 payload
            携带陈旧 ID。
        display_viewer_state: 显示查看器状态（可选）；与 graph 的
            viewer_state 按键合并而不是替换。
        modified_at: 修改时间戳；默认当前时间。

    Returns:
        合并后的新记录（深拷贝，不与 live graph 共享可变对象）。
    """
    base = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    if base:
        merged: Dict[str, Any] = dict(base)
    else:
        # 新记录：graph payload 的非 graph-owned 键也一并保留（没有可
        # 保护的既有数据），避免新建路径丢字段。
        merged = (copy.deepcopy(graph_data)
                  if isinstance(graph_data, dict) else {})

    for key in GRAPH_OWNED_KEYS:
        if key in graph_data:
            merged[key] = copy.deepcopy(graph_data[key])

    # viewer_state 按键合并；三方都未提供时不凭空造空键，避免同步比较
    # 误判 dirty。
    if (isinstance(base.get("viewer_state"), dict)
            or isinstance(graph_data.get("viewer_state"), dict)
            or isinstance(display_viewer_state, dict)):
        merged["viewer_state"] = merge_viewer_state(
            base.get("viewer_state"),
            graph_data.get("viewer_state"),
            display_viewer_state,
        )

    merged["composite_id"] = composite_id
    merged["modified_at"] = (
        modified_at if modified_at is not None else time.time())

    _sync_output_dimensions(merged)
    return merged
