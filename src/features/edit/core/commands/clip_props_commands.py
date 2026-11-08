"""Clip property commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: clip_props cluster — single-clip
property edits (Motion Graphic retime / source re-point / multicam angle
switch / fade / label color / waveform display / timeline range / enable
toggle / speed / freeze frame / blend mode / placeholder-asset replacement
/ audio gain / sample-level fields). The cluster-private source-fps
validator (_validate_source_curve_fps) moved with the cluster (its only
two callers are here); the track-lock guard assert_track_editable lives
in _support.py, and SwitchMulticamAngleCommand splits via
SplitClipCommand imported from clip_commands. This module must never
import the infra stacks (MacroCommand / CommandStack) — the dependency
direction is _support -> clip_commands -> clip_props, with the package
__init__ re-exporting for deep imports. Class sources are byte-identical
to the original module; to_dict/from_dict keys are a persistence
contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
import copy
import copy as _copy
import logging
import uuid

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TimelineMarker,
)

from src.features.edit.core.clip_params import SpeedParams

from ._support import assert_track_editable
from .clip_commands import SplitClipCommand


def _tuple_or_none(value: Any) -> Any:
    """JSON 往返辅助：嵌套 tuple → list（None 透传），读取端再转回。"""
    if value is None:
        return None
    if isinstance(value, tuple):
        return [_tuple_or_none(v) for v in value]
    return value


def _nested_tuple_or_none(value: Any) -> Any:
    """``_tuple_or_none`` 的逆：JSON list → 嵌套 tuple（None 透传）。"""
    if value is None:
        return None
    if isinstance(value, list):
        return tuple(_nested_tuple_or_none(v) for v in value)
    return value

logger = logging.getLogger(__name__)


# ================================================================
# RetimeMotionGraphicClipCommand
# ================================================================

@dataclass(frozen=True)
class RetimeMotionGraphicClipCommand:
    """Change only a Motion Graphic clip's timeline duration.

    A Motion Graphic is a scene reference rather than a finite source file.
    Its authored scene duration stays intact while the timeline duration is
    remapped proportionally at render time.  Keeping this as a separate
    command prevents the regular right-trim command from increasing the
    scene's ``source_duration`` and accidentally freezing the final frame.
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    new_duration: int
    old_duration: int

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        from src.features.teaching_animation.host_adapters import is_motion_graphic_asset_id

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        if not is_motion_graphic_asset_id(clip.asset_id):
            raise ValueError("RetimeMotionGraphicClipCommand requires a Motion Graphic clip")
        if self.new_duration <= 0:
            raise ValueError("Motion Graphic duration must be positive")

        updated = replace(clip, timeline_duration=int(self.new_duration))
        for existing in track.clips:
            if existing.clip_id == clip.clip_id:
                continue
            if (updated.timeline_start < existing.timeline_end
                    and updated.timeline_end > existing.timeline_start):
                raise ValueError(f"Retimed clip overlaps with {existing.clip_id}")
        return timeline.with_track_updated(track.with_clip_updated(updated))

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        restored = replace(clip, timeline_duration=int(self.old_duration))
        return timeline.with_track_updated(track.with_clip_updated(restored))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "RetimeMotionGraphicClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_duration": self.new_duration,
            "old_duration": self.old_duration,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RetimeMotionGraphicClipCommand":
        """Deserialize command."""
        return RetimeMotionGraphicClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Retiming Motion Graphic"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_duration=data.get("new_duration", 0),
            old_duration=data.get("old_duration", 0),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        new_duration: int,
        old_duration: int,
    ) -> "RetimeMotionGraphicClipCommand":
        return RetimeMotionGraphicClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Retiming Motion Graphic to {int(new_duration)} frames",
            track_id=track_id,
            clip_id=clip_id,
            new_duration=int(new_duration),
            old_duration=int(old_duration),
        )


# ================================================================
# UpdateClipSourceCommand (multicam / re-point a clip at a different asset)
# ================================================================


def _validate_source_curve_fps(
    clip: ClipInstance, source_fps_num: int | None, source_fps_den: int | None,
) -> None:
    """Reject a curve-domain conversion that the integer schema cannot retain."""
    from fractions import Fraction

    if (source_fps_num is None or not isinstance(clip.speed, SpeedParams)
            or not clip.speed.speed_points):
        return
    target_fps = Fraction(source_fps_num, source_fps_den or 1)
    active_fps = (Fraction(clip._source_fps_num, clip._source_fps_den or 1)
                  if clip._source_fps_num else None)
    if active_fps != target_fps:
        raise ValueError(
            "Cannot switch a speed-curve clip to a different source frame rate: "
            "the curve's integer source-frame domain cannot be converted losslessly. "
            "Use an angle with the same source frame rate or remove the speed curve.")


