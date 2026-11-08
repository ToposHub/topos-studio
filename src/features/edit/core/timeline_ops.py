"""
Timeline Operations Module - Pure Functions for Timeline Editing (P3).

时间线纯函数操作模块。

## 设计原则

- No PyQt dependencies (纯函数，无 UI 依赖)
- No side effects (无副作用)
- No mutation of input data (不修改输入)
- All functions are testable without UI (可独立测试)

## 操作列表

- split_clip: 在指定帧分割剪辑 (刀片切割)
- trim_clip_left: 从左侧裁切剪辑
- trim_clip_right: 从右侧裁切剪辑
- move_clip: 移动剪辑位置
- delete_clip: 删除剪辑
- add_transition: 在两个剪辑间添加转场

## 吸附系统

- SnapPointType: 吸附点类型枚举
- SnapPoint: 吸附点数据类
- SnapManager: 吸附点管理器 (参考 kdenlive SnapModel)

## 参考

- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P3
- docs/edit_page_timeline_architecture.md P0.B ADR
- kdenlive timeline2/model/snapmodel.hpp/cpp (参考实现)
"""

from __future__ import annotations

from typing import Optional, Dict, List, Set, Tuple, Any
from dataclasses import dataclass, fields, replace
from enum import Enum
import uuid

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    TimelineMarker,
    EditTrack,
    ClipInstance,
    TransitionInstance,
)


class TimelineOperationError(ValueError):
    """时间线操作错误。"""
    pass


def _translate_model_error(exc: ValueError, operation: str) -> TimelineOperationError:
    """AUD-18809：把模型层（timeline_v2）抛出的裸 ValueError 转译为
    本模块公共异常契约 TimelineOperationError。

    各操作的 docstring 承诺 "Raises: TimelineOperationError"，但
    ``with_clip_added / with_clip_updated / with_transition_added`` 的
    重叠/越界校验直接抛 ``ValueError("Clip overlaps with ...")`` 等，
    未转译时会穿透到按 docstring 捕获的调用方之外。保留原始信息并
    链上根因。
    """
    return TimelineOperationError(f"{operation}: {exc}")


def copy_clip_with(clip: ClipInstance, **field_updates: Any) -> ClipInstance:
    """AUD-18800：ClipInstance 不可变更新 helper。

    语义等价于 ``dataclasses.replace(clip, **field_updates)``，但额外保证：

    1. **全部 dataclass 字段（含未来新增字段）自动保留**——调用方只覆写
       操作语义要求的字段（如 slip 只改 ``source_start``），调色联动
       （color_stack_id/color_shared）、关键帧动画（animated_params）、
       淡入淡出、混音增益、冻结帧、混合模式、RAW 参数、FPS 元数据
       （_source_fps_* / _source_pts_index）等不再因手工构造而回落默认值。
    2. **非字段动态属性不丢**——replace 会重建实例导致动态附加属性丢失，
       此处显式回拷，保证"未知新字段不丢"。

    当 ``timeline_duration`` 被缩短时，原淡入/淡出时长可能超过新时长而
    违反模型约束（ClipInstance.__post_init__ 会抛错）。这里按模型自身的
    归一化方向将未显式覆写的淡入/淡出钳制到新时长内（与 __post_init__
    的边界一致），避免修剪类操作在带淡入的片段上从"静默丢字段"变成
    "直接失败"。

    Args:
        clip: 原 ClipInstance（frozen，不会被修改）。
        **field_updates: 仅覆写操作语义要求的字段。

    Returns:
        新的 ClipInstance；除指定字段与钳制后的淡入/淡出外，其余
        字段与原实例逐字段相等。
    """
    updates: dict[str, Any] = dict(field_updates)
    new_duration: int = updates.get("timeline_duration", clip.timeline_duration)
    if "fade_in_duration" not in updates:
        updates["fade_in_duration"] = min(clip.fade_in_duration, new_duration)
    if "fade_out_duration" not in updates:
        updates["fade_out_duration"] = min(clip.fade_out_duration, new_duration)
    timeline_offset = updates.pop("mapping_timeline_offset", None)
    if "speed" not in updates and (
            timeline_offset is not None
            or updates.get("timeline_duration", clip.timeline_duration) != clip.timeline_duration):
        # A duration change is a window edit, not a slip or a plain move.
        offset = (updates.get("timeline_start", clip.timeline_start) - clip.timeline_start
                  if timeline_offset is None else int(timeline_offset))
        updates.update(clip.source_window_mapping_updates(
            updates.get("source_start", clip.source_start),
            updates.get("source_duration", clip.source_duration), offset))
    new_clip = replace(clip, **updates)
    # 回拷非 dataclass 字段的动态属性（"未知新字段不丢"回归点）。
    field_names: set[str] = {f.name for f in fields(ClipInstance)}
    for key, value in vars(clip).items():
        if key not in field_names and key not in vars(new_clip):
            object.__setattr__(new_clip, key, value)
    return new_clip


# ================================================================
# Clip Operations (剪辑操作)
# ================================================================


def split_clip(
    sequence: TimelineSequence,
    clip_id: str,
    frame: int
) -> TimelineSequence:
    """
    在指定帧位置分割剪辑。

    将剪辑分成两部分：左半部分保留原 clip_id，右半部分生成新 clip_id。
    两个剪辑的 source 范围和 timeline 范围都相应调整。

    Args:
        sequence: 时间线序列
        clip_id: 要分割的剪辑 ID
        frame: 分割位置（时间线帧号，必须在剪辑范围内）

    Returns:
        新的 TimelineSequence，包含分割后的两个剪辑

    Raises:
        TimelineOperationError: 如果剪辑不存在或分割位置无效

    Examples:
        >>> # 分割一个 100 帧的剪辑在第 50 帧
        >>> new_seq = split_clip(sequence, "clip1", 50)
        >>> # 结果：两个 50 帧的剪辑
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    if not (clip.timeline_start < frame < clip.timeline_end):
        raise TimelineOperationError(
            f"Split frame {frame} must be inside clip range "
            f"[{clip.timeline_start}, {clip.timeline_end})"
        )

    track = sequence.get_track_by_id(clip.track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {clip.track_id}")
    if track.is_locked:
        raise TimelineOperationError(f"Split clip rejected: track {clip.track_id!r} is locked")
    ls, ld, rs, rd = split_source_partition(clip, frame)
    right_id = str(uuid.uuid4())
    left = copy_clip_with(clip, timeline_duration=frame - clip.timeline_start,
                          source_start=ls, source_duration=ld)
    right = copy_clip_with(clip, clip_id=right_id, timeline_start=frame,
                           timeline_duration=clip.timeline_end - frame,
                           source_start=rs, source_duration=rd,
                           linked_clip_id=None, fade_in_duration=0)
    clips = tuple(sorted(
        (*tuple(c for c in track.clips if c.clip_id != clip_id), left, right),
        key=lambda c: c.timeline_start))
    transitions = tuple(
        replace(t, left_clip_id=right_id) if t.left_clip_id == clip_id else t
        for t in track.transitions)
    updated = sequence.with_track_updated(track._rebuild(clips=clips, transitions=transitions))
    return updated._rebuild(markers=tuple(
        replace(m, clip_id=right_id) if m.clip_id == clip_id and m.frame >= frame else m
        for m in sequence.markers))


def trim_clip_left(
    sequence: TimelineSequence,
    clip_id: str,
    new_start: int
) -> TimelineSequence:
    """
    从左侧裁切剪辑。

    改变剪辑的 timeline_start 和 source_start，相当于向右"拖动"剪辑左边界。

    Args:
        sequence: 时间线序列
        clip_id: 要裁切的剪辑 ID
        new_start: 新的起始帧（必须在当前起始帧之后，且不能超过结束帧）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑不存在或新起始帧无效
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    if new_start < clip.timeline_start:
        raise TimelineOperationError(
            f"new_start {new_start} cannot be before current start {clip.timeline_start}"
        )
    if new_start >= clip.timeline_end:
        raise TimelineOperationError(
            f"new_start {new_start} must be before end {clip.timeline_end}"
        )

    track = sequence.get_track_by_id(clip.track_id)
    if track is not None:
        prev, _ = track.get_adjacent_clips(clip_id)
        if prev is not None and new_start < prev.timeline_end:
            raise TimelineOperationError(
                f"Trim would overlap with previous clip {prev.clip_id}"
            )

    # R07：经 clip 自身方向/速度/FPS 感知映射推导新源区间（旧
    # int(delta×effective_speed) 反向/混合帧率/变速点全部错位）。
    # copy_clip_with 保留动态扩展字段（AUD-18800），fade 随钳制。
    new_source_start, new_source_duration = clip.with_left_edge_source_values(
        new_start
    )
    new_timeline_duration = clip.timeline_duration - (
        new_start - clip.timeline_start
    )
    new_clip = copy_clip_with(
        clip,
        timeline_start=new_start,
        timeline_duration=new_timeline_duration,
        source_start=new_source_start,
        source_duration=new_source_duration,
        fade_in_duration=min(clip.fade_in_duration, new_timeline_duration),
        fade_out_duration=min(clip.fade_out_duration, new_timeline_duration),
    )

    result = sequence.with_clip_updated(new_clip)
    return result._rebuild(markers=tuple(m for m in result.markers
        if m.clip_id != clip.clip_id or new_clip.timeline_start <= m.frame < new_clip.timeline_end))


def trim_clip_right(
    sequence: TimelineSequence,
    clip_id: str,
    new_end: int
) -> TimelineSequence:
    """
    从右侧裁切剪辑。

    改变剪辑的 timeline_end，相当于向左"拖动"剪辑右边界。

    Args:
        sequence: 时间线序列
        clip_id: 要裁切的剪辑 ID
        new_end: 新的结束帧（必须在当前结束帧之前，且不能小于起始帧）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑不存在或新结束帧无效
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    if new_end > clip.timeline_end:
        raise TimelineOperationError(
            f"new_end {new_end} cannot be after current end {clip.timeline_end}"
        )
    if new_end <= clip.timeline_start:
        raise TimelineOperationError(
            f"new_end {new_end} must be after start {clip.timeline_start}"
        )

    track = sequence.get_track_by_id(clip.track_id)
    if track is not None:
        _, next_clip = track.get_adjacent_clips(clip_id)
        if next_clip is not None and new_end > next_clip.timeline_start:
            raise TimelineOperationError(
                f"Trim would overlap with next clip {next_clip.clip_id}"
            )

    # R07：同 trim_clip_left——方向/速度/FPS 感知；copy_clip_with 保
    # 扩展字段，fade 随钳制（反向右修同时前移 source_start）。
    new_duration = new_end - clip.timeline_start
    new_source_start, new_source_duration = clip.with_right_edge_source_values(
        new_duration
    )
    new_clip = copy_clip_with(
        clip,
        timeline_duration=new_duration,
        source_start=new_source_start,
        source_duration=new_source_duration,
        fade_in_duration=min(clip.fade_in_duration, new_duration),
        fade_out_duration=min(clip.fade_out_duration, new_duration),
    )

    result = sequence.with_clip_updated(new_clip)
    return result._rebuild(markers=tuple(m for m in result.markers
        if m.clip_id != clip.clip_id or new_clip.timeline_start <= m.frame < new_clip.timeline_end))


def move_clip(
    sequence: TimelineSequence,
    clip_id: str,
    new_track_id: str,
    new_start: int,
    ripple: bool = False
) -> TimelineSequence:
    """
    移动剪辑到新位置/轨道。

    Args:
        sequence: 时间线序列
        clip_id: 要移动的剪辑 ID
        new_track_id: 目标轨道 ID
        new_start: 新的起始帧
        ripple: 是否 ripple（后续剪辑跟随移动）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑或轨道不存在，或移动会导致重叠
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    new_track = sequence.get_track_by_id(new_track_id)
    if new_track is None:
        raise TimelineOperationError(f"Track not found: {new_track_id}")

    offset = new_start - clip.timeline_start

    moved_clip = copy_clip_with(  # AUD-18806：replace 语义保留全部扩展字段
        clip,
        track_id=new_track_id,
        timeline_start=new_start,
    )

    if new_track_id == clip.track_id:
        other_clips = tuple(c for c in new_track.clips if c.clip_id != clip_id)
        for other in other_clips:
            if (moved_clip.timeline_start < other.timeline_end and
                moved_clip.timeline_end > other.timeline_start):
                raise TimelineOperationError(
                    f"Move would overlap with clip {other.clip_id}"
                )

    if new_track_id != clip.track_id:
        old_track = sequence.get_track_by_id(clip.track_id)
        if old_track is not None:
            old_track_without = old_track.with_clip_removed(clip_id)
            sequence = sequence.with_track_updated(old_track_without)

        # AUD-18809：跨轨落点的重叠校验由模型层 with_clip_added 抛
        # ValueError——转译为公共异常契约（同轨分支上方已显式校验）。
        try:
            new_track_with = new_track.with_clip_added(moved_clip)
        except ValueError as exc:
            raise _translate_model_error(exc, f"Move clip {clip_id} to track {new_track_id}") from exc
        sequence = sequence.with_track_updated(new_track_with)
    else:
        try:
            new_track_with = new_track.with_clip_updated(moved_clip)
        except ValueError as exc:
            raise _translate_model_error(exc, f"Move clip {clip_id}") from exc
        sequence = sequence.with_track_updated(new_track_with)

    if ripple and new_track_id == clip.track_id:
        track = sequence.get_track_by_id(new_track_id)
        if track is not None:
            clips_after = tuple(
                c for c in track.clips
                if c.timeline_start > moved_clip.timeline_end
            )
            # R08：跟随片段的动画 key 与 scoped marker 共同位移（此前只
            # 改 timeline_start，局部曲线错位）
            for after_clip in clips_after:
                new_after_start = after_clip.timeline_start + offset
                new_after = after_clip.with_timeline_start(
                    new_after_start
                ).with_temporal_shift(offset)
                track = track.with_clip_updated(new_after)
                sequence = sequence._rebuild(
                    markers=tuple(
                        replace(
                            marker, frame=max(0, marker.frame + offset),
                        )
                        if marker.clip_id == after_clip.clip_id
                        else marker
                        for marker in sequence.markers
                    )
                )
            sequence = sequence.with_track_updated(track)

    return sequence


