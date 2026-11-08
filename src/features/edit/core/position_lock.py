"""W06：Position Lock 统一编辑策略（接线计划契约 D3）。

锁 = 冻结 clip 的时间线足迹 ``(track_id, timeline_start, timeline_duration)``。
判定不依赖命令名称或黑名单，而是对提交前的时间线做 **footprint 级
dry-run diff**：命令先 ``apply``（命令纯数据、apply 返回新实例不改输入），
对比前后所有既有 clip 的足迹——任一被锁 clip 足迹变化或被移除 → 拒绝；
纯新增 clip 不算违规。

接入点（D3）：
- ``TimelineView._execute_command`` —— 覆盖鼠标/键盘/菜单/拖放全部
  UI 编辑；undo/redo 不经此路径（历史恢复优先，允许）。
- ``EditProjectService.commit_command(position_locked_clip_ids=...)`` ——
  AI/service 显式传入锁定策略，同一判定函数。

拒绝语义：``PositionLockViolation``（ValueError 子类）——调用方按既有
ValueError 反馈路径显示；命令未执行、零 revision、零 history。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from src.features.edit.core.timeline_v2 import TimelineSequence


class PositionLockViolation(ValueError):
    """命令会改变被锁 clip 的时间线足迹（契约 D3）。"""


@dataclass(frozen=True)
class FootprintChange:
    """单个既有 clip 的足迹变化描述（新增 clip 不产生条目）。"""

    clip_id: str
    kind: str  # "removed" | "track" | "start" | "duration"
    before: str
    after: str

    def describe(self) -> str:
        return f"clip {self.clip_id}: {self.kind} {self.before} -> {self.after}"


def _clips_by_id(timeline: TimelineSequence) -> dict[str, tuple[str, Any]]:
    """clip_id → (track_id, clip)；跨 video/audio 全轨收集。"""
    result: dict[str, tuple[str, Any]] = {}
    for track in (list(timeline.video_tracks) + list(timeline.audio_tracks)):
        for clip in track.clips:
            result.setdefault(clip.clip_id, (track.track_id, clip))
    return result


def footprint_changes(
    before: TimelineSequence, after: TimelineSequence,
) -> tuple[FootprintChange, ...]:
    """对比两个时间线快照中既有 clip 的足迹变化。

    只检查 ``before`` 中已存在的 clip（新增 clip 不受锁约束）；
    subtitle 轨不在 Position Lock 语义内（字幕布局有独立锁链）。
    """
    after_clips = _clips_by_id(after)
    changes: list[FootprintChange] = []
    for clip_id, (track_id, clip) in _clips_by_id(before).items():
        found = after_clips.get(clip_id)
        if found is None:
            changes.append(FootprintChange(
                clip_id, "removed", f"{track_id}@{clip.timeline_start}"
                f"+{clip.timeline_duration}", "gone"))
            continue
        new_track_id, new_clip = found
        if new_track_id != track_id:
            changes.append(FootprintChange(
                clip_id, "track", track_id, new_track_id))
        elif new_clip.timeline_start != clip.timeline_start:
            changes.append(FootprintChange(
                clip_id, "start", str(clip.timeline_start),
                str(new_clip.timeline_start)))
        elif new_clip.timeline_duration != clip.timeline_duration:
            changes.append(FootprintChange(
                clip_id, "duration", str(clip.timeline_duration),
                str(new_clip.timeline_duration)))
    return tuple(changes)


def _unwrap_apply_result(result: Any) -> Optional[TimelineSequence]:
    """归一 apply 返回值：宏命令可能返回 (timeline, updated_command)。"""
    if isinstance(result, tuple):
        result = result[0] if result else None
    return result if isinstance(result, TimelineSequence) else None


def evaluate_command_footprint(
    command: Any,
    timeline: TimelineSequence,
    locked_clip_ids: Optional[Iterable[str]] = None,
) -> tuple[FootprintChange, ...]:
    """Dry-run 命令并校验锁策略；未违规时返回空元组。

    Args:
        command: 待提交的时间线命令（纯数据、apply 不改输入时间线）。
        timeline: 提交前的当前时间线快照。
        locked_clip_ids: 被锁 clip ID 集合；``None`` 表示会话级全局锁
            （所有既有 clip 都被锁，对应工具栏 Position Lock 开启）。
            显式集合用于 AI/service 策略（只锁指定 clip）。

    Raises:
        PositionLockViolation: 命令会改变被锁 clip 的足迹。
        ValueError: 命令本身非法（与直接提交时相同的预检失败）。

    Returns:
        空元组（违规场景总是抛异常而非返回）。
    """
    after = _unwrap_apply_result(command.apply(timeline))
    if after is None or after is timeline:
        return ()  # no-op 或非时间线结果——不改足迹
    changes = footprint_changes(timeline, after)
    if not changes:
        return ()
    if locked_clip_ids is None:
        violations = changes
    else:
        locked = set(locked_clip_ids)
        violations = tuple(c for c in changes if c.clip_id in locked)
    if violations:
        first = violations[0].describe()
        raise PositionLockViolation(
            f"Position lock: edit rejected ({first}); "
            f"{len(violations)} locked clip(s) would move, resize or vanish")
    return ()


def evaluate_command_footprint_safe(
    command: Any,
    timeline: TimelineSequence,
    locked_clip_ids: Optional[Iterable[str]] = None,
) -> tuple[bool, Optional[str]]:
    """非抛出版本：返回 (allowed, reason)；供 service 层 CommitResult 使用。"""
    try:
        evaluate_command_footprint(command, timeline, locked_clip_ids)
    except PositionLockViolation as exc:
        return False, str(exc)
    return True, None
