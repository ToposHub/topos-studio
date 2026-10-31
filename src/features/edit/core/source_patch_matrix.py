"""M13.1/M13.2 · 源 patch 完整矩阵与剪辑边界导航（模型层）。

- ``SourcePatchSpec.channel_matrix``：逐源声道 × 逐目标通道的完整布线
  矩阵（轨头引脚 UI 的数据形态；未映射目标 = 静音轨，显式可见）；
- ``resolve_mixed_values``：属性面板多选混合值判定；
- ``nearest_edit_boundary``：时间线编辑边界导航（JKL 预后卷落点 /
  ↑↓ 键跳切点 / 普通 trim 的左边界帧定位）——纯函数，键盘/Trim
  Viewer 控制器共用。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence, Tuple

from src.features.edit.core.source_patch import SourcePatchSpec


def channel_matrix(
    spec: SourcePatchSpec, n_source_channels: int, n_target_channels: Optional[int] = None,
) -> "tuple[tuple[bool, ...], ...]":
    """逐源声道 × 逐目标通道布线矩阵（M13.1 轨头引脚 UI 数据形态）。

    ``matrix[target][source] = True`` 表示该源声道接入该目标通道；
    目标行全 False = 静音轨（未映射显式可见，不静默丢）。无映射 =
    恒等直通矩阵（min(n_source, n_target) 对角线）。

    M13.1 修复：无显式目标宽且映射稀疏（如 dst {0,2} 两项）时，
    目标宽 = ``max(dst)+1``——中间未映射目标行成为显式静音行
    （此前按 len(map) 取宽，dst≥宽的映射被静默丢弃）。
    """
    if spec.audio_channel_map is None:
        n_target = n_target_channels or n_source_channels
        return tuple(
            tuple(t == s and s < n_source_channels for s in range(n_source_channels))
            for t in range(n_target))
    if n_target_channels is None:
        n_target = (
            max(dst for _src, dst in spec.audio_channel_map) + 1
            if spec.audio_channel_map else n_source_channels)
    else:
        n_target = n_target_channels
    rows = [[False] * n_source_channels for _ in range(n_target)]
    for src, dst in spec.audio_channel_map:
        if dst < n_target and src < n_source_channels:
            rows[dst][src] = True
    return tuple(tuple(row) for row in rows)


#: 混合值哨兵（属性面板三态显示）
MIXED = "mixed"


def resolve_mixed_values(values: Sequence[Any]) -> Any:
    """多选属性混合值判定：全同 → 该值；不同 → MIXED 哨兵。"""
    if not values:
        return MIXED
    first = values[0]
    for v in values[1:]:
        if v != first:
            return MIXED
    return first


def nearest_edit_boundary(
    boundaries: Iterable[int], frame: int, *,
    direction: str = "next",
    include_current: bool = False,
) -> Optional[int]:
    """剪辑编辑边界导航（M13.2）。

    Args:
        boundaries: 候选边界帧集合（切点/片段头）。
        direction: "next"（向右找首个 > frame 的边界——JKL 预卷落点/
            下一编辑）或 "prev"（向左——后卷/上一编辑/普通 trim 左
            边界帧）。
        include_current: True 时 frame 自身也算边界（落点即切点）。
    """
    ordered = sorted({int(b) for b in boundaries})
    if not ordered:
        return None
    if direction == "next":
        for b in ordered:
            if b > frame or (include_current and b == frame):
                return b
        return None
    for b in reversed(ordered):
        if b < frame or (include_current and b == frame):
            return b
    return None


def clip_boundaries(clips: Sequence[Any]) -> Tuple[int, ...]:
    """全部片段头边界（导航候选集：片段起点即编辑边界）。"""
    return tuple(sorted({
        int(getattr(c, "timeline_start", 0) or 0) for c in clips}))


__all__ = [
    "MIXED",
    "channel_matrix",
    "resolve_mixed_values",
    "nearest_edit_boundary",
    "clip_boundaries",
]