def delete_clip(
    sequence: TimelineSequence,
    clip_id: str,
    ripple: bool = False
) -> TimelineSequence:
    """
    删除剪辑。

    Args:
        sequence: 时间线序列
        clip_id: 要删除的剪辑 ID
        ripple: 是否 ripple（后续剪辑填补空隙）

    Returns:
        新的 TimelineSequence
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    clip_duration = clip.timeline_duration
    sequence = sequence.with_clip_removed(clip_id)
    sequence = sequence._rebuild(
        markers=tuple(
            marker for marker in sequence.markers
            if marker.clip_id != clip_id
        )
    )

    if ripple:
        track = sequence.get_track_by_id(clip.track_id)
        if track is not None:
            clips_after = tuple(
                c for c in track.clips
                if c.timeline_start >= clip.timeline_end
            )
            for after_clip in clips_after:
                new_start = after_clip.timeline_start - clip_duration
                # R08：动画 key 随 clip 位移（此前只改 start，局部曲线
                # 值错位；marker 原已处理）
                marker_delta = new_start - after_clip.timeline_start
                new_after = after_clip.with_timeline_start(
                    new_start
                ).with_temporal_shift(marker_delta)
                track = track.with_clip_updated(new_after)
                sequence = sequence._rebuild(
                    markers=tuple(
                        replace(
                            marker,
                            frame=max(0, marker.frame + marker_delta),
                        )
                        if marker.clip_id == after_clip.clip_id
                        else marker
                        for marker in sequence.markers
                    )
                )
            sequence = sequence.with_track_updated(track)

    return sequence


def build_range_delete_commands(
    sequence: TimelineSequence,
    track_ids: "List[str]",
    start_frame: int,
    end_frame: int,
    ripple: bool = False,
) -> "List[object]":
    """Build the command sequence for a range Lift / Extract (P1-03).

    Semantics per track (professional NLE Lift = ``ripple=False``,
    Extract = ``ripple=True``):

    - Clips fully inside ``[start_frame, end_frame)`` are removed.
    - Clips straddling a range boundary are split at that boundary first,
      so only the in-range half is removed — source frames outside the
      range are preserved on the surviving halves.
    - ``ripple=True`` shifts every clip starting at or after ``end_frame``
      left by exactly ``(end_frame - start_frame)``. Explicit moves are
      used instead of per-clip ripple deletes because sequential ripple
      deletes only close gaps by the deleted clip durations, which would
      leave partial gaps when the range contains internal gaps.
      P1-05: tracks with ``is_sync_locked=False`` keep their positions
      (lift semantics on that track — deliberate desync).

    Composed entirely of existing undoable commands (SplitClipCommand,
    BatchDeleteCommand, MoveClipCommand), so a wrapping MacroCommand gets
    atomicity, rollback and full undo for free — including clip-scoped
    marker removal with deleted clips and marker migration with moved
    clips.

    Args:
        sequence: Current timeline (immutable).
        track_ids: Tracks to operate on. Locked or missing tracks are
            skipped; subtitle tracks are not supported by this operation
            (they use a different clip model).
        start_frame: Range start (inclusive, timeline frames).
        end_frame: Range end (exclusive, timeline frames).
        ripple: Extract (close the gap) when True, Lift (keep the gap)
            when False.

    Returns:
        List of commands in execution order; empty when nothing in the
        range needs to change on any track.

    Raises:
        ValueError: If the frame range is invalid.
    """
    if end_frame <= start_frame:
        raise ValueError(
            f"Range end ({end_frame}) must be greater than range start ({start_frame})"
        )

    # Local imports to keep timeline_ops free of command-layer coupling at
    # module import time (commands.py imports timeline_ops).
    from src.features.edit.core.commands import (
        BatchDeleteCommand,
        MoveClipCommand,
        SplitClipCommand,
    )

    def _apply(timeline: TimelineSequence, command: object) -> TimelineSequence:
        result = command.apply(timeline)  # type: ignore[attr-defined]
        if isinstance(result, tuple):
            new_timeline, _updated = result
            return new_timeline
        return result

    commands: "List[object]" = []
    current = sequence
    range_duration = end_frame - start_frame

    for track_id in track_ids:
        track = current.get_track_by_id(track_id)
        if track is None or getattr(track, "is_locked", False):
            continue
        if track.track_type == "subtitle":
            continue

        # Split clips straddling the start boundary, then re-fetch and
        # split clips straddling the end boundary (the right halves created
        # by the first round may straddle the end too). The right-half IDs
        # are pre-generated so later sub-commands (delete of the in-range
        # halves) can reference them deterministically.
        for boundary in (start_frame, end_frame):
            straddling = [
                c for c in current.get_track_by_id(track_id).clips
                if c.timeline_start < boundary < c.timeline_end
            ]
            for clip in straddling:
                split_cmd = SplitClipCommand.create(
                    track_id, clip.clip_id, boundary,
                    right_clip_id=str(uuid.uuid4()),
                )
                commands.append(split_cmd)
                current = _apply(current, split_cmd)

        # After the splits, the range is tiled by whole clips. Collect the
        # halves fully inside the range.
        track = current.get_track_by_id(track_id)
        in_range_ids = [
            c.clip_id for c in track.clips
            if c.timeline_start >= start_frame and c.timeline_end <= end_frame
        ]
        if in_range_ids:
            delete_cmd = BatchDeleteCommand.create(
                current, in_range_ids, ripple=False
            )
            commands.append(delete_cmd)
            current = _apply(current, delete_cmd)

        if ripple:
            # P1-05 Sync Lock：关闭同步锁的轨道不跟随波纹位移（该轨退化为
            # Lift 语义——范围内的 clip 仍被删除，但后续 clip 保持原位，
            # 允许与其他轨有意失步）。默认 True 保持原有 Extract 行为。
            if not getattr(track, "is_sync_locked", True):
                continue
            track = current.get_track_by_id(track_id)
            clips_after = sorted(
                (c for c in track.clips if c.timeline_start >= end_frame),
                key=lambda c: c.timeline_start,
            )
            for clip in clips_after:
                new_start = clip.timeline_start - range_duration
                move_cmd = MoveClipCommand.create(
                    track_id, clip.clip_id, new_start, clip.timeline_start
                )
                commands.append(move_cmd)
                current = _apply(current, move_cmd)

    return commands


# ================================================================
# P1-05: 跨轨 Sync Lock 跟随（波纹编辑统一语义）
# ================================================================


def shift_sync_lock_followers(
    sequence: TimelineSequence,
    exclude_track_ids: "List[str]",
    from_frame: int,
    delta: int,
) -> "Tuple[TimelineSequence, Tuple[Dict[str, Any], ...]]":
    """把其他同步锁轨道上位于 ``from_frame`` 及之后的片段平移 ``delta`` 帧。

    专业 Sync Lock 语义（对应 Premiere）：一条轨道上的波纹编辑（波纹删除、
    波纹修剪、插入）发生时，其余开启同步锁、未锁定、非字幕轨道上位于
    波纹点之后的片段跟随相同的位移，下游内容跨轨保持对齐。

    负向位移按各跟随轨的可用间隙钳制（首个被移片段顶住前一片段结尾），
    不会抛出重叠异常；钳制后的位移量对所有被移片段一致，保持它们之间
    的相对间距。

    Args:
        sequence: 当前时间线（不可变）。
        exclude_track_ids: 波纹编辑自身已处理的轨道（编辑轨 + 链接轨），
            跟随位移跳过这些轨道，避免双重移动。
        from_frame: 波纹点——跟随片段必须 start >= 该帧。
        delta: 位移帧数（负 = 左移）。

    Returns:
        (new_sequence, moves)：moves 为 undo 快照，每项
        ``{track_id, clip_id, old_start, new_start}``。
    """
    if delta == 0:
        return sequence, ()

    exclude = set(exclude_track_ids)
    # AUD-18814: 快照项含 track_id/clip_id 字符串值，注解改为 Dict[str, Any]
    moves: List[Dict[str, Any]] = []
    new_sequence = sequence

    for track in list(sequence.video_tracks) + list(sequence.audio_tracks):
        if track.track_id in exclude:
            continue
        if getattr(track, "is_locked", False):
            continue
        if not getattr(track, "is_sync_locked", True):
            continue

        moved = sorted(
            (c for c in track.clips if c.timeline_start >= from_frame),
            key=lambda c: c.timeline_start,
        )
        if not moved:
            continue

        effective = delta
        if delta < 0:
            previous_end = max(
                (
                    c.timeline_end
                    for c in track.clips
                    if c.timeline_start < from_frame
                ),
                default=0,
            )
            available = moved[0].timeline_start - previous_end
            effective = max(delta, -available)
            if effective == 0:
                continue

        moves.extend(
            {
                "track_id": track.track_id,
                "clip_id": c.clip_id,
                "old_start": c.timeline_start,
                "new_start": c.timeline_start + effective,
            }
            for c in moved
        )
        new_clips = tuple(
            (
                c.with_timeline_start(c.timeline_start + effective)
                .with_temporal_shift(effective)
                if c.timeline_start >= from_frame
                else c
            )
            for c in track.clips
        )
        new_sequence = new_sequence.with_track_updated(
            track._rebuild(clips=new_clips)
        )
        # C16：跨轨跟随同步平移 clip-scoped markers（此前只动 clip，
        # marker 相对位置漂移）
        new_sequence = _shift_scoped_markers_for_track_clips(
            new_sequence, track.track_id,
            {m["clip_id"]: m["old_start"] for m in moves if m["track_id"] == track.track_id},
            effective,
        )

    return new_sequence, tuple(moves)


def _shift_scoped_markers_for_track_clips(
    sequence: TimelineSequence,
    track_id: str,
    old_starts: "Dict[str, int]",
    delta: int,
) -> TimelineSequence:
    """C16：按位移同步平移指定轨道片段的 scoped markers。"""
    if delta == 0 or not old_starts:
        return sequence
    moved_ids = set(old_starts)
    new_markers = tuple(
        replace(marker, frame=marker.frame + delta)
        if marker.clip_id in moved_ids
        else marker
        for marker in sequence.markers
    )
    if new_markers == sequence.markers:
        return sequence
    return sequence._rebuild(markers=new_markers)


def shift_sync_lock_followers_merged(
    sequence: TimelineSequence,
    exclude_track_ids: "List[str]",
    spans: "List[Tuple[int, int]]",
) -> "Tuple[TimelineSequence, Tuple[Dict[str, Any], ...]]":
    """C03：按原始坐标系的合并删除跨度一次性位移同步锁跟随者。

    旧的逐 span 调用 :func:`shift_sync_lock_followers` 有两处错位：
    1. 第一个 span 移过的跟随片段已不在原始位置，后续 span 仍用原始
       ``from_frame`` 阈值判断，跳过本应再次位移的片段（跨轨少移）；
    2. 同一片段跨多个 span 被记录多条 move，undo 用最后一条 old_start
       回填（漂移）。

    这里在原始坐标系为每条同步锁片段求累计位移
    ``sum(span_end - span_start for spans if start >= span_end)``，每条
    片段只记录一份最初 before 与最终 after；跨度互斥，累计长度不会超
    过片段起点，无需逐轨 clamp。
    """
    if not spans:
        return sequence, ()
    sorted_spans = tuple(sorted(spans))
    moves: "List[Dict[str, Any]]" = []

    for track in sequence.video_tracks + sequence.audio_tracks:
        if track.track_id in exclude_track_ids:
            continue
        # R01：沿用旧 shift_sync_lock_followers 的锁轨策略——锁定轨道的
        # 片段不参与波纹跟随位移（复查实测累计删除把锁音轨 100→80）。
        if getattr(track, "is_locked", False):
            continue
        if not getattr(track, "is_sync_locked", True):
            continue

        track_moves: "List[Tuple[str, int, int]]" = []
        for clip in track.clips:
            delta = sum(
                end - start for start, end in sorted_spans
                if clip.timeline_start >= end
            )
            if delta <= 0:
                continue
            new_start = max(0, clip.timeline_start - delta)
            if new_start == clip.timeline_start:
                continue
            track_moves.append((clip.clip_id, clip.timeline_start, new_start))
        if not track_moves:
            continue

        moves.extend(
            {
                "track_id": track.track_id,
                "clip_id": clip_id,
                "old_start": old_start,
                "new_start": new_start,
            }
            for clip_id, old_start, new_start in track_moves
        )
        new_starts = {clip_id: new for clip_id, _, new in track_moves}
        new_clips = tuple(
            (
                c.with_timeline_start(new_starts[c.clip_id])
                .with_temporal_shift(new_starts[c.clip_id] - c.timeline_start)
                if c.clip_id in new_starts
                else c
            )
            for c in track.clips
        )
        sequence = sequence.with_track_updated(track._rebuild(clips=new_clips))
        # C16：merged 位移同样平移 scoped markers（用每片各自 delta）
        per_clip_delta = {
            clip_id: new_starts[clip_id] - old
            for clip_id, old, _ in track_moves
        }
        moved_ids = set(per_clip_delta)
        new_markers = tuple(
            replace(marker, frame=marker.frame + per_clip_delta[marker.clip_id])
            if marker.clip_id in moved_ids
            else marker
            for marker in sequence.markers
        )
        if new_markers != sequence.markers:
            sequence = sequence._rebuild(markers=new_markers)

    return sequence, tuple(moves)


def restore_sync_lock_shifts(
    sequence: TimelineSequence,
    moves: "Tuple[Dict[str, Any], ...]",
) -> TimelineSequence:
    """按 :func:`shift_sync_lock_followers` 返回的快照还原跨轨位移。"""
    if not moves:
        return sequence

    # C03：同一片段多条 move 时，撤销目标是它最初的位置——保留首条
    # old_start，禁止后来记录覆盖。
    starts_by_track: "Dict[str, Dict[str, int]]" = {}
    for move in moves:
        per_track = starts_by_track.setdefault(move["track_id"], {})
        per_track.setdefault(move["clip_id"], move["old_start"])

    for track_id, old_starts in starts_by_track.items():
        track = sequence.get_track_by_id(track_id)
        if track is None:
            continue
        per_clip_delta: "Dict[str, int]" = {}
        new_clips_list = []
        for c in track.clips:
            if c.clip_id in old_starts:
                delta = old_starts[c.clip_id] - c.timeline_start
                per_clip_delta[c.clip_id] = delta
                new_clips_list.append(
                    c.with_timeline_start(old_starts[c.clip_id])
                    .with_temporal_shift(delta)
                )
            else:
                new_clips_list.append(c)
        sequence = sequence.with_track_updated(
            track._rebuild(clips=tuple(new_clips_list))
        )
        # C16：undo 同步还原 scoped markers 到最初位置
        if per_clip_delta:
            moved_ids = set(per_clip_delta)
            new_markers = tuple(
                replace(marker, frame=marker.frame + per_clip_delta[marker.clip_id])
                if marker.clip_id in moved_ids
                else marker
                for marker in sequence.markers
            )
            if new_markers != sequence.markers:
                sequence = sequence._rebuild(markers=new_markers)

    return sequence


def reapply_sync_lock_shifts(
    sequence: TimelineSequence,
    moves: "Tuple[Dict[str, Any], ...]",
) -> TimelineSequence:
    """C02：redo 侧按快照回放跨轨位移（new_start），与 restore 对称。"""
    if not moves:
        return sequence

    # C03：同一片段多条 move 时，redo 目标是它最终的位置——保留末条
    # new_start。
    starts_by_track: "Dict[str, Dict[str, int]]" = {}
    for move in moves:
        starts_by_track.setdefault(move["track_id"], {})[move["clip_id"]] = move["new_start"]

    for track_id, new_starts in starts_by_track.items():
        track = sequence.get_track_by_id(track_id)
        if track is None:
            continue
        per_clip_delta: "Dict[str, int]" = {}
        new_clips_list = []
        for c in track.clips:
            if c.clip_id in new_starts:
                delta = new_starts[c.clip_id] - c.timeline_start
                per_clip_delta[c.clip_id] = delta
                new_clips_list.append(
                    c.with_timeline_start(new_starts[c.clip_id])
                    .with_temporal_shift(delta)
                )
            else:
                new_clips_list.append(c)
        sequence = sequence.with_track_updated(
            track._rebuild(clips=tuple(new_clips_list))
        )
        if per_clip_delta:
            moved_ids = set(per_clip_delta)
            new_markers = tuple(
                replace(marker, frame=marker.frame + per_clip_delta[marker.clip_id])
                if marker.clip_id in moved_ids
                else marker
                for marker in sequence.markers
            )
            if new_markers != sequence.markers:
                sequence = sequence._rebuild(markers=new_markers)

    return sequence


def solve_speed_for_timeline_duration(
    source_duration: int,
    target_duration: int,
    source_fps_num: "int | None" = None,
    source_fps_den: "int | None" = None,
    timeline_fps_num: "int | None" = None,
    timeline_fps_den: "int | None" = None,
    min_speed: float = 0.1,
    max_speed: float = 8.0,
) -> "tuple[float, int, str | None]":
    """R04：反解常速 s，使 ``timeline_duration_for_source`` 命中目标时长。

    旧换算 ``s' = s × old_duration / new_duration`` 用**已舍入**的旧时长
    推速（源 5 帧、3x、时间线 2 帧，拉到 10 帧得 0.6x → 实际只有 8
    帧）。权威解：s = base_ticks / target，其中 base_ticks = 源帧数 ×
    时间线fps/源fps（全程 Fraction，无舍入），目标时长天然可精确命中。

    Returns:
        ``(speed, achievable_duration, clamp_reason)``：speed 已钳到
        [min, max]；achievable = 该 speed 的权威时长（未钳时 == target，
        钳制时为实际可实现值）；clamp_reason 为 None | "min" | "max"。
        target ≤ 0 抛 ``ValueError``。
    """
    from fractions import Fraction

    if target_duration <= 0:
        raise ValueError(f"target duration must be positive: {target_duration}")
    has_fps = bool(source_fps_num and timeline_fps_num)
    if has_fps:
        source_fps = Fraction(source_fps_num, source_fps_den or 1)
        timeline_fps = Fraction(timeline_fps_num, timeline_fps_den or 1)
        base_ticks = Fraction(source_duration) * timeline_fps / source_fps
    else:
        base_ticks = Fraction(source_duration)

    exact = float(base_ticks / target_duration)
    speed = max(float(min_speed), min(float(max_speed), exact))
    reason = None
    if exact < float(min_speed):
        reason = "min"
    elif exact > float(max_speed):
        reason = "max"

    from src.features.edit.core.clip_params import SpeedParams
    achievable = timeline_duration_for_source(
        source_duration, SpeedParams(speed=speed),
        source_fps_num, source_fps_den,
        timeline_fps_num, timeline_fps_den,
    )
    return speed, achievable, reason


def timeline_duration_for_source(
    source_duration: int,
    speed: "float | Any",
    source_fps_num: "int | None" = None,
    source_fps_den: "int | None" = None,
    timeline_fps_num: "int | None" = None,
    timeline_fps_den: "int | None" = None,
) -> int:
    """C07/FE-B04：混合帧率素材的统一时长换算。

    常速：``source_duration / source_fps / speed * timeline_fps``（全程
    Fraction，round 取整，最小 1 帧）。变速点：逐段积分
    ``(end-start) / source_fps / segment_speed`` 秒再转 timeline ticks。
    SpeedParams 无 fps 元数据时退回其自身的分段时长方法；纯数值无元数
    据时维持 legacy 1:1 公式。
    """
    from fractions import Fraction

    from src.features.edit.core.clip_params import SpeedParams

    has_fps = bool(source_fps_num and timeline_fps_num)
    source_fps = (
        Fraction(source_fps_num, source_fps_den or 1) if has_fps else None
    )
    timeline_fps = (
        Fraction(timeline_fps_num, timeline_fps_den or 1) if has_fps else None
    )

    if isinstance(speed, SpeedParams) and speed.speed_points:
        if source_fps is not None and timeline_fps is not None and source_fps > 0:
            # R06：smooth 斜坡不能拿 constant 段积分冒充（审计实测
            # smooth fixture 权威 102 帧、旧 helper 90）——委托
            # SpeedParams 的曲线积分（speed 域 timeline 帧）再按 fps
            # 比例换算。
            if speed._has_smooth_ramp(source_duration):
                smooth_total = Fraction(
                    speed.timeline_duration_for_source_duration(source_duration)
                )
                return max(1, int(round(smooth_total * timeline_fps / source_fps)))
            total_ticks = Fraction(0)
            for seg_start, seg_end, seg_speed in speed._segments(source_duration):
                seconds = (
                    Fraction(seg_end - seg_start) / source_fps / Fraction(seg_speed)
                )
                total_ticks += seconds * timeline_fps
            return max(1, int(round(total_ticks)))
        return speed.timeline_duration_for_source_duration(source_duration)

    speed_value = float(
        speed.speed if isinstance(speed, SpeedParams) else speed
    )
    if source_fps is not None and timeline_fps is not None and speed_value > 0:
        seconds = (
            Fraction(source_duration) / source_fps
            / Fraction(speed_value).limit_denominator(100000)
        )
        return max(1, int(round(seconds * timeline_fps)))
    return max(1, int(source_duration / speed_value))


# ── F2.2：三/四点编辑判定（纯函数，无 UI 依赖） ─────────────────────────


@dataclass(frozen=True)
class SourceEditPlan:
    """三/四点编辑解析结果（timeline_view 接线层消费）。

    mode ∈ {"three_point", "backtime", "four_point_match",
            "four_point_fit", "four_point_trim"}；speed 为 float
    （fit-to-fill 时 ≠ 1.0）；timeline_duration 为该 speed 下片段在
    目标轨的实际时长（timeline_duration_for_source 权威口径）。
    """

    mode: str
    playhead: int
    source_in: int
    source_out: int
    speed: float
    timeline_duration: int


@dataclass(frozen=True)
class SourceEditConflict:
    """四点时长冲突：需用户选择解决策略（变速填充/裁剪源/取消）。

    trim_available=False 表示目标比源长（裁剪无法反向延长源窗）；
    speed_in_range=False 表示建议速度越过 SpeedParams 的 0.1–8.0 边界。
    """

    source_duration: int
    target_duration: int
    suggested_speed: float
    speed_in_range: bool
    trim_available: bool


def _speed_for_target_duration(
    source_frames: int,
    target_duration: int,
    source_fps_num: "int | None",
    source_fps_den: "int | None",
    timeline_fps_num: "int | None",
    timeline_fps_den: "int | None",
) -> "float | None":
    """求使片段恰好占满 ``target_duration`` 时间线帧的速度（精确命中或 None）。

    闭式初值 s0 = 源帧数/目标帧数（混合 fps 时经 Fraction 换算），再按
    实际换算时长做最多 8 次比例校正消除取整偏差；取整后无法恰好命中
    （理论极小间隙）返回 None，由上层按冲突呈现。
    """
    from fractions import Fraction

    if target_duration <= 0 or source_frames <= 0:
        return None
    has_fps = bool(source_fps_num and timeline_fps_num)
    if has_fps:
        source_fps = Fraction(source_fps_num, source_fps_den or 1)
        timeline_fps = Fraction(timeline_fps_num, timeline_fps_den or 1)
        speed0 = Fraction(source_frames) * timeline_fps / (
            source_fps * Fraction(target_duration))
    else:
        speed0 = Fraction(source_frames, target_duration)
    speed = float(speed0)
    for _ in range(8):
        actual = timeline_duration_for_source(
            source_frames, speed,
            source_fps_num, source_fps_den,
            timeline_fps_num, timeline_fps_den,
        )
        if actual == target_duration:
            return speed
        speed = speed * actual / target_duration
    return None


def resolve_source_edit_plan(
    source_in: int,
    source_out: int,
    range_start: "int | None",
    range_end: "int | None",
    *,
    source_fps_num: "int | None" = None,
    source_fps_den: "int | None" = None,
    timeline_fps_num: "int | None" = None,
    timeline_fps_den: "int | None" = None,
    fit_policy: str = "ask",
    default_playhead: int = 0,
) -> "SourceEditPlan | SourceEditConflict":
    """F2.2：按源 In/Out + 时间线 In/Out 解析编辑落点（三点/四点/backtime）。

    判定表（range 单边语义与 resolve_timeline_in_out 一致）：
    - 无 range：三点，落点 = ``default_playhead``（播放头）。
    - 仅时间线 In：三点，落点 = In。
    - 仅时间线 Out：backtime——片段**恰好结束**在 Out（落点 = Out-时长+1）。
    - 双边：四点。源时长（1x 换算）== range 时长 → 直接落 In；
      不等 → ``SourceEditConflict``；``fit_policy`` 为 "fit_speed" /
      "trim_source" 时按策略出 plan（trim 在目标更长、fit 在速度越界
      或无法精确命中时抛 ValueError），默认 "ask" 交 UI 选择。

    Raises:
        ValueError: range_end < range_start，或指定 fit_policy 不可行。
    """
    if source_out < source_in:
        raise ValueError(f"source range invalid: {source_in}..{source_out}")
    if range_start is not None and range_end is not None \
            and range_end < range_start:
        raise ValueError(
            f"timeline range invalid: {range_start}..{range_end}")

    source_frames = source_out - source_in + 1
    source_duration = timeline_duration_for_source(
        source_frames, 1.0,
        source_fps_num, source_fps_den,
        timeline_fps_num, timeline_fps_den,
    )

    def _plan(mode: str, playhead: int, speed: float = 1.0,
              out_frame: "int | None" = None,
              duration: "int | None" = None) -> SourceEditPlan:
        effective_out = source_out if out_frame is None else out_frame
        return SourceEditPlan(
            mode=mode,
            playhead=max(0, playhead),
            source_in=source_in,
            source_out=effective_out,
            speed=speed,
            timeline_duration=duration if duration is not None else
            timeline_duration_for_source(
                effective_out - source_in + 1, speed,
                source_fps_num, source_fps_den,
                timeline_fps_num, timeline_fps_den,
            ),
        )

    if range_start is None and range_end is None:
        return _plan("three_point", default_playhead)
    if range_end is None:
        return _plan("three_point", range_start)
    if range_start is None:
        # backtime：恰好结束在时间线 Out
        return _plan("backtime", range_end - source_duration + 1)

    target_duration = range_end - range_start + 1
    if target_duration == source_duration:
        return _plan("four_point_match", range_start)

    speed = _speed_for_target_duration(
        source_frames, target_duration,
        source_fps_num, source_fps_den,
        timeline_fps_num, timeline_fps_den,
    )
    speed_in_range = speed is not None and 0.1 <= speed <= 8.0
    trim_available = target_duration < source_duration
    if fit_policy == "fit_speed":
        if speed is None or not speed_in_range:
            raise ValueError(
                f"fit-to-fill 不可行：目标 {target_duration} 帧无法用 "
                f"0.1–8.0 变速精确填充（建议速度 {speed}）")
        return _plan("four_point_fit", range_start, speed=speed,
                     duration=target_duration)
    if fit_policy == "trim_source":
        if not trim_available:
            raise ValueError(
                f"裁剪源不可行：目标 {target_duration} 帧长于源 "
                f"{source_duration} 帧（无法反向延长源窗）")
        # 1x 速度下占满 target 帧所需的源帧数（Fraction 精确换算）
        from fractions import Fraction
        if source_fps_num and timeline_fps_num:
            needed = Fraction(target_duration) * Fraction(
                source_fps_num, source_fps_den or 1) / Fraction(
                timeline_fps_num, timeline_fps_den or 1)
        else:
            needed = Fraction(target_duration)
        needed_int = min(source_frames, max(1, int(round(needed))))
        return _plan(
            "four_point_trim", range_start,
            out_frame=source_in + needed_int - 1,
            duration=timeline_duration_for_source(
                needed_int, 1.0,
                source_fps_num, source_fps_den,
                timeline_fps_num, timeline_fps_den,
            ),
        )
    return SourceEditConflict(
        source_duration=source_duration,
        target_duration=target_duration,
        suggested_speed=float(speed) if speed is not None else 0.0,
        speed_in_range=speed_in_range,
        trim_available=trim_available,
    )


def merged_ripple_spans(
    clips: "List[ClipInstance]",
) -> "List[Tuple[int, int]]":
    """把删除片段的 ``[start, end)`` 区间做并集，输出去重后的波纹事件。

    链接 A/V 对（同跨度出现在两条轨）合并为一个事件——其他轨道只跟随
    一次位移；不同轨不重叠的删除保持独立事件，下游跟随各自累计。
    返回按区间起点排序的 ``(start, end)`` 列表。
    """
    spans = sorted(
        (c.timeline_start, c.timeline_end) for c in clips
    )
    merged: "List[Tuple[int, int]]" = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def add_transition(
    sequence: TimelineSequence,
    left_clip_id: str,
    right_clip_id: str,
    duration: int
) -> TimelineSequence:
    """
    在两个相邻剪辑间添加转场。

    Args:
        sequence: 时间线序列
        left_clip_id: 左剪辑 ID
        right_clip_id: 右剪辑 ID
        duration: 转场时长（帧数）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑不存在或不相邻
    """
    left_clip = sequence.get_clip_by_id(left_clip_id)
    if left_clip is None:
        raise TimelineOperationError(f"Left clip not found: {left_clip_id}")

    right_clip = sequence.get_clip_by_id(right_clip_id)
    if right_clip is None:
        raise TimelineOperationError(f"Right clip not found: {right_clip_id}")

    if left_clip.track_id != right_clip.track_id:
        raise TimelineOperationError(
            "Transitions only supported between clips on the same track"
        )

    track = sequence.get_track_by_id(left_clip.track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {left_clip.track_id}")

    prev, next_ = track.get_adjacent_clips(left_clip_id)
    if next_ is None or next_.clip_id != right_clip_id:
        raise TimelineOperationError(
            f"Clips {left_clip_id} and {right_clip_id} are not adjacent"
        )

    cut_frame = left_clip.timeline_end

    existing_transition = next(
        (
            transition
            for transition in track.transitions
            if transition.left_clip_id == left_clip_id
            and transition.right_clip_id == right_clip_id
        ),
        None,
    )
    if existing_transition is not None:
        # Re-adding at the same cut replaces the effect but preserves the
        # existing editorial extent and alignment.
        # AUD-18806：重加时保留原 transition 的 params（此前重建为空 dict，
        # 已配置的转场参数被静默丢弃——transition 的"扩展字段"）。
        transition = TransitionInstance(
            transition_id=existing_transition.transition_id,
            transition_type="cross_dissolve",
            track_id=existing_transition.track_id,
            left_clip_id=existing_transition.left_clip_id,
            right_clip_id=existing_transition.right_clip_id,
            center_frame=existing_transition.center_frame,
            duration=existing_transition.duration,
            alignment=existing_transition.alignment,
            params=dict(existing_transition.params),
        )
        track_with_transition = track._rebuild(
            transitions=tuple(
                transition if current.transition_id == existing_transition.transition_id else current
                for current in track.transitions
            )
        )
        return sequence.with_track_updated(track_with_transition)

    transition = TransitionInstance(
        transition_id=str(uuid.uuid4()),
        transition_type="cross_dissolve",
        track_id=left_clip.track_id,
        left_clip_id=left_clip_id,
        right_clip_id=right_clip_id,
        center_frame=cut_frame,
        duration=duration,
        alignment="centered",
        params={}
    )

    # AUD-18809：时长越界等模型校验错误经 with_transition_added 以裸
    # ValueError 抛出——转译为公共异常契约。
    try:
        track_with_transition = track.with_transition_added(transition)
    except ValueError as exc:
        raise _translate_model_error(
            exc,
            f"Add transition between {left_clip_id} and {right_clip_id}",
        ) from exc

    return sequence.with_track_updated(track_with_transition)


# ================================================================
# Snap Operations (吸附操作)
# ================================================================


def find_snap_points(
    sequence: TimelineSequence,
    playhead_frame: int | None = None,
    include_markers: bool = True,
    include_clip_edges: bool = True
) -> tuple[int, ...]:
    """
    查找所有吸附点。

    Args:
        sequence: 时间线序列
        playhead_frame: 当前播放头位置
        include_markers: 是否包含标记
        include_clip_edges: 是否包含剪辑边界

    Returns:
        吸附帧位置元组
    """
    snap_points = []

    snap_points.append(0)

    if playhead_frame is not None:
        snap_points.append(playhead_frame)

    if include_clip_edges:
        for track in sequence.all_tracks:
            for clip in track.clips:
                snap_points.append(clip.timeline_start)
                snap_points.append(clip.timeline_end)

    if include_markers:
        for marker in sequence.markers:
            snap_points.append(marker.frame)

    return tuple(sorted(set(snap_points)))


def snap_to_nearest(
    frame: int,
    snap_points: tuple[int, ...],
    threshold: int = 6
) -> int:
    """
    将帧吸附到最近的吸附点。

    Args:
        frame: 要吸附的帧
        snap_points: 吸附点列表
        threshold: 吸附阈值（帧数）

    Returns:
        吸附后的帧位置
    """
    if not snap_points:
        return frame

    min_distance = threshold
    snapped_frame = frame

    for snap_point in snap_points:
        distance = abs(frame - snap_point)
        if distance < min_distance:
            min_distance = distance
            snapped_frame = snap_point

    return snapped_frame


# ================================================================
# Validation Helpers (验证辅助)
# ================================================================


def validate_timeline(sequence: TimelineSequence) -> list[str]:
    """
    验证时间线序列的有效性。

    Args:
        sequence: 时间线序列

    Returns:
        错误信息列表（空列表表示有效）
    """
    errors = []

    clip_owner_by_id = {}
    for track in sequence.all_tracks:
        for clip in track.clips:
            if clip.track_id != track.track_id:
                errors.append(
                    f"Clip {clip.clip_id} track_id mismatch: "
                    f"belongs to {track.track_id} but references {clip.track_id}"
                )
            clip_owner_by_id[clip.clip_id] = clip

    # R03：链接必须双向一致——单向残链会让 linked 编辑单元计算错位
    for clip_id, clip in clip_owner_by_id.items():
        if clip.linked_clip_id:
            partner = clip_owner_by_id.get(clip.linked_clip_id)
            if partner is None:
                errors.append(
                    f"Clip {clip_id} links to non-existent clip "
                    f"{clip.linked_clip_id}"
                )
            elif partner.linked_clip_id != clip_id:
                errors.append(
                    f"Clip {clip_id} link to {clip.linked_clip_id} is not "
                    f"reciprocal (partner links to {partner.linked_clip_id})"
                )

    # R03：clip-scoped marker 必须落在其归属片段范围内
    for marker in getattr(sequence, "markers", ()) or ():
        if not marker.clip_id:
            continue
        owner = clip_owner_by_id.get(marker.clip_id)
        if owner is None:
            errors.append(
                f"Marker {marker.marker_id} references non-existent clip "
                f"{marker.clip_id}"
            )
        elif not (owner.timeline_start <= marker.frame < owner.timeline_end):
            errors.append(
                f"Marker {marker.marker_id} frame {marker.frame} outside "
                f"owning clip {marker.clip_id} range "
                f"[{owner.timeline_start}, {owner.timeline_end})"
            )

    # 转场不变量（含 R03 的 seam 锚定检查）
    for track in sequence.all_tracks:
        for trans in track.transitions:
            left = track.get_clip_by_id(trans.left_clip_id) if trans.left_clip_id else None
            right = track.get_clip_by_id(trans.right_clip_id) if trans.right_clip_id else None

            if trans.left_clip_id and left is None:
                errors.append(
                    f"Transition {trans.transition_id} references "
                    f"non-existent left clip {trans.left_clip_id}"
                )
            if trans.right_clip_id and right is None:
                errors.append(
                    f"Transition {trans.transition_id} references "
                    f"non-existent right clip {trans.right_clip_id}"
                )

            if left is not None and right is not None:
                prev, next_ = track.get_adjacent_clips(trans.left_clip_id)
                if next_ is None or next_.clip_id != trans.right_clip_id:
                    errors.append(
                        f"Transition {trans.transition_id} clips "
                        f"{trans.left_clip_id} and {trans.right_clip_id} "
                        f"are not adjacent"
                    )
                # R03：双边转场 center 必须钉在真实 seam 上（时间变换后
                # 残留旧绝对时间即报错，而不是静默保存非法状态）
                elif trans.center_frame != left.timeline_end:
                    errors.append(
                        f"Transition {trans.transition_id} center "
                        f"{trans.center_frame} != seam {left.timeline_end}"
                    )
                if trans.start_frame < left.timeline_start or trans.end_frame > right.timeline_end:
                    errors.append(
                        f"Transition {trans.transition_id} range exceeds adjacent clips"
                    )
            elif right is not None:
                if trans.alignment != "start_on_cut" or trans.center_frame != right.timeline_start:
                    errors.append(
                        f"Transition {trans.transition_id} is not anchored to clip start"
                    )
                if trans.start_frame < right.timeline_start or trans.end_frame > right.timeline_end:
                    errors.append(f"Transition {trans.transition_id} range exceeds clip")
            elif left is not None:
                if trans.alignment != "end_on_cut" or trans.center_frame != left.timeline_end:
                    errors.append(
                        f"Transition {trans.transition_id} is not anchored to clip end"
                    )
                if trans.start_frame < left.timeline_start or trans.end_frame > left.timeline_end:
                    errors.append(f"Transition {trans.transition_id} range exceeds clip")

    return errors


def get_timeline_duration(sequence: TimelineSequence) -> int:
    """
    获取时间线总时长。

    Args:
        sequence: 时间线序列

    Returns:
        时长（帧数）
    """
    return sequence.duration_frames


# ================================================================
# Transition Operations (转场操作)
# ================================================================


def remove_transition(
    sequence: TimelineSequence,
    transition_id: str,
    track_id: str
) -> TimelineSequence:
    """
    删除转场。

    Args:
        sequence: 时间线序列
        transition_id: 转场 ID
        track_id: 轨道 ID

    Returns:
        新的 TimelineSequence（不包含该转场）

    Raises:
        TimelineOperationError: 如果转场不存在
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {track_id}")

    # 查找转场
    transition_exists = False
    for t in track.transitions:
        if t.transition_id == transition_id:
            transition_exists = True
            break

    if not transition_exists:
        raise TimelineOperationError(f"Transition not found: {transition_id}")

    # 删除转场
    new_transitions = tuple(
        t for t in track.transitions
        if t.transition_id != transition_id
    )

    # AUD-18805：改用 _rebuild（与 add_transition 一致）。手工 EditTrack 构造
    # 会丢弃 volume/pan/is_sync_locked/is_visible/color/stem_group/automation/
    # effect_chain 等轨道设置（音频轨可承载转场，删除转场即静默重置混音）。
    new_track = track._rebuild(transitions=new_transitions)

    return sequence.with_track_updated(new_track)