@dataclass(frozen=True)
class UpdateClipSourceCommand:
    """Re-point a clip at a different source asset (and optionally source offset).

    Used by multicam angle switching and any "swap the underlying media while
    keeping the timeline position/duration" workflow. The clip's timeline
    placement is preserved; only ``asset_id`` and ``source_start`` change.
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    new_asset_id: str
    new_source_start: int | None = None
    # F-R04/R23：新角度源 fps 元数据（调用方知素材；None = 沿用旧值）
    _new_source_fps_num: int | None = None
    _new_source_fps_den: int | None = None  # None → keep current source_start
    _original_asset_id: str | None = None
    _original_source_start: int | None = None
    # N19/RF03：完整原片段快照——undo 精确还原 fps/源窗/时长/PTS。此
    # 前只还原 asset/source_start 且用"当前"（新流的）source_duration，
    # A24fps 换 B48fps 后 Undo 变成 [A, 24, 144, 48fps, 时长144]。
    _original_clip: ClipInstance | None = None

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        _validate_source_curve_fps(clip, self._new_source_fps_num, self._new_source_fps_den)
        original_clip = copy.deepcopy(clip)

        new_source_start = (
            self.new_source_start if self.new_source_start is not None else clip.source_start
        )
        # Swap asset + source_start, preserving timeline placement and duration.
        # P1-01 审计修复：``with_source_range`` 会按 source_duration 重算
        # timeline_duration——裁剪过的片段（source_duration > 时间线窗口）
        # 换源后时长会被改写成整个源长度。多机位切换（以及任何换源操作）
        # 必须保住时间线窗口。
        # F-R04/R23：源窗口 = timeline_duration × 每时间线帧消耗的源帧数
        # （速度 × 新源 fps/时间线 fps 比）——旧公式漏 fps 比例，24fps
        # A 切 48fps B 时 duration72 应 144。比例按**新**角度 fps 计算；
        # with_source_range 会按旧元数据重算 timeline_duration，换装后
        # 显式保持时间线窗口。
        from fractions import Fraction as _Fr

        factor = clip.source_frames_per_timeline_frame()
        if self._new_source_fps_num is not None:
            factor = _Fr(abs(clip.effective_speed())).limit_denominator(100000)
            new_src = _Fr(self._new_source_fps_num, self._new_source_fps_den or 1)
            if clip._timeline_fps_num:
                tl_fps = _Fr(
                    clip._timeline_fps_num, clip._timeline_fps_den or 1
                )
                factor *= new_src / tl_fps
        preserved_source_duration = max(
            1, int(round(clip.timeline_duration * float(factor)))
        )
        if isinstance(clip.speed, SpeedParams) and clip.speed.speed_points:
            # A curve's source window is its authored time domain. Replacing
            # it with duration × base speed changes smooth ramp integration,
            # even when swapping to an equally rated camera.
            preserved_source_duration = clip.source_duration
        swapped = clip.with_asset_id(self.new_asset_id).with_source_range(
            new_start=new_source_start, new_duration=preserved_source_duration
        )
        # N19/RF03：PTS 索引属于旧流（A24 的 cadence 不能给 48fps B 求
        # 帧索引）——换源即清空，回落 CFR 数学按新 fps 采样。
        swapped = replace(
            swapped,
            timeline_duration=clip.timeline_duration,
            _source_pts_index=None,
            _source_frame_phase_num=0,
            _source_frame_phase_den=1,
            _source_fps_num=(
                self._new_source_fps_num
                if self._new_source_fps_num is not None
                else swapped._source_fps_num
            ),
            _source_fps_den=(
                self._new_source_fps_den or 1
                if self._new_source_fps_num is not None
                else swapped._source_fps_den
            ),
        )
        new_track = track.with_clip_updated(swapped)
        new_timeline = timeline.with_track_updated(new_track)

        updated_cmd = UpdateClipSourceCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            new_asset_id=self.new_asset_id,
            new_source_start=self.new_source_start,
            # N19/RF03：updated 命令必须保留换装 fps（此前丢失使
            # CommandStack 持有的 redo 分支按旧 fps 换装）
            _new_source_fps_num=self._new_source_fps_num,
            _new_source_fps_den=self._new_source_fps_den,
            _original_asset_id=clip.asset_id,
            _original_source_start=clip.source_start,
            _original_clip=original_clip,
        )
        logger.debug(
            f"UpdateClipSourceCommand applied: clip {self.clip_id} asset "
            f"{clip.asset_id}→{self.new_asset_id}"
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._original_clip is not None:
            # N19/RF03：完整快照一次还原（fps/源窗/时长/PTS 全 payload）
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")
            new_track = track.with_clip_updated(self._original_clip)
            return timeline.with_track_updated(new_track)
        if self._original_asset_id is None:
            logger.warning("UpdateClipSourceCommand undo: original not stored")
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        orig_start = (
            self._original_source_start
            if self._original_source_start is not None
            else clip.source_start
        )
        restored = clip.with_asset_id(self._original_asset_id).with_source_range(
            new_start=orig_start, new_duration=clip.source_duration
        )
        new_track = track.with_clip_updated(restored)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateClipSourceCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_asset_id": self.new_asset_id,
            "new_source_start": self.new_source_start,
            "new_source_fps_num": self._new_source_fps_num,
            "new_source_fps_den": self._new_source_fps_den,
            "original_asset_id": self._original_asset_id,
            "original_source_start": self._original_source_start,
            # N19/RF03：完整快照随载荷往返
            "original_clip": (
                self._original_clip.to_dict()
                if self._original_clip else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateClipSourceCommand:
        original_clip_data = data.get("original_clip")
        return UpdateClipSourceCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Clip Source"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_asset_id=data.get("new_asset_id", ""),
            new_source_start=data.get("new_source_start"),
            _new_source_fps_num=data.get("new_source_fps_num"),
            _new_source_fps_den=data.get("new_source_fps_den"),
            _original_asset_id=data.get("original_asset_id"),
            _original_source_start=data.get("original_source_start"),
            _original_clip=(
                ClipInstance.from_dict(original_clip_data)
                if original_clip_data else None
            ),
        )

    @staticmethod
    def create(
        track_id: str, clip_id: str, new_asset_id: str, new_source_start: int | None = None
    ) -> UpdateClipSourceCommand:
        return UpdateClipSourceCommand(
            command_id=str(uuid.uuid4()),
            label=f"Switch source: {new_asset_id[:8]}",
            track_id=track_id,
            clip_id=clip_id,
            new_asset_id=new_asset_id,
            new_source_start=new_source_start,
        )


# ================================================================
# SwitchMulticamAngleCommand (split + re-point to a camera angle)
# ================================================================


@dataclass(frozen=True)
class SwitchMulticamAngleCommand:
    """Switch a multicam clip to a different angle at the playhead frame.

    Splits the target clip at ``switch_frame`` (if not already on a boundary),
    then re-points the right-hand segment to the chosen angle's asset with the
    angle's sync offset applied. This records a hard cut between angles.

    If ``switch_frame`` coincides exactly with the clip's start, no split is
    performed — the whole clip is re-pointed (an angle switch with no preceding
    segment).
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    switch_frame: int
    angle_asset_id: str
    angle_sync_offset: int = 0
    # FE-B06：切换前正在播放的角度的同步偏移。当前源窗已含该偏移，
    # 必须先映射源帧再减去它得到组公共时间，最后叠目标角度偏移——
    # 否则 A→B→A 每次切换都把偏移再累计一次（审计实测 frame20 回 A
    # 实际源 30，期望 20）。
    active_sync_offset: int = 0
    # F-R04/R23：目标角度源 fps 元数据（换装到切换后的片段）
    _angle_fps_num: int | None = None
    _angle_fps_den: int | None = None
    # Internal: ids of segments produced/modified, plus originals for undo.
    _right_clip_id: str | None = None  # the clip that was re-pointed (post-split)
    _did_split: bool = False
    _original_asset_id: str | None = None  # for the no-split undo path
    _original_source_start: int | None = None
    # AUD-16208: split 前的原始片段快照。undo 用它精确还原（移除两段、
    # 放回原片段），不再依赖事后重建 SplitClipCommand 的邻接查找。
    _original_clip: ClipInstance | None = None
    _before_track: EditTrack | None = None
    _before_markers: tuple | None = None

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        # Preflight before splitting or assigning any command snapshots.
        _validate_source_curve_fps(clip, self._angle_fps_num, self._angle_fps_den)

        # Validate switch frame is within the clip.
        if not (clip.timeline_start <= self.switch_frame < clip.timeline_end):
            raise ValueError(
                f"Switch frame {self.switch_frame} outside clip "
                f"[{clip.timeline_start}, {clip.timeline_end})"
            )

        did_split = False
        current = timeline
        target_clip_id = self.clip_id

        # Split if the switch is strictly inside the clip (not at its start).
        if self.switch_frame > clip.timeline_start:
            split_cmd = SplitClipCommand.create(
                self.track_id, self.clip_id, self.switch_frame,
                right_clip_id=self._right_clip_id,
            )
            current = split_cmd.apply(current)
            # After split, the right clip is the one starting at switch_frame.
            right_track = current.get_track_by_id(self.track_id)
            target_clip = right_track.get_clip_at(self.switch_frame) if right_track else None
            if target_clip is not None:
                target_clip_id = target_clip.clip_id
            did_split = True

        # Compute new source_start: map switch_frame into the angle's source
        # space, then apply the angle's sync offset.
        track_now = current.get_track_by_id(self.track_id)
        target = track_now.get_clip_by_id(target_clip_id) if track_now else None
        if target is None:
            raise ValueError(f"Target clip not found after split: {target_clip_id}")

        # FE-B06：经真实帧映射取切换点源帧（含当前角度偏移），减去活动
        # 偏移得到组公共时间，再叠目标角度偏移。不再直接 source_start +
        # target_offset（会把活动偏移算两遍）。
        switch_source = target.timeline_to_source_frame(self.switch_frame)
        from fractions import Fraction
        from src.features.edit.core.multicam import map_angle_source_frame

        active_fps = (Fraction(target._source_fps_num, target._source_fps_den or 1)
                      if target._source_fps_num else None)
        target_fps = (Fraction(self._angle_fps_num, self._angle_fps_den or 1)
                      if self._angle_fps_num else None)
        new_source_start = map_angle_source_frame(
            switch_source, self.active_sync_offset, self.angle_sync_offset,
            active_fps, target_fps)

        # F-R04/R23：新角度 fps 元数据随换源换装
        src_cmd = UpdateClipSourceCommand(
            command_id=str(uuid.uuid4()),
            label=f"Switch angle source: {self.angle_asset_id}",
            track_id=self.track_id,
            clip_id=target_clip_id,
            new_asset_id=self.angle_asset_id,
            new_source_start=new_source_start,
            _new_source_fps_num=self._angle_fps_num,
            _new_source_fps_den=self._angle_fps_den,
        )
        current, _ = src_cmd.apply(current)

        updated_cmd = SwitchMulticamAngleCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            switch_frame=self.switch_frame,
            angle_asset_id=self.angle_asset_id,
            angle_sync_offset=self.angle_sync_offset,
            active_sync_offset=self.active_sync_offset,
            _angle_fps_num=self._angle_fps_num,
            _angle_fps_den=self._angle_fps_den,
            _right_clip_id=target_clip_id,
            _did_split=did_split,
            _original_asset_id=clip.asset_id,
            _original_source_start=clip.source_start,
            # N19/RF03：no-split 边界切角同样带完整快照——undo 只还
            # asset/source_start 且沿用"当前"（目标角度的）source_duration
            # /fps，此前 A24 切 B48 后 Undo 时长 72→144。
            _original_clip=copy.deepcopy(clip),
            _before_track=copy.deepcopy(track),
            _before_markers=copy.deepcopy(timeline.markers),
        )
        logger.debug(
            f"SwitchMulticamAngleCommand applied: clip {self.clip_id} at "
            f"frame {self.switch_frame} → angle asset {self.angle_asset_id} "
            f"(split={did_split})"
        )
        return current, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        # If we split, undoing the split merges the segments back and restores
        # the original asset/source for the whole clip. If we didn't split,
        # undo the source swap on the single clip.
        if self._before_track is not None:
            restored = timeline.with_track_updated(self._before_track)
            if self._before_markers is not None:
                restored = restored._rebuild(markers=self._before_markers)
            return restored
        if self._did_split:
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")
            if self._original_clip is not None and self._right_clip_id is not None:
                # AUD-16208: 精确还原——split 保留原 clip_id 在左段、右段
                # 持新 id，直接移除两段并放回快照的原始片段（asset、源窗
                # 口、fades、链接等全部回到 split 前）。
                track_without_left = track.with_clip_removed(self.clip_id)
                track_without_both = track_without_left.with_clip_removed(self._right_clip_id)
                restored = timeline.with_track_updated(
                    track_without_both.with_clip_added(self._original_clip))
                logger.debug(
                    f"SwitchMulticamAngleCommand undone: restored original "
                    f"clip {self.clip_id} from snapshot"
                )
                return restored
            # 快照缺失（历史序列化数据）：回落到重建 SplitClipCommand 的
            # 通用合并路径。AUD-16204/AUD-16208：合并失败必须上抛（栈会
            # 回填命令）；吞掉异常返回未回滚的时间线会让栈状态推进而数据
            # 未回滚，redo 再叠加。
            split_cmd = SplitClipCommand(
                command_id=str(uuid.uuid4()),
                label="undo",
                track_id=self.track_id,
                clip_id=self.clip_id,
                split_frame=self.switch_frame,
            )
            try:
                return split_cmd.undo(timeline)
            except (ValueError, AttributeError):
                logger.error(
                    "SwitchMulticamAngleCommand undo: split undo failed for "
                    "clip %s at frame %s; re-raising to keep the undo stack "
                    "consistent with the timeline", self.clip_id, self.switch_frame
                )
                raise
        # No split — undo the source swap.
        if self._right_clip_id is None or self._original_asset_id is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        clip = track.get_clip_by_id(self._right_clip_id)
        if clip is None:
            return timeline
        if self._original_clip is not None:
            # N19/RF03：完整快照精确还原（fps/源窗/时长/PTS）
            return timeline.with_track_updated(
                track.with_clip_updated(self._original_clip))
        orig_start = (
            self._original_source_start
            if self._original_source_start is not None
            else clip.source_start
        )
        restored = clip.with_asset_id(self._original_asset_id).with_source_range(
            new_start=orig_start, new_duration=clip.source_duration
        )
        return timeline.with_track_updated(track.with_clip_updated(restored))

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        switch_frame: int,
        angle_asset_id: str,
        angle_sync_offset: int = 0,
        angle_label: str = "",
        active_sync_offset: int = 0,
        angle_fps_num: int | None = None,
        angle_fps_den: int | None = None,
    ) -> SwitchMulticamAngleCommand:
        return SwitchMulticamAngleCommand(
            command_id=str(uuid.uuid4()),
            label=f"Switch Angle: {angle_label}" if angle_label else "Switch Angle",
            track_id=track_id,
            clip_id=clip_id,
            switch_frame=switch_frame,
            angle_asset_id=angle_asset_id,
            angle_sync_offset=angle_sync_offset,
            active_sync_offset=active_sync_offset,
            _angle_fps_num=angle_fps_num,
            _angle_fps_den=angle_fps_den,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured during apply)."""
        return {
            "command_id": self.command_id,
            "command_type": "SwitchMulticamAngleCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "switch_frame": self.switch_frame,
            "angle_asset_id": self.angle_asset_id,
            "angle_sync_offset": self.angle_sync_offset,
            "active_sync_offset": self.active_sync_offset,
            "angle_fps_num": self._angle_fps_num,
            "angle_fps_den": self._angle_fps_den,
            "before_track": self._before_track.to_dict() if self._before_track is not None else None,
            "before_markers": ([m.to_dict() for m in self._before_markers]
                               if self._before_markers is not None else None),
            "right_clip_id": self._right_clip_id,
            "did_split": self._did_split,
            "original_asset_id": self._original_asset_id,
            "original_source_start": self._original_source_start,
            "original_clip": (
                self._original_clip.to_dict() if self._original_clip else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SwitchMulticamAngleCommand:
        """Deserialize command."""
        original_clip_data = data.get("original_clip")
        return SwitchMulticamAngleCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Switch Angle"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            switch_frame=data.get("switch_frame", 0),
            angle_asset_id=data.get("angle_asset_id", ""),
            angle_sync_offset=data.get("angle_sync_offset", 0),
            active_sync_offset=data.get("active_sync_offset", 0),
            _angle_fps_num=data.get("angle_fps_num"),
            _angle_fps_den=data.get("angle_fps_den"),
            _before_track=(EditTrack.from_dict(data["before_track"])
                           if data.get("before_track") is not None else None),
            _before_markers=(tuple(TimelineMarker.from_dict(m) for m in data["before_markers"])
                             if data.get("before_markers") is not None else None),
            _right_clip_id=data.get("right_clip_id"),
            _did_split=bool(data.get("did_split", False)),
            _original_asset_id=data.get("original_asset_id"),
            _original_source_start=data.get("original_source_start"),
            _original_clip=(
                ClipInstance.from_dict(original_clip_data)
                if original_clip_data
                else None
            ),
        )


# ================================================================
# ChangeClipFadeCommand (Phase 1.2)
# ================================================================

@dataclass(frozen=True)
class ChangeClipFadeCommand:
    """
    Command to change clip fade in/out duration.

    Phase 1.2: 渐变淡入淡出手柄功能。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to update
        fade_type: Which fade to change ('in', 'out', or 'both')
        new_fade_in: New fade in duration (frames)
        new_fade_out: New fade out duration (frames)
        old_fade_in: Old fade in duration (for undo)
        old_fade_out: Old fade out duration (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    fade_type: str  # 'in', 'out', or 'both'
    new_fade_in: int = 0
    new_fade_out: int = 0
    old_fade_in: int | None = None  # Set during apply
    old_fade_out: int | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: change clip fade duration.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip fade updated
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old values for undo.  During a live drag the view updates the
        # timeline for immediate preview and passes the pre-drag values into
        # this command.  Prefer those explicit values when present; otherwise
        # read the current clip for commands created outside that preview path.
        old_fade_in = (
            self.old_fade_in
            if self.old_fade_in is not None
            else clip.fade_in_duration
        )
        old_fade_out = (
            self.old_fade_out
            if self.old_fade_out is not None
            else clip.fade_out_duration
        )

        # Create updated clip based on fade_type
        if self.fade_type == "in":
            updated_clip = clip.with_fade_in(self.new_fade_in)
        elif self.fade_type == "out":
            updated_clip = clip.with_fade_out(self.new_fade_out)
        elif self.fade_type == "both":
            updated_clip = clip.with_fade(self.new_fade_in, self.new_fade_out)
        else:
            raise ValueError(f"Invalid fade_type: {self.fade_type}")

        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"ChangeClipFadeCommand applied: {self.clip_id} "
            f"fade_type={self.fade_type} "
            f"in={old_fade_in}->{self.new_fade_in} "
            f"out={old_fade_out}->{self.new_fade_out}"
        )

        # Return timeline and updated command with stored old values
        return new_timeline, ChangeClipFadeCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            fade_type=self.fade_type,
            new_fade_in=self.new_fade_in,
            new_fade_out=self.new_fade_out,
            old_fade_in=old_fade_in,
            old_fade_out=old_fade_out,
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original fade durations.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original fade durations restored
        """
        if self.old_fade_in is None or self.old_fade_out is None:
            raise ValueError("Cannot undo: old values not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Restore old values
        restored_clip = clip.with_fade(self.old_fade_in, self.old_fade_out)
        new_track = track.with_clip_updated(restored_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"ChangeClipFadeCommand undone: {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "ChangeClipFadeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "fade_type": self.fade_type,
            "new_fade_in": self.new_fade_in,
            "new_fade_out": self.new_fade_out,
            "old_fade_in": self.old_fade_in,
            "old_fade_out": self.old_fade_out,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ChangeClipFadeCommand:
        """Deserialize command."""
        return ChangeClipFadeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Change Clip Fade"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            fade_type=data.get("fade_type", "both"),
            new_fade_in=data.get("new_fade_in", 0),
            new_fade_out=data.get("new_fade_out", 0),
            old_fade_in=data.get("old_fade_in"),
            old_fade_out=data.get("old_fade_out"),
        )

    @staticmethod
    def create_fade_in(
        track_id: str,
        clip_id: str,
        new_fade_in: int,
        old_fade_in: int | None = None
    ) -> ChangeClipFadeCommand:
        """Factory method to create fade in change command."""
        return ChangeClipFadeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change Fade In to {new_fade_in} frames",
            track_id=track_id,
            clip_id=clip_id,
            fade_type="in",
            new_fade_in=new_fade_in,
            old_fade_in=old_fade_in,
        )

    @staticmethod
    def create_fade_out(
        track_id: str,
        clip_id: str,
        new_fade_out: int,
        old_fade_out: int | None = None
    ) -> ChangeClipFadeCommand:
        """Factory method to create fade out change command."""
        return ChangeClipFadeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change Fade Out to {new_fade_out} frames",
            track_id=track_id,
            clip_id=clip_id,
            fade_type="out",
            new_fade_out=new_fade_out,
            old_fade_out=old_fade_out,
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        new_fade_in: int,
        new_fade_out: int,
        old_fade_in: int | None = None,
        old_fade_out: int | None = None
    ) -> ChangeClipFadeCommand:
        """Factory method to create both fade change command."""
        return ChangeClipFadeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change Fade In={new_fade_in}, Out={new_fade_out}",
            track_id=track_id,
            clip_id=clip_id,
            fade_type="both",
            new_fade_in=new_fade_in,
            new_fade_out=new_fade_out,
            old_fade_in=old_fade_in,
            old_fade_out=old_fade_out,
        )


# ================================================================
# P2: SetClipLabelColorCommand
# ================================================================

class SetClipLabelColorCommand:
    """Set the label color of one or more clips.

    Supports single clip and batch operations (via MacroCommand for multi-clip).
    """

    def __init__(self, command_id: str, label: str,
                 track_id: str, clip_id: str,
                 new_color: str | None, old_color: str | None = None) -> None:
        self.command_id = command_id
        self.label = label
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_color = new_color
        self._old_color = old_color  # None means "read at execute time"

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        if self._old_color is None:
            self._old_color = clip.label_color
        return timeline.with_clip_updated(clip.with_label_color(self.new_color))

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        return timeline.with_clip_updated(clip.with_label_color(self._old_color))

    @staticmethod
    def create(track_id: str, clip_id: str, new_color: str | None,
               old_color: str | None = None) -> SetClipLabelColorCommand:
        return SetClipLabelColorCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set clip color to {new_color or 'default'}",
            track_id=track_id,
            clip_id=clip_id,
            new_color=new_color,
            old_color=old_color,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured during apply)."""
        return {
            "command_id": self.command_id,
            "command_type": "SetClipLabelColorCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_color": self.new_color,
            "old_color": self._old_color,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetClipLabelColorCommand:
        """Deserialize command."""
        return SetClipLabelColorCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set clip color"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_color=data.get("new_color"),
            old_color=data.get("old_color"),
        )


