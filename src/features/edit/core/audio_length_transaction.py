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
) -> LengthChangePlan:
    """构造改长度事务（冲突可见；blocked 非空时 after_sequence 原样返回）。"""
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

    new_output_samples = output_length_after_steps(
        new_audio_edit, int(old_output_samples))
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
    length_steps = [
        step for step in (new_audio_edit or {}).get("steps", []) or []
        if isinstance(step, dict)
        and step.get("processor_id") in LENGTH_CHANGING_PROCESSORS
    ]
    remapped_stack, remap_warnings = remap_audio_edit_for_length_steps(
        new_audio_edit, length_steps)
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
    new_track = replace(track, clips=tuple(new_clips))
    after = sequence.with_track_updated(new_track)
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