def update_transition(
    sequence: TimelineSequence,
    transition_id: str,
    track_id: str,
    duration: int | None = None,
    alignment: str | None = None,
    params: dict | None = None
) -> TimelineSequence:
    """
    更新转场参数。

    Args:
        sequence: 时间线序列
        transition_id: 转场 ID
        track_id: 轨道 ID
        duration: 新时长（帧数），None 表示保持原值
        alignment: 新对齐方式，None 表示保持原值
        params: 新参数字典，None 表示保持原值

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果转场不存在
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {track_id}")

    # 查找转场
    old_transition = None
    for t in track.transitions:
        if t.transition_id == transition_id:
            old_transition = t
            break

    if old_transition is None:
        raise TimelineOperationError(f"Transition not found: {transition_id}")

    # 创建更新后的转场
    new_duration = duration if duration is not None else old_transition.duration
    new_alignment = alignment if alignment is not None else old_transition.alignment
    new_params = params if params is not None else old_transition.params

    updated_transition = TransitionInstance(
        transition_id=old_transition.transition_id,
        transition_type=old_transition.transition_type,
        track_id=old_transition.track_id,
        left_clip_id=old_transition.left_clip_id,
        right_clip_id=old_transition.right_clip_id,
        center_frame=old_transition.center_frame,
        duration=new_duration,
        alignment=new_alignment,
        params=new_params,
    )

    # 替换转场
    new_transitions = tuple(
        updated_transition if t.transition_id == transition_id else t
        for t in track.transitions
    )

    # AUD-18805：改用 _rebuild（同 remove_transition）。手工 EditTrack 构造
    # 会丢弃 volume/pan/is_sync_locked/is_visible/color/stem_group/automation/
    # effect_chain 等轨道设置。
    new_track = track._rebuild(transitions=new_transitions)

    return sequence.with_track_updated(new_track)


def find_adjacent_clips_at_frame(
    sequence: TimelineSequence,
    track_id: str,
    frame: int,
    threshold: int = 0
) -> tuple[Optional[ClipInstance], Optional[ClipInstance]]:
    """
    在指定帧位置查找相邻剪辑（用于添加转场）。

    查找结束帧接近指定帧的左剪辑和开始帧接近指定帧的右剪辑。
    允许一定的间隙阈值。

    Args:
        sequence: 时间线序列
        track_id: 轨道 ID
        frame: 目标帧位置（通常是 cut 点）
        threshold: 允许的间隙阈值（帧数），0 表示必须紧邻

    Returns:
        (左剪辑, 右剪辑) 元组，如果不存在则返回 None
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        return (None, None)

    left_clip: Optional[ClipInstance] = None
    right_clip: Optional[ClipInstance] = None

    # 查找结束帧接近 frame 的剪辑（左剪辑）
    for clip in track.clips:
        # 检查是否是潜在的左剪辑（结束帧在 frame 附近）
        if abs(clip.timeline_end - frame) <= threshold:
            if left_clip is None or clip.timeline_end > left_clip.timeline_end:
                left_clip = clip

    # 查找开始帧接近 frame 的剪辑（右剪辑）
    for clip in track.clips:
        # 检查是否是潜在的右剪辑（开始帧在 frame 附近）
        if abs(clip.timeline_start - frame) <= threshold:
            if right_clip is None or clip.timeline_start < right_clip.timeline_start:
                right_clip = clip

    # 验证两个剪辑是不同的
    if left_clip is not None and right_clip is not None:
        if left_clip.clip_id == right_clip.clip_id:
            return (None, None)

        # 验证左剪辑确实在右剪辑左边
        if left_clip.timeline_start > right_clip.timeline_start:
            return (None, None)

    return (left_clip, right_clip)