class SetClipWaveformDisplayCommand:
    """Set one audio clip to mixed mono or separate L/R waveform display."""

    def __init__(
        self,
        command_id: str,
        label: str,
        track_id: str,
        clip_id: str,
        new_show_channels: bool,
        old_show_channels: bool | None = None,
    ) -> None:
        self.command_id = command_id
        self.label = label
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_show_channels = bool(new_show_channels)
        self._old_show_channels = old_show_channels

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        if self._old_show_channels is None:
            self._old_show_channels = bool(
                getattr(clip, "waveform_show_channels", False)
            )
        return timeline.with_clip_updated(
            clip.with_waveform_show_channels(self.new_show_channels),
            preserve_waveform_display=False,
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_clip_by_id(self.clip_id)
        if clip is None or self._old_show_channels is None:
            return timeline
        return timeline.with_clip_updated(
            clip.with_waveform_show_channels(self._old_show_channels),
            preserve_waveform_display=False,
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        new_show_channels: bool,
        old_show_channels: bool | None = None,
    ) -> "SetClipWaveformDisplayCommand":
        mode = "stereo" if new_show_channels else "mono"
        return SetClipWaveformDisplayCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set waveform display to {mode}",
            track_id=track_id,
            clip_id=clip_id,
            new_show_channels=new_show_channels,
            old_show_channels=old_show_channels,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured during apply)."""
        return {
            "command_id": self.command_id,
            "command_type": "SetClipWaveformDisplayCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_show_channels": self.new_show_channels,
            "old_show_channels": self._old_show_channels,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetClipWaveformDisplayCommand":
        """Deserialize command."""
        return SetClipWaveformDisplayCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set waveform display"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_show_channels=bool(data.get("new_show_channels", False)),
            old_show_channels=data.get("old_show_channels"),
        )


# ================================================================
# P4: SetTimelineRangeCommand
# ================================================================

class SetTimelineRangeCommand:
    """Set or clear the In/Out range on a timeline."""

    def __init__(self, command_id: str, label: str,
                 new_start: int | None, new_end: int | None,
                 old_start: int | None = None, old_end: int | None = None) -> None:
        self.command_id = command_id
        self.label = label
        self.new_start = new_start
        self.new_end = new_end
        self._old_start = old_start
        self._old_end = old_end

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        return timeline.with_range(self.new_start, self.new_end)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return timeline.with_range(self._old_start, self._old_end)

    @staticmethod
    def create(new_start: int | None, new_end: int | None,
               old_start: int | None = None, old_end: int | None = None) -> SetTimelineRangeCommand:
        return SetTimelineRangeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set range {new_start}-{new_end}",
            new_start=new_start,
            new_end=new_end,
            old_start=old_start,
            old_end=old_end,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured at creation)."""
        return {
            "command_id": self.command_id,
            "command_type": "SetTimelineRangeCommand",
            "label": self.label,
            "new_start": self.new_start,
            "new_end": self.new_end,
            "old_start": self._old_start,
            "old_end": self._old_end,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTimelineRangeCommand:
        """Deserialize command."""
        return SetTimelineRangeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set range"),
            new_start=data.get("new_start"),
            new_end=data.get("new_end"),
            old_start=data.get("old_start"),
            old_end=data.get("old_end"),
        )


# ================================================================
# ToggleClipEnabledCommand
# ================================================================

