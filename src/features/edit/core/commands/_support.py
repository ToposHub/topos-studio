"""Shared helpers for the commands package (P6 split).

assert_track_editable: unified track-lock guard shared by the clip and asset
clusters (also imported directly from the package by edit_controller).
ProjectTransactionResult: project-transaction result protocol shared by the
asset cluster (UpdateTimelineSettingsCommand) and the infra stacks
(ProjectCommandStack / ProjectMacroCommand).
_clips_from_dicts / _transitions_from_dicts: whole-track clip / transition
snapshot deserializers shared by the clip cluster (clip_commands.py) and the
remaining ripple/batch-delete commands in __init__.py (added 7th cut).
Sources are byte-identical to commands/__init__.py at extraction time.
"""


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.features.edit.core.timeline_v2 import ClipInstance, TimelineSequence

def assert_track_editable(
    timeline: TimelineSequence, track_id: str, operation: str
) -> None:
    """C09/INT-05 统一轨锁守卫：内容写操作不得绕过锁定轨道。

    菜单、快捷键、AI、脚本等任何入口最终都经命令 apply 落到时间线，
    在这里统一拒绝可保证锁策略一致（此前 ToggleEnabled/SetLinked/
    ChangeSpeed/Split 的 apply 都不检查 track.is_locked，锁轨片段仍可
    被变速/禁用/切分）。只拦 apply，不拦 undo——撤销必须能恢复合法
    历史，锁状态只由专门的轨道锁命令改变。
    """
    if not track_id:
        return
    track = timeline.get_track_by_id(track_id)
    if track is not None and track.is_locked:
        raise ValueError(
            f"{operation} rejected: track '{track.name or track.track_id}' is locked"
        )


def _clips_from_dicts(data: Any) -> tuple | None:
    """C02：反序列化整轨 clip 快照（None 透传表示无快照）。"""
    if not data:
        return None
    return tuple(ClipInstance.from_dict(item) for item in data)


def _transitions_from_dicts(data: Any) -> tuple | None:
    """C02：反序列化整轨转场快照。"""
    if not data:
        return None
    from src.features.edit.core.timeline_v2 import TransitionInstance

    return tuple(TransitionInstance.from_dict(item) for item in data)


@dataclass(frozen=True)
class ProjectTransactionResult:
    """N20/RF04：项目级事务的结构化结果协议。

    project：提交后的新工程（发布 revision 由 ProjectCommandStack 统一
    单调推进）；sequence_map：{timeline_id: 内容序列}——内容快照**不携
    发布 revision 语义**，消费方（controller）落缓存时按自己当前序列
    revision 单调 +1（此前命令内固定 before+1，redo 后序列 revision 从
    9 倒退回 3）；updated_command：入栈的更新后命令。
    """

    project: Any
    sequence_map: dict = field(default_factory=dict)
    updated_command: Any = None