def get_transition_at_frame(
    track: EditTrack,
    frame: int
) -> Optional[TransitionInstance]:
    """
    获取指定帧位置的转场。

    Args:
        track: 编辑轨道
        frame: 帧位置

    Returns:
        TransitionInstance 或 None
    """
    for trans in track.transitions:
        if trans.start_frame <= frame < trans.end_frame:
            return trans
    return None


def get_all_cut_points(
    sequence: TimelineSequence,
    track_id: str
) -> tuple[int, ...]:
    """
    获取轨道上所有 cut 点（相邻剪辑的接合位置）。

    Args:
        sequence: 时间线序列
        track_id: 轨道 ID

    Returns:
        cut 点帧位置元组
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        return tuple()

    cut_points: list[int] = []
    sorted_clips = sorted(track.clips, key=lambda c: c.timeline_start)

    for i in range(len(sorted_clips) - 1):
        left = sorted_clips[i]
        right = sorted_clips[i + 1]
        # Cut 点是左剪辑结束的位置
        cut_frame = left.timeline_end
        # 只有当剪辑相邻或接近时才记录
        if right.timeline_start - left.timeline_end <= 10:  # 允许 10 帧间隙
            cut_points.append(cut_frame)

    return tuple(cut_points)


# ================================================================
# Convenience Methods (便捷方法)
# ================================================================


def ripple_delete(
    sequence: TimelineSequence,
    clip_id: str
) -> TimelineSequence:
    """
    Ripple 删除剪辑（后续剪辑向左移动填补空隙）。

    Args:
        sequence: 时间线序列
        clip_id: 要删除的剪辑 ID

    Returns:
        新的 TimelineSequence
    """
    return delete_clip(sequence, clip_id, ripple=True)


# ================================================================
# Insert / Overwrite / Append Operations (P3.2)
# ================================================================


def split_source_partition(
    clip, timeline_frame: int
) -> "Tuple[int, int, int, int]":
    """R07：统一切分分区——(left_start, left_dur, right_start, right_dur)。

    半开区间精确分割，方向/速度/FPS 感知（经 clip 自身帧映射）。反向片
    段左半播放源区间高端。SplitClipCommand、Insert 跨点切分、Overwrite
    半覆盖共用，不再各入口复制公式。
    """
    split_source = clip.timeline_to_source_frame(timeline_frame)
    end = clip.source_start + clip.source_duration
    left_start, left_dur = clip.with_right_edge_source_values(
        timeline_frame - clip.timeline_start)
    if clip.is_reverse_playback():
        right_start = clip.source_start
        right_dur = max(1, split_source - clip.source_start + 1)
    else:
        right_start = split_source
        right_dur = end - right_start
    return left_start, left_dur, right_start, right_dur


def insert_clip_at_playhead(
    sequence: TimelineSequence,
    asset_id: str,
    in_frame: int,
    out_frame: int,
    playhead: int,
    track_id: str,
    speed: float = 1.0,
    source_fps_num: int | None = None,
    source_fps_den: int | None = None,
    timeline_fps_num: int | None = None,
    timeline_fps_den: int | None = None,
    source_pts_index: tuple | list | None = None,
) -> TimelineSequence:
    """
    在 playhead 位置插入剪辑，后方剪辑 ripple 后移。

    Insert 操作：
    1. 在 playhead 位置创建新剪辑
    2. 将 playhead 之后的所有剪辑向后移动 clip_duration 帧
    3. 保持剪辑之间的相对位置

    Args:
        sequence: 时间线序列
        asset_id: 素材 ID
        in_frame: 素材起始帧（source_start）
        out_frame: 素材结束帧（source_end，包含）
        playhead: 时间线插入位置
        track_id: 目标轨道 ID
        speed: 播放速度（默认 1.0）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果轨道不存在或参数无效
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {track_id}")

    if in_frame < 0:
        raise TimelineOperationError(f"in_frame cannot be negative: {in_frame}")
    if out_frame < in_frame:
        raise TimelineOperationError(f"out_frame must be >= in_frame: {out_frame} < {in_frame}")

    # 计算素材范围
    source_start = in_frame
    source_duration = out_frame - in_frame + 1  # 包含 out_frame

    # 计算时间线时长（考虑速度）
    timeline_duration = timeline_duration_for_source(
        source_duration, speed,
        source_fps_num, source_fps_den,
        timeline_fps_num, timeline_fps_den,
    )

    # 创建新剪辑
    new_clip = ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=playhead,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=speed,
        _source_fps_num=source_fps_num,
        _source_fps_den=source_fps_den,
        _timeline_fps_num=timeline_fps_num,
        _timeline_fps_den=timeline_fps_den,
        # F5.4：VFR 生产绑定——导入时抽取的 PTS 秒表随插入落到片段
        # （timeline_to_source_frame 经 timeline_frame_mapper 走精确
        # PTS 求帧；CFR 素材调用方不传，保持 CFR 数学零开销）
        _source_pts_index=tuple(source_pts_index)
        if source_pts_index else None,
    )

    # AUD-18803：先分割跨越 playhead 的片段（专业 NLE 的 Insert 语义）。
    # 起始帧 < playhead < 结束帧的片段既不在 clips_after（后移集合）也不被
    # 分割，直接 with_clip_added 会触发重叠校验 ValueError("Clip overlaps")，
    # 使播放头落在片段中间时 Insert 整体失败。分割走 copy_clip_with
    # （source 侧按 effective_speed 缩放，与 split_clip 同口径），扩展字段保留。
    new_track = track
    for clip in track.clips:
        if not (clip.timeline_start < playhead < clip.timeline_end):
            continue
        left_timeline_duration = playhead - clip.timeline_start
        # R07：经统一分区 helper（方向/速度/FPS 感知；旧
        # effective_speed 公式在反向/变速点/混合帧率下错切内容）
        ls, ldur, rs, rdur = split_source_partition(clip, playhead)
        left_part = copy_clip_with(
            clip,
            timeline_duration=left_timeline_duration,
            source_start=ls,
            source_duration=ldur,
            fade_in_duration=min(clip.fade_in_duration, left_timeline_duration),
            fade_out_duration=min(clip.fade_out_duration, left_timeline_duration),
        )
        right_timeline_duration = clip.timeline_end - playhead
        right_part = copy_clip_with(
            clip,
            clip_id=str(uuid.uuid4()),
            timeline_start=playhead,
            timeline_duration=right_timeline_duration,
            source_start=rs,
            source_duration=rdur,
            fade_in_duration=0,
            fade_out_duration=min(clip.fade_out_duration, right_timeline_duration),
        )
        new_track = new_track._rebuild(clips=tuple(
            left_part if c.clip_id == clip.clip_id else c
            for c in new_track.clips
        ) + (right_part,))
        # N07/C3-07：切分点右半的 scoped marker 改挂右半新 ID——右半属
        # 于 moved_ids（start==playhead），随后的波纹位移把它 +delta；
        # 此前 marker 仍指左半 ID，既不随波纹移动又落在左半范围外
        #（validator 报 outside owning clip）。
        sequence = sequence._rebuild(
            markers=tuple(
                replace(marker, clip_id=right_part.clip_id)
                if marker.clip_id == clip.clip_id and marker.frame >= playhead
                else marker
                for marker in sequence.markers
            )
        )

    # Ripple：将 playhead 之后的所有剪辑向后移动（分割产生的右半片起始
    # 帧等于 playhead，也会随后移集合移动，为新剪辑让位）。
    #
    # AUD-18816：单次原子 `_rebuild` 完成整体平移。此前逐个
    # with_clip_updated 平移是 O(n²)：每次调用都对全轨做一遍重叠校验
    # + 排序重建，数百片段的轨道上 UI 插入可感卡顿；且每个片段单独
    # 平移的中间态本身没有意义。与 ripple_trim_clip_left/right 相同的
    # 原子重建模式（_rebuild 不做重叠校验，最终一致性由随后的
    # with_clip_added 统一校验保证）。
    moved_ids = {
        c.clip_id for c in new_track.clips if c.timeline_start >= playhead
    }
    new_clips = tuple(
        c.with_timeline_start(c.timeline_start + timeline_duration)
        .with_temporal_shift(timeline_duration)
        if c.timeline_start >= playhead else c
        for c in new_track.clips
    )
    new_track = new_track._rebuild(clips=new_clips)
    # R08：目标轨跟随片段的 scoped marker 同步右移（跨轨跟随由调用方
    # 的 sync-lock 位移处理；此前目标轨 marker 留在原绝对帧，validator
    # 报 marker outside owning clip）
    if moved_ids:
        sequence = sequence._rebuild(
            markers=tuple(
                replace(marker, frame=marker.frame + timeline_duration)
                if marker.clip_id in moved_ids else marker
                for marker in sequence.markers
            )
        )

    # 后添加新剪辑（模型层 ValueError 转译为本模块承诺的异常类型）
    try:
        new_track = new_track.with_clip_added(new_clip)
    except ValueError as exc:
        raise TimelineOperationError(f"Insert failed at playhead {playhead}: {exc}") from exc

    return sequence.with_track_updated(new_track)