class ToggleClipEnabledCommand:
    """Toggle the enabled/disabled state of a clip."""

    def __init__(self, command_id: str, label: str,
                 track_id: str, clip_id: str,
                 new_enabled: bool, old_enabled: bool) -> None:
        self.command_id = command_id
        self.label = label
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_enabled = new_enabled
        self._old_enabled = old_enabled

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        # C09/INT-05：锁轨片段的启用/禁用同样拒绝（菜单曾绕过）
        assert_track_editable(timeline, self.track_id, "Toggle clip enabled")
        clip = timeline.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        return timeline.with_clip_updated(clip.with_enabled(self.new_enabled))

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        return timeline.with_clip_updated(clip.with_enabled(self._old_enabled))

    @staticmethod
    def create(track_id: str, clip_id: str, new_enabled: bool,
               old_enabled: bool) -> ToggleClipEnabledCommand:
        label = "Enable clip" if new_enabled else "Disable clip"
        return ToggleClipEnabledCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            track_id=track_id,
            clip_id=clip_id,
            new_enabled=new_enabled,
            old_enabled=old_enabled,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured at creation)."""
        return {
            "command_id": self.command_id,
            "command_type": "ToggleClipEnabledCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_enabled": self.new_enabled,
            "old_enabled": self._old_enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ToggleClipEnabledCommand:
        """Deserialize command."""
        return ToggleClipEnabledCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Toggle clip enabled"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_enabled=bool(data.get("new_enabled", False)),
            old_enabled=bool(data.get("old_enabled", False)),
        )


# =============================================================================
# ChangeClipSpeedCommand
# =============================================================================


@dataclass(frozen=True)
class ChangeClipSpeedCommand:
    """
    Command to change clip playback speed.

    Adjusts the clip's speed parameter and recalculates timeline_duration
    based on the new speed ratio.

    Args:
        command_id: Unique command ID
        label: Display label
        clip_id: Target clip ID
        track_id: Track containing the clip
        new_speed: New speed value (SpeedParams or float)
        _old_speed: Previous speed (set on apply)
        _old_timeline_duration: Previous timeline_duration (set on apply)
    """

    command_id: str
    label: str
    clip_id: str
    track_id: str
    new_speed: Any  # SpeedParams or float
    _old_speed: Any = None
    _old_timeline_duration: int = 0
    _linked_clip_id: str | None = None
    _old_linked_speed: Any = None
    _old_linked_timeline_duration: int = 0
    # R05：变速缩短会钳制 fade 与转场时长，undo 用快照逐字段还原
    _old_fade_in: int = 0
    _old_fade_out: int = 0
    _old_linked_fade_in: int = 0
    _old_linked_fade_out: int = 0
    _before_transitions: tuple = None
    _before_linked_transitions: tuple = None
    # B17：apply 前完整动画快照（clip/effect 关键帧，deepcopy 隔离可变
    # KeyframeTrack 列表）。undo 直接还原快照，禁止反向重映射——高倍
    # 压缩后反算不可逆（主片关键帧回不到原帧位），链接伙伴此前完全
    # 缺少动画恢复路径。
    _before_animation: tuple | None = None
    _before_linked_animation: tuple | None = None

    def apply(self, timeline: Any) -> tuple:
        """Apply speed change."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # C09/INT-05：主轨与锁定的链接伙伴轨都不可被变速改写。锁住
        # 伙伴时整组拒绝，不允许只改主轨留下失步的 A/V。
        assert_track_editable(timeline, self.track_id, "Change clip speed")
        for linked_candidate in timeline.all_tracks:
            for candidate in linked_candidate.clips:
                if candidate.linked_clip_id == self.clip_id:
                    assert_track_editable(
                        timeline, candidate.track_id, "Change clip speed"
                    )

        # Store old values
        old_speed = clip.speed
        old_timeline_duration = clip.timeline_duration
        # B17：apply 前完整动画快照（主片）；链接伙伴快照在下方成对取
        before_animation = (
            _copy.deepcopy(clip.animated_params), _copy.deepcopy(clip.effects))

        # Calculate new timeline_duration based on speed ratio
        from src.features.edit.core.clip_params import SpeedParams

        if isinstance(self.new_speed, SpeedParams):
            new_speed_value = self.new_speed.speed
        elif isinstance(self.new_speed, (int, float)):
            new_speed_value = float(self.new_speed)
        else:
            new_speed_value = 1.0

        new_params = (
            self.new_speed
            if isinstance(self.new_speed, SpeedParams)
            else SpeedParams(speed=new_speed_value)
        )
        # A retime control can contain multiple constant-speed segments.  Its
        # duration is therefore the sum of every segment, not source/speed.
        # C07：fps 元数据可用时经统一 helper 换算（source_frames /
        # source_fps / speed * timeline_fps），修正混合帧率下 30fps 源
        # 2x 变速在 24fps 序列得到 60 帧而非 48 帧的漂移。
        from src.features.edit.core.timeline_ops import timeline_duration_for_source

        seq_fps = getattr(timeline, "fps", None)
        seq_fps_num = seq_fps.num if hasattr(seq_fps, "num") else None
        seq_fps_den = seq_fps.den if hasattr(seq_fps, "den") else 1
        # C07：SpeedParams 原样传入（变速点走分段积分），数值速度归一为
        # SpeedParams 由 helper 统一处理
        new_timeline_duration = timeline_duration_for_source(
            clip.source_duration,
            new_params,
            clip._source_fps_num,
            clip._source_fps_den,
            clip._timeline_fps_num or seq_fps_num,
            clip._timeline_fps_den or seq_fps_den,
        )

        new_speed_params = (
            self.new_speed if isinstance(self.new_speed, SpeedParams)
            else SpeedParams(speed=new_speed_value)
        )

        # A valid A/V link is one editing unit.  Apply the same speed to both
        # members and keep their timeline duration calculation in lockstep.
        linked_clip = timeline.get_reciprocal_linked_clip(clip)
        # R06：一步构造最终实例——with_speed_params 的中间时长是
        # speed 域（无 fps），先钳到中间值再放大到 fps 域时长会把
        # 介于两者之间的 fade 错误钳短。
        new_clip = replace(
            clip,
            speed=new_speed_params,
            timeline_duration=new_timeline_duration,
            fade_in_duration=min(clip.fade_in_duration, new_timeline_duration),
            fade_out_duration=min(clip.fade_out_duration, new_timeline_duration),
        )
        # F2.4：变速重锚——clip/effect 动画关键帧按新旧时长比例重映射，
        # 此前留在原绝对帧位导致曲线与画面错位。
        new_clip = new_clip.with_animation_time_remapped(
            old_timeline_duration, new_timeline_duration)

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)
        linked_old_speed = None
        linked_old_duration = 0
        before_linked_animation = None
        # C04（第六轮审计）：链接轨转场快照必须在 with_clip_updated 之前
        # 取——旧代码从已更新的 new_timeline 取，拿到的是被新时长钳制过
        # 的转场（实测 80→50→undo 仍 50，原始 80 帧转场永久丢失）。
        before_linked_transitions = None
        if linked_clip is not None:
            linked_track_pre = new_timeline.get_track_by_id(linked_clip.track_id)
            if linked_track_pre is not None:
                before_linked_transitions = _copy.deepcopy(
                    linked_track_pre.transitions)
        if linked_clip is not None:
            linked_old_speed = linked_clip.speed
            linked_old_duration = linked_clip.timeline_duration
            before_linked_animation = (
                _copy.deepcopy(linked_clip.animated_params),
                _copy.deepcopy(linked_clip.effects))
            linked_duration = timeline_duration_for_source(
                linked_clip.source_duration,
                new_params,
                linked_clip._source_fps_num,
                linked_clip._source_fps_den,
                linked_clip._timeline_fps_num or seq_fps_num,
                linked_clip._timeline_fps_den or seq_fps_den,
            )
            linked_updated = replace(
                linked_clip,
                speed=new_speed_params,
                timeline_duration=linked_duration,
                fade_in_duration=min(
                    linked_clip.fade_in_duration, linked_duration
                ),
                fade_out_duration=min(
                    linked_clip.fade_out_duration, linked_duration
                ),
            )
            # F2.4：链接伙伴同步重锚动画关键帧（与主片同语义）
            linked_updated = linked_updated.with_animation_time_remapped(
                linked_old_duration, linked_duration)
            linked_track = new_timeline.get_track_by_id(linked_clip.track_id)
            if linked_track is not None:
                new_timeline = new_timeline.with_track_updated(
                    linked_track.with_clip_updated(linked_updated)
                )

        updated_cmd = ChangeClipSpeedCommand(
            command_id=self.command_id,
            label=self.label,
            clip_id=self.clip_id,
            track_id=self.track_id,
            new_speed=self.new_speed,
            _old_speed=old_speed,
            _old_timeline_duration=old_timeline_duration,
            _linked_clip_id=linked_clip.clip_id if linked_clip else None,
            _old_linked_speed=linked_old_speed,
            _old_linked_timeline_duration=linked_old_duration,
            _old_fade_in=clip.fade_in_duration,
            _old_fade_out=clip.fade_out_duration,
            _old_linked_fade_in=(
                linked_clip.fade_in_duration if linked_clip else 0
            ),
            _old_linked_fade_out=(
                linked_clip.fade_out_duration if linked_clip else 0
            ),
            _before_transitions=track.transitions,
            _before_linked_transitions=before_linked_transitions,
            _before_animation=before_animation,
            _before_linked_animation=before_linked_animation,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: Any) -> Any:
        """Undo speed change."""
        if self._old_speed is None:
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline

        old_speed = (
            self._old_speed
            if isinstance(self._old_speed, SpeedParams)
            else SpeedParams(speed=float(self._old_speed))
        )
        new_clip = clip.with_speed_params(old_speed).with_timeline_duration(
            self._old_timeline_duration
        )
        # B17：动画直接还原 apply 前快照，禁止反算——高倍率压缩后的
        # 反向重映射不可逆（帧位四舍五入漂移），且快照含完整
        # clip/effect 关键帧、tangent/handles 与 effect 动画。旧命令
        # （无快照）回退到 F2.4 的反向重映射近似路径。
        if self._before_animation is not None:
            new_clip = replace(
                new_clip,
                animated_params=self._before_animation[0],
                effects=self._before_animation[1],
            )
        else:
            # F2.4：反向重映射动画关键帧（apply 按比例迁到新 span，undo
            # 从当前时长映射回旧时长，曲线与画面保持同锚）
            new_clip = new_clip.with_animation_time_remapped(
                clip.timeline_duration, self._old_timeline_duration)
        # R05：变速 apply 钳制了 fade，undo 恢复原 envelope
        if self._old_fade_in or self._old_fade_out:
            new_clip = replace(
                new_clip,
                fade_in_duration=self._old_fade_in,
                fade_out_duration=self._old_fade_out,
            )

        new_track = track.with_clip_updated(new_clip)
        current = timeline.with_track_updated(new_track)
        # R05：恢复变速（可能钳制时长）前的整轨转场快照
        if self._before_transitions is not None:
            track = current.get_track_by_id(self.track_id)
            if track is not None:
                current = current.with_track_updated(
                    track._rebuild(transitions=self._before_transitions)
                )
        if self._linked_clip_id and self._old_linked_speed is not None:
            linked = current.get_clip_by_id(self._linked_clip_id)
            if linked is not None:
                linked_speed = (
                    self._old_linked_speed
                    if isinstance(self._old_linked_speed, SpeedParams)
                    else SpeedParams(speed=float(self._old_linked_speed))
                )
                restored_linked = linked.with_speed_params(
                    linked_speed
                ).with_timeline_duration(self._old_linked_timeline_duration)
                # B17：链接伙伴动画恢复——此前 undo 完全遗漏伙伴的
                # 关键帧重映射还原，apply 压缩过的曲线永久停留在新 span
                if self._before_linked_animation is not None:
                    restored_linked = replace(
                        restored_linked,
                        animated_params=self._before_linked_animation[0],
                        effects=self._before_linked_animation[1],
                    )
                elif linked.timeline_duration != self._old_linked_timeline_duration:
                    restored_linked = (
                        restored_linked.with_animation_time_remapped(
                            linked.timeline_duration,
                            self._old_linked_timeline_duration))
                if self._old_linked_fade_in or self._old_linked_fade_out:
                    restored_linked = replace(
                        restored_linked,
                        fade_in_duration=self._old_linked_fade_in,
                        fade_out_duration=self._old_linked_fade_out,
                    )
                linked_track = current.get_track_by_id(linked.track_id)
                if linked_track is not None:
                    current = current.with_track_updated(
                        linked_track.with_clip_updated(restored_linked)
                    )
                    if self._before_linked_transitions is not None:
                        linked_track = current.get_track_by_id(linked.track_id)
                        if linked_track is not None:
                            current = current.with_track_updated(
                                linked_track._rebuild(
                                    transitions=self._before_linked_transitions
                                )
                            )
        return current

    @staticmethod
    def create(
        clip_id: str,
        track_id: str,
        speed: SpeedParams | float,
    ) -> ChangeClipSpeedCommand:
        """Factory: create a constant or variable-speed change command."""
        from src.features.edit.core.clip_params import SpeedParams
        speed_params = speed if isinstance(speed, SpeedParams) else SpeedParams(speed=speed)
        return ChangeClipSpeedCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change speed to {speed}x",
            clip_id=clip_id,
            track_id=track_id,
            new_speed=speed_params,
        )


# ================================================================
# B15: MoveSpeedPointCommand（速度点沿源坐标移动）
# ================================================================

@dataclass(frozen=True)
class MoveSpeedPointCommand:
    """B15：把既有速度点沿源坐标移动到新分界（窗口坐标输入）。

    与 ``ChangeClipSpeedCommand`` 同族的提交语义：

    - 命令在**源坐标**上定义（``source_offset``/``new_source_offset``
      为 clip 窗口坐标）；UI 的 timeline 拖动由调用方换算后传入；
    - 互链 A/V 是一个编辑单元：移动后的完整 SpeedParams 同步应用到
      互链伙伴（与变速命令一致），各成员按自身源窗与 fps 元数据重算
      ``timeline_duration``（``timeline_duration_for_source`` 统一时基，
      混合帧率正确）；
    - 曲线编辑不改采样相位：移动点之前的段映射逐帧不变，之后的段按
      新分界重积分；窗口 ``curve_time_offset`` 由 SpeedParams 重锚；
    - undo 用 apply 期完整快照直接还原（速度/时长/淡变/动画，
      B17 语义——禁止反算）；fade 随新时长钳制，undo 恢复。

    Attributes:
        source_offset: 旧分界（clip 窗口坐标）
        new_source_offset: 新分界（clip 窗口坐标；必须严格落在相邻
            点位/域边界构成的开区间内）
        _snapshots: ((speed_dict, duration, fade_in, fade_out, animation),
                     linked 同构或 None)；animation 为 deepcopy 的
            (animated_params, effects)，undo 直接还原
    """

    command_id: str
    label: str
    clip_id: str
    track_id: str
    source_offset: int
    new_source_offset: int
    _snapshots: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        assert_track_editable(
            timeline, self.track_id, "Move speed point")
        if not isinstance(clip.speed, SpeedParams):
            raise ValueError("Clip has no retime speed points")
        moved = clip.speed.with_speed_point_moved(
            self.source_offset, self.new_source_offset, clip.source_duration)
        if moved is clip.speed:
            return timeline  # 同位移动 no-op（不入历史）

        linked = timeline.get_reciprocal_linked_clip(clip)
        if linked is not None:
            assert_track_editable(
                timeline, linked.track_id, "Move speed point")

        from src.features.edit.core.timeline_ops import (
            timeline_duration_for_source,
        )

        seq_fps = getattr(timeline, "fps", None)
        seq_fps_num = seq_fps.num if hasattr(seq_fps, "num") else None
        seq_fps_den = seq_fps.den if hasattr(seq_fps, "den") else 1

        def _member_snapshot(member: ClipInstance) -> tuple:
            return (
                member.speed.to_dict(), member.timeline_duration,
                member.fade_in_duration, member.fade_out_duration,
                (_copy.deepcopy(member.animated_params),
                 _copy.deepcopy(member.effects)),
            )

        snapshots: list[tuple] = [_member_snapshot(clip)]

        result = timeline
        members = [clip] + ([linked] if linked is not None else [])
        for member in members:
            member_track = result.get_track_by_id(member.track_id)
            member_clip = member_track.get_clip_by_id(member.clip_id)
            if member is linked:
                snapshots.append(_member_snapshot(member_clip))
            new_duration = timeline_duration_for_source(
                member_clip.source_duration, moved,
                member_clip._source_fps_num, member_clip._source_fps_den,
                member_clip._timeline_fps_num or seq_fps_num,
                member_clip._timeline_fps_den or seq_fps_den,
            )
            updated = replace(
                member_clip,
                speed=moved,
                timeline_duration=new_duration,
                fade_in_duration=min(
                    member_clip.fade_in_duration, new_duration),
                fade_out_duration=min(
                    member_clip.fade_out_duration, new_duration),
            ).with_animation_time_remapped(
                member_clip.timeline_duration, new_duration)
            result = result.with_track_updated(
                member_track.with_clip_updated(updated))

        object.__setattr__(self, "_snapshots", tuple(snapshots))
        return result

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        from src.features.edit.core.clip_params import SpeedParams as _SP

        result = timeline
        linked = timeline.get_reciprocal_linked_clip(clip)
        targets = [clip] + ([linked] if linked is not None else [])
        if self._snapshots:
            for member, snapshot in zip(targets, self._snapshots):
                (speed_dict, duration, fade_in, fade_out,
                 (animated, effects)) = snapshot
                member_track = result.get_track_by_id(member.track_id)
                member_clip = member_track.get_clip_by_id(member.clip_id)
                restored = replace(
                    member_clip,
                    speed=_SP.from_dict(speed_dict),
                    timeline_duration=duration,
                    fade_in_duration=fade_in,
                    fade_out_duration=fade_out,
                    animated_params=animated,
                    effects=effects,
                )
                result = result.with_track_updated(
                    member_track.with_clip_updated(restored))
            return result

        # 序列化恢复后无快照（to_dict 不携带动画/淡变）：降级为反向
        # 移动 + 时长重算——曲线/时长可还原，动画停留当前锚（记录在案
        # 的降级路径，非主路径）。
        if not isinstance(clip.speed, _SP):
            raise ValueError("Cannot undo: no snapshot and no speed points")
        reverse = clip.speed.with_speed_point_moved(
            self.new_source_offset, self.source_offset, clip.source_duration)
        from src.features.edit.core.timeline_ops import (
            timeline_duration_for_source,
        )
        seq_fps = getattr(timeline, "fps", None)
        seq_fps_num = seq_fps.num if hasattr(seq_fps, "num") else None
        seq_fps_den = seq_fps.den if hasattr(seq_fps, "den") else 1
        for member in targets:
            member_track = result.get_track_by_id(member.track_id)
            member_clip = member_track.get_clip_by_id(member.clip_id)
            new_duration = timeline_duration_for_source(
                member_clip.source_duration, reverse,
                member_clip._source_fps_num, member_clip._source_fps_den,
                member_clip._timeline_fps_num or seq_fps_num,
                member_clip._timeline_fps_den or seq_fps_den,
            )
            restored = replace(
                member_clip, speed=reverse, timeline_duration=new_duration)
            result = result.with_track_updated(
                member_track.with_clip_updated(restored))
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "MoveSpeedPointCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "source_offset": self.source_offset,
            "new_source_offset": self.new_source_offset,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MoveSpeedPointCommand":
        return MoveSpeedPointCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move speed point"),
            clip_id=data.get("clip_id", ""),
            track_id=data.get("track_id", ""),
            source_offset=int(data.get("source_offset", 0)),
            new_source_offset=int(data.get("new_source_offset", 0)),
        )

    @staticmethod
    def create(
        clip_id: str, track_id: str,
        source_offset: int, new_source_offset: int,
    ) -> "MoveSpeedPointCommand":
        return MoveSpeedPointCommand(
            command_id=str(uuid.uuid4()),
            label=(f"Move speed point {source_offset}"
                   f"→{new_source_offset}"),
            clip_id=clip_id,
            track_id=track_id,
            source_offset=int(source_offset),
            new_source_offset=int(new_source_offset),
        )


