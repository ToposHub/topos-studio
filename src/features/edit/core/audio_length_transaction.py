"""Length-changing audio edit commits: conflict checks + project transaction.

§3.4 / §6.2 of the clip editor roadmap: a step that changes the clip's output
length (speed/pitch, interior delete, trim-to-selection) must, in one
history entry, update the clip's sequence extent, offer gap-vs-ripple
semantics for the following clips, and surface conflicts (linked video,
audio transitions, neighbours that an extension would overwrite, locked
tracks, sequence bounds) instead of silently reshuffling the timeline.

The planner is pure: it takes the current sequence plus the intended new
``audio_edit`` / new output length and produces a
``TimelineProjectTransactionCommand`` payload (before/after sequences built
with the same immutable-model helpers the rest of the timeline uses), along
with the diagnostics the UI must show before executing.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from src.features.edit.core.audio_edit import (
    LENGTH_CHANGING_PROCESSORS,
    output_length_after_steps,
    remap_audio_edit_for_length_steps,
)
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE


@dataclass(frozen=True)
class LengthChangePlan:
    """一次改长度提交的全部结果（纯数据；blocked 非空时不允许执行）。"""

    before_sequence: Any
    after_sequence: Any
    label: str
    new_output_samples: int
    new_duration_frames: int
    delta_frames: int
    ripple: bool
    warnings: tuple = ()
    blocked: tuple = ()
    # 重定位 inline 步骤/包络/标记时产生的冲突说明（并入 warnings 展示）。
    remap_warnings: tuple = ()


def _frames_for_samples(samples: int, fps: float) -> int:
    return max(1, int(round(samples * float(fps) / AUDIO_REFERENCE_SAMPLE_RATE)))


def plan_clip_length_change(
    sequence,
    track_id: str,
    clip_id: str,
    *,
    new_audio_edit,
    old_output_samples: int,
    ripple: bool,
    fps: float = 24.0,
    label: str = "Change clip length",
    allow_independent_audio: bool = False,
    removed_length_steps: tuple = (),
    base_output_samples: "int | None" = None,
) -> LengthChangePlan:
    """构造改长度事务（冲突可见；blocked 非空时 after_sequence 原样返回）。

    P1-3 · 反向编辑：``removed_length_steps`` 携带被旁路/删除/替换/清空
    的改长度步骤——新 ``new_audio_edit`` 的 inline/包络/标记坐标仍在旧
    最终输出域，先经逆映射折回新域（不可映射项移除并点名）。
    Ripple 语义扩展：后续自动化关键帧、序列标记与 Sync Lock 轨的下游
    片段一并随 delta 平移（P1-3：改时长事务统一重算）。
    """
    blocked: list[str] = []
    warnings: list[str] = []

    track = sequence.get_track_by_id(track_id)
    if track is None:
        return _blocked_plan(sequence, f"track {track_id} not found", label)
    if getattr(track, "is_locked", False):
        return _blocked_plan(sequence, "track is locked", label)
    clip = track.get_clip_by_id(clip_id)
    if clip is None:
        return _blocked_plan(sequence, f"clip {clip_id} not found", label)

    # 链接 A/V：默认保持视频同步（§6.2）——改音频长度会破坏链接对齐，
    # v1 要求显式选择独立音频编辑。
    linked_clip_id = getattr(clip, "linked_clip_id", None)
    if linked_clip_id and not allow_independent_audio:
        blocked.append(
            f"clip is linked to video clip {linked_clip_id[:8]} — choose "
            "independent audio editing or move the linked content together")

    # 音频转场锚定两侧片段边界；改长度会移动切点（v1 阻断并列出）。
    touching = [
        tr for tr in (getattr(track, "transitions", None) or ())
        if getattr(tr, "left_clip_id", None) == clip_id
        or getattr(tr, "right_clip_id", None) == clip_id
    ]
    if touching:
        blocked.append(
            f"{len(touching)} audio transition(s) anchor this clip's edge — "
            "remove or retime them first")

    # P1-3 · 反向编辑：长度步进以**原始输出长度**为基底（raw 域）——
    # 旁路/移除改长度步骤时旧输出≠基底；缺省沿用旧行为（追加场景旧
    # 输出即基底）。
    walk_base = int(base_output_samples
                    if base_output_samples is not None else old_output_samples)
    new_output_samples = output_length_after_steps(new_audio_edit, walk_base)
    new_frames = _frames_for_samples(new_output_samples, fps)
    old_frames = int(getattr(clip, "timeline_duration", 0) or 0)
    delta = new_frames - old_frames
    clip_start = int(getattr(clip, "timeline_start", 0) or 0)
    clip_end = clip_start + old_frames
    new_end = clip_start + new_frames

    # 邻接冲突：留空模式向下变长会覆盖后邻（ripple 模式整体后移则安全）。
    following = sorted(
        (other for other in track.clips
         if other.clip_id != clip_id
         and int(other.timeline_start or 0) >= clip_start),
        key=lambda c: int(c.timeline_start or 0))
    if delta > 0 and not ripple and following:
        first_gap = int(following[0].timeline_start or 0) - clip_end
        if delta > first_gap:
            blocked.append(
                f"extending by {delta} frames overwrites the next clip "
                f"(only {max(0, first_gap)} frames of gap) — use ripple")

    # 序列边界：尾邻/序列时长不足时给出说明（不阻断；序列长度是软约束）。
    sequence_end = int(getattr(sequence, "duration_frames", 0) or 0)
    if new_end > sequence_end:
        warnings.append(
            f"clip now ends {new_end - sequence_end} frames past the "
            "sequence end (sequence grows)")

    # inline 步骤/包络/标记重定位到新输出域（塌缩步骤被移除并点名）。
    # P1-3 · 反向编辑：先逆映射被移除的改长度步骤（坐标从旧最终域折回），
    # 再正向重映射留下的改长度步骤。
    working_edit = new_audio_edit
    remap_warnings: list[str] = []
    if removed_length_steps:
        from src.features.edit.core.audio_edit import (
            unmap_audio_edit_for_removed_length_steps,
        )
        working_edit, unmap_warnings = unmap_audio_edit_for_removed_length_steps(
            working_edit, list(removed_length_steps))
        remap_warnings.extend(unmap_warnings)
    length_steps = [
        step for step in (working_edit or {}).get("steps", []) or []
        if isinstance(step, dict) and step.get("enabled", True)
        and step.get("processor_id") in LENGTH_CHANGING_PROCESSORS
    ]
    remapped_stack, forward_warnings = remap_audio_edit_for_length_steps(
        working_edit, length_steps)
    remap_warnings.extend(forward_warnings)
    new_clip = replace(clip, audio_edit=remapped_stack)
    # 淡化钳制到新时长（命令构造期的保守处理；写入 warning 供状态行展示）。
    new_fade_in = min(int(new_clip.fade_in_duration or 0), new_frames)
    new_fade_out = min(int(new_clip.fade_out_duration or 0),
                       max(0, new_frames - new_fade_in))
    if (new_fade_in != int(new_clip.fade_in_duration or 0)
            or new_fade_out != int(new_clip.fade_out_duration or 0)):
        warnings.append("fades clamped to the new clip length")
    new_clip = replace(
        new_clip, timeline_duration=new_frames,
        fade_in_duration=new_fade_in, fade_out_duration=new_fade_out)

    if blocked:
        return LengthChangePlan(
            before_sequence=sequence, after_sequence=sequence, label=label,
            new_output_samples=new_output_samples, new_duration_frames=new_frames,
            delta_frames=delta, ripple=bool(ripple),
            warnings=tuple(warnings), blocked=tuple(blocked),
            remap_warnings=tuple(remap_warnings))

    new_clips = [new_clip]
    for other in track.clips:
        if other.clip_id == clip_id:
            continue
        other_start = int(other.timeline_start or 0)
        if ripple and delta != 0 and other_start >= clip_end:
            other = replace(other, timeline_start=other_start + delta)
        new_clips.append(other)
    new_clips.sort(key=lambda c: int(c.timeline_start or 0))
    # P1-3 · ripple 重算后续自动化：clip_end 之后的关键帧随 delta 平移
    # （轨自动化按绝对帧号锚定，片段后移不平移会错位）。
    if ripple and delta != 0 and getattr(track, "automation", None):
        shifted_automation = {}
        for param, param_track in track.automation.items():
            keyframes = getattr(param_track, "keyframes", None)
            if not keyframes:
                shifted_automation[param] = param_track
                continue
            from dataclasses import replace as _kf_replace
            moved = [
                _kf_replace(key, frame=int(key.frame) + delta)
                if int(key.frame) >= clip_end else key
                for key in keyframes
            ]
            moved.sort(key=lambda key: int(key.frame))
            shifted_automation[param] = _kf_replace(
                param_track, keyframes=moved)
        track = replace(track, automation=shifted_automation)
    new_track = replace(track, clips=tuple(new_clips))
    after = sequence.with_track_updated(new_track)
    # P1-3 · ripple 重算序列标记（时间线域标记随内容平移；片段域标记
    # 跟随其所属片段，不在此平移）。
    if ripple and delta != 0 and getattr(sequence, "markers", None):
        from dataclasses import replace as _mk_replace
        shifted_markers = tuple(
            _mk_replace(marker, frame=int(marker.frame) + delta)
            if int(marker.frame) >= clip_end and marker.clip_id is None
            else marker
            for marker in sequence.markers)
        after = replace(after, markers=shifted_markers)
    # P1-3 · Sync Lock：开启了同步锁的其他轨，clip_end 之后的下游片段
    # 一并随 ripple 平移（保持跨轨对齐；未开锁的轨不动 = gap 语义）。
    if ripple and delta != 0:
        for other_track in after.all_tracks:
            if other_track.track_id == track_id:
                continue
            if not getattr(other_track, "is_sync_locked", False):
                continue
            if any(
                int(clip.timeline_start or 0) >= clip_end
                for clip in other_track.clips
            ):
                shifted = tuple(
                    replace(clip, timeline_start=int(clip.timeline_start or 0) + delta)
                    if int(clip.timeline_start or 0) >= clip_end else clip
                    for clip in other_track.clips)
                after = after.with_track_updated(
                    replace(other_track, clips=shifted))
    return LengthChangePlan(
        before_sequence=sequence, after_sequence=after, label=label,
        new_output_samples=new_output_samples, new_duration_frames=new_frames,
        delta_frames=delta, ripple=bool(ripple),
        warnings=tuple(warnings), blocked=(),
        remap_warnings=tuple(remap_warnings))


def _blocked_plan(sequence, reason: str, label: str) -> LengthChangePlan:
    return LengthChangePlan(
        before_sequence=sequence, after_sequence=sequence, label=label,
        new_output_samples=0, new_duration_frames=0, delta_frames=0,
        ripple=False, blocked=(reason,))