def overwrite_clip_at_playhead(
    sequence: TimelineSequence,
    asset_id: str,
    in_frame: int,
    out_frame: int,
    playhead: int,
    track_id: str,
    speed: float = 1.0,
    source_fps_num: int | None = None,
    source_fps_den: int | None = None,
    timeline_fps_num: int | None = None,
    timeline_fps_den: int | None = None,
    source_pts_index: tuple | list | None = None,
) -> TimelineSequence:
    """
    在 playhead 位置覆盖剪辑，不移动后方剪辑。

    Overwrite 操作：
    1. 在 playhead 位置创建新剪辑
    2. 如果覆盖区域有现有剪辑：
       - 如果完全覆盖，删除现有剪辑
       - 如果部分覆盖，切割现有剪辑（左切或右切）
    3. 不移动任何后方剪辑

    Args:
        sequence: 时间线序列
        asset_id: 素材 ID
        in_frame: 素材起始帧（source_start）
        out_frame: 素材结束帧（source_end，包含）
        playhead: 时间线插入位置
        track_id: 目标轨道 ID
        speed: 播放速度（默认 1.0）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果轨道不存在或参数无效
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {track_id}")

    if in_frame < 0:
        raise TimelineOperationError(f"in_frame cannot be negative: {in_frame}")
    if out_frame < in_frame:
        raise TimelineOperationError(f"out_frame must be >= in_frame: {out_frame} < {in_frame}")

    # 计算素材范围
    source_start = in_frame
    source_duration = out_frame - in_frame + 1

    # 计算时间线时长
    timeline_duration = timeline_duration_for_source(
        source_duration, speed,
        source_fps_num, source_fps_den,
        timeline_fps_num, timeline_fps_den,
    )
    new_clip_end = playhead + timeline_duration

    # 创建新剪辑
    new_clip = ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=playhead,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=speed,
        _source_fps_num=source_fps_num,
        _source_fps_den=source_fps_den,
        _source_pts_index=tuple(source_pts_index)
        if source_pts_index is not None else None,
        _timeline_fps_num=timeline_fps_num,
        _timeline_fps_den=timeline_fps_den,
    )

    # 处理覆盖区域内的现有剪辑
    clips_to_remove: list[str] = []
    clips_to_update: list[ClipInstance] = []
    # N08/C3-08：关联迁移登记——middle_right_map: 原ID→右残片ID（tail
    # 转场+右半 marker 迁移）；middle_covered: 中覆盖原ID（marker 分段
    # 策略）；fully_deleted: 全删除原ID（marker 一并删）。
    middle_right_map: dict[str, str] = {}
    middle_covered: set[str] = set()
    fully_deleted: set[str] = set()
    tail_trimmed: set[str] = set()

    for clip in track.clips:
        # 检查是否与新剪辑重叠
        if clip.timeline_start >= new_clip_end:
            # 在新剪辑之后，不重叠
            continue
        if clip.timeline_end <= playhead:
            # 在新剪辑之前，不重叠
            continue

        # 有重叠，需要处理
        clip_start = clip.timeline_start
        clip_end = clip.timeline_end

        # 计算重叠情况
        if playhead <= clip_start and new_clip_end >= clip_end:
            # 新剪辑完全覆盖现有剪辑 -> 删除
            clips_to_remove.append(clip.clip_id)
            # N08/C3-08：owner 已删，scoped marker 一并删除（不留孤儿）
            fully_deleted.add(clip.clip_id)
        elif playhead > clip_start and new_clip_end < clip_end:
            # 新剪辑在现有剪辑中间 -> 需要切成两部分
            # R07：两侧残片经统一分区 helper（方向/速度/FPS 感知；旧
            # effective_speed 公式反向/变速点/混合帧率错切）
            left_duration = playhead - clip_start
            ls, ldur, _rs, _rd = split_source_partition(clip, playhead)
            left_source_duration = ldur

            right_duration = clip_end - new_clip_end
            _ls2, _ld2, rs2, rd2 = split_source_partition(clip, new_clip_end)
            right_source_start = rs2
            right_source_duration = rd2

            # 删除原剪辑，添加两个新剪辑
            clips_to_remove.append(clip.clip_id)

            # 左剪辑
            if left_duration > 0 and left_source_duration > 0:
                # AUD-18800：不可变更新——半片继承原片段全部扩展字段
                # （调色/关键帧/淡入出/增益/冻结帧/FPS 元数据等），
                # 仅覆写切分产生的范围字段。
                # N08/C3-08：左半保留原 clip_id——head 转场/链接/既有
                # marker 的 owner 不悬空（此前换新 ID 使全部引用孤儿化）。
                left_clip = copy_clip_with(
                    clip,
                    track_id=track_id,
                    timeline_duration=left_duration,
                    source_start=ls,
                    source_duration=left_source_duration,
                )
                clips_to_update.append(left_clip)

            # 右剪辑
            if right_duration > 0 and right_source_duration > 0:
                # AUD-18800：同上；右半段为新生片段，fade_in 清零与
                # split_clip 的右半语义一致，链接关系按原代码不延续。
                right_clip = copy_clip_with(
                    clip,
                    clip_id=str(uuid.uuid4()),
                    track_id=track_id,
                    timeline_start=new_clip_end,
                    timeline_duration=right_duration,
                    source_start=right_source_start,
                    source_duration=right_source_duration,
                    linked_clip_id=None,  # 右剪辑不再链接
                    fade_in_duration=0,
                )
                clips_to_update.append(right_clip)
                # N08/C3-08：tail 转场与右半 scoped marker 迁移到右残片
                middle_right_map[clip.clip_id] = right_clip.clip_id
            # N08/C3-08：覆盖区 [playhead, new_clip_end) 的 scoped marker
            # 随内容删除；< playhead 留左半（原 ID）；>= new_clip_end 归
            # 右残片。右半不存在时覆盖区外也一并删除。
            middle_covered.add(clip.clip_id)
        elif playhead <= clip_start:
            # 新剪辑覆盖剪辑左部分 -> 切掉左部分
            # R07：残片源区间经 clip 自身边缘映射（方向/FPS 感知）
            new_clip_end_in_source = new_clip_end - clip_start
            new_source_start, new_source_duration = (
                clip.with_left_edge_source_values(new_clip_end)
            )
            new_timeline_duration = clip.timeline_duration - new_clip_end_in_source

            if new_timeline_duration > 0 and new_source_duration > 0:
                # AUD-18800：不可变更新——残片保留原 clip_id 与全部扩展
                # 字段，仅覆写被切掉左半后的范围字段。
                trimmed_clip = copy_clip_with(
                    clip,
                    track_id=track_id,
                    timeline_start=new_clip_end,
                    timeline_duration=new_timeline_duration,
                    source_start=new_source_start,
                    source_duration=new_source_duration,
                )
                clips_to_update.append(trimmed_clip)
            else:
                clips_to_remove.append(clip.clip_id)
                fully_deleted.add(clip.clip_id)
        elif new_clip_end >= clip_end:
            # 新剪辑覆盖剪辑右部分 -> 切掉右部分
            # R07：残片源区间经 clip 自身边缘映射（方向/FPS 感知）
            overlap_start = playhead - clip_start
            new_timeline_duration = overlap_start
            new_source_start, new_source_duration = clip.with_right_edge_source_values(
                overlap_start
            )

            if new_timeline_duration > 0 and new_source_duration > 0:
                # AUD-18800：不可变更新——残片保留原 clip_id 与全部扩展
                # 字段，仅覆写被切掉右半后的范围字段。
                trimmed_clip = copy_clip_with(
                    clip,
                    track_id=track_id,
                    timeline_duration=new_timeline_duration,
                    source_start=new_source_start,
                    source_duration=new_source_duration,
                )
                clips_to_update.append(trimmed_clip)
                # N08/C3-08：被切掉的右半内容区域 marker 一并删除
                tail_trimmed.add(clip.clip_id)
            else:
                clips_to_remove.append(clip.clip_id)
                fully_deleted.add(clip.clip_id)
        else:
            # 其他重叠情况，需要切
            # 新剪辑部分覆盖现有剪辑
            overlap_start_in_clip = max(0, playhead - clip_start)
            clip_duration = clip.timeline_duration
            overlap_end_in_clip = min(clip_duration, new_clip_end - clip_start)

            # 如果重叠部分在中间，切成两部分
            if overlap_start_in_clip > 0 and overlap_end_in_clip < clip_duration:
                # 左部分（R07：统一分区 helper）
                left_duration = overlap_start_in_clip
                ls, ldur, _rs, _rd = split_source_partition(
                    clip, clip_start + overlap_start_in_clip
                )
                left_source_duration = ldur
                # AUD-18800：不可变更新——半片继承原片段全部扩展字段。
                # N08/C3-08：左半保留原 ID（同主中覆盖分支）
                left_clip = copy_clip_with(
                    clip,
                    track_id=track_id,
                    timeline_duration=left_duration,
                    source_start=ls,
                    source_duration=left_source_duration,
                )
                clips_to_remove.append(clip.clip_id)
                clips_to_update.append(left_clip)

                # 右部分
                right_duration = clip_duration - overlap_end_in_clip
                _ls2, _ld2, rs2, rd2 = split_source_partition(
                    clip, clip_start + overlap_end_in_clip
                )
                right_source_start = rs2
                right_source_duration = rd2
                if right_duration > 0:
                    # AUD-18800：同上；右半段 fade_in 清零与 split_clip 语义一致。
                    right_clip = copy_clip_with(
                        clip,
                        clip_id=str(uuid.uuid4()),
                        track_id=track_id,
                        timeline_start=clip_start + overlap_end_in_clip,
                        timeline_duration=right_duration,
                        source_start=right_source_start,
                        source_duration=right_source_duration,
                        linked_clip_id=None,
                        fade_in_duration=0,
                    )
                    clips_to_update.append(right_clip)
                    middle_right_map[clip.clip_id] = right_clip.clip_id
                middle_covered.add(clip.clip_id)

    # N08/C3-08：原子终态重建——一次 `_rebuild` 应用删除/更新与 tail
    # 转场迁移，再统一 reanchor。此前逐片 with_clip_removed 触发中间态
    # reanchor，先把本应迁移给右残片的 tail 转场删掉（转场 1→0 且
    # Undo 只回 clips 不回转场）。
    update_map = {c.clip_id: c for c in clips_to_update}
    remove_set = set(clips_to_remove)
    kept_clips = []
    for existing_clip in track.clips:
        if existing_clip.clip_id in remove_set:
            continue
        kept_clips.append(update_map.pop(existing_clip.clip_id, existing_clip))
    kept_clips.extend(update_map.values())
    remapped_transitions = tuple(
        replace(transition, right_clip_id=middle_right_map[transition.right_clip_id])
        if transition.right_clip_id in middle_right_map else transition
        for transition in track.transitions
    )
    new_track = track._rebuild(
        clips=tuple(kept_clips), transitions=remapped_transitions
    ).reanchor_transitions()

    # 添加新剪辑（AUD-18809：模型层校验错误统一转译为公共异常契约）
    try:
        new_track = new_track.with_clip_added(new_clip)
    except ValueError as exc:
        raise _translate_model_error(
            exc, f"Overwrite at playhead {playhead} on track {track_id}"
        ) from exc

    # N08/C3-08：scoped marker 按 owner 命运迁移/删除——中覆盖右半归右
    # 残片、覆盖区删除；全删除/尾切区域删除；不留下指已删 ID 的孤儿。
    if middle_covered or fully_deleted or tail_trimmed:
        new_markers = []
        for marker in sequence.markers:
            if marker.clip_id in middle_covered:
                if marker.frame < playhead:
                    new_markers.append(marker)  # 左半（原 ID 保留）
                elif (marker.frame >= new_clip_end
                        and marker.clip_id in middle_right_map):
                    new_markers.append(replace(
                        marker, clip_id=middle_right_map[marker.clip_id]))
                # 覆盖区 [playhead, new_clip_end)：随内容删除
                continue
            if marker.clip_id in fully_deleted:
                continue  # owner 已删
            if (marker.clip_id in tail_trimmed
                    and marker.frame >= playhead):
                continue  # 被切掉的右半内容
            new_markers.append(marker)
        sequence = sequence._rebuild(markers=tuple(new_markers))

    return sequence.with_track_updated(new_track)


def append_clip_to_timeline(
    sequence: TimelineSequence,
    asset_id: str,
    in_frame: int,
    out_frame: int,
    track_id: str,
    speed: float = 1.0,
    source_fps_num: int | None = None,
    source_fps_den: int | None = None,
    timeline_fps_num: int | None = None,
    timeline_fps_den: int | None = None,
    source_pts_index: tuple | list | None = None,
) -> TimelineSequence:
    """
    追加剪辑到时间线末尾。

    Append 操作：
    1. 找到目标轨道的最后一个剪辑
    2. 在轨道末尾追加新剪辑

    Args:
        sequence: 时间线序列
        asset_id: 素材 ID
        in_frame: 素材起始帧（source_start）
        out_frame: 素材结束帧（source_end，包含）
        track_id: 目标轨道 ID
        speed: 播放速度（默认 1.0）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果轨道不存在或参数无效
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {track_id}")

    if in_frame < 0:
        raise TimelineOperationError(f"in_frame cannot be negative: {in_frame}")
    if out_frame < in_frame:
        raise TimelineOperationError(f"out_frame must be >= in_frame: {out_frame} < {in_frame}")

    # 计算素材范围
    source_start = in_frame
    source_duration = out_frame - in_frame + 1

    # 计算时间线时长
    timeline_duration = timeline_duration_for_source(
        source_duration, speed,
        source_fps_num, source_fps_den,
        timeline_fps_num, timeline_fps_den,
    )

    # 找到轨道末尾位置
    timeline_end = track.duration_frames  # 轨道最后一个剪辑的结束位置

    # 创建新剪辑
    new_clip = ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=timeline_end,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=speed,
        # W07：与 insert/overwrite 同契约透传时间基快照（追加此前直接
        # 丢弃 fps/pts 元数据——混合帧率追加后采样回退默认时基）
        _source_fps_num=source_fps_num,
        _source_fps_den=source_fps_den,
        _source_pts_index=tuple(source_pts_index)
        if source_pts_index is not None else None,
        _timeline_fps_num=timeline_fps_num,
        _timeline_fps_den=timeline_fps_den,
    )

    new_track = track.with_clip_added(new_clip)

    return sequence.with_track_updated(new_track)


# ================================================================
# Roll Trim Operations (P4.3)
# ================================================================


def roll_trim_delta_bounds(
    left_clip: ClipInstance, right_clip: ClipInstance,
    left_max_source_frames: int | None = None,
    right_max_source_frames: int | None = None,
) -> tuple[int, int]:
    """Feasible integer Roll delta for preview and command validation.

    Binary searches are logarithmic in timeline length. Each predicate uses
    the same source-window mapping as the committed transaction, including
    reverse, speed curves and FPS, rather than treating timeline ticks as
    source frames. Unknown source tails leave only that tail unbounded.
    """
    lo = 1 - left_clip.timeline_duration
    hi = right_clip.timeline_duration - 1

    def feasible(delta: int) -> bool:
        for clip, window, maximum in (
            (left_clip, left_clip.with_right_edge_source_values(
                left_clip.timeline_duration + delta), left_max_source_frames),
            (right_clip, right_clip.with_left_edge_source_values(
                right_clip.timeline_start + delta), right_max_source_frames),
        ):
            start, duration = window
            if start < 0 or duration <= 0 or (maximum is not None and start + duration > maximum):
                return False
        return True

    start, end = lo, 0
    while start < end:
        mid = (start + end) // 2
        if feasible(mid):
            end = mid
        else:
            start = mid + 1
    minimum = start
    start, end = 0, hi
    while start < end:
        mid = (start + end + 1) // 2
        if feasible(mid):
            start = mid
        else:
            end = mid - 1
    return minimum, start