# ================================================================
# B14: SetClipSpeedSettingsCommand（速度对话框全部意图的单事务提交）
# ================================================================

@dataclass(frozen=True)
class SetClipSpeedSettingsCommand:
    """B14：一次提交速度对话框的**全部**设置（不静默丢字段）。

    支持意图（stage-1 矩阵）：

    - ``speed``/``reverse``/``preserve_pitch``：基础变速（互链伙伴
      同步，与 ChangeClipSpeedCommand 同语义）；
    - ``keyframe_timing``：``"stretch"`` 按新旧时长比例重锚 clip/
      effect 动画（既有 F2.4 语义）；``"maintain"`` 关键帧保持绝对
      时间线帧位不动；
    - ``duration_frames``：请求时长仅作一致性校验（由接收层完成），
      命令内时长以完整速度积分（统一混合帧率 helper）为准；
    - ``freeze``：整段定格在当前源 In 帧（``freeze_source_frame``），
      时长 = 请求时长；速度参数仍写入（解冻后生效）；
    - ``ripple``：时长变化时，主片轨与互链伙伴轨上其后继片段整体
      平移闭合/让位（同一 delta，单步 undo）。

    undo 用 apply 期完整快照直接还原（B17 语义）。任何字段非法/
    前提不满足抛 ``ValueError``——由接收层转为明确诊断，零写入。

    Attributes:
        _snapshots: [member] + [follower] 的还原快照元组列表
    """

    command_id: str
    label: str
    clip_id: str
    track_id: str
    speed: float
    reverse: bool = False
    preserve_pitch: bool = False
    freeze: bool = False
    keyframe_timing: str = "stretch"
    duration_frames: int = 0
    ripple: bool = False
    _snapshots: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        if self.keyframe_timing not in ("stretch", "maintain"):
            raise ValueError(
                f"Invalid keyframe_timing: {self.keyframe_timing!r}")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        assert_track_editable(
            timeline, self.track_id, "Apply speed settings")
        linked = timeline.get_reciprocal_linked_clip(clip)
        if linked is not None:
            assert_track_editable(
                timeline, linked.track_id, "Apply speed settings")

        from src.features.edit.core.clip_params import SpeedParams as _SP
        from src.features.edit.core.timeline_ops import (
            timeline_duration_for_source,
        )

        base = (clip.speed if isinstance(clip.speed, _SP)
                else _SP(speed=float(clip.speed)))
        new_params = (base.with_speed(float(self.speed))
                      .with_reverse(bool(self.reverse))
                      .with_preserve_pitch(bool(self.preserve_pitch)))

        seq_fps = getattr(timeline, "fps", None)
        seq_fps_num = seq_fps.num if hasattr(seq_fps, "num") else None
        seq_fps_den = seq_fps.den if hasattr(seq_fps, "den") else 1

        snapshots: list[tuple] = []

        def _member_snapshot(member: ClipInstance) -> tuple:
            return (
                member.clip_id, member.track_id,
                member.speed.to_dict()
                if isinstance(member.speed, _SP)
                else {"speed": float(member.speed)},
                member.timeline_duration, member.fade_in_duration,
                member.fade_out_duration, member.freeze_source_frame,
                (_copy.deepcopy(member.animated_params),
                 _copy.deepcopy(member.effects)),
            )

        members = [clip] + ([linked] if linked is not None else [])
        member_plans: list[tuple[ClipInstance, ClipInstance, int]] = []
        for member in members:
            member_track = timeline.get_track_by_id(member.track_id)
            member_clip = member_track.get_clip_by_id(member.clip_id)
            if self.freeze:
                new_duration = max(1, int(self.duration_frames))
            else:
                new_duration = timeline_duration_for_source(
                    member_clip.source_duration, new_params,
                    member_clip._source_fps_num, member_clip._source_fps_den,
                    member_clip._timeline_fps_num or seq_fps_num,
                    member_clip._timeline_fps_den or seq_fps_den,
                )
            member_plans.append((member_track, member_clip, new_duration))
            snapshots.append(_member_snapshot(member_clip))

        # 无变化判定（速度参数、冻结态、时长都不变）→ no-op 同一实例
        primary_new = member_plans[0]
        no_change = (
            new_params == base
            and primary_new[2] == clip.timeline_duration
            and (not self.freeze
                 or clip.freeze_source_frame == clip.source_start))
        if no_change:
            return timeline

        result = timeline
        for member_track, member_clip, new_duration in member_plans:
            updated = replace(
                member_clip,
                speed=new_params,
                timeline_duration=new_duration,
                fade_in_duration=min(
                    member_clip.fade_in_duration, new_duration),
                fade_out_duration=min(
                    member_clip.fade_out_duration, new_duration),
            )
            if self.freeze:
                updated = updated.with_freeze_frame(member_clip.source_start)
            if self.keyframe_timing == "stretch" and not self.freeze:
                updated = updated.with_animation_time_remapped(
                    member_clip.timeline_duration, new_duration)

            # ripple：同轨后继按时长 delta 平移。成员更新与后继平移必须
            # 在**同一次轨重建**里落盘——先更新成员会在增长方向与未移的
            # 后继形成重叠中间态，EditTrack 校验会拒绝。
            if self.ripple and new_duration != member_clip.timeline_duration:
                delta = new_duration - member_clip.timeline_duration
                old_end = member_clip.timeline_end
                final_clips: list[ClipInstance] = []
                for c in member_track.clips:
                    if c.clip_id == member_clip.clip_id:
                        final_clips.append(updated)
                    elif c.timeline_start >= old_end:
                        snapshots.append(
                            (c.clip_id, c.track_id, c.timeline_start))
                        final_clips.append(
                            replace(c, timeline_start=c.timeline_start + delta))
                    else:
                        final_clips.append(c)
                result = result.with_track_updated(
                    member_track._rebuild(
                        clips=tuple(sorted(
                            final_clips, key=lambda c: c.timeline_start))))
            else:
                result = result.with_track_updated(
                    member_track.with_clip_updated(updated))

        object.__setattr__(self, "_snapshots", tuple(snapshots))
        return result

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if not self._snapshots:
            raise ValueError("Cannot undo: command was never applied")
        from src.features.edit.core.clip_params import SpeedParams as _SP

        # 按轨分组一次重建：逐个 with_clip_updated 会产生"成员还原但
        # 后继未回位"的 overlaps 中间态（apply 的镜像问题）。
        by_track: dict[str, dict[str, Any]] = {}
        for snapshot in self._snapshots:
            if len(snapshot) == 3:
                clip_id, track_id, start = snapshot
                entry = {"kind": "start", "start": start}
            else:
                (clip_id, track_id, speed_dict, duration, fade_in, fade_out,
                 freeze, (animated, effects)) = snapshot
                entry = {
                    "kind": "full", "speed_dict": speed_dict,
                    "duration": duration, "fade_in": fade_in,
                    "fade_out": fade_out, "freeze": freeze,
                    "animated": animated, "effects": effects,
                }
            by_track.setdefault(track_id, {})[clip_id] = entry

        result = timeline
        for track_id, entries in by_track.items():
            track = result.get_track_by_id(track_id)
            final_clips: list[ClipInstance] = []
            for c in track.clips:
                entry = entries.get(c.clip_id)
                if entry is None:
                    final_clips.append(c)
                elif entry["kind"] == "start":
                    final_clips.append(
                        replace(c, timeline_start=entry["start"]))
                else:
                    final_clips.append(replace(
                        c,
                        speed=_SP.from_dict(entry["speed_dict"]),
                        timeline_duration=entry["duration"],
                        fade_in_duration=entry["fade_in"],
                        fade_out_duration=entry["fade_out"],
                        animated_params=entry["animated"],
                        effects=entry["effects"],
                    ).with_freeze_frame(entry["freeze"]))
            result = result.with_track_updated(
                track._rebuild(clips=tuple(sorted(
                    final_clips, key=lambda c: c.timeline_start))))
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetClipSpeedSettingsCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "speed": self.speed,
            "reverse": self.reverse,
            "preserve_pitch": self.preserve_pitch,
            "freeze": self.freeze,
            "keyframe_timing": self.keyframe_timing,
            "duration_frames": self.duration_frames,
            "ripple": self.ripple,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetClipSpeedSettingsCommand":
        return SetClipSpeedSettingsCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Apply speed settings"),
            clip_id=data.get("clip_id", ""),
            track_id=data.get("track_id", ""),
            speed=float(data.get("speed", 1.0)),
            reverse=bool(data.get("reverse", False)),
            preserve_pitch=bool(data.get("preserve_pitch", False)),
            freeze=bool(data.get("freeze", False)),
            keyframe_timing=data.get("keyframe_timing", "stretch"),
            duration_frames=int(data.get("duration_frames", 0)),
            ripple=bool(data.get("ripple", False)),
        )

    @staticmethod
    def create(
        clip_id: str, track_id: str, settings: dict[str, Any],
    ) -> "SetClipSpeedSettingsCommand":
        return SetClipSpeedSettingsCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change clip speed ({settings.get('speed', 1.0):g}x)",
            clip_id=clip_id,
            track_id=track_id,
            speed=float(settings.get("speed", 1.0)),
            reverse=bool(settings.get("reverse", False)),
            preserve_pitch=bool(settings.get("preserve_pitch", False)),
            freeze=bool(settings.get("freeze", False)),
            keyframe_timing=str(settings.get("keyframe_timing", "stretch")),
            duration_frames=int(settings.get("duration_frames", 0) or 0),
            ripple=bool(settings.get("ripple", False)),
        )


# ================================================================
# B16: SetFadeCurveCommand（淡化曲线形状）
# ================================================================

@dataclass(frozen=True)
class SetFadeCurveCommand:
    """B16：设置 clip 淡化曲线形状（沿 fade_in/out_duration 归一进度）。

    曲线在模型持久化（``ClipInstance.fade_curve``），视频预览/导出与
    音频实时/离线消费者共同读取——不是只画 UI 曲线。``None`` = legacy
    默认（视频 linear / 音频 equal_power，旧工程逐帧/逐样本不变）。
    白名单外新写拒绝；undo 快照直接还原。

    Attributes:
        fade_curve: "linear" | "equal_power" | "smooth" | None（清除）
        _previous: apply 期捕获的原值
    """

    command_id: str
    label: str
    clip_id: str
    track_id: str
    fade_curve: str | None
    _previous: str | None = None
    _had_value: bool = False

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        if self.fade_curve is not None and self.fade_curve not in (
                "linear", "equal_power", "smooth"):
            raise ValueError(
                f"Invalid fade curve: {self.fade_curve!r}; expected "
                f"linear|equal_power|smooth or None")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        assert_track_editable(timeline, self.track_id, "Set fade curve")
        if clip.fade_curve == self.fade_curve:
            return timeline  # no-op 同一实例（不入历史）
        object.__setattr__(self, "_previous", clip.fade_curve)
        object.__setattr__(self, "_had_value", True)
        new_clip = clip.with_fade_curve(self.fade_curve)
        new_track = track._rebuild(clips=tuple(
            new_clip if c.clip_id == clip.clip_id else c for c in track.clips))
        return timeline.with_track_updated(new_track)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous is None and not self._had_value:
            raise ValueError("Cannot undo: command was never applied")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        restored = clip.with_fade_curve(self._previous)
        new_track = track._rebuild(clips=tuple(
            restored if c.clip_id == clip.clip_id else c for c in track.clips))
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetFadeCurveCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "fade_curve": self.fade_curve,
            "_previous": self._previous,  # None 也写出：键存在 = 已 apply
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetFadeCurveCommand":
        return SetFadeCurveCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set fade curve"),
            clip_id=data.get("clip_id", ""),
            track_id=data.get("track_id", ""),
            fade_curve=data.get("fade_curve"),
            _previous=data.get("_previous"),
            # 序列化恢复：_previous 键存在 = 已 apply 过（含原值为 None）
            _had_value="_previous" in data,
        )

    @staticmethod
    def create(
        clip_id: str, track_id: str, fade_curve: str | None,
    ) -> "SetFadeCurveCommand":
        return SetFadeCurveCommand(
            command_id=str(uuid.uuid4()),
            label=f"Fade curve: {fade_curve or 'default'}",
            clip_id=clip_id,
            track_id=track_id,
            fade_curve=fade_curve,
        )


