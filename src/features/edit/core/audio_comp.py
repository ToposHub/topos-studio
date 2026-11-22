"""B14 · AudioComp 逐句选 Take 的区域模型与编译计划（主计划 §20/B14）。

- ``CompRegion``：半开样本区间 + take 引用 + 源内绝对样本起点；同一 comp 活动
  区有序、不重叠（未覆盖区域明确静音或指定底层 lane，不隐式拼别的 take）。
- ``replace_region``：替换区段先切原区域两端，保留左右残片（手册算法
  oracle；源偏移随残片同步推进）。
- ``validate_comp``：有序/不重叠/take 存在性检查（缺 take 的区段显式
  offline，不偷偷换音）。
- ``compile_comp_plan``：编译为 clip/transition 计划（落到既有模型语义，
  不另建音频求和器）——每区段输出 (take, source_start, start, end,
  fade_in/out)；保存显式区段淡化，不虚构相邻源的重叠 handles。
  真正的跨take重叠淡化需在源handles合同下扩展，当前不自动生成。

坐标：``start/end`` 为 comp 时基绝对样本（与时间线同样本率；跨率先经
sample_time 有理映射——B12/B08 合同），``source_start`` 为 take 源内样本。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

AUDIO_COMP_VERSION = 1


@dataclass(frozen=True)
class CompRegion:
    """comp 活动区（半开 [start, end)；take 引用 + 源内起点）。"""

    start: int
    end: int
    take_id: str
    source_start: int
    fade_in: int = 0
    fade_out: int = 0

    def __post_init__(self) -> None:
        for name in ("start", "end", "source_start", "fade_in", "fade_out"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer sample position")
        if self.end <= self.start:
            raise ValueError("empty comp region")
        if self.start < 0 or self.source_start < 0:
            raise ValueError("negative comp or source position")
        if self.fade_in < 0 or self.fade_out < 0:
            raise ValueError("negative comp fade")
        if not self.take_id:
            raise ValueError("empty comp take identity")


@dataclass(frozen=True)
class AudioComp:
    """一条轨的逐句 comp（底层 lane 只引用已有 AuditionTake/asset）。"""

    comp_id: str
    track_id: str
    regions: Tuple[CompRegion, ...] = ()
    version: int = AUDIO_COMP_VERSION

    def __post_init__(self) -> None:
        if self.version != AUDIO_COMP_VERSION:
            raise ValueError(f"unsupported audio comp version: {self.version}")
        if not self.comp_id or not self.track_id:
            raise ValueError("comp and track identities are required")

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "comp_id": self.comp_id,
                "track_id": self.track_id,
                "regions": [dict(start=r.start, end=r.end, take_id=r.take_id,
                                 source_start=r.source_start, fade_in=r.fade_in,
                                 fade_out=r.fade_out) for r in self.regions]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AudioComp":
        return cls(comp_id=data["comp_id"], track_id=data["track_id"],
                   version=data.get("version", AUDIO_COMP_VERSION),
                   regions=tuple(CompRegion(**r) for r in data.get("regions", ())))

    def with_regions(self, regions: Tuple[CompRegion, ...]) -> "AudioComp":
        return replace(self, regions=tuple(regions))


def validate_comp_regions(regions: Tuple[CompRegion, ...]) -> List[str]:
    """有序 + 不重叠 + 数值合法检查；返回错误列表（空=通过）。"""
    errors: List[str] = []
    prev: Optional[CompRegion] = None
    for i, region in enumerate(regions):
        if region.end <= region.start:
            errors.append(f"region {i}: empty range [{region.start}, {region.end})")
            continue
        if prev is not None and region.start < prev.end:
            errors.append(
                f"region {i} [{region.start}, {region.end}) overlaps previous "
                f"ending at {prev.end}")
        prev = region
    return errors


def replace_region(regions: Tuple[CompRegion, ...],
                   new: CompRegion) -> Tuple[CompRegion, ...]:
    """替换活动区段，保留左右残片（源偏移随残片同步推进）。

    手册算法（§20/B14）：被新区段覆盖的旧区段切两端；左残片保留原
    source_start，右残片 source 前移 ``new.end - old.start``；结果按 start
    排序。空区段拒绝。
    """
    errors = validate_comp_regions(regions)
    if errors:
        raise ValueError("; ".join(errors))
    if new.end <= new.start:
        raise ValueError("empty region")
    result: List[CompRegion] = []
    for old in regions:
        if old.end <= new.start or old.start >= new.end:
            result.append(old)
            continue
        if old.start < new.start:
            result.append(replace(old, end=new.start, fade_out=0,
                                  fade_in=min(old.fade_in, new.start - old.start)))
        if old.end > new.end:
            result.append(replace(old, start=new.end, fade_in=0,
                                  fade_out=min(old.fade_out, old.end - new.end),
                                  source_start=old.source_start
                                  + new.end - old.start))
    return tuple(sorted([*result, new], key=lambda r: r.start))


def compile_comp_plan(
    comp: AudioComp,
    take_lengths: Mapping[str, int],
    *,
    max_crossfade: int = 64,
) -> Tuple[Dict[str, Tuple[CompRegion, ...]], List[str]]:
    """编译 comp → 每区段渲染计划 + 诊断。

    返回 ``(plan_by_take, warnings)``：plan 按 take 分组（同一 take 的
    各区段可合并渲染），每区段带源内窗与边界 crossfade——crossfade 取
    ``max_crossfade`` 与两侧区段长/源可用长度的最小值；源窗越界（源短
    于需求）的区段标 offline 诊断，不静默换 take（§20/B14 缺 take 语义）。
    """
    if isinstance(max_crossfade, bool) or not isinstance(max_crossfade, int) or max_crossfade < 0:
        raise ValueError("max_crossfade must be a nonnegative integer")
    warnings: List[str] = []
    errors = validate_comp_regions(comp.regions)
    if errors:
        raise ValueError("; ".join(errors))
    plan: Dict[str, List[CompRegion]] = {}
    ordered = sorted(comp.regions, key=lambda r: r.start)
    for i, region in enumerate(ordered):
        length = int(take_lengths.get(region.take_id, -1))
        if length < 0:
            warnings.append(
                f"region [{region.start}, {region.end}) take "
                f"{region.take_id!r} missing — region renders offline")
            continue
        available = length - region.source_start
        if available < region.end - region.start:
            warnings.append(
                f"region [{region.start}, {region.end}) exceeds take "
                f"{region.take_id!r} remaining samples ({available}) — "
                "region renders offline")
            continue
        # crossfade：区段切换边界的短交叉淡化（comp 的核心语义）——受
        # 本区段长度与 max_crossfade 限制；相邻区段在边界处各自半交叠。
        fade_in = max(0, min(int(region.fade_in), int(max_crossfade),
                             region.end - region.start))
        fade_out = max(0, min(int(region.fade_out), int(max_crossfade),
                              region.end - region.start))
        plan.setdefault(region.take_id, []).append(
            CompRegion(start=region.start, end=region.end,
                       take_id=region.take_id,
                       source_start=region.source_start,
                       fade_in=fade_in, fade_out=fade_out))
    return ({take: tuple(regions) for take, regions in plan.items()}, warnings)


def flatten_comp_clips(
    comp: AudioComp,
    takes: Mapping[str, ClipInstance],
    take_lengths: Mapping[str, int],
    fps: Fraction,
) -> Tuple[ClipInstance, ...]:
    """Compile the representable v1 subset to ordinary production clips.

    All sample coordinates are at the existing model's 48 kHz reference rate.
    ``source_start`` addresses the underlying asset, not an offset relative to
    the supplied take clip's existing source window. The model has no sample-domain end field: a region whose end is not a frame
    boundary is rejected rather than lengthened. Edited/retimed/animated take
    windows require an explicit bake before flattening. This routine does not
    invent a separate renderer or silently omit an offline region.
    """
    fps = Fraction(fps)
    if fps <= 0:
        raise ValueError("fps must be positive")
    plan, warnings = compile_comp_plan(comp, take_lengths)
    if warnings:
        raise ValueError("cannot flatten offline comp: " + "; ".join(warnings))
    regions = sorted((r for group in plan.values() for r in group),
                     key=lambda r: r.start)
    output: List[ClipInstance] = []
    for index, region in enumerate(regions):
        take = takes.get(region.take_id)
        if take is None or not take.asset_id:
            raise ValueError(f"missing source clip for take {region.take_id}")
        speed = take.speed
        constant_speed = float(getattr(speed, "speed", getattr(speed, "value", speed)))
        if (constant_speed != 1.0 or take.audio_edit or take.animated_params
                or take.is_reverse_playback() or getattr(speed, "speed_points", ())):
            raise ValueError(f"take {region.take_id} requires baking before flatten")
        # floor inversion is corrected against the absolute frame boundary;
        # this remains exact for 30000/1001 and negative residuals never occur.
        def frame_and_offset(position: int) -> Tuple[int, int]:
            frame = int(Fraction(position) * fps / 48000)
            while int(Fraction(frame + 1) * 48000 / fps) <= position:
                frame += 1
            return frame, position - int(Fraction(frame) * 48000 / fps)

        start_frame, start_offset = frame_and_offset(region.start)
        end_frame, end_offset = frame_and_offset(region.end)
        if end_offset:
            raise ValueError(f"comp region {index} end is not representable by frame-domain duration")
        if end_frame <= start_frame:
            raise ValueError(f"comp region {index} has no representable frame duration")
        source_frame, source_offset = frame_and_offset(region.source_start)
        # Decode enough source frames for the exact audible region; the common
        # timeline gate trims the tail at the absolute end frame.
        source_end = region.source_start + region.end - region.start
        source_end_frame, source_end_offset = frame_and_offset(source_end)
        source_duration = source_end_frame + bool(source_end_offset) - source_frame
        output.append(replace(
            take, clip_id=f"comp-{comp.comp_id}-{index}", track_id=comp.track_id,
            timeline_start=start_frame, timeline_duration=end_frame - start_frame,
            audio_sample_offset=start_offset, source_start=source_frame,
            source_sample_offset=source_offset, source_duration=source_duration,
            fade_in_samples=region.fade_in, fade_out_samples=region.fade_out,
            fade_in_duration=0, fade_out_duration=0, linked_clip_id=None,
            render_cache_id=None,
        ))
    return tuple(output)


@dataclass(frozen=True)
class FlattenAudioCompCommand:
    """Replace one destination track by normal clips, in one undoable commit."""

    command_id: str
    label: str
    comp: AudioComp
    takes: Tuple[Tuple[str, ClipInstance], ...]
    take_lengths: Tuple[Tuple[str, int], ...]
    expected_revision: Optional[int] = None
    _old_track: Optional[EditTrack] = field(default=None, repr=False)

    @classmethod
    def create(cls, comp: AudioComp, takes: Mapping[str, ClipInstance],
               take_lengths: Mapping[str, int], *,
               expected_revision: Optional[int] = None) -> "FlattenAudioCompCommand":
        return cls(str(uuid.uuid4()), "Flatten audio comp", comp,
                   tuple(takes.items()), tuple(take_lengths.items()), expected_revision)

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "FlattenAudioCompCommand"]:
        if self._old_track is None and self.expected_revision is not None and timeline.revision != self.expected_revision:
            raise ValueError("comp proposal revision changed; recompute required")
        track = timeline.get_track_by_id(self.comp.track_id)
        if track is None or track.track_type != "audio" or track.is_bus:
            raise ValueError("comp destination must be an existing audio track")
        if track.is_locked:
            raise ValueError("comp destination track is locked")
        raw_fps = timeline.fps
        fps = (Fraction(raw_fps.numerator, raw_fps.denominator)
               if hasattr(raw_fps, "numerator") else Fraction(str(raw_fps)))
        clips = flatten_comp_clips(self.comp, dict(self.takes), dict(self.take_lengths), fps)
        if track.clips == clips:
            return timeline, self
        return (timeline.with_track_updated(track._rebuild(clips=clips, transitions=())),
                replace(self, _old_track=track))

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_track is None:
            return timeline
        if timeline.get_track_by_id(self.comp.track_id) is None:
            raise ValueError("comp destination track no longer exists")
        return timeline.with_track_updated(self._old_track)

    def to_dict(self) -> Dict[str, Any]:
        return dict(command_id=self.command_id, command_type="FlattenAudioCompCommand",
                    label=self.label, comp=self.comp.to_dict(),
                    takes={key: clip.to_dict() for key, clip in self.takes},
                    take_lengths=dict(self.take_lengths), expected_revision=self.expected_revision,
                    old_track=self._old_track.to_dict() if self._old_track else None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FlattenAudioCompCommand":
        return cls(command_id=data["command_id"], label=data["label"],
                   comp=AudioComp.from_dict(data["comp"]),
                   takes=tuple((key, ClipInstance.from_dict(value)) for key, value in data["takes"].items()),
                   take_lengths=tuple(data["take_lengths"].items()),
                   expected_revision=data.get("expected_revision"),
                   _old_track=EditTrack.from_dict(data["old_track"]) if data.get("old_track") else None)


__all__ = [
    "AUDIO_COMP_VERSION",
    "AudioComp",
    "FlattenAudioCompCommand",
    "flatten_comp_clips",
    "CompRegion",
    "compile_comp_plan",
    "replace_region",
    "validate_comp_regions",
]