def roll_trim_clips(
    sequence: TimelineSequence,
    left_clip_id: str,
    right_clip_id: str,
    delta_frames: int,
    max_source_frames: int | None = None,
    right_max_source_frames: int | None = None,
) -> TimelineSequence:
    """
    Roll trim: 移动两个相邻clip的cut point。

    Roll trim 的特点：
    - 左clip的右边界移动，duration增减
    - 右clip的左边界移动，source_start和duration相反变化
    - timeline总时长不变

    Args:
        sequence: 时间线序列
        left_clip_id: 左侧clip ID
        right_clip_id: 右侧clip ID
        delta_frames: 移动的帧数（正数向右，负数向左）
        max_source_frames: 左剪辑素材的可用总帧数（AUD-18804）。提供时，
            左剪辑延长（delta>0）不得使 source 末端越过该上界；None 表示
            调用方无素材长度信息，不做上界校验（与 ripple 右缘同口径）。

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑不存在、不相邻、或超出素材范围

    Examples:
        >>> # Roll trim 向右移动 10 帧
        >>> # 左剪辑延长 10 帧，右剪辑缩短 10 帧
        >>> new_seq = roll_trim_clips(sequence, "left_clip", "right_clip", 10)
    """
    # 获取剪辑
    left_clip = sequence.get_clip_by_id(left_clip_id)
    if left_clip is None:
        raise TimelineOperationError(f"Left clip not found: {left_clip_id}")

    right_clip = sequence.get_clip_by_id(right_clip_id)
    if right_clip is None:
        raise TimelineOperationError(f"Right clip not found: {right_clip_id}")

    # 验证在同一轨道
    if left_clip.track_id != right_clip.track_id:
        raise TimelineOperationError(
            "Roll trim only supported between clips on the same track"
        )

    track = sequence.get_track_by_id(left_clip.track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {left_clip.track_id}")

    # 验证剪辑相邻
    prev, next_ = track.get_adjacent_clips(left_clip_id)
    if next_ is None or next_.clip_id != right_clip_id:
        raise TimelineOperationError(
            f"Clips {left_clip_id} and {right_clip_id} are not adjacent"
        )

    if left_clip.timeline_end != right_clip.timeline_start:
        raise TimelineOperationError("Roll trim requires a shared cut without a gap")
    minimum, maximum = roll_trim_delta_bounds(
        left_clip, right_clip, max_source_frames, right_max_source_frames)
    if not minimum <= delta_frames <= maximum:
        raise TimelineOperationError(
            f"Roll trim delta {delta_frames} outside source bounds [{minimum}, {maximum}]")


    # R07：source 侧增量经各自片段的方向/速度/FPS 感知边缘映射（旧
    # int(delta×effective_speed) 只覆盖正向常速——反向 roll +10 时 A
    # 首帧 199 变 209、B 保留帧 189 变 199，审计实测内容错乱）。
    new_left_duration = left_clip.timeline_duration + delta_frames
    new_right_start = right_clip.timeline_start + delta_frames
    (
        left_new_source_start,
        left_new_source_duration,
    ) = left_clip.with_right_edge_source_values(new_left_duration)
    (
        right_new_source_start,
        right_new_source_duration,
    ) = right_clip.with_left_edge_source_values(new_right_start)

    # 约束检查：不能超过左剪辑的素材起点
    # delta_frames > 0 时，左剪辑延长，右剪辑缩短
    # 左剪辑延长时，需要检查是否有足够的素材可扩展
    if delta_frames > 0:
        # AUD-18804：素材上界校验。此前左延长只校验右片段最短 1 帧，
        # source 末端可无限越过素材实际长度（下游读到素材外内容）。
        if max_source_frames is not None:
            new_left_source_end = (
                left_new_source_start + left_new_source_duration
            )
            if new_left_source_end > max_source_frames:
                raise TimelineOperationError(
                    f"Roll trim would exceed left clip's source material "
                    f"(source end {new_left_source_end} > {max_source_frames} frames)"
                )
        # 为了安全，检查右剪辑是否可以缩短
        min_right_duration = 1  # 最少保留 1 帧
        if right_clip.timeline_duration - delta_frames < min_right_duration:
            raise TimelineOperationError(
                f"Roll trim would make right clip too short "
                f"(duration would be {right_clip.timeline_duration - delta_frames})"
            )

    # delta_frames < 0 时，左剪辑缩短，右剪辑延长
    if delta_frames < 0:
        # 左剪辑缩短：检查左剪辑是否可以缩短
        min_left_duration = 1
        if left_clip.timeline_duration + delta_frames < min_left_duration:
            raise TimelineOperationError(
                f"Roll trim would make left clip too short "
                f"(duration would be {left_clip.timeline_duration + delta_frames})"
            )

        # The shared bounds already validate the exact curve window. A
        # second delta × base-speed check rejects legal extensions of a
        # sliced curve whose current speed differs from its original base.

    # 约束检查：不能让剪辑时间线位置重叠
    # 新的左剪辑结束帧不能超过新的右剪辑开始帧
    new_left_end = left_clip.timeline_start + left_clip.timeline_duration + delta_frames
    new_right_start = right_clip.timeline_start + delta_frames

    # 验证时间线位置有效
    if new_left_end <= left_clip.timeline_start:
        raise TimelineOperationError(
            "Roll trim would make left clip duration zero or negative"
        )
    if new_right_start + (right_clip.timeline_duration - delta_frames) <= new_right_start:
        raise TimelineOperationError(
            "Roll trim would make right clip duration zero or negative"
        )

    # 计算新的剪辑属性（source 侧来自 R07 边缘映射）
    new_left_timeline_duration = new_left_duration
    new_left_source_duration = left_new_source_duration

    new_right_timeline_start = new_right_start
    new_right_timeline_duration = right_clip.timeline_duration - delta_frames
    new_right_source_start = right_new_source_start
    new_right_source_duration = right_new_source_duration

    # 创建新的剪辑实例
    # AUD-18800：不可变更新（copy_clip_with）——仅覆写本操作语义要求的
    # 字段，扩展字段（调色/关键帧/淡入出/增益/冻结帧/FPS 元数据等）保留。
    # 左剪辑：延长或缩短（duration 变化，source 右缘随 cut point 移动）
    new_left_clip = copy_clip_with(
        left_clip,
        timeline_duration=new_left_timeline_duration,
        source_start=left_new_source_start,
        source_duration=new_left_source_duration,
        fade_in_duration=min(
            left_clip.fade_in_duration, new_left_timeline_duration
        ),
        fade_out_duration=min(
            left_clip.fade_out_duration, new_left_timeline_duration
        ),
    )

    # 右剪辑：缩短或延长，source_start 相反移动（R05：fade 钳制）
    new_right_clip = copy_clip_with(
        right_clip,
        timeline_start=new_right_timeline_start,
        timeline_duration=new_right_timeline_duration,
        source_start=new_right_source_start,
        source_duration=new_right_source_duration,
        fade_in_duration=min(
            right_clip.fade_in_duration, new_right_timeline_duration
        ),
        fade_out_duration=min(
            right_clip.fade_out_duration, new_right_timeline_duration
        ),
    )

    # R04：单次原子重建两片最终几何。此前分两步 with_clip_updated——
    # 第一步的中间态在切点留下瞬时 gap，_rebuild 重锚把合法 seam 转场
    # 当分离删除（审计实测 Roll 后转场 1→0→undo 仍 0），且顺序更新还
    # 会撞重叠校验。最终快照只重锚一次，相接转场保留。
    new_clips = tuple(
        new_left_clip if c.clip_id == left_clip.clip_id
        else (new_right_clip if c.clip_id == right_clip.clip_id else c)
        for c in track.clips
    )
    new_track = track._rebuild(clips=new_clips)

    result = sequence.with_track_updated(new_track)
    windows = {new_left_clip.clip_id: new_left_clip, new_right_clip.clip_id: new_right_clip}
    return result._rebuild(markers=tuple(
        m for m in result.markers if m.clip_id not in windows
        or windows[m.clip_id].timeline_start <= m.frame < windows[m.clip_id].timeline_end))


def find_roll_points(
    sequence: TimelineSequence,
    track_id: str
) -> tuple[tuple[str, str, int], ...]:
    """
    获取轨道上所有可以进行 roll trim 的 cut point。

    Roll trim 要求剪辑相邻且没有间隙。

    Args:
        sequence: 时间线序列
        track_id: 轨道 ID

    Returns:
        (左剪辑ID, 右剪辑ID, cut_point帧) 元组列表
    """
    track = sequence.get_track_by_id(track_id)
    if track is None:
        return tuple()

    roll_points: list[tuple[str, str, int]] = []
    sorted_clips = sorted(track.clips, key=lambda c: c.timeline_start)

    for i in range(len(sorted_clips) - 1):
        left = sorted_clips[i]
        right = sorted_clips[i + 1]

        # Cut point 是左剪辑结束的位置
        cut_frame = left.timeline_end

        # 只有当剪辑紧邻时（无间隙）才能 roll trim
        if right.timeline_start == left.timeline_end:
            roll_points.append((left.clip_id, right.clip_id, cut_frame))

    return tuple(roll_points)


def get_roll_trim_constraints(
    sequence: TimelineSequence,
    left_clip_id: str,
    right_clip_id: str,
    left_max_source_frames: int = 0,  # 左剪辑素材最大帧数
    right_max_source_frames: int = 0  # 右剪辑素材最大帧数
) -> tuple[int, int]:
    """
    计算 roll trim 的约束范围。

    Args:
        sequence: 时间线序列
        left_clip_id: 左侧剪辑 ID
        right_clip_id: 右侧剪辑 ID
        left_max_source_frames: 左剪辑素材可用的最大帧数（用于扩展）
        right_max_source_frames: 右剪辑素材可用的最大帧数

    Returns:
        (最小delta, 最大delta) 元组
        - 最小 delta：可以向左移动的最大帧数（负数）
        - 最大 delta：可以向右移动的最大帧数（正数）
    """
    left_clip = sequence.get_clip_by_id(left_clip_id)
    right_clip = sequence.get_clip_by_id(right_clip_id)

    if left_clip is None or right_clip is None:
        return (0, 0)
    track = sequence.get_track_by_id(left_clip.track_id)
    if (track is None or track.is_locked or left_clip.track_id != right_clip.track_id
            or left_clip.timeline_end != right_clip.timeline_start):
        return (0, 0)
    # Public/numeric and drag callers share the command's exact source-window
    # constraints, including reverse playback, ramps, phase and both assets.
    return roll_trim_delta_bounds(
        left_clip, right_clip,
        left_max_source_frames if left_max_source_frames > 0 else None,
        right_max_source_frames if right_max_source_frames > 0 else None)


# ================================================================
# Slide Operations (P4.5)
# ================================================================


def slide_clip(
    sequence: TimelineSequence,
    clip_id: str,
    delta_frames: int,
    prev_max_source_frames: int | None = None,
    next_max_source_frames: int | None = None,
) -> TimelineSequence:
    """
    Slide: clip在timeline位置移动，前后clip duration被调整。

    Slide 操作特点：
    - 当前 clip 在 timeline 上移动位置（timeline_start 改变）
    - 前方 clip 的 duration 可能被缩短或延长（timeline_end 改变）
    - 后方 clip 的 start 可能被后移或前移（timeline_start 改变）
    - 整体 gap 保持不变
    - clip 的 source 范围不变（只改变 timeline 位置）

    Args:
        sequence: 时间线序列
        clip_id: clip ID
        delta_frames: timeline 偏移帧数（正数向右，负数向左）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑不存在或操作无效

    Examples:
        >>> # Clip A: [0, 100], Clip B: [100, 200], Clip C: [200, 300]
        >>> # Slide B 向右 50 帧
        >>> # 结果: A: [0, 150], B: [150, 250], C: [250, 300]
        >>> # A 延长 50，B 平移 50，C 左缘右移 50（右缘固定）
        >>> new_seq = slide_clip(sequence, "B", 50)
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    track = sequence.get_track_by_id(clip.track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {clip.track_id}")
    if track.is_locked:
        raise TimelineOperationError(f"Slide rejected: track {clip.track_id!r} is locked")
    if delta_frames == 0:
        return sequence
    minimum, maximum = get_slide_bounds(
        sequence, clip_id, prev_max_source_frames, next_max_source_frames)
    if not minimum <= delta_frames <= maximum:
        raise TimelineOperationError(
            f"Slide delta {delta_frames} outside source bounds [{minimum}, {maximum}]")

    # 获取前后相邻 clip
    prev_clip, next_clip = track.get_adjacent_clips(clip_id)

    # 单侧邻居也支持 slide；没有邻居时 bounds 只允许零偏移。

    # 计算新的 timeline 位置
    new_timeline_start = clip.timeline_start + delta_frames
    new_timeline_end = clip.timeline_end + delta_frames

    # 验证新位置的有效性
    if new_timeline_start < 0:
        raise TimelineOperationError(
            f"Slide would move clip start before 0: {new_timeline_start}"
        )

    # 验证不会与其他 clip 重叠（排除前后相邻 clip）
    other_clips = tuple(
        c for c in track.clips
        if c.clip_id != clip_id
        and c.clip_id != (prev_clip.clip_id if prev_clip else None)
        and c.clip_id != (next_clip.clip_id if next_clip else None)
    )
    for other in other_clips:
        if (new_timeline_start < other.timeline_end and
            new_timeline_end > other.timeline_start):
            raise TimelineOperationError(
                f"Slide would overlap with clip {other.clip_id}"
            )

    # 更新当前 clip（只改变 timeline 位置，source 不变）
    # AUD-18800：不可变更新——仅覆写 timeline_start，扩展字段保留。
    shifted_animation = clip.with_temporal_shift(delta_frames)
    slid_clip = copy_clip_with(
        clip, timeline_start=new_timeline_start,
        animated_params=shifted_animation.animated_params,
        effects=shifted_animation.effects,
    )

    # 更新前方 clip（如果存在）
    updated_prev_clip = None
    if prev_clip is not None:
        # 邻居边缘与选中片段同量平移，保留原有 gap。
        new_prev_end = prev_clip.timeline_end + delta_frames
        new_prev_duration = new_prev_end - prev_clip.timeline_start

        # 验证前方 clip 的 duration 有效
        if new_prev_duration <= 0:
            raise TimelineOperationError(
                f"Slide would make previous clip {prev_clip.clip_id} have zero or negative duration"
            )

        # 前方 clip 的 source duration 也需要相应调整
        new_prev_source_start, new_prev_source_duration = prev_clip.with_right_edge_source_values(
            new_prev_duration)

        # 验证 source duration 有效
        if new_prev_source_duration <= 0:
            raise TimelineOperationError(
                f"Slide would make previous clip {prev_clip.clip_id} have zero or negative source duration"
            )

        # AUD-18800：不可变更新——邻居片段同样只改操作语义要求的字段。
        updated_prev_clip = copy_clip_with(
            prev_clip,
            timeline_duration=new_prev_duration,
            source_start=new_prev_source_start,
            source_duration=new_prev_source_duration,
        )

    # 更新后方 clip（如果存在）
    updated_next_clip = None
    if next_clip is not None:
        # 后邻居右缘固定，仅左缘同量平移，保留原有 gap。
        new_next_start = next_clip.timeline_start + delta_frames
        new_next_duration = next_clip.timeline_end - new_next_start

        # 验证后方 clip 的 duration 有效
        if new_next_duration <= 0:
            raise TimelineOperationError(
                f"Slide would make next clip {next_clip.clip_id} have zero or negative duration"
            )

        # 后方 clip 的 source start 也需要相应调整
        new_next_source_start, new_next_source_duration = next_clip.with_left_edge_source_values(
            new_next_start)

        # 验证 source start 有效
        if new_next_source_start < 0:
            raise TimelineOperationError(
                f"Slide would make next clip {next_clip.clip_id} have negative source start"
            )

        # 验证 source duration 有效
        if new_next_source_duration <= 0:
            raise TimelineOperationError(
                f"Slide would make next clip {next_clip.clip_id} have zero or negative source duration"
            )

        updated_next_clip = copy_clip_with(
            next_clip,
            timeline_start=new_next_start,
            timeline_duration=new_next_duration,
            source_start=new_next_source_start,
            source_duration=new_next_source_duration,
        )

    # 应用更新。
    # AUD-18801：逐个 with_clip_updated 会因中间态触发重叠校验——先把
    # slid_clip 移到新位置时前/后邻居尚未收缩/后移，紧邻布局（slide 的
    # 典型场景）下任何非零 delta 都抛裸 ValueError。改为一次原子
    # _rebuild：最终态无重叠由上方校验保证（两侧 gap 与原值相等；
    # 与其他 clip 的重叠已由
    # 函数前部的显式重叠检查拒绝）。
    replacements: dict[str, ClipInstance] = {}
    if updated_prev_clip is not None:
        replacements[updated_prev_clip.clip_id] = updated_prev_clip
    if updated_next_clip is not None:
        replacements[updated_next_clip.clip_id] = updated_next_clip
    replacements[slid_clip.clip_id] = slid_clip
    new_clips = tuple(
        replacements.get(c.clip_id, c) for c in track.clips
    )
    new_track = track._rebuild(clips=new_clips)

    result = sequence.with_track_updated(new_track)
    return result._rebuild(markers=tuple(
        replace(marker, frame=marker.frame + delta_frames) if marker.clip_id == clip_id else marker
        for marker in result.markers
        if marker.clip_id not in replacements or marker.clip_id == clip_id
        or replacements[marker.clip_id].timeline_start <= marker.frame < replacements[marker.clip_id].timeline_end))


def get_slide_bounds(
    sequence: TimelineSequence,
    clip_id: str,
    prev_max_source_frames: int | None = None,
    next_max_source_frames: int | None = None
) -> tuple[int, int]:
    """
    获取Slide操作的可用范围。

    Args:
        sequence: 时间线序列
        clip_id: clip ID
        prev_max_source_frames: 前方clip素材最大帧数（可选，用于扩展约束）
        next_max_source_frames: 后方clip素材最大帧数（可选，用于扩展约束）

    Returns:
        (min_delta, max_delta) 可用的偏移范围

    Raises:
        TimelineOperationError: 如果剪辑不存在
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    track = sequence.get_track_by_id(clip.track_id)
    if track is None:
        return (0, 0)

    prev_clip, next_clip = track.get_adjacent_clips(clip_id)

    if track.is_locked or (prev_clip is None and next_clip is None):
        return (0, 0)

    # Slide preserves both existing gaps. Positive delta extends the previous
    # tail and trims the next head; negative delta does the converse.
    minimum = max(-clip.timeline_start,
                  1 - prev_clip.timeline_duration if prev_clip is not None else -clip.timeline_start)
    maximum = (next_clip.timeline_duration - 1 if next_clip is not None
               else 2**31 - 1 - clip.timeline_end)

    def feasible(delta: int) -> bool:
        windows = []
        if prev_clip is not None:
            windows.append((prev_clip.with_right_edge_source_values(
                prev_clip.timeline_duration + delta), prev_max_source_frames))
        if next_clip is not None:
            windows.append((next_clip.with_left_edge_source_values(
                next_clip.timeline_start + delta), next_max_source_frames))
        return all(start >= 0 and duration > 0
                   and (limit is None or start + duration <= limit)
                   for (start, duration), limit in windows)

    if not feasible(0):
        return (0, 0)
    low, high = minimum, 0
    while low < high:
        middle = (low + high) // 2
        if feasible(middle):
            high = middle
        else:
            low = middle + 1
    minimum = low
    low, high = 0, maximum
    while low < high:
        middle = (low + high + 1) // 2
        if feasible(middle):
            low = middle
        else:
            high = middle - 1
    return minimum, low


# ================================================================
# Slip Operations (P4.4)
# ================================================================


def slip_clip(
    sequence: TimelineSequence,
    clip_id: str,
    delta_frames: int,
    max_source_frames: int | None = None
) -> TimelineSequence:
    """
    Slip: clip在timeline的位置和duration不变，只改变source范围。

    Slip操作：
    - timeline位置不变（timeline_start, timeline_duration 保持不变）
    - 同时移动source_start和source_end（都增加delta_frames）
    - 不超过素材实际长度

    Args:
        sequence: 时间线序列
        clip_id: clip ID
        delta_frames: source偏移帧数（正数向后，负数向前）
        max_source_frames: 素材最大帧数（可选，用于边界检查）

    Returns:
        新的 TimelineSequence

    Raises:
        TimelineOperationError: 如果剪辑不存在或超出素材范围

    Examples:
        >>> # Slip: clip显示素材的后面10帧内容
        >>> new_seq = slip_clip(sequence, "clip1", 10)
        >>> # 结果：source_start和source_end都+10，timeline位置不变
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    # 计算新的source范围
    new_source_start = clip.source_start + delta_frames
    new_source_end = clip.source_start + clip.source_duration + delta_frames

    # 边界检查：新的source范围必须在素材范围内
    if new_source_start < 0:
        raise TimelineOperationError(
            f"Slip would move source_start to {new_source_start}, "
            f"which is before the start of the source material"
        )

    # 如果提供了最大帧数，检查是否超出素材末尾
    if max_source_frames is not None:
        if new_source_end > max_source_frames:
            raise TimelineOperationError(
                f"Slip would move source_end to {new_source_end}, "
                f"which exceeds source material length ({max_source_frames} frames)"
            )

    # 创建新的clip实例（timeline位置不变，只改变source范围）
    # AUD-18800：不可变更新——仅覆写 source_start，其余字段（含调色联动、
    # 关键帧动画、淡入出、混音增益、冻结帧、FPS 元数据等）全部保留。
    new_clip = copy_clip_with(clip, source_start=new_source_start)

    return sequence.with_clip_updated(new_clip)


def get_slip_bounds(
    sequence: TimelineSequence,
    clip_id: str,
    max_source_frames: int | None = None
) -> tuple[int, int]:
    """
    获取Slip操作的可用范围。

    Args:
        sequence: 时间线序列
        clip_id: clip ID
        max_source_frames: 素材最大帧数（可选）

    Returns:
        (min_delta, max_delta) 可用的偏移范围

    Raises:
        TimelineOperationError: 如果剪辑不存在
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    # 计算最小偏移（向前滑动的极限）
    min_delta = -clip.source_start  # source_start不能小于0

    # 计算最大偏移（向后滑动的极限）
    if max_source_frames is not None:
        # source_end不能超过max_source_frames
        max_delta = max_source_frames - clip.source_start - clip.source_duration
    else:
        # 如果没有提供max_source_frames，没有上限限制
        max_delta = 1000000  # 一个很大的值

    return (min_delta, max_delta)


# ================================================================
# Ripple Trim Operations (P4.2)
# ================================================================


def ripple_trim_clip_left(
    sequence: TimelineSequence,
    clip_id: str,
    new_start_frame: int
) -> TimelineSequence:
    """
    Ripple trim clip from left edge.

    When trimming the left edge (in point):
    1. Clip's timeline_start changes to new_start_frame
    2. Clip's source_start is adjusted accordingly
    3. All clips after this clip in the same track shift by the delta
    4. No gap is created

    Args:
        sequence: TimelineSequence to modify
        clip_id: Clip ID to trim
        new_start_frame: New timeline start frame

    Returns:
        New TimelineSequence with trimmed clip and shifted subsequent clips

    Raises:
        TimelineOperationError: If clip not found or invalid new_start_frame
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    track = sequence.get_track_by_id(clip.track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {clip.track_id}")

    # Calculate delta (how much we're extending/trimming from left)
    # Positive delta = extending left (new_start < current_start)
    # Negative delta = trimming left edge right (new_start > current_start)
    delta = clip.timeline_start - new_start_frame

    # Validate: new_start must not exceed clip end
    if new_start_frame >= clip.timeline_end:
        raise TimelineOperationError(
            f"new_start_frame {new_start_frame} must be before clip end {clip.timeline_end}"
        )

    # Check for overlap with previous clip (when extending left)
    prev_clip, _ = track.get_adjacent_clips(clip_id)
    if prev_clip is not None and new_start_frame < prev_clip.timeline_end:
        raise TimelineOperationError(
            f"Ripple trim would overlap with previous clip {prev_clip.clip_id}"
        )

    # N01/C3-01：源区间经片段自身映射（方向/FPS/速度曲线感知）——旧
    # ``int(delta * effective_speed)`` 基速公式在反向片（左裁保留帧应从
    # 179 开头却得 199）与混合帧率（30→24 裁 20 应推进 25 却得 20）下
    # 全部错域。
    if delta > 0:
        # 延长：入点左移，走左缘 helper（反向片从源高端扩展）
        new_source_start, new_source_duration = (
            clip.with_left_edge_source_values(new_start_frame)
        )
    else:
        # 缩短：等价删除头部 n = new_start_frame - timeline_start 个时间
        # 线帧的内容；保留内容自 new_start_frame 起，保留窗口 = [该处源
        # 帧, 原右缘]（方向由映射给出）
        cut_source = clip.timeline_to_source_frame(new_start_frame)
        # 保留边缘按播放方向：正向裁头保尾缘；反向裁头（时间线头部）
        # 播放的是源低端，保 source_start 缘
        from src.features.edit.core.clip_params import SpeedParams as _SP

        plays_forward = not (
            isinstance(clip.speed, _SP) and clip.speed.reverse)
        # clip.source_end 已是含末帧（inclusive）
        anchor_edge = (
            clip.source_end if plays_forward else clip.source_start)
        lo, hi = sorted((cut_source, anchor_edge))
        new_source_start = lo
        new_source_duration = hi - lo + 1
    new_timeline_duration = clip.timeline_duration + delta

    # Validate source range
    if new_source_start < 0:
        raise TimelineOperationError(
            "Ripple trim would exceed source start (negative source_start)"
        )

    # P1-04 审计修复：入点波纹的锚定语义（与波纹删除等价）：
    # - 缩短（delta<0，入点右移 n）：等价于波纹删除头部 n 帧——起点
    #   锚定在原位，右缘与后续片段整体左移 n（时间线收短，不留洞）。
    #   旧实现把起点移到 new_start 且后续右移，缩短反而在尾部留洞。
    # - 延长（delta>0，入点左移 m）：左缘移到 new_start 吃掉左侧空间，
    #   右缘不动，后续片段不动。旧实现后续左移会撞出重叠异常。
    anchored_start = new_start_frame if delta > 0 else clip.timeline_start

    # Create trimmed clip
    # AUD-18800：不可变更新——仅覆写入点波纹要求的四个字段，扩展字段保留。
    trimmed_clip = copy_clip_with(
        clip,
        timeline_start=anchored_start,
        mapping_timeline_offset=new_start_frame - clip.timeline_start,
        timeline_duration=new_timeline_duration,
        source_start=new_source_start,
        source_duration=new_source_duration,
    )

    # 后续片段：缩短时左移收拢（delta<0），延长时不动（右缘未变）。
    # 单次原子重建（逐个 with_clip_updated 会因瞬时重叠误抛）。
    follower_shift = delta if delta < 0 else 0
    moved_ids = {
        c.clip_id for c in track.clips
        if c.timeline_start > clip.timeline_start and follower_shift
    }
    new_clips = tuple(
        trimmed_clip if c.clip_id == clip.clip_id
        else (c.with_timeline_start(c.timeline_start + follower_shift)
              .with_temporal_shift(follower_shift)
              if c.timeline_start > clip.timeline_start else c)
        for c in track.clips
    )
    new_track = track._rebuild(clips=new_clips)
    # R08：跟随片段的 scoped marker 同移（keys 已由 with_temporal_shift）
    if moved_ids:
        sequence = sequence._rebuild(
            markers=tuple(
                replace(marker, frame=marker.frame + follower_shift)
                if marker.clip_id in moved_ids else marker
                for marker in sequence.markers
            )
        )

    sequence = sequence._rebuild(markers=tuple(
        replace(m, frame=m.frame + delta) if m.clip_id == clip_id and delta < 0 else m
        for m in sequence.markers
        if m.clip_id != clip_id or (new_start_frame <= m.frame < clip.timeline_end
                                   if delta < 0 else True)))
    return sequence.with_track_updated(new_track)


def ripple_trim_clip_right(
    sequence: TimelineSequence,
    clip_id: str,
    new_end_frame: int,
    max_source_frames: int | None = None,
) -> TimelineSequence:
    """
    Ripple trim clip from right edge.

    When trimming the right edge (out point):
    1. Clip's timeline_end changes to new_end_frame
    2. Clip's source_duration is adjusted accordingly
    3. All clips after this clip in the same track shift by the delta
    4. No gap is created

    Args:
        sequence: TimelineSequence to modify
        clip_id: Clip ID to trim
        new_end_frame: New timeline end frame
        max_source_frames: 素材可用总帧数（AUD-18804）。提供时右延长
            （new_end_frame > timeline_end）不得使 source 末端越过上界；
            None 表示调用方无素材长度信息，不做上界校验。

    Returns:
        New TimelineSequence with trimmed clip and shifted subsequent clips

    Raises:
        TimelineOperationError: If clip not found or invalid new_end_frame
    """
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        raise TimelineOperationError(f"Clip not found: {clip_id}")

    track = sequence.get_track_by_id(clip.track_id)
    if track is None:
        raise TimelineOperationError(f"Track not found: {clip.track_id}")

    # Calculate delta (positive = extending right, negative = trimming right)
    delta = new_end_frame - clip.timeline_end

    # Validate: new_end must be after clip start
    if new_end_frame <= clip.timeline_start:
        raise TimelineOperationError(
            f"new_end_frame {new_end_frame} must be after clip start {clip.timeline_start}"
        )

    # N01/C3-01：源区间经片段自身映射（方向/FPS/速度曲线感知）。右缘
    # 走 with_right_edge_source_values（目标时长→新源窗，方向感知且支持
    # 延长到旧窗口外——timeline_to_source_frame 对延长帧会抛越界）。
    # 旧 ``int(delta * effective_speed)`` 基速公式在反向/混合帧率/变速
    # 点下错域。
    edge_source, edge_duration = clip.with_right_edge_source_values(
        clip.timeline_duration + delta)
    lo, hi = sorted((edge_source, edge_source + edge_duration - 1))
    new_source_duration = hi - lo + 1
    new_timeline_duration = clip.timeline_duration + delta

    # Validate source range
    if new_source_duration <= 0:
        raise TimelineOperationError(
            "Ripple trim would result in zero or negative duration"
        )

    # AUD-18804：右延长时的素材上界校验（与左入点的 source_start < 0
    # 下界校验对称）。此前素材仅 100 帧、clip 已用满 100 帧时右延至 150
    # 帧仍被允许，下游按越界 source 帧取数读到素材外内容。
    if delta > 0 and max_source_frames is not None:
        new_source_end = clip.source_start + new_source_duration
        if new_source_end > max_source_frames:
            raise TimelineOperationError(
                f"Ripple trim would exceed source material "
                f"(source end {new_source_end} > {max_source_frames} frames)"
            )

    # Create trimmed clip
    # AUD-18800：不可变更新——仅覆写出点波纹要求的字段，扩展字段保留。
    # N01/C3-01：反向片的保留窗口低端 = lo（新 source_start），此前不
    # 改 source_start 使反向右裁保留错误低端内容。
    trimmed_clip = copy_clip_with(
        clip,
        timeline_duration=new_timeline_duration,
        source_start=lo,
        source_duration=new_source_duration,
    )

    # P1-04 bug fix：替换 trimmed 片段 + 平移后续片段必须一次原子完成。
    # 旧实现先 with_clip_updated(trimmed) 再逐个平移——向右延长到相邻
    # 片段时，第一步就与未平移的邻居瞬时重叠，抛
    # "Updated clip overlaps"，波纹右延长在相邻轨道布局下从不可用。
    moved_ids = {
        c.clip_id for c in track.clips
        if c.timeline_start >= clip.timeline_end and delta
    }
    new_clips = tuple(
        trimmed_clip if c.clip_id == clip.clip_id
        else (c.with_timeline_start(c.timeline_start + delta)
              .with_temporal_shift(delta)
              if c.timeline_start >= clip.timeline_end else c)
        for c in track.clips
    )
    new_track = track._rebuild(clips=new_clips)
    # R08：跟随片段的 scoped marker 同移（keys 已由 with_temporal_shift）
    if moved_ids:
        sequence = sequence._rebuild(
            markers=tuple(
                replace(marker, frame=marker.frame + delta)
                if marker.clip_id in moved_ids else marker
                for marker in sequence.markers
            )
        )

    sequence = sequence._rebuild(markers=tuple(m for m in sequence.markers
        if m.clip_id != clip_id or clip.timeline_start <= m.frame < new_end_frame))
    return sequence.with_track_updated(new_track)


# ================================================================
# Enhanced Snap System (参考 kdenlive SnapModel)
# ================================================================


class SnapPointType(Enum):
    """吸附点类型枚举。

    参考 kdenlive 的吸附点类型定义，扩展支持更多吸附场景。
    """
    CLIP_START = "clip_start"       # 剪辑起始点
    CLIP_END = "clip_end"           # 剪辑结束点
    PLAYHEAD = "playhead"           # 播放头位置
    MARKER = "marker"               # 标记点
    GUIDE = "guide"                 # Guide 线 (参考线)
    TRANSITION_START = "trans_start"  # 转场起始点
    TRANSITION_CENTER = "trans_center"  # 转场中心点
    TRANSITION_END = "trans_end"    # 转场结束点
    SEQUENCE_START = "seq_start"    # 序列起点 (frame 0)


@dataclass(frozen=True)
class SnapPoint:
    """
    吸附点数据类。

    定义单个吸附点，包含帧位置、类型和来源 ID。

    Attributes:
        frame: 帧位置
        type: 吸附点类型
        source_id: 来源对象 ID（用于显示标签和追踪来源）
    """
    frame: int
    type: SnapPointType
    source_id: Optional[str] = None  # 来源对象 ID（clip_id, marker_id, etc.）

    def __hash__(self) -> int:
        """哈希函数，用于集合存储。"""
        # 同一帧位置可以有多个不同类型的吸附点
        # 所以用 (frame, type, source_id) 组合作为唯一标识
        return hash((self.frame, self.type, self.source_id or ""))

    def __eq__(self, other: object) -> bool:
        """相等比较。"""
        if not isinstance(other, SnapPoint):
            return False
        return (
            self.frame == other.frame
            and self.type == other.type
            and self.source_id == other.source_id
        )


class SnapConfig:
    """
    吸附配置类。

    定义吸附行为可配置的选项。

    Attributes:
        threshold_pixels: 吸附阈值（像素），默认 10
        enabled_types: 启用的吸附点类型集合
        snap_to_clip_edges: 是否吸附到剪辑边界
        snap_to_playhead: 是否吸附到播放头
        snap_to_markers: 是否吸附到标记点
        snap_to_transitions: 是否吸附到转场
        snap_to_guides: 是否吸附到 Guide 线
    """

    # 默认阈值（像素）- 参考 kdenlive SNAP_DISTANCE = 10
    DEFAULT_THRESHOLD_PIXELS: int = 10

    def __init__(
        self,
        threshold_pixels: int = DEFAULT_THRESHOLD_PIXELS,
        enabled_types: Optional[Set[SnapPointType]] = None,
        snap_to_clip_edges: bool = True,
        snap_to_playhead: bool = True,
        snap_to_markers: bool = True,
        snap_to_transitions: bool = True,
        snap_to_guides: bool = True,
    ) -> None:
        self._threshold_pixels = threshold_pixels
        self._snap_to_clip_edges = snap_to_clip_edges
        self._snap_to_playhead = snap_to_playhead
        self._snap_to_markers = snap_to_markers
        self._snap_to_transitions = snap_to_transitions
        self._snap_to_guides = snap_to_guides

        # 如果提供了 enabled_types，使用它；否则根据布尔配置生成
        if enabled_types is not None:
            self._enabled_types = enabled_types
        else:
            self._enabled_types = self._build_enabled_types()

    def _build_enabled_types(self) -> Set[SnapPointType]:
        """根据布尔配置生成启用的类型集合。"""
        types: Set[SnapPointType] = {SnapPointType.SEQUENCE_START}

        if self._snap_to_clip_edges:
            types.add(SnapPointType.CLIP_START)
            types.add(SnapPointType.CLIP_END)

        if self._snap_to_playhead:
            types.add(SnapPointType.PLAYHEAD)

        if self._snap_to_markers:
            types.add(SnapPointType.MARKER)

        if self._snap_to_transitions:
            types.add(SnapPointType.TRANSITION_START)
            types.add(SnapPointType.TRANSITION_CENTER)
            types.add(SnapPointType.TRANSITION_END)

        if self._snap_to_guides:
            types.add(SnapPointType.GUIDE)

        return types

    @property
    def threshold_pixels(self) -> int:
        """吸附阈值（像素）。"""
        return self._threshold_pixels

    @property
    def enabled_types(self) -> Set[SnapPointType]:
        """启用的吸附点类型。"""
        return self._enabled_types

    def is_type_enabled(self, snap_type: SnapPointType) -> bool:
        """检查指定类型的吸附是否启用。"""
        return snap_type in self._enabled_types

    def set_threshold_pixels(self, threshold: int) -> None:
        """设置吸附阈值。"""
        self._threshold_pixels = max(1, min(50, threshold))  # 限制范围 1-50

    def enable_type(self, snap_type: SnapPointType) -> None:
        """启用指定类型的吸附。"""
        self._enabled_types.add(snap_type)

    def disable_type(self, snap_type: SnapPointType) -> None:
        """禁用指定类型的吸附。"""
        self._enabled_types.discard(snap_type)

    def with_temporary_disable_all(self) -> SnapConfig:
        """创建临时禁用所有吸附的配置副本（用于 Shift 键临时禁用）。"""
        return SnapConfig(
            threshold_pixels=self._threshold_pixels,
            enabled_types=set(),  # 空集合 = 禁用所有
        )


class SnapManager:
    """
    吸附点管理器。

    参考 kdenlive 的 SnapModel 设计，管理时间线上的所有吸附点。
    提供吸附点集合的高效存储、更新和查找功能。

    核心功能：
    - 从时间线更新吸附点集合
    - 查找最近的吸附点（基于阈值）
    - 支持排除特定剪辑（移动时）
    - 支持类型过滤

    数据结构：
    - _points_by_frame: 按帧位置索引的吸附点字典 {frame: [SnapPoint]}
    - _all_points: 所有吸附点集合（用于快速遍历）

    性能优化：
    - 按帧索引，O(1) 查找特定帧的吸附点
    - 阈值搜索时使用增量偏移，减少遍历范围

    参考：
    - kdenlive timeline2/model/snapmodel.hpp/cpp
    """

    def __init__(self, config: Optional[SnapConfig] = None) -> None:
        """
        初始化吸附管理器。

        Args:
            config: 吸附配置，默认使用 SnapConfig 的默认值
        """
        self._config = config or SnapConfig()
        self._points_by_frame: Dict[int, List[SnapPoint]] = {}
        self._all_points: Set[SnapPoint] = set()
        self._exclude_clip_ids: Set[str] = set()  # 排除的剪辑 ID

    # ==================== 吸附点管理 ====================

    def clear(self) -> None:
        """清除所有吸附点。"""
        self._points_by_frame.clear()
        self._all_points.clear()

    def add_point(self, point: SnapPoint) -> None:
        """
        添加单个吸附点。

        Args:
            point: 要添加的吸附点
        """
        # 检查类型是否启用
        if not self._config.is_type_enabled(point.type):
            return

        # 添加到集合
        self._all_points.add(point)

        # 添加到帧索引
        if point.frame not in self._points_by_frame:
            self._points_by_frame[point.frame] = []
        self._points_by_frame[point.frame].append(point)

    def remove_point(self, point: SnapPoint) -> None:
        """
        移除单个吸附点。

        Args:
            point: 要移除的吸附点
        """
        self._all_points.discard(point)

        if point.frame in self._points_by_frame:
            points_at_frame = self._points_by_frame[point.frame]
            points_at_frame = [p for p in points_at_frame if p != point]
            if points_at_frame:
                self._points_by_frame[point.frame] = points_at_frame
            else:
                del self._points_by_frame[point.frame]

    def remove_points_for_clip(self, clip_id: str) -> None:
        """
        移除指定剪辑的所有吸附点。

        Args:
            clip_id: 剪辑 ID
        """
        # 找出该剪辑的所有吸附点
        points_to_remove = [
            p for p in self._all_points
            if p.source_id == clip_id and p.type in (SnapPointType.CLIP_START, SnapPointType.CLIP_END)
        ]

        for point in points_to_remove:
            self.remove_point(point)

    # ==================== 时间线更新 ====================

    def update_from_timeline(
        self,
        timeline: TimelineSequence,
        playhead: int,
        exclude_clip_ids: Optional[Set[str]] = None,
    ) -> None:
        """
        从时间线更新所有吸附点。

        清除现有吸附点，并根据时间线内容重新生成。

        Args:
            timeline: 时间线序列
            playhead: 当前播放头帧位置
            exclude_clip_ids: 要排除的剪辑 ID 集合（移动时排除自身）
        """
        self.clear()
        self._exclude_clip_ids = exclude_clip_ids or set()

        # 1. 序列起点 (frame 0)
        self.add_point(SnapPoint(0, SnapPointType.SEQUENCE_START))

        # 2. 播放头位置
        self.add_point(SnapPoint(playhead, SnapPointType.PLAYHEAD))

        # 3. 轨道剪辑和转场
        for track in timeline.all_tracks:
            # 剪辑边界
            for clip in track.clips:
                # 排除指定剪辑
                if clip.clip_id in self._exclude_clip_ids:
                    continue

                self.add_point(SnapPoint(
                    clip.timeline_start,
                    SnapPointType.CLIP_START,
                    clip.clip_id
                ))
                self.add_point(SnapPoint(
                    clip.timeline_end,
                    SnapPointType.CLIP_END,
                    clip.clip_id
                ))

            # SUB-P1-08: subtitle cue edges are first-class snap candidates so
            # drags/trims can align to other cues as well as video cuts. They
            # live in track.subtitle_clips (a separate field from track.clips).
            for sub in getattr(track, "subtitle_clips", ()) or ():
                if sub.clip_id in self._exclude_clip_ids:
                    continue
                self.add_point(SnapPoint(
                    sub.timeline_start,
                    SnapPointType.CLIP_START,
                    sub.clip_id
                ))
                self.add_point(SnapPoint(
                    sub.timeline_end,
                    SnapPointType.CLIP_END,
                    sub.clip_id
                ))

            # 转场边界
            for trans in track.transitions:
                half_duration = trans.duration // 2
                self.add_point(SnapPoint(
                    trans.center_frame - half_duration,
                    SnapPointType.TRANSITION_START,
                    trans.transition_id
                ))
                self.add_point(SnapPoint(
                    trans.center_frame,
                    SnapPointType.TRANSITION_CENTER,
                    trans.transition_id
                ))
                self.add_point(SnapPoint(
                    trans.center_frame + half_duration,
                    SnapPointType.TRANSITION_END,
                    trans.transition_id
                ))

        # 4. 时间线标记
        for marker in timeline.markers:
            self.add_point(SnapPoint(
                marker.frame,
                SnapPointType.MARKER,
                marker.marker_id
            ))

    def update_playhead(self, playhead: int) -> None:
        """
        更新播放头吸附点位置。

        Args:
            playhead: 新的播放头帧位置
        """
        # 移除旧的播放头吸附点
        old_playhead_points = [
            p for p in self._all_points
            if p.type == SnapPointType.PLAYHEAD
        ]
        for point in old_playhead_points:
            self.remove_point(point)

        # 添加新的播放头吸附点
        self.add_point(SnapPoint(playhead, SnapPointType.PLAYHEAD))

    def add_guide(self, frame: int, guide_id: Optional[str] = None) -> None:
        """
        添加 Guide 线吸附点。

        Args:
            frame: Guide 线帧位置
            guide_id: Guide 线 ID
        """
        self.add_point(SnapPoint(frame, SnapPointType.GUIDE, guide_id))

    def remove_guide(self, frame: int, guide_id: Optional[str] = None) -> None:
        """
        移除 Guide 线吸附点。

        Args:
            frame: Guide 线帧位置
            guide_id: Guide 线 ID
        """
        point = SnapPoint(frame, SnapPointType.GUIDE, guide_id)
        self.remove_point(point)

    # ==================== 吸附查找 ====================

    def find_nearest(
        self,
        frame: int,
        pixels_per_frame: float,
        exclude_types: Optional[Set[SnapPointType]] = None,
        exclude_source_ids: Optional[Set[str]] = None,
    ) -> Optional[SnapPoint]:
        """
        查找最近的吸附点。

        使用阈值搜索，找到距离目标帧最近的吸附点。
        阈值基于像素距离计算，转换为帧数。

        Args:
            frame: 目标帧位置
            pixels_per_frame: 当前缩放比例（像素/帧）
            exclude_types: 要排除的吸附点类型集合
            exclude_source_ids: 要排除的剪辑/标记来源 ID 集合

        Returns:
            最近吸附点的 SnapPoint，如果没有则返回 None
        """
        if not self._all_points:
            return None

        # 计算阈值帧数
        threshold_frames = int(self._config.threshold_pixels / pixels_per_frame)
        threshold_frames = max(1, threshold_frames)  # 至少 1 帧

        # 排除类型集合
        excluded = exclude_types or set()
        excluded_sources = exclude_source_ids or set()

        # 增量偏移搜索（从近到远）
        for offset in range(threshold_frames + 1):
            # 先检查正向偏移，再检查负向偏移（偏向更近的点）
            for check_frame in (frame + offset, frame - offset):
                if check_frame < 0:
                    continue  # 跳过负帧

                if check_frame not in self._points_by_frame:
                    continue

                # 获取该帧的所有吸附点
                points_at_frame = self._points_by_frame[check_frame]

                # 过滤排除的类型
                valid_points = [
                    p for p in points_at_frame
                    if p.type not in excluded and p.source_id not in excluded_sources
                ]

                if valid_points:
                    # 返回第一个有效吸附点（按优先级排序）
                    # 优先级：剪辑边界 > 播放头 > 标记 > 转场 > Guide
                    return self._select_best_point(valid_points)

        return None

    def _select_best_point(self, points: List[SnapPoint]) -> SnapPoint:
        """
        从多个同帧吸附点中选择最佳的一个。

        按类型优先级排序：
        1. CLIP_START / CLIP_END (最高)
        2. PLAYHEAD
        3. MARKER
        4. TRANSITION_*
        5. GUIDE
        6. SEQUENCE_START (最低)

        Args:
            points: 同一帧的多个吸附点列表

        Returns:
            最佳吸附点
        """
        # 类型优先级映射
        priority = {
            SnapPointType.CLIP_START: 1,
            SnapPointType.CLIP_END: 1,
            SnapPointType.PLAYHEAD: 2,
            SnapPointType.MARKER: 3,
            SnapPointType.TRANSITION_CENTER: 4,
            SnapPointType.TRANSITION_START: 4,
            SnapPointType.TRANSITION_END: 4,
            SnapPointType.GUIDE: 5,
            SnapPointType.SEQUENCE_START: 6,
        }

        # 按优先级排序，返回最高优先级的
        sorted_points = sorted(points, key=lambda p: priority.get(p.type, 10))
        return sorted_points[0]

    def find_nearest_frame(
        self,
        frame: int,
        pixels_per_frame: float,
        exclude_types: Optional[Set[SnapPointType]] = None,
        exclude_source_ids: Optional[Set[str]] = None,
    ) -> Tuple[int, bool]:
        """
        查找最近的吸附帧位置。

        返回吸附后的帧位置和是否吸附成功的标志。

        Args:
            frame: 目标帧位置
            pixels_per_frame: 当前缩放比例
            exclude_types: 要排除的吸附点类型集合
            exclude_source_ids: 要排除的剪辑/标记来源 ID 集合

        Returns:
            (吸附后的帧位置, 是否吸附成功)
        """
        snap_point = self.find_nearest(
            frame,
            pixels_per_frame,
            exclude_types,
            exclude_source_ids,
        )

        if snap_point is None:
            return (frame, False)

        return (snap_point.frame, True)

    # ==================== 辅助方法 ====================

    def get_all_frames(self) -> Tuple[int, ...]:
        """
        获取所有吸附点帧位置（排序后）。

        Returns:
            吸附帧位置元组
        """
        frames = sorted(self._points_by_frame.keys())
        return tuple(frames)

    def get_points_at_frame(self, frame: int) -> List[SnapPoint]:
        """
        获取指定帧的所有吸附点。

        Args:
            frame: 帧位置

        Returns:
            吸附点列表
        """
        return self._points_by_frame.get(frame, [])

    def get_point_count(self) -> int:
        """获取吸附点总数。"""
        return len(self._all_points)

    def has_points(self) -> bool:
        """检查是否有吸附点。"""
        return len(self._all_points) > 0

    # ==================== 配置 ====================

    def get_config(self) -> SnapConfig:
        """获取当前配置。"""
        return self._config

    def set_config(self, config: SnapConfig) -> None:
        """设置新配置。"""
        self._config = config

    def set_threshold_pixels(self, threshold: int) -> None:
        """设置吸附阈值（像素）。"""
        self._config.set_threshold_pixels(threshold)

    def get_threshold_pixels(self) -> int:
        """获取吸附阈值（像素）。"""
        return self._config.threshold_pixels


# ================================================================
# Snap Helper Functions (保留原有函数，兼容现有代码)
# ================================================================

# 保留原有的 find_snap_points 和 snap_to_nearest 函数
# 用于向后兼容，新代码应使用 SnapManager


# ================================================================
# P1-04：专业 Trim 操作 —— 手柄余量 / 最近标记 / 数值 trim
# ================================================================

def clip_handle_headroom(
    clip: ClipInstance,
    max_source_frames: int | None = None,
) -> tuple[int, int]:
    """剪辑手柄余量（head, tail，帧）。

    head = 入点之前的素材余量（source_start）；
    tail = 出点之后的素材余量（max_source_frames − source_end）。
    max_source_frames 未知时 tail 按 0 处理（保守：不允许延长）。
    """
    end = int(clip.source_start + clip.source_duration)
    if max_source_frames is None:
        max_frames = end
    else:
        max_frames = max(end, int(max_source_frames))
    head = max(0, int(clip.source_start))
    tail = max(0, max_frames - end)
    return head, tail


def nearest_marker_frame(
    markers: tuple[TimelineMarker, ...], frame: int, direction: int = 1,
) -> int | None:
    """方向 direction 上最近标记的帧号（wrap-around，排除当前帧）。

    与 jump_to_next/prev_marker 的内联逻辑一致，抽为公共 helper 供
    数值 trim / 键盘步进"对齐到标记"复用。无标记返回 None。
    """
    frames = sorted({int(m.frame) for m in markers})
    if not frames:
        return None
    if direction >= 0:
        for candidate in frames:
            if candidate > frame:
                return candidate
        return frames[0]  # wrap
    for candidate in reversed(frames):
        if candidate < frame:
            return candidate
    return frames[-1]  # wrap


def _neighbor_bounds(track: EditTrack, clip: ClipInstance) -> tuple[int, int]:
    """(prev_end, next_start)：同轨前一片段结束帧与后一片段开始帧。

    无前邻居 prev_end=0（时间线起点）；无后邻居 next_start=巨大值。
    按时间线位置判断相邻（允许间隙，间隙即可扩展空间）。
    """
    prev_end = 0
    next_start = 1 << 40
    for other in track.clips:
        if other.clip_id == clip.clip_id:
            continue
        other_end = other.timeline_start + other.timeline_duration
        if other_end <= clip.timeline_start:
            prev_end = max(prev_end, other_end)
        elif other.timeline_start >= clip.timeline_start + clip.timeline_duration:
            next_start = min(next_start, other.timeline_start)
    return prev_end, next_start


def numeric_trim_bounds(
    track: EditTrack,
    clip: ClipInstance,
    trim_side: str,
    max_source_frames: int | None = None,
    ripple: bool = False,
) -> tuple[int, int]:
    """数值 trim 的合法 delta 范围（min_delta, max_delta）。

    语义与 TrimClipCommand 一致：
    - left：delta>0 = 入点右移（缩短），delta<0 = 入点左移（延长）
    - right：delta>0 = 出点右移（延长），delta<0 = 出点左移（缩短）

    约束：
    - 片段至少保留 1 帧；
    - 延长受手柄余量限制（source 范围）；
    - 非 ripple 模式延长还受相邻片段限制；ripple 模式后续片段平移，
      不受邻居限制。
    """
    duration = int(clip.timeline_duration)
    head, tail = clip_handle_headroom(clip, max_source_frames)
    prev_end, next_start = _neighbor_bounds(track, clip)

    if trim_side == "left":
        max_delta = duration - 1  # 至少留 1 帧
        if ripple:
            # 审计修复：入点左延长受前邻物理限制（ripple_trim_clip_left
            # 的前邻重叠守卫），否则 apply 抛未捕获 TimelineOperationError
            min_delta = -min(head, clip.timeline_start - prev_end)
        else:
            min_delta = -min(head, clip.timeline_start - prev_end)
        return min_delta, max_delta
    # right
    min_delta = -(duration - 1)
    if ripple:
        max_delta = tail
    else:
        gap = max(0, next_start - (clip.timeline_start + duration))
        max_delta = min(tail, gap)
    return min_delta, max_delta


def build_numeric_trim_command(
    sequence: TimelineSequence,
    track_id: str,
    clip_id: str,
    trim_side: str,
    delta_frames: int,
    mode: str = "trim",
    max_source_frames: int | None = None,
    apply_sync_lock: bool = True,
) -> tuple[Optional[Any], int]:
    """构建数值 trim 命令（P1-04：数值输入 / 键盘步进共用入口）。

    Args:
        mode: "trim"（普通）| "ripple"（波纹）| "roll"（滚动，需右邻）
        trim_side: "left" | "right"（roll 模式忽略）
        delta_frames: 帧增量（自动钳制到 numeric_trim_bounds /
            get_roll_trim_constraints 的合法范围）
        apply_sync_lock: ripple 模式下是否施加 P1-05 跨轨 sync-lock
            跟随。链接 A/V 宏的联动命令应传 False（主命令已跟随，
            避免其他轨道双重位移）。

    Returns:
        (command, clamped_delta)；delta 钳制为 0 或前提不满足时
        (None, 0)。
    """
    from src.features.edit.core.commands import (
        RippleTrimCommand,
        RollTrimCommand,
        TrimClipCommand,
    )

    track = sequence.get_track_by_id(track_id)
    if track is None:
        return None, 0
    clip = track.get_clip_by_id(clip_id)
    if clip is None:
        return None, 0

    if mode == "roll":
        right = None
        clip_end = clip.timeline_start + clip.timeline_duration
        for other in track.clips:
            if other.clip_id != clip.clip_id and other.timeline_start >= clip_end:
                if right is None or other.timeline_start < right.timeline_start:
                    right = other
        if right is None:
            return None, 0
        # 左侧（被延长的）片段的素材上限决定 cut 右移上限；
        # 左移上限由右片段 source_start 决定（constraints 内部处理）。
        # 素材信息缺失时保守：不许超出已用范围（与 trim 的 tail=0 一致）。
        left_max = (max_source_frames if max_source_frames is not None
                    else clip.source_start + clip.source_duration)
        right_max = right.source_start + right.source_duration
        min_delta, max_delta = get_roll_trim_constraints(
            sequence, clip.clip_id, right.clip_id,
            left_max_source_frames=left_max,
            right_max_source_frames=right_max,
        )
        clamped = max(min_delta, min(max_delta, int(delta_frames)))
        if clamped == 0:
            return None, 0
        return (RollTrimCommand.create(track_id, clip.clip_id, right.clip_id, clamped),
                clamped)

    ripple = mode == "ripple"
    min_delta, max_delta = numeric_trim_bounds(
        track, clip, trim_side, max_source_frames=max_source_frames,
        ripple=ripple,
    )
    clamped = max(min_delta, min(max_delta, int(delta_frames)))
    if clamped == 0:
        return None, 0

    if trim_side == "left":
        new_start = clip.timeline_start + clamped
        new_source_start = clip.source_start + clamped
        if ripple:
            # affected_clips 语义与鼠标提交处一致（后续片段波纹平移）
            affected = tuple(
                c for c in track.clips if c.timeline_start > clip.timeline_start)
            return (RippleTrimCommand.create_left_trim(
                track_id, clip.clip_id, new_start,
                original_clip=clip, affected_clips=affected,
                apply_sync_lock=apply_sync_lock), clamped)
        # 审计修复：old_* 必传——TrimClipCommand.apply 不回填旧值，
        # 缺省时 undo 抛 "old values not stored" 且历史条目丢失
        return (TrimClipCommand.create_left_trim(
            track_id, clip.clip_id, new_start, new_source_start,
            old_start=clip.timeline_start,
            old_source_start=clip.source_start,
            old_duration=clip.timeline_duration,
            old_source_duration=clip.source_duration), clamped)
    new_end = clip.timeline_start + clip.timeline_duration + clamped
    new_source_duration = clip.source_duration + clamped
    if ripple:
        # ripple 右缘工厂接受新结束帧（timeline_end），非时长
        affected = tuple(
            c for c in track.clips if c.timeline_start >= clip.timeline_end)
        return (RippleTrimCommand.create_right_trim(
            track_id, clip.clip_id, new_end,
            original_clip=clip, affected_clips=affected,
            apply_sync_lock=apply_sync_lock), clamped)
    new_duration = clip.timeline_duration + clamped
    return (TrimClipCommand.create_right_trim(
        track_id, clip.clip_id, new_duration, new_source_duration,
        old_duration=clip.timeline_duration,
        old_source_duration=clip.source_duration), clamped)


def build_linked_numeric_trim_command(
    sequence: TimelineSequence,
    track_id: str,
    clip_id: str,
    trim_side: str,
    delta_frames: int,
    mode: str = "trim",
    max_source_frames: int | None = None,
    linked_max_source_frames: int | None = None,
    mismatch: str = "reject",
) -> tuple[Optional[Any], int]:
    """B19：互链 A/V 数值 trim 的**共同边界**计划（域级，UI/service 共用）。

    逐成员独立 ``build_numeric_trim_command`` 会按各自源余量得到不同
    钳制（视频 +5、音频 +3 仍可组宏提交成 105/103 的失步对）。本函数
    先对**全部成员**求合法范围，取共同区间：

    - ``mismatch="reject"``（默认）：请求 delta 超出任一成员边界 →
      整次拒绝 ``(None, 0)``——与 UI 既有提示一致；
    - ``mismatch="clamp"``：钳制到共同区间（全体同一 delta），共同
      区间为空或钳后为 0 → 拒绝。

    成员 = 主片 + 互链伙伴（``get_reciprocal_linked_clip``，悬空/单向
    链接不参与）。sync-lock 只由主命令施加一次（联动命令
    ``apply_sync_lock=False``）；宏 = 单步 undo。

    Returns:
        (MacroCommand | 单命令 | None, common_delta)。
    """
    track = sequence.get_track_by_id(track_id)
    clip = track.get_clip_by_id(clip_id) if track is not None else None
    if clip is None:
        return None, 0

    members: list[tuple[str, str, int | None]] = [
        (track_id, clip_id, max_source_frames)]
    linked = sequence.get_reciprocal_linked_clip(clip)
    if linked is not None:
        members.append(
            (linked.track_id, linked.clip_id, linked_max_source_frames))

    # ① 共同范围预检：每成员独立求界，再取交集
    per_member_bounds: list[tuple[int, int]] = []
    for member_track_id, member_clip_id, member_max in members:
        member_track = sequence.get_track_by_id(member_track_id)
        member_clip = (member_track.get_clip_by_id(member_clip_id)
                       if member_track is not None else None)
        if member_clip is None:
            return None, 0
        ripple = mode == "ripple"
        lo, hi = numeric_trim_bounds(
            member_track, member_clip, trim_side,
            max_source_frames=member_max, ripple=ripple)
        per_member_bounds.append((lo, hi))
    common_lo = max(lo for lo, _hi in per_member_bounds)
    common_hi = min(hi for lo, hi in per_member_bounds)
    if common_lo > common_hi:
        return None, 0
    requested = int(delta_frames)
    if mismatch == "clamp":
        common_delta = max(common_lo, min(common_hi, requested))
    else:
        # reject：请求必须落在每个成员的边界内（不悄悄改变用户输入）
        common_delta = requested
        if not all(lo <= requested <= hi
                   for lo, hi in per_member_bounds):
            return None, 0
    if common_delta == 0:
        return None, 0

    # ③ 全组同一 delta 计划；④ sync-lock 主命令跟随一次
    commands = []
    for index, (member_track_id, member_clip_id, member_max) in enumerate(
            members):
        cmd, clamped = build_numeric_trim_command(
            sequence, member_track_id, member_clip_id, trim_side,
            common_delta, mode=mode, max_source_frames=member_max,
            apply_sync_lock=(index == 0),
        )
        if cmd is None or clamped != common_delta:
            # 共同区间已预检，此处不应触发；防御性整次拒绝
            return None, 0
        commands.append(cmd)
    if len(commands) == 1:
        return commands[0], common_delta
    from src.features.edit.core.commands import MacroCommand

    return (MacroCommand.create(
        f"Trim linked clips {common_delta:+d}f", tuple(commands)),
        common_delta)