# ================================================================
# A2: FreezeFrameAtPlayheadCommand（冻结帧，2026-09-07 优化计划）
# ================================================================

@dataclass(frozen=True)
class FreezeFrameAtPlayheadCommand:
    """
    在播放头处冻结：分割片段，后半段全程定格在播放头对应源帧。

    单步原子操作（undo 一步整体还原分割+冻结）。分割语义与
    SplitClipCommand 一致（左段保留原 clip_id，右段新 ID）；冻结源帧 =
    分割点上原片段映射到的源帧。采用独立 freeze 字段而非 speed=0，
    不动 SpeedParams 校验与帧映射器积分逻辑（计划 A2 已定方案）。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_id: Clip to freeze at playhead
        playhead_frame: Playhead position (must be strictly inside clip)
        _right_clip_id: ID of the frozen (right) segment, set during apply
        _original_clip: Pre-split clip (for undo)
        _frozen_clip: Post-split frozen segment (for redo)
    """

    command_id: str
    label: str
    clip_id: str
    playhead_frame: int
    _right_clip_id: str | None = None
    _original_clip: ClipInstance | None = None
    _frozen_clip: ClipInstance | None = None
    # C05（第六轮审计）：freeze = 切分 + 右段定格；快照整轨 clips/transitions
    # 与序列 markers，undo 原子恢复（与 SplitClipCommand 的 C11/C15 同型）。
    _before_clips: tuple[ClipInstance, ...] | None = None
    _before_transitions: tuple = None
    _before_markers: tuple = None

    @staticmethod
    def _find_clip_track(timeline: TimelineSequence, clip_id: str):
        for t in timeline.all_tracks:
            c = t.get_clip_by_id(clip_id)
            if c is not None:
                return t, c
        return None, None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track, clip = self._find_clip_track(timeline, self.clip_id)
        if track is None or clip is None:
            raise ValueError(f"Clip not found on any track: {self.clip_id}")

        # N04/C3-04：锁轨拒绝（Freeze 是内容写入口，此前可绕锁）
        assert_track_editable(timeline, track.track_id, "Freeze frame")

        if not (clip.timeline_start < self.playhead_frame < clip.timeline_end):
            raise ValueError(
                f"Playhead {self.playhead_frame} must be strictly inside clip "
                f"[{clip.timeline_start}, {clip.timeline_end})"
            )

        # 记录分割点对应源帧（分割前的映射；考虑速度）
        freeze_source = clip.timeline_to_source_frame(self.playhead_frame)

        right_clip_id = self._right_clip_id or str(uuid.uuid4())
        object.__setattr__(self, "_original_clip", clip)
        object.__setattr__(self, "_right_clip_id", right_clip_id)

        left_timeline_duration = self.playhead_frame - clip.timeline_start
        right_timeline_duration = clip.timeline_end - self.playhead_frame

        # C05（第六轮审计）：源窗口切分复用 SplitClipCommand 的同一事务
        # helper（split_source_partition + source_window_mapping_updates），
        # 保证变速/反向/混合 fps 下左右段源映射与刀片切分一致；此前手工
        # 计算且左段保留整段源范围。
        from src.features.edit.core.timeline_ops import split_source_partition

        (left_source_start, left_source_duration,
         right_source_start, right_source_duration) = split_source_partition(
            clip, self.playhead_frame)
        left_mapping = clip.source_window_mapping_updates(
            left_source_start, left_source_duration, 0)
        right_mapping = clip.source_window_mapping_updates(
            right_source_start, right_source_duration, left_timeline_duration)

        # 左段保留原 ID 与除时长外的全部属性；缩短后 clamp fade 以满足
        # fade ≤ duration 的不变量（C10：fade_out 同样钳制）。
        left_clip = replace(
            clip,
            timeline_duration=left_timeline_duration,
            source_start=left_source_start,
            source_duration=left_source_duration,
            **left_mapping,
            fade_in_duration=min(clip.fade_in_duration, left_timeline_duration),
            fade_out_duration=min(clip.fade_out_duration, left_timeline_duration),
        )

        # 右段 = 分割 + 冻结（linked_clip_id 置空，与 SplitClipCommand 一致）。
        # replace() 保留其余全部属性（blend mode/audio gain/label color/
        # waveform/fps 元数据…），手工重建曾把这些重置为默认值。
        frozen_clip = replace(
            clip,
            clip_id=right_clip_id,
            timeline_start=self.playhead_frame,
            timeline_duration=right_timeline_duration,
            source_start=right_source_start,
            source_duration=right_source_duration,
            **right_mapping,
            linked_clip_id=None,
            fade_in_duration=0,
            fade_out_duration=min(clip.fade_out_duration, right_timeline_duration),
            freeze_source_frame=freeze_source,
        )
        object.__setattr__(self, "_frozen_clip", frozen_clip)

        # C05：整轨/序列快照（与 SplitClipCommand 的 C11/C15 同型——
        # freeze 本质是"切分 + 右段定格"，marker/转场归属迁移不能缺席）
        object.__setattr__(self, "_before_clips", copy.deepcopy(track.clips))
        object.__setattr__(self, "_before_transitions", track.transitions)
        object.__setattr__(self, "_before_markers", timeline.markers)

        track_without_original = track.with_clip_removed(clip.clip_id)
        track_with_left = track_without_original.with_clip_added(left_clip)
        track_with_both = track_with_left.with_clip_added(frozen_clip)

        # C05：转场归属迁移——头转场/左邻切点转场挂左段，尾转场/右邻切点
        # 转场挂右段（冻结右段），再按新边缘重锚。此前尾淡出被错误重锚到
        # 左段结尾，冻结段反而没有尾部淡出。
        migrated_transitions = []
        for trans in track.transitions:
            if trans.right_clip_id == clip.clip_id:
                migrated_transitions.append(
                    replace(trans, right_clip_id=clip.clip_id))
            elif trans.left_clip_id == clip.clip_id:
                migrated_transitions.append(
                    replace(trans, left_clip_id=right_clip_id))
            else:
                migrated_transitions.append(trans)
        track_with_both = track_with_both._rebuild(
            transitions=tuple(migrated_transitions)
        ).reanchor_transitions()

        new_timeline = timeline.with_track_updated(track_with_both)

        # C05：clip-scoped marker 按内容位置迁移到所属半段（切点帧属右段，
        # 半开区间语义与 SplitClipCommand 一致）。此前 marker75 仍挂只到
        # 50 帧的左段，validate_timeline 报 outside owning clip。
        migrated_markers = []
        markers_changed = False
        for marker in timeline.markers:
            if marker.clip_id == clip.clip_id:
                target = (
                    clip.clip_id
                    if marker.frame < self.playhead_frame
                    else right_clip_id
                )
                if target != marker.clip_id:
                    markers_changed = True
                migrated_markers.append(replace(marker, clip_id=target))
            else:
                migrated_markers.append(marker)
        if markers_changed:
            new_timeline = new_timeline._rebuild(
                markers=tuple(migrated_markers))

        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._original_clip is None or self._right_clip_id is None:
            raise ValueError("Cannot undo: freeze command was never applied")
        track, _clip = self._find_clip_track(timeline, self._right_clip_id)
        if track is None:
            track, _ = self._find_clip_track(timeline, self.clip_id)
        if track is None:
            raise ValueError(f"Frozen clip not found on any track: {self._right_clip_id}")
        # C05：有快照时整轨原子恢复（clips + transitions + markers），
        # 与 SplitClipCommand.undo 的 C11/C15 路径同型。
        if self._before_clips is not None:
            restored = track._rebuild(
                clips=self._before_clips,
                transitions=self._before_transitions,
            )
            restored_timeline = timeline.with_track_updated(restored)
            if self._before_markers is not None:
                restored_timeline = restored_timeline._rebuild(
                    markers=self._before_markers
                )
            return restored_timeline
        # 还原：右段（冻结段）替换回分割前的原始片段，左段消失
        new_clips: list[ClipInstance] = []
        restored = False
        for c in track.clips:
            if c.clip_id == self._right_clip_id:
                new_clips.append(self._original_clip)
                restored = True
            elif c.clip_id == self._original_clip.clip_id:
                continue  # 左段移除
            else:
                new_clips.append(c)
        if not restored:
            raise ValueError("Frozen segment missing; timeline state unexpected")
        new_clips.sort(key=lambda c: c.timeline_start)
        new_track = track._rebuild(clips=tuple(new_clips))
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "FreezeFrameAtPlayheadCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "playhead_frame": self.playhead_frame,
            "right_clip_id": self._right_clip_id,
            "original_clip": self._original_clip.to_dict() if self._original_clip else None,
            "frozen_clip": self._frozen_clip.to_dict() if self._frozen_clip else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FreezeFrameAtPlayheadCommand":
        original_data = data.get("original_clip")
        frozen_data = data.get("frozen_clip")
        return FreezeFrameAtPlayheadCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Freeze Frame at Playhead"),
            clip_id=data.get("clip_id", ""),
            playhead_frame=data.get("playhead_frame", 0),
            _right_clip_id=data.get("right_clip_id"),
            _original_clip=ClipInstance.from_dict(original_data) if original_data else None,
            _frozen_clip=ClipInstance.from_dict(frozen_data) if frozen_data else None,
        )

    @staticmethod
    def create(clip_id: str, playhead_frame: int) -> "FreezeFrameAtPlayheadCommand":
        return FreezeFrameAtPlayheadCommand(
            command_id=str(uuid.uuid4()),
            label=f"Freeze Frame at Playhead (clip {clip_id})",
            clip_id=clip_id,
            playhead_frame=playhead_frame,
        )


# ================================================================
# A1: SetClipBlendModeCommand（片段图层混合模式，2026-09-07 优化计划）
# ================================================================

@dataclass(frozen=True)
class SetClipBlendModeCommand:
    """
    设置一个或多个片段的图层混合模式（多选批量 = 一步撤销）。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_ids: 目标片段 ID 元组（多选批量）
        blend_mode: 目标模式（ClipBlendMode 或其字符串值）
        _previous_modes: 内部：各片段原模式（undo 用）
    """

    command_id: str
    label: str
    clip_ids: tuple
    blend_mode: Any
    _previous_modes: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        from src.features.edit.core.clip_params import ClipBlendMode

        if isinstance(self.blend_mode, ClipBlendMode):
            target = self.blend_mode
        else:
            try:
                target = ClipBlendMode(str(self.blend_mode))
            except (ValueError, TypeError):
                raise ValueError(f"Invalid blend mode: {self.blend_mode}")

        previous: list[tuple[str, Any]] = []
        new_timeline = timeline
        for clip_id in self.clip_ids:
            track = None
            clip = None
            for t in new_timeline.all_tracks:
                c = t.get_clip_by_id(clip_id)
                if c is not None:
                    track, clip = t, c
                    break
            if clip is None:
                continue
            previous.append((clip_id, clip.blend_mode))
            updated = clip.with_blend_mode(target)
            new_clips = tuple(updated if c.clip_id == clip_id else c for c in track.clips)
            new_timeline = new_timeline.with_track_updated(track._rebuild(clips=new_clips))

        object.__setattr__(self, "_previous_modes", tuple(previous))
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous_modes is None:
            raise ValueError("Cannot undo: command was never applied")
        new_timeline = timeline
        for clip_id, old_mode in self._previous_modes:
            track = None
            clip = None
            for t in new_timeline.all_tracks:
                c = t.get_clip_by_id(clip_id)
                if c is not None:
                    track, clip = t, c
                    break
            if clip is None:
                continue
            updated = clip.with_blend_mode(old_mode)
            new_clips = tuple(updated if c.clip_id == clip_id else c for c in track.clips)
            new_timeline = new_timeline.with_track_updated(track._rebuild(clips=new_clips))
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetClipBlendModeCommand",
            "label": self.label,
            "clip_ids": list(self.clip_ids),
            "blend_mode": getattr(self.blend_mode, "value", str(self.blend_mode)),
            "_previous_modes": [
                (cid, getattr(mode, "value", str(mode)))
                for cid, mode in (self._previous_modes or ())
            ],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetClipBlendModeCommand":
        from src.features.edit.core.clip_params import ClipBlendMode

        try:
            target = ClipBlendMode(data.get("blend_mode", "normal"))
        except ValueError:
            # 与 _parse_blend_mode 的容错口径一致：未知模式回退 normal，
            # 而不是让整份历史数据加载失败。
            target = ClipBlendMode.NORMAL
        previous = tuple(
            (cid, ClipBlendMode(mode) if mode in ClipBlendMode._value2member_map_ else ClipBlendMode.NORMAL)
            for cid, mode in (data.get("_previous_modes") or ())
        )
        return SetClipBlendModeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Blend Mode"),
            clip_ids=tuple(data.get("clip_ids", ())),
            blend_mode=target,
            _previous_modes=previous,
        )

    @staticmethod
    def create(clip_ids, blend_mode) -> "SetClipBlendModeCommand":
        ids = tuple(clip_ids)
        return SetClipBlendModeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Blend Mode ({getattr(blend_mode, 'value', blend_mode)}) on {len(ids)} clip(s)",
            clip_ids=ids,
            blend_mode=blend_mode,
        )


# ================================================================
# B7: ReplaceClipAssetCommand（占位符替换）
# ================================================================

@dataclass(frozen=True)
class ReplaceClipAssetCommand:
    """
    用真实素材替换时间线上的占位片段（B7）。

    保留 timeline 位置/时长/速度/链接关系/效果，仅替换 asset_id；
    真素材源时长短于占位时长时按真素材时长截断（truncated 标记）。
    占位素材本身不被删除（可撤销后复用）。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_id: 目标片段（占位片段）
        track_id: 所在轨道
        new_asset_id: 真实素材 ID
        _previous_asset_id: 原占位 asset_id（undo 用）
        _truncated: 真素材较短导致截断时置位（apply 时写）
    """

    command_id: str
    label: str
    clip_id: str
    track_id: str
    new_asset_id: str
    # B7: 真实素材较短时把片段截断到该源时长（帧），避免 source_duration
    # 超出真素材范围导致解码请求越界帧。None = 不截断。
    clamp_source_duration: int | None = None
    _previous_asset_id: str | None = None
    _truncated: bool = False
    _original_source_duration: int | None = None
    _original_timeline_duration: int | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        object.__setattr__(self, "_previous_asset_id", clip.asset_id)
        object.__setattr__(self, "_original_source_duration", clip.source_duration)
        object.__setattr__(self, "_original_timeline_duration", clip.timeline_duration)

        # 只换素材指针；真素材较短时按其时长截断（保留位置/速度/链接/
        # 效果，timeline_duration 按基础速度折算）。
        updates: dict[str, Any] = {"asset_id": self.new_asset_id}
        clamp = self.clamp_source_duration
        if clamp is not None and 0 < int(clamp) < clip.source_duration:
            new_source = int(clamp)
            speed = abs(clip.effective_speed()) or 1.0
            updates["source_duration"] = new_source
            updates["timeline_duration"] = max(1, int(round(new_source / speed)))
            object.__setattr__(self, "_truncated", True)
        new_clip = replace(clip, **updates)
        new_clips = tuple(new_clip if c.clip_id == clip.clip_id else c for c in track.clips)
        new_track = track._rebuild(clips=new_clips)
        return timeline.with_track_updated(new_track)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous_asset_id is None:
            raise ValueError("Cannot undo: command was never applied")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        # 还原 asset 与（可能被截断的）时长——截断改过 source/timeline
        # duration，只换回 asset_id 会留下缩短后的时长。
        restored = replace(
            clip,
            asset_id=self._previous_asset_id,
            source_duration=self._original_source_duration
            if self._original_source_duration is not None else clip.source_duration,
            timeline_duration=self._original_timeline_duration
            if self._original_timeline_duration is not None else clip.timeline_duration,
        )
        new_clips = tuple(restored if c.clip_id == clip.clip_id else c for c in track.clips)
        new_track = track._rebuild(clips=new_clips)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ReplaceClipAssetCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "new_asset_id": self.new_asset_id,
            "clamp_source_duration": self.clamp_source_duration,
            "_previous_asset_id": self._previous_asset_id,
            "_original_source_duration": self._original_source_duration,
            "_original_timeline_duration": self._original_timeline_duration,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ReplaceClipAssetCommand":
        return ReplaceClipAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Replace Clip Asset"),
            clip_id=data.get("clip_id", ""),
            track_id=data.get("track_id", ""),
            new_asset_id=data.get("new_asset_id", ""),
            clamp_source_duration=data.get("clamp_source_duration"),
            _previous_asset_id=data.get("_previous_asset_id"),
            _original_source_duration=data.get("_original_source_duration"),
            _original_timeline_duration=data.get("_original_timeline_duration"),
        )

    @staticmethod
    def create(
        clip_id: str, track_id: str, new_asset_id: str,
        clamp_source_duration: int | None = None,
    ) -> "ReplaceClipAssetCommand":
        return ReplaceClipAssetCommand(
            command_id=str(uuid.uuid4()),
            label=f"Replace clip {clip_id} with asset {new_asset_id}",
            clip_id=clip_id,
            track_id=track_id,
            new_asset_id=new_asset_id,
            clamp_source_duration=clamp_source_duration,
        )


# ================================================================
# W01: ReplaceClipWithSourceCommand（Resolve 风格 Replace Edit）
# ================================================================

@dataclass(frozen=True)
class ReplaceClipWithSourceCommand:
    """按源对齐帧替换片段素材（W01，接线计划契约 D1）。

    与 ``ReplaceClipAssetCommand``（占位片段替换素材指针、可截短）职责
    不同：本命令实现 Source Viewer/时间线定位帧对齐的 Replace Edit——
    保留 clip_id、track、timeline_start/duration、speed、效果/动画/淡变/
    label/链接等全部字段，仅变更：

    - ``asset_id`` → 新素材；
    - ``source_start`` → 源对齐帧（Source Viewer In，源 fps 域）；
    - ``source_duration`` → 按目标 timeline_duration × 有效速度换算的
      源帧数（含混合帧率 fps 比）；
    - fps 时基元数据（``_source_fps_num/_den``、``_timeline_fps_num/_den``、
      ``_source_pts_index``、``_source_frame_phase_*``）→ 新素材时基，避免
      换素材后沿用旧素材帧率导致映射错位；
    - ``freeze_source_frame``（B04）：冻结片整段只播一帧，冻结帧重映射
      到新源对齐帧 ``new_source_start``——旧素材的绝对帧引用不跨素材
      保留；undo 快照记录原冻结帧。

    源不足预检（可用源帧 < new_source_duration）由调用方完成；命令层只
    保证写入值非负且 source_duration > 0。undo 完整还原上述全部字段。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_id: 目标片段
        track_id: 所在轨道
        new_asset_id: 新素材 ID
        new_source_start: 源对齐帧（inclusive，源 fps 域）
        new_source_duration: 源侧消耗帧数
        new_fps_meta: 新素材时基 (s_num, s_den, t_num, t_den, pts_index|None)
        new_phase: CFR 采样相位 (num, den)，缺省 (0, 1)
        _snapshot: apply 期捕获的 (asset_id, source_start, source_duration,
            fps_meta, phase) 五元组，undo 还原
    """

    command_id: str
    label: str
    clip_id: str
    track_id: str
    new_asset_id: str
    new_source_start: int
    new_source_duration: int
    new_fps_meta: tuple | None = None
    new_phase: tuple = (0, 1)
    _snapshot: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        assert_track_editable(
            timeline, self.track_id, "Replace edit")
        if self.new_source_start < 0:
            raise ValueError(
                f"Replace source start cannot be negative: "
                f"{self.new_source_start}")
        if self.new_source_duration <= 0:
            raise ValueError(
                f"Replace source duration must be positive: "
                f"{self.new_source_duration}")

        object.__setattr__(self, "_snapshot", (
            clip.asset_id, clip.source_start, clip.source_duration,
            (clip._source_fps_num, clip._source_fps_den,
             clip._timeline_fps_num, clip._timeline_fps_den,
             clip._source_pts_index),
            (clip._source_frame_phase_num, clip._source_frame_phase_den),
            clip.freeze_source_frame,
        ))

        s_num, s_den, t_num, t_den, pts = self._resolved_fps_meta(clip)
        phase_num, phase_den = self.new_phase
        new_clip = replace(
            clip,
            asset_id=self.new_asset_id,
            source_start=self.new_source_start,
            source_duration=self.new_source_duration,
            _source_fps_num=s_num,
            _source_fps_den=s_den,
            _timeline_fps_num=t_num,
            _timeline_fps_den=t_den,
            _source_pts_index=pts,
            _source_frame_phase_num=phase_num,
            _source_frame_phase_den=phase_den,
        )
        # B04：冻结语义重映射——冻结片整段只播一帧，冻结帧 = 新源
        # 对齐帧。旧素材的绝对帧引用（freeze_source_frame）对新窗口
        # 无意义，不得沿用；undo 快照记录原值完整还原。
        if clip.freeze_source_frame is not None:
            new_clip = new_clip.with_freeze_frame(self.new_source_start)
        new_clips = tuple(
            new_clip if c.clip_id == clip.clip_id else c for c in track.clips)
        new_track = track._rebuild(clips=new_clips)
        return timeline.with_track_updated(new_track)

    def _resolved_fps_meta(self, clip: ClipInstance) -> tuple:
        """Merge explicit new metadata with clip-local timeline fps fallbacks."""
        if self.new_fps_meta is None:
            return (
                clip._source_fps_num, clip._source_fps_den,
                clip._timeline_fps_num, clip._timeline_fps_den,
                clip._source_pts_index,
            )
        s_num, s_den, t_num, t_den, pts = self.new_fps_meta
        if t_num is None:
            t_num, t_den = clip._timeline_fps_num, clip._timeline_fps_den
        return (s_num, s_den, t_num, t_den, pts)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._snapshot is None:
            raise ValueError("Cannot undo: command was never applied")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        (asset_id, source_start, source_duration, fps_meta, phase,
         *freeze_rest) = self._snapshot
        s_num, s_den, t_num, t_den, pts = fps_meta
        phase_num, phase_den = phase
        restored = replace(
            clip,
            asset_id=asset_id,
            source_start=source_start,
            source_duration=source_duration,
            _source_fps_num=s_num,
            _source_fps_den=s_den,
            _timeline_fps_num=t_num,
            _timeline_fps_den=t_den,
            _source_pts_index=pts,
            _source_frame_phase_num=phase_num,
            _source_frame_phase_den=phase_den,
        )
        # B04：undo 还原冻结帧；旧序列化 5 元快照没有该位，保持现状
        if freeze_rest:
            restored = restored.with_freeze_frame(freeze_rest[0])
        new_clips = tuple(
            restored if c.clip_id == clip.clip_id else c for c in track.clips)
        new_track = track._rebuild(clips=new_clips)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ReplaceClipWithSourceCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "new_asset_id": self.new_asset_id,
            "new_source_start": self.new_source_start,
            "new_source_duration": self.new_source_duration,
            "new_fps_meta": _tuple_or_none(self.new_fps_meta),
            "new_phase": self.new_phase,
            "_snapshot": _tuple_or_none(self._snapshot),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ReplaceClipWithSourceCommand":
        return ReplaceClipWithSourceCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Replace Clip With Source"),
            clip_id=data.get("clip_id", ""),
            track_id=data.get("track_id", ""),
            new_asset_id=data.get("new_asset_id", ""),
            new_source_start=data.get("new_source_start", 0),
            new_source_duration=data.get("new_source_duration", 0),
            new_fps_meta=_nested_tuple_or_none(data.get("new_fps_meta")),
            new_phase=tuple(data.get("new_phase", (0, 1))),
            _snapshot=_nested_tuple_or_none(data.get("_snapshot")),
        )

    @staticmethod
    def create(
        clip_id: str,
        track_id: str,
        new_asset_id: str,
        new_source_start: int,
        new_source_duration: int,
        new_fps_meta: tuple | None = None,
        new_phase: tuple = (0, 1),
    ) -> "ReplaceClipWithSourceCommand":
        return ReplaceClipWithSourceCommand(
            command_id=str(uuid.uuid4()),
            label=f"Replace edit: clip {clip_id} ← asset {new_asset_id}",
            clip_id=clip_id,
            track_id=track_id,
            new_asset_id=new_asset_id,
            new_source_start=new_source_start,
            new_source_duration=new_source_duration,
            new_fps_meta=new_fps_meta,
            new_phase=new_phase,
        )


# ================================================================
# W05: SetClipFlagCommand（clip 级彩色旗标，契约 D4）
# ================================================================

@dataclass(frozen=True)
class SetClipFlagCommand:
    """设置/清除一组 clip 的彩色旗标（接线计划 W05 / 契约 D4）。

    多选批量：``targets`` 全部应用同一颜色，一条命令单步 undo。旗标是
    clip 级独立数据（``flag_color``），不改 ``label_color``（配色）也不
    改 ``MediaAsset.flag``（素材级 reject 标记）；不随 linked 伙伴传播。
    全部目标已是目标色时 apply 为 no-op（CommandStack 不入栈不清 redo）。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        targets: ((track_id, clip_id), ...) 元组
        flag_color: "#RRGGBB" 白名单色或 None（清除）
        _previous: apply 期捕获的 ((track_id, clip_id, 原色), ...) 快照；
            只记录实际变更的目标（no-op 目标不进快照、undo 不触碰），
            身份用 track+clip 双键（B01）
    """

    command_id: str
    label: str
    targets: tuple
    flag_color: str | None
    _previous: tuple | None = None

    @staticmethod
    def _validate_flag_color(flag_color: str | None) -> None:
        """B08：新写统一 validator——白名单色或 None。

        与 UI（``TimelineView.set_clip_flags``）、Flags 菜单共用
        ``CLIP_FLAG_COLORS`` 单一真值；历史工程里已存在的未知值在模型
        读取层保留，不经本命令重写。白名单外新写（含格式合法的
        "#zzzzzz"）在命令边界拒绝，防止 AI/脚本路径落盘非法内容。
        """
        from src.features.edit.core.timeline_v2 import CLIP_FLAG_COLORS

        if flag_color is not None and flag_color not in CLIP_FLAG_COLORS:
            raise ValueError(
                f"Invalid clip flag color: {flag_color!r}; expected one of "
                f"{CLIP_FLAG_COLORS} or None")

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        self._validate_flag_color(self.flag_color)
        # 第一遍：目标存在性 + 轨锁校验 + 是否真有变化（no-op 判定）
        pending: list[tuple[str, str, ClipInstance]] = []
        for track_id, clip_id in self.targets:
            track = timeline.get_track_by_id(track_id)
            if track is None:
                raise ValueError(f"Track not found: {track_id}")
            clip = track.get_clip_by_id(clip_id)
            if clip is None:
                raise ValueError(f"Clip not found: {clip_id}")
            assert_track_editable(timeline, track_id, "Set clip flag")
            if clip.flag_color != self.flag_color:
                pending.append((track_id, clip_id, clip))
        if not pending:
            # 全部目标已是目标色：no-op 返回同一实例（CommandStack 不入栈）
            return timeline
        previous: list[tuple[str, str, str | None]] = []
        result = timeline
        for track_id, clip_id, clip in pending:
            track = result.get_track_by_id(track_id)
            # B01：快照记 track+clip 双键与原色；只含实际变更项，
            # 混合 no-op 批次 undo 时不会把"本来就已是目标色"的
            # clip 清成 None。
            previous.append((track_id, clip_id, clip.flag_color))
            new_clip = clip.with_flag_color(self.flag_color)
            new_clips = tuple(
                new_clip if c.clip_id == clip_id else c for c in track.clips)
            result = result.with_track_updated(
                track._rebuild(clips=new_clips))
        object.__setattr__(self, "_previous", tuple(previous))
        return result

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous is None:
            raise ValueError("Cannot undo: command was never applied")
        result = timeline
        for entry in self._previous:
            # 新格式 (track_id, clip_id, 原色)；旧序列化只有
            # (clip_id, 原色) 二元组——按 clip_id 全轨扫描兜底。
            if len(entry) == 3:
                track_id, clip_id, flag_color = entry
            else:
                clip_id, flag_color = entry
                track_id = None
            track = (result.get_track_by_id(track_id)
                     if track_id is not None else None)
            if track is None:
                for candidate in (list(result.video_tracks)
                                  + list(result.audio_tracks)):
                    if candidate.get_clip_by_id(clip_id) is not None:
                        track = candidate
                        break
            if track is None:
                raise ValueError(f"Track not found for clip: {clip_id}")
            clip = track.get_clip_by_id(clip_id)
            if clip is None:
                raise ValueError(f"Clip not found: {clip_id}")
            new_clip = clip.with_flag_color(flag_color)
            new_clips = tuple(
                new_clip if c.clip_id == clip_id else c for c in track.clips)
            result = result.with_track_updated(
                track._rebuild(clips=new_clips))
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetClipFlagCommand",
            "label": self.label,
            "targets": [
                {"track_id": track_id, "clip_id": clip_id}
                for track_id, clip_id in self.targets
            ],
            "flag_color": self.flag_color,
            "_previous": [
                {"track_id": track_id, "clip_id": clip_id,
                 "flag_color": flag_color}
                for track_id, clip_id, flag_color in (self._previous or ())
            ] or None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetClipFlagCommand":
        targets = tuple(
            (str(item["track_id"]), str(item["clip_id"]))
            for item in data.get("targets", [])
        )
        previous = tuple(
            (str(item["track_id"]), str(item["clip_id"]),
             item.get("flag_color"))
            if item.get("track_id") is not None
            else (str(item["clip_id"]), item.get("flag_color"))
            for item in (data.get("_previous") or [])
        )
        revived = SetClipFlagCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Clip Flag"),
            targets=targets,
            flag_color=data.get("flag_color"),
            _previous=previous or None,
        )
        # B08：序列化恢复同样过新写校验——命令边界之外没有第二条
        # 合法入口；非法载荷明确失败优于静默执行。
        revived._validate_flag_color(revived.flag_color)
        return revived

    @staticmethod
    def create(
        targets: tuple | list, flag_color: str | None,
        label: str | None = None,
    ) -> "SetClipFlagCommand":
        # B08：构造即校验——服务/AI 直接 create 的路径与 UI 同规则。
        SetClipFlagCommand._validate_flag_color(flag_color)
        targets = tuple((track_id, clip_id) for track_id, clip_id in targets)
        if flag_color is None:
            text = "Clear clip flags"
        else:
            text = "Flag clips"
        return SetClipFlagCommand(
            command_id=str(uuid.uuid4()),
            label=label or f"{text} ({len(targets)})",
            targets=targets,
            flag_color=flag_color,
        )


# ================================================================
# C3: SetClipAudioGainCommand（片段级音频增益，阶段 1）
# ================================================================

@dataclass(frozen=True)
class SetClipAudioGainCommand:
    """
    设置片段级音频增益（dB）——对齐 Premiere Audio Gain。

    多选批量：clip_ids 全部应用同一增益，一步撤销。
    混音顺序：clip gain → clip 包络 → track automation → track volume → bus。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_ids: 目标片段 ID 元组
        gain_db: 目标增益（clamp [-48, +12]）
        _previous_gains: 原增益快照（undo 用）
    """

    command_id: str
    label: str
    clip_ids: tuple
    gain_db: float
    _previous_gains: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        gain = max(-48.0, min(12.0, float(self.gain_db)))
        previous: list[tuple[str, float]] = []
        new_timeline = timeline
        for clip_id in self.clip_ids:
            track = None
            clip = None
            for t in new_timeline.all_tracks:
                c = t.get_clip_by_id(clip_id)
                if c is not None:
                    track, clip = t, c
                    break
            if clip is None:
                continue
            previous.append((clip_id, float(clip.audio_gain_db)))
            updated = clip.with_audio_gain_db(gain)
            new_clips = tuple(updated if c.clip_id == clip_id else c for c in track.clips)
            new_timeline = new_timeline.with_track_updated(track._rebuild(clips=new_clips))
        object.__setattr__(self, "_previous_gains", tuple(previous))
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous_gains is None:
            raise ValueError("Cannot undo: command was never applied")
        new_timeline = timeline
        for clip_id, old_gain in self._previous_gains:
            track = None
            clip = None
            for t in new_timeline.all_tracks:
                c = t.get_clip_by_id(clip_id)
                if c is not None:
                    track, clip = t, c
                    break
            if clip is None:
                continue
            updated = clip.with_audio_gain_db(old_gain)
            new_clips = tuple(updated if c.clip_id == clip_id else c for c in track.clips)
            new_timeline = new_timeline.with_track_updated(track._rebuild(clips=new_clips))
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetClipAudioGainCommand",
            "label": self.label,
            "clip_ids": list(self.clip_ids),
            "gain_db": self.gain_db,
            "_previous_gains": list(self._previous_gains or ()),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetClipAudioGainCommand":
        return SetClipAudioGainCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Clip Audio Gain"),
            clip_ids=tuple(data.get("clip_ids", ())),
            gain_db=float(data.get("gain_db", 0.0)),
            _previous_gains=tuple(
                (cid, float(g)) for cid, g in (data.get("_previous_gains") or ())
            ),
        )

    @staticmethod
    def create(clip_ids, gain_db: float) -> "SetClipAudioGainCommand":
        ids = tuple(clip_ids)
        return SetClipAudioGainCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set clip gain {gain_db:+.1f} dB on {len(ids)} clip(s)",
            clip_ids=ids,
            gain_db=float(gain_db),
        )


# ================================================================
# F3.3 · 采样级音频编辑（子帧位置/源侧子帧修剪/采样域淡化）
# ================================================================

@dataclass(frozen=True)
class UpdateClipSampleFieldsCommand:
    """F3.3：采样级字段事务（nudge/slip/fade 命令的公共底座）。

    音频 clip 的采样域字段（audio_sample_offset / source_sample_offset /
    fade_in_samples / fade_out_samples）以整 clip before/after 快照原子
    更新；undo 一次恢复。位置语义见 core/sample_time.py（有理数时钟，
    非整数帧率下不做帧进位归一化——任何进位都引入有理数残差错位）。
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    _before: Any = None   # ClipInstance 快照（apply 时填充）
    _after: Any = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        if self._after is None:
            raise ValueError("sample-fields command not prepared")
        updated_cmd = UpdateClipSampleFieldsCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, clip_id=self.clip_id,
            _before=clip, _after=self._after)
        new_track = track.with_clip_updated(self._after)
        return timeline.with_track_updated(new_track), updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._before is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        return timeline.with_track_updated(
            track.with_clip_updated(self._before))

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateClipSampleFieldsCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "before": self._before.to_dict() if self._before else None,
            "after": self._after.to_dict() if self._after else None,
        }

    @staticmethod
    def from_dict(data: dict) -> "UpdateClipSampleFieldsCommand":
        return UpdateClipSampleFieldsCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Sample-level edit"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            _before=ClipInstance.from_dict(data["before"])
            if data.get("before") else None,
            _after=ClipInstance.from_dict(data["after"])
            if data.get("after") else None,
        )

    @staticmethod
    def create(
        track_id: str,
        clip: ClipInstance,
        *,
        audio_sample_offset: int | None = None,
        source_sample_offset: int | None = None,
        fade_in_samples: int | None = None,
        fade_out_samples: int | None = None,
        label: str = "Sample-level edit",
    ) -> "UpdateClipSampleFieldsCommand":
        """按字段差构造命令；未提供的字段沿用 clip 现值。"""
        updated = replace(
            clip,
            audio_sample_offset=clip.audio_sample_offset
            if audio_sample_offset is None else int(audio_sample_offset),
            source_sample_offset=clip.source_sample_offset
            if source_sample_offset is None else int(source_sample_offset),
            fade_in_samples=clip.fade_in_samples
            if fade_in_samples is None else (
                int(fade_in_samples) if fade_in_samples >= 0 else None),
            fade_out_samples=clip.fade_out_samples
            if fade_out_samples is None else (
                int(fade_out_samples) if fade_out_samples >= 0 else None),
        )
        return UpdateClipSampleFieldsCommand(
            command_id=str(uuid.uuid4()), label=label,
            track_id=track_id, clip_id=clip.clip_id, _after=updated)
