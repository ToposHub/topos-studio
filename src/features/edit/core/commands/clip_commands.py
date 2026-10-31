"""Clip basic-operation commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: clip_basic cluster — the clip lifecycle
commands (add / delete / move / move-to-track / copy / overwrite-move / trim /
slip / slide / split / insert / overwrite / roll trim / ripple trim / link).
Cluster-private helpers (_move_scoped_markers_with_clip,
_assert_caption_graphic_track_destination, _split_clip_transitions_for_move)
moved with the cluster; the shared snapshot deserializers (_clips_from_dicts,
_transitions_from_dicts) and the track-lock guard assert_track_editable live
in _support.py. This module must never import the infra stacks
(MacroCommand / CommandStack) — the dependency direction is
_support -> clip_commands, with insert/props/infra clusters re-exported via
the package __init__. Class sources are byte-identical to the original
module; to_dict/from_dict keys are a persistence contract — do not rename.
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
    TransitionInstance,
    TimelineMarker,
)

from ._support import _clips_from_dicts, _transitions_from_dicts, assert_track_editable

logger = logging.getLogger(__name__)

def _move_scoped_markers_with_clip(
    timeline: TimelineSequence,
    clip_id: str,
    old_start: int,
    new_start: int,
    target_track_id: str | None = None,
) -> TimelineSequence:
    """Keep markers scoped to a clip at the same relative position.

    Clip-scoped markers store an absolute timeline frame, so moving the clip
    must translate their frame by the same delta.  Cross-track moves also
    update the marker's track scope so its visual anchor follows the clip.
    """
    delta = new_start - old_start
    updated_markers: list[TimelineMarker] = []
    changed = False

    for marker in timeline.markers:
        if marker.clip_id != clip_id:
            updated_markers.append(marker)
            continue

        updated_marker = replace(
            marker,
            frame=max(0, marker.frame + delta),
            track_id=(
                marker.track_id
                if target_track_id is None
                else target_track_id
            ),
        )
        updated_markers.append(updated_marker)
        changed = changed or updated_marker != marker

    if not changed:
        return timeline
    return timeline._rebuild(markers=tuple(updated_markers))


def _assert_caption_graphic_track_destination(
    timeline: TimelineSequence,
    clip_id: str,
    target_track: EditTrack,
) -> None:
    """Keep bound caption graphics on a graphics-role video track."""
    binding = timeline.get_caption_graphic_binding_by_clip(clip_id)
    if binding is None:
        return
    if target_track.track_type != "video" or target_track.track_role != "graphics":
        raise ValueError(
            f"Caption graphic clip {clip_id} must stay on a graphics track"
        )


# ================================================================
# AddClipCommand
# ================================================================

@dataclass(frozen=True)
class AddClipCommand:
    """
    Command to add a clip to timeline.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Target track ID
        clip: Clip to add
        _previous_timeline_id: Internal: timeline ID before apply (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip: ClipInstance
    _previous_timeline_id: str | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: add clip to track.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with clip added

        Raises:
            ValueError: If track not found or clip overlaps
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Check for overlap
        for existing in track.clips:
            if (self.clip.timeline_start < existing.timeline_end and
                self.clip.timeline_end > existing.timeline_start):
                raise ValueError(
                    f"New clip overlaps with {existing.clip_id} "
                    f"at [{existing.timeline_start}, {existing.timeline_end})"
                )

        # Create new clip with correct track_id while preserving timing
        # metadata used by the precise timeline frame mapper.
        clip_with_track = self.clip.with_track(self.track_id)

        new_track = track.with_clip_added(clip_with_track)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"AddClipCommand applied: {self.clip.clip_id} to track {self.track_id}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove the clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the clip
        """
        new_timeline = timeline.with_clip_removed(self.clip.clip_id)
        logger.debug(f"AddClipCommand undone: removed {self.clip.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "AddClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip": self.clip.to_dict(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddClipCommand:
        """Deserialize command."""
        return AddClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Clip"),
            track_id=data.get("track_id", ""),
            clip=ClipInstance.from_dict(data.get("clip", {})),
        )

    @staticmethod
    def create(track_id: str, clip: ClipInstance) -> AddClipCommand:
        """Factory method to create command."""
        return AddClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add Clip at {clip.timeline_start}",
            track_id=track_id,
            clip=clip,
        )


# ================================================================
# DeleteClipCommand
# ================================================================

@dataclass(frozen=True)
class DeleteClipCommand:
    """
    Command to delete a clip from timeline.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to delete
        _deleted_clip: Internal: clip data for undo (set during apply)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    _deleted_clip: ClipInstance | None = None  # Set during apply
    _deleted_markers: tuple[TimelineMarker, ...] = ()  # Scoped markers for undo
    _unlinked_partner: ClipInstance | None = None  # Partner cleared during apply
    # C11：with_clip_removed 会顺带删除引用该片段的转场，undo 此前只回
    # 片段不回转场（审计实测 delete 后转场 1→0→undo 仍 0）。快照整轨
    # 转场，undo 精确恢复。
    _before_transitions: tuple = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: delete clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the clip
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # R01：锁定轨道拒绝删除（守卫须在任何 self 回写之前，拒绝的
        # 命令实例保持可复用、不残留快照）
        assert_track_editable(timeline, self.track_id, "Delete clip")

        # Store clip for undo (we need to return a new frozen instance)
        deleted_clip = clip
        before_transitions = track.transitions  # C11 快照
        deleted_markers = tuple(
            marker for marker in timeline.markers
            if marker.clip_id == self.clip_id
        )
        unlinked_partner = (
            timeline.get_clip_by_id(clip.linked_clip_id)
            if clip.linked_clip_id else None
        )
        if (
            unlinked_partner is not None
            and unlinked_partner.linked_clip_id != clip.clip_id
        ):
            unlinked_partner = None

        new_timeline = timeline.with_clip_removed(self.clip_id)
        if deleted_markers:
            new_timeline = new_timeline._rebuild(
                markers=tuple(
                    marker for marker in new_timeline.markers
                    if marker.clip_id != self.clip_id
                )
            )

        # Return command with stored clip for undo
        # Note: Since dataclass is frozen, we need to create new instance
        return new_timeline, DeleteClipCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            _deleted_clip=deleted_clip,
            _deleted_markers=deleted_markers,
            _unlinked_partner=unlinked_partner,
            _before_transitions=before_transitions,
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore deleted clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip restored
        """
        if self._deleted_clip is None:
            raise ValueError("Cannot undo: deleted clip not stored")

        new_timeline = timeline.with_clip_added(self.track_id, self._deleted_clip)
        if self._unlinked_partner is not None:
            partner = new_timeline.get_clip_by_id(self._unlinked_partner.clip_id)
            if partner is not None:
                new_timeline = new_timeline.with_clip_updated(self._unlinked_partner)
        if self._deleted_markers:
            existing_ids = {marker.marker_id for marker in new_timeline.markers}
            restored_markers = tuple(
                marker for marker in self._deleted_markers
                if marker.marker_id not in existing_ids
            )
            new_timeline = new_timeline._rebuild(
                markers=new_timeline.markers + restored_markers
            )
        # C11：最后一步精确恢复整轨转场快照（clips/transitions 同传不走
        # 自动重锚，保持与原始状态一致）
        if self._before_transitions is not None:
            track = new_timeline.get_track_by_id(self.track_id)
            if track is not None:
                new_timeline = new_timeline.with_track_updated(
                    track._rebuild(transitions=self._before_transitions)
                )
        logger.debug(f"DeleteClipCommand undone: restored {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "DeleteClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "deleted_clip": self._deleted_clip.to_dict() if self._deleted_clip else None,
            "deleted_markers": [marker.to_dict() for marker in self._deleted_markers],
            "unlinked_partner": self._unlinked_partner.to_dict() if self._unlinked_partner else None,
            "before_transitions": (
                [t.to_dict() for t in self._before_transitions]
                if self._before_transitions else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteClipCommand:
        """Deserialize command."""
        deleted_clip_data = data.get("deleted_clip")
        deleted_clip = ClipInstance.from_dict(deleted_clip_data) if deleted_clip_data else None
        deleted_markers = tuple(
            TimelineMarker.from_dict(marker)
            for marker in data.get("deleted_markers", [])
        )
        partner_data = data.get("unlinked_partner")

        return DeleteClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Clip"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            _deleted_clip=deleted_clip,
            _deleted_markers=deleted_markers,
            _unlinked_partner=ClipInstance.from_dict(partner_data) if partner_data else None,
            _before_transitions=_transitions_from_dicts(data.get("before_transitions")),
        )

    @staticmethod
    def create(track_id: str, clip_id: str) -> DeleteClipCommand:
        """Factory method to create command."""
        return DeleteClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Delete Clip {clip_id}",
            track_id=track_id,
            clip_id=clip_id,
        )


# ================================================================
# MoveClipCommand
# ================================================================

@dataclass(frozen=True)
class MoveClipCommand:
    """
    Command to move a clip to new position.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to move
        new_timeline_start: New start position (frames)
        old_timeline_start: Old start position (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    new_timeline_start: int
    old_timeline_start: int | None = None  # Set during apply
    # C11/C12：移动把相接的双边转场分离时，重锚策略会删除该转场；
    # undo 需要整轨转场快照才能完整恢复。
    _before_transitions: tuple = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: move clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip moved
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # R01：锁定轨道拒绝移动（此前仅 4 类命令有守卫，Move 绕锁）。
        # 守卫先于 _before_transitions 快照回写，拒绝的命令实例无残留。
        assert_track_editable(timeline, self.track_id, "Move clip")

        # C11/C12：记录整轨转场快照（移动可能分离并删除双边转场）
        object.__setattr__(self, "_before_transitions", track.transitions)

        # Store old position for undo
        old_start = clip.timeline_start
        # AUD-16205：旧值必须持久化（此前仅局部变量，undo 在
        # old_timeline_start=None 时退化为 no-op）。沿用本文件既有的
        # object.__setattr__ 回写机制（MoveClipToTrackCommand._created_track_id
        # 同型）；工厂显式传入的旧值优先，不覆盖。
        if self.old_timeline_start is None:
            object.__setattr__(self, "old_timeline_start", old_start)

        # Clamp to non-negative
        clamped_start = max(0, self.new_timeline_start)

        # APP-01：no-op 移动返回原实例（提交边界据此跳过 revision bump）
        if clamped_start == clip.timeline_start:
            return timeline

        # Create moved clip（C13：关键帧随 clip 平移，局部曲线不变）
        moved_clip = clip.with_timeline_start(clamped_start).with_temporal_shift(
            clamped_start - clip.timeline_start
        )

        # Check for overlap with other clips
        other_clips = [c for c in track.clips if c.clip_id != self.clip_id]
        for existing in other_clips:
            if (moved_clip.timeline_start < existing.timeline_end and
                moved_clip.timeline_end > existing.timeline_start):
                raise ValueError(
                    f"Moved clip overlaps with {existing.clip_id} "
                    f"at [{existing.timeline_start}, {existing.timeline_end})"
                )

        new_track = track.with_clip_updated(moved_clip)
        new_timeline = timeline.with_track_updated(new_track)
        new_timeline = _move_scoped_markers_with_clip(
            new_timeline,
            self.clip_id,
            old_start,
            clamped_start,
            self.track_id,
        )

        logger.debug(
            f"MoveClipCommand applied: {self.clip_id} "
            f"from {old_start} to {self.new_timeline_start}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: move clip back.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip at original position
        """
        if self.old_timeline_start is None:
            # Get current clip and use its current position as old
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")
            clip = track.get_clip_by_id(self.clip_id)
            if clip is None:
                raise ValueError(f"Clip not found: {self.clip_id}")
            old_start = clip.timeline_start
        else:
            old_start = self.old_timeline_start

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        current_start = clip.timeline_start
        restored_clip = clip.with_timeline_start(old_start).with_temporal_shift(
            old_start - clip.timeline_start
        )
        new_track = track.with_clip_updated(restored_clip)
        new_timeline = timeline.with_track_updated(new_track)
        new_timeline = _move_scoped_markers_with_clip(
            new_timeline,
            self.clip_id,
            current_start,
            old_start,
            self.track_id,
        )
        # C11/C12：恢复移动（可能分离删除双边转场）前的整轨转场快照
        if self._before_transitions is not None:
            track = new_timeline.get_track_by_id(self.track_id)
            if track is not None:
                new_timeline = new_timeline.with_track_updated(
                    track._rebuild(transitions=self._before_transitions)
                )

        logger.debug(f"MoveClipCommand undone: {self.clip_id} back to {old_start}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_timeline_start": self.new_timeline_start,
            "old_timeline_start": self.old_timeline_start,
            # R09：C11 转场快照随载荷往返（None 与空区分）
            "before_transitions": (
                None
                if self._before_transitions is None
                else [t.to_dict() for t in self._before_transitions]
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveClipCommand:
        """Deserialize command."""
        return MoveClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Clip"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_timeline_start=data.get("new_timeline_start", 0),
            old_timeline_start=data.get("old_timeline_start"),
            _before_transitions=_transitions_from_dicts(
                data.get("before_transitions")
            ),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        new_start: int,
        old_start: int | None = None
    ) -> MoveClipCommand:
        """Factory method to create command."""
        return MoveClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Move Clip to {new_start}",
            track_id=track_id,
            clip_id=clip_id,
            new_timeline_start=new_start,
            old_timeline_start=old_start,
        )


# ================================================================
# MoveClipToTrackCommand (P4: Cross-track move with auto-create track)
# ================================================================

def _split_clip_transitions_for_move(
    transitions: tuple,
    clip_id: str,
    new_track_id: str,
    new_start: int,
    clip_duration: int,
) -> tuple:
    """C03（第六轮审计）：跨轨移动时拆分引用该片段的转场。

    - 单边淡入/淡出（只引用本片段，另一侧为空）随片段迁移到目标轨并
      重锚到新边界（head → center=new_start，tail → center=new_start+
      duration）。
    - 双边转场（引用本片段与邻片）不迁移——邻片留在源轨，跨轨后不再
      相邻；undo 由整轨快照恢复。

    Returns:
        (migrated, dropped)：migrated 为重锚后应加到目标轨的转场；
        dropped 为因移动而失效的双边转场（供诊断/日志）。
    """
    migrated: list[TransitionInstance] = []
    dropped: list[TransitionInstance] = []
    for t in transitions:
        left_ref = t.left_clip_id == clip_id
        right_ref = t.right_clip_id == clip_id
        if not left_ref and not right_ref:
            continue
        if left_ref != right_ref and not (t.left_clip_id and t.right_clip_id):
            # 单边：head（right 引用）锚到新片段起始；tail（left 引用）
            # 锚到新片段结尾。
            center = new_start if right_ref else new_start + clip_duration
            migrated.append(replace(t, track_id=new_track_id, center_frame=center))
        else:
            # 双边（或 left==right==clip 的畸形数据）：邻片关系随移动断开
            dropped.append(t)
    return tuple(migrated), tuple(dropped)


@dataclass(frozen=True)
class MoveClipToTrackCommand:
    """
    P4: Command to move a clip to a different track.

    Used when dragging clips beyond track boundaries to auto-create new tracks.
    Handles both track creation (if needed) and clip transfer.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        source_track_id: Original track containing the clip
        clip_id: Clip ID to move
        target_track_type: 'video' or 'audio' (type of track to create/use)
        create_new_track: Whether to create a new track (True for auto-create)
        new_track_position: Position for new track ('above' or 'below' existing)
        new_timeline_start: New start position (frames)
        old_timeline_start: Old start position (for undo)
        _created_track_id: ID of created track (set during apply, if create_new_track)
    """

    command_id: str
    label: str
    source_track_id: str
    clip_id: str
    target_track_type: str
    create_new_track: bool = False
    new_track_position: str = "above"  # 'above' for video, 'below' for audio
    new_timeline_start: int = 0
    old_timeline_start: int | None = None
    # P0-04: destination for moves onto an existing track (ignored when
    # create_new_track=True; required otherwise).
    target_track_id: str | None = None
    _created_track_id: str | None = None
    # C02（第六轮审计）：跨轨移动的时域平移量（undo 对称反向平移）
    _temporal_delta: int | None = None
    # C03（第六轮审计）：源/目标轨转场快照——with_clip_removed 会顺带删除
    # 引用片段的转场，undo 此前只回片段不回转场（实测 1→0→undo 仍 0）。
    _source_transitions_before: tuple | None = None
    _target_transitions_before: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply: create track (if needed) and move clip."""
        import logging
        logger = logging.getLogger(__name__)

        # Get source track info
        source_track = timeline.get_track_by_id(self.source_track_id)
        if source_track is None:
            raise ValueError(f"Source track not found: {self.source_track_id}")

        clip = source_track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old position for undo
        old_start = clip.timeline_start
        # AUD-16205：持久化旧起始帧（此前仅局部变量；新建轨路径的 undo
        # 在 old_timeline_start=None 时把片段回退到第 0 帧而非原位）。
        # 与下方 _created_track_id 同用 object.__setattr__ 回写机制。
        if self.old_timeline_start is None:
            object.__setattr__(self, "old_timeline_start", old_start)

        # C02：跨轨移动与同轨 MoveClipCommand 一致——clip/effect 动画关键帧
        # 随时间位置整体平移，保持 clip-local 曲线不变（旧代码只改
        # timeline_start，关键帧留在原绝对帧位，片头透明度等语义漂移）。
        clamped_start = max(0, self.new_timeline_start)
        temporal_delta = clamped_start - old_start
        if self._temporal_delta is None:
            object.__setattr__(self, "_temporal_delta", temporal_delta)
        shifted_clip = clip.with_temporal_shift(temporal_delta)

        # C03：源轨转场快照（含引用本片段的单/双边转场，undo 精确恢复）
        if self._source_transitions_before is None:
            object.__setattr__(
                self, "_source_transitions_before", _copy.deepcopy(source_track.transitions))

        # Determine target track
        if self.create_new_track:
            bound_graphic = timeline.get_caption_graphic_binding_by_clip(self.clip_id)
            if bound_graphic is not None and self.target_track_type != "video":
                raise ValueError("Caption graphic clips can only move to video tracks")
            # Create new track
            track_id = str(uuid.uuid4())
            object.__setattr__(self, '_created_track_id', track_id)

            existing_count = len(
                timeline.video_tracks if self.target_track_type == "video"
                else timeline.audio_tracks
            )

            # Position interpretation:
            # - Video tracks: 'above' (visual top) → append to end (highest number)
            # - Audio tracks: 'below' (visual bottom) → append to end (highest number)
            if self.target_track_type == "video" and self.new_track_position == "above":
                insert_pos = existing_count  # Append to create V4 above V3
            elif self.target_track_type == "audio" and self.new_track_position == "below":
                insert_pos = existing_count  # Append to create A4 below A3
            else:
                insert_pos = existing_count  # Default: append

            track_name = (
                f"V{insert_pos + 1}" if self.target_track_type == "video"
                else f"A{insert_pos + 1}"
            )

            new_track = EditTrack(
                track_id=track_id,
                name=track_name,
                track_type=self.target_track_type,
                index=insert_pos,
                clips=(),
                transitions=(),
                is_enabled=True,
                is_locked=False,
                is_muted=False,
                is_solo=False,
                volume=1.0,
                track_role="graphics" if bound_graphic is not None else "standard",
            )

            # Create moved clip with new track_id（C02：动画关键帧已随
            # temporal_delta 平移到 shifted_clip）
            moved_clip = shifted_clip.with_track(track_id).with_timeline_start(clamped_start)

            # C03：单边淡入/淡出随片段迁移到新轨并重锚；双边丢弃（undo 恢复）
            migrated_transitions, dropped_transitions = _split_clip_transitions_for_move(
                source_track.transitions, self.clip_id, track_id,
                clamped_start, clip.timeline_duration)
            if dropped_transitions:
                logger.info(
                    "MoveClipToTrackCommand: %d 个双边转场随跨轨移动失效（undo 恢复）",
                    len(dropped_transitions))

            # Add clip to new track
            new_track = new_track.with_clip_added(moved_clip)
            if migrated_transitions:
                new_track = new_track._rebuild(
                    transitions=tuple(migrated_transitions))

            # Update timeline with new track
            if self.target_track_type == "video":
                new_video_tracks = list(timeline.video_tracks)
                new_video_tracks.insert(insert_pos, new_track)
                # ``replace`` preserves fields added after this command was
                # originally written (for example subtitle clips and track
                # visibility).  Rebuild only the changed collection so other
                # timeline state, including subtitle_tracks, is retained.
                new_video_tracks = [replace(t, index=i) for i, t in enumerate(new_video_tracks)]
                new_timeline = timeline._rebuild(video_tracks=tuple(new_video_tracks))
            else:
                new_audio_tracks = list(timeline.audio_tracks)
                new_audio_tracks.insert(insert_pos, new_track)
                new_audio_tracks = [replace(t, index=i) for i, t in enumerate(new_audio_tracks)]
                new_timeline = timeline._rebuild(audio_tracks=tuple(new_audio_tracks))
        else:
            # P0-04: move onto an existing track. Cross-type moves, locked
            # tracks and overlaps are rejected with actionable errors — the
            # destructive (overwrite) variant is MoveClipsOverwriteCommand.
            track_id = self._resolve_existing_target_track(timeline, source_track, clip)
            target_track = timeline.get_track_by_id(track_id)
            assert target_track is not None  # resolved above

            clamped_start = max(0, self.new_timeline_start)
            new_end = clamped_start + clip.timeline_duration
            for other in target_track.clips:
                if clamped_start < other.timeline_end and new_end > other.timeline_start:
                    raise ValueError(
                        f"Move would overlap clip '{other.clip_id}' on target "
                        f"track {track_id} (use overwrite semantics to replace it)"
                    )

            # C03：目标轨转场快照（undo 恢复目标轨原状）
            if self._target_transitions_before is None:
                object.__setattr__(
                    self, "_target_transitions_before",
                    _copy.deepcopy(target_track.transitions))

            moved_clip = shifted_clip.with_track(track_id).with_timeline_start(clamped_start)
            new_target_track = target_track.with_clip_added(moved_clip)

            # C03：单边淡入/淡出随片段迁移并重锚；双边丢弃（undo 恢复）
            migrated_transitions, dropped_transitions = _split_clip_transitions_for_move(
                source_track.transitions, self.clip_id, track_id,
                clamped_start, clip.timeline_duration)
            if dropped_transitions:
                logger.info(
                    "MoveClipToTrackCommand: %d 个双边转场随跨轨移动失效（undo 恢复）",
                    len(dropped_transitions))
            if migrated_transitions:
                new_target_track = new_target_track._rebuild(
                    transitions=new_target_track.transitions + tuple(migrated_transitions))

            new_timeline = timeline.with_track_updated(new_target_track)

        # Remove clip from source track
        source_track_after = new_timeline.get_track_by_id(self.source_track_id)
        if source_track_after:
            new_timeline = new_timeline.with_track_updated(
                source_track_after.with_clip_removed(self.clip_id)
            )

        new_timeline = _move_scoped_markers_with_clip(
            new_timeline,
            self.clip_id,
            old_start,
            clamped_start,
            track_id,
        )

        logger.debug(
            f"MoveClipToTrackCommand applied: {self.clip_id} "
            f"from {self.source_track_id} to new track ({self.target_track_type}), "
            f"position {old_start} to {self.new_timeline_start}"
        )
        return new_timeline

    def _resolve_existing_target_track(
        self,
        timeline: TimelineSequence,
        source_track: "EditTrack",
        clip: "ClipInstance",
    ) -> str:
        """Validate an existing-track destination and return its track id.

        Raises:
            ValueError: target missing/unknown, same track, cross-type move,
                locked source/target track, or a caption-graphic clip heading
                to a non-video track.
        """
        if not self.target_track_id:
            raise ValueError(
                "MoveClipToTrackCommand requires target_track_id when "
                "create_new_track=False"
            )
        if self.target_track_id == self.source_track_id:
            raise ValueError(
                "Target track must differ from source track "
                f"({self.source_track_id})"
            )
        target_track = timeline.get_track_by_id(self.target_track_id)
        if target_track is None:
            raise ValueError(f"Target track not found: {self.target_track_id}")
        if target_track.track_type != source_track.track_type:
            raise ValueError(
                f"Cannot move {source_track.track_type} clip to "
                f"{target_track.track_type} track"
            )
        if (
            self.target_track_type
            and self.target_track_type != target_track.track_type
        ):
            raise ValueError(
                f"target_track_type {self.target_track_type} does not match "
                f"actual target track type {target_track.track_type}"
            )
        if getattr(source_track, "is_locked", False):
            raise ValueError(f"Source track is locked: {self.source_track_id}")
        if getattr(target_track, "is_locked", False):
            raise ValueError(f"Target track is locked: {self.target_track_id}")
        bound_graphic = timeline.get_caption_graphic_binding_by_clip(clip.clip_id)
        if bound_graphic is not None and target_track.track_type != "video":
            raise ValueError("Caption graphic clips can only move to video tracks")
        return self.target_track_id

    def _restored_clip_for_undo(self, clip: ClipInstance, restored_start: int) -> ClipInstance:
        """C02：undo 时把片段连同动画关键帧对称平移回原位。"""
        restored = clip.with_track(self.source_track_id).with_timeline_start(
            restored_start)
        delta = self._temporal_delta
        if delta:
            restored = restored.with_temporal_shift(-delta)
        return restored

    def _restore_source_transitions(
        self, timeline: TimelineSequence, restored_source: "EditTrack",
    ) -> TimelineSequence:
        """C03：源轨转场按 apply 前快照精确恢复。"""
        if self._source_transitions_before is not None:
            restored_source = restored_source._rebuild(
                transitions=self._source_transitions_before)
        return timeline.with_track_updated(restored_source)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo: remove clip from new track, restore to source track, delete new track."""
        import logging
        logger = logging.getLogger(__name__)

        # Get current clip location
        if self._created_track_id:
            target_track = timeline.get_track_by_id(self._created_track_id)
            if target_track is None:
                raise ValueError(f"Created track not found: {self._created_track_id}")

            clip = target_track.get_clip_by_id(self.clip_id)
            if clip is None:
                raise ValueError(f"Clip not found in target track: {self.clip_id}")

            # Restore clip to source track
            restored_start = (
                self.old_timeline_start
                if self.old_timeline_start is not None
                else 0
            )
            current_start = clip.timeline_start
            restored_clip = self._restored_clip_for_undo(clip, restored_start)

            source_track = timeline.get_track_by_id(self.source_track_id)
            if source_track:
                restored_source = source_track.with_clip_added(restored_clip)
                # C03：恢复源轨被 with_clip_removed 顺带删除的转场
                new_timeline = self._restore_source_transitions(
                    timeline, restored_source)
            else:
                # Source track may have been deleted - recreate it
                # This is a edge case, for now just raise
                raise ValueError(f"Source track not found for undo: {self.source_track_id}")

            new_timeline = _move_scoped_markers_with_clip(
                new_timeline,
                self.clip_id,
                current_start,
                restored_start,
                self.source_track_id,
            )

            # Remove clip from target track and delete track if empty
            remaining_clips = [c for c in target_track.clips if c.clip_id != self.clip_id]
            if not remaining_clips:
                # Delete empty track
                if self.target_track_type == "video":
                    new_video = [t for t in new_timeline.video_tracks if t.track_id != self._created_track_id]
                    new_timeline = new_timeline._rebuild(
                        video_tracks=tuple(replace(t, index=i) for i, t in enumerate(new_video))
                    )
                else:
                    new_audio = [t for t in new_timeline.audio_tracks if t.track_id != self._created_track_id]
                    new_timeline = new_timeline._rebuild(
                        audio_tracks=tuple(replace(t, index=i) for i, t in enumerate(new_audio))
                    )
        else:
            # P0-04 undo for existing-track moves: put the clip back on the
            # source track; no track is created or deleted.
            if not self.target_track_id:
                raise ValueError(
                    "Cannot undo cross-track move: no target_track_id stored"
                )
            target_track = timeline.get_track_by_id(self.target_track_id)
            if target_track is None:
                raise ValueError(f"Target track not found for undo: {self.target_track_id}")
            clip = target_track.get_clip_by_id(self.clip_id)
            if clip is None:
                raise ValueError(f"Clip not found in target track: {self.clip_id}")

            current_start = clip.timeline_start
            restored_start = (
                self.old_timeline_start
                if self.old_timeline_start is not None
                else current_start
            )
            restored_clip = self._restored_clip_for_undo(clip, restored_start)

            source_track = timeline.get_track_by_id(self.source_track_id)
            if source_track is None:
                raise ValueError(f"Source track not found for undo: {self.source_track_id}")

            new_timeline = self._restore_source_transitions(
                timeline, source_track.with_clip_added(restored_clip))
            new_timeline = _move_scoped_markers_with_clip(
                new_timeline,
                self.clip_id,
                current_start,
                restored_start,
                self.source_track_id,
            )
            target_after = new_timeline.get_track_by_id(self.target_track_id)
            if target_after is not None:
                target_after = target_after.with_clip_removed(self.clip_id)
                # C03：目标轨转场恢复 apply 前快照（去掉迁移进来的单边转场）
                if self._target_transitions_before is not None:
                    target_after = target_after._rebuild(
                        transitions=self._target_transitions_before)
                new_timeline = new_timeline.with_track_updated(target_after)

        logger.debug(
            f"MoveClipToTrackCommand undone: {self.clip_id} "
            f"restored to {self.source_track_id}"
        )
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveClipToTrackCommand",
            "label": self.label,
            "source_track_id": self.source_track_id,
            "clip_id": self.clip_id,
            "target_track_type": self.target_track_type,
            "create_new_track": self.create_new_track,
            "new_track_position": self.new_track_position,
            "new_timeline_start": self.new_timeline_start,
            "old_timeline_start": self.old_timeline_start,
            "target_track_id": self.target_track_id,
            "_created_track_id": self._created_track_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveClipToTrackCommand:
        """Deserialize command."""
        return MoveClipToTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Clip to New Track"),
            source_track_id=data.get("source_track_id", ""),
            clip_id=data.get("clip_id", ""),
            target_track_type=data.get("target_track_type", "video"),
            create_new_track=data.get("create_new_track", False),
            new_track_position=data.get("new_track_position", "above"),
            new_timeline_start=data.get("new_timeline_start", 0),
            old_timeline_start=data.get("old_timeline_start"),
            target_track_id=data.get("target_track_id"),
            _created_track_id=data.get("_created_track_id"),
        )

    @staticmethod
    def create_for_new_track(
        source_track_id: str,
        clip_id: str,
        target_track_type: str,
        new_track_position: str,
        new_start: int,
        old_start: int | None = None
    ) -> MoveClipToTrackCommand:
        """Factory method for auto-create track on drag."""
        return MoveClipToTrackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Move Clip to New {target_track_type} Track",
            source_track_id=source_track_id,
            clip_id=clip_id,
            target_track_type=target_track_type,
            create_new_track=True,
            new_track_position=new_track_position,
            new_timeline_start=new_start,
            old_timeline_start=old_start,
        )

    @staticmethod
    def create_for_existing_track(
        source_track_id: str,
        clip_id: str,
        target_track_id: str,
        new_start: int,
        old_start: int | None = None
    ) -> MoveClipToTrackCommand:
        """Factory: move a clip onto an existing track (P0-04).

        Strict semantics — the move is rejected if the destination overlaps
        existing material. Use MoveClipsOverwriteCommand for destructive drops.
        """
        return MoveClipToTrackCommand(
            command_id=str(uuid.uuid4()),
            label="Move Clip to Existing Track",
            source_track_id=source_track_id,
            clip_id=clip_id,
            target_track_type="",
            create_new_track=False,
            new_timeline_start=new_start,
            old_timeline_start=old_start,
            target_track_id=target_track_id,
        )


# ================================================================
# CopyClipsToNewTrackCommand (Alt-drag copy into auto-create zone)
# ================================================================

@dataclass(frozen=True)
class CopyClipsToNewTrackCommand:
    """Create ONE new track and add pre-built clip copies to it atomically.

    Used when Alt-dragging clips beyond the outermost track (auto-create
    zone): the originals stay in place while the copies land on a freshly
    created track. Because the created track contains only the copies,
    undo simply removes the whole track.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_type: 'video' or 'audio' (type of track to create)
        track_position: 'above' (video) or 'below' (audio)
        clips: Prepared copy clips (new IDs, resolved starts); the source
            clips are NOT removed.
        _created_track_id: ID of created track (set during apply)
    """

    command_id: str
    label: str
    track_type: str
    track_position: str
    clips: tuple[ClipInstance, ...]
    _created_track_id: str | None = None
    track_role: str = "standard"

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply: create the new track and add all copies to it."""
        if not self.clips:
            raise ValueError("CopyClipsToNewTrackCommand: no clips to copy")
        if self.track_type not in ("video", "audio"):
            raise ValueError(f"Invalid track_type: {self.track_type}")

        track_id = str(uuid.uuid4())
        object.__setattr__(self, '_created_track_id', track_id)

        existing_count = len(
            timeline.video_tracks if self.track_type == "video"
            else timeline.audio_tracks
        )
        # Video tracks: 'above' (visual top) -> append to end (highest number)
        # Audio tracks: 'below' (visual bottom) -> append to end (highest number)
        insert_pos = existing_count
        track_name = (
            f"V{insert_pos + 1}" if self.track_type == "video"
            else f"A{insert_pos + 1}"
        )

        new_track = EditTrack(
            track_id=track_id,
            name=track_name,
            track_type=self.track_type,
            index=insert_pos,
            clips=(),
            subtitle_clips=(),
            transitions=(),
            is_enabled=True,
            is_locked=False,
            is_muted=False,
            is_solo=False,
            volume=1.0,
            track_role=self.track_role,
        )

        # Add every copy to the fresh track (empty, so only batch
        # self-overlap could fail — the caller resolves starts beforehand).
        for clip in self.clips:
            new_track = new_track.with_clip_added(
                clip.with_track(track_id).with_timeline_start(max(0, clip.timeline_start))
            )

        if self.track_type == "video":
            new_tracks = list(timeline.video_tracks)
            new_tracks.insert(insert_pos, new_track)
            new_tracks = [replace(t, index=i) for i, t in enumerate(new_tracks)]
            new_timeline = timeline._rebuild(video_tracks=tuple(new_tracks))
        else:
            new_tracks = list(timeline.audio_tracks)
            new_tracks.insert(insert_pos, new_track)
            new_tracks = [replace(t, index=i) for i, t in enumerate(new_tracks)]
            new_timeline = timeline._rebuild(audio_tracks=tuple(new_tracks))

        logger.debug(
            f"CopyClipsToNewTrackCommand applied: {len(self.clips)} copies "
            f"to new {self.track_type} track {track_id}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo: remove the created track (it only contains the copies)."""
        if not self._created_track_id:
            return timeline
        if self.track_type == "video":
            remaining = [t for t in timeline.video_tracks if t.track_id != self._created_track_id]
            new_timeline = timeline._rebuild(
                video_tracks=tuple(replace(t, index=i) for i, t in enumerate(remaining))
            )
        else:
            remaining = [t for t in timeline.audio_tracks if t.track_id != self._created_track_id]
            new_timeline = timeline._rebuild(
                audio_tracks=tuple(replace(t, index=i) for i, t in enumerate(remaining))
            )
        logger.debug(
            f"CopyClipsToNewTrackCommand undone: removed track {self._created_track_id}"
        )
        return new_timeline

    @staticmethod
    def create(
        track_type: str,
        track_position: str,
        clips: list[ClipInstance],
        track_role: str = "standard",
    ) -> CopyClipsToNewTrackCommand:
        """Factory method."""
        return CopyClipsToNewTrackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Copy {len(clips)} clip(s) to new {track_type} track",
            track_type=track_type,
            track_position=track_position,
            clips=tuple(clips),
            track_role=track_role,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "CopyClipsToNewTrackCommand",
            "label": self.label,
            "track_type": self.track_type,
            "track_position": self.track_position,
            "clips": [c.to_dict() for c in self.clips],
            "track_role": self.track_role,
            "_created_track_id": self._created_track_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CopyClipsToNewTrackCommand:
        """Deserialize command."""
        return CopyClipsToNewTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Copy clips to new track"),
            track_type=data.get("track_type", "video"),
            track_position=data.get("track_position", "above"),
            clips=tuple(ClipInstance.from_dict(c) for c in data.get("clips", [])),
            track_role=data.get("track_role", "standard"),
            _created_track_id=data.get("_created_track_id"),
        )


# ================================================================
# MoveClipToExistingTrackCommand
# ================================================================

@dataclass(frozen=True)
class MoveClipToExistingTrackCommand:
    """
    P4: Command to move a clip to an existing track.

    Used when dragging clips between existing tracks (no track creation).
    Handles clip transfer and position update.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        source_track_id: Original track containing the clip
        target_track_id: Destination track ID
        clip_id: Clip ID to move
        new_timeline_start: New start position (frames)
        old_timeline_start: Old start position (for undo)
    """

    command_id: str
    label: str
    source_track_id: str
    target_track_id: str
    clip_id: str
    new_timeline_start: int = 0
    old_timeline_start: int | None = None
    # C02/C03（第六轮审计）：与 MoveClipToTrackCommand 同语义——动画关键帧
    # 随平移量移动；源/目标轨转场快照供 undo 恢复。
    _temporal_delta: int | None = None
    _source_transitions_before: tuple | None = None
    _target_transitions_before: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply: move clip from source track to existing target track."""
        # Get source track
        source_track = timeline.get_track_by_id(self.source_track_id)
        if source_track is None:
            raise ValueError(f"Source track not found: {self.source_track_id}")

        # Get clip from source track
        clip = source_track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get target track
        target_track = timeline.get_track_by_id(self.target_track_id)
        if target_track is None:
            raise ValueError(f"Target track not found: {self.target_track_id}")

        # P0-04: same validation contract as MoveClipToTrackCommand —
        # self-moves, cross-type moves, locked tracks, caption-graphic
        # destinations and overlapping drops are rejected instead of
        # silently producing a broken timeline. Destructive drops use
        # MoveClipsOverwriteCommand.
        if self.target_track_id == self.source_track_id:
            raise ValueError(
                "Target track must differ from source track "
                f"({self.source_track_id})"
            )
        if target_track.track_type != source_track.track_type:
            raise ValueError(
                f"Cannot move {source_track.track_type} clip to "
                f"{target_track.track_type} track"
            )
        if getattr(source_track, "is_locked", False):
            raise ValueError(f"Source track is locked: {self.source_track_id}")
        if getattr(target_track, "is_locked", False):
            raise ValueError(f"Target track is locked: {self.target_track_id}")

        _assert_caption_graphic_track_destination(
            timeline, self.clip_id, target_track)

        overlap_start = max(0, self.new_timeline_start)
        overlap_end = overlap_start + clip.timeline_duration
        for other in target_track.clips:
            if (
                overlap_start < other.timeline_end
                and overlap_end > other.timeline_start
            ):
                raise ValueError(
                    f"Move would overlap clip '{other.clip_id}' on target "
                    f"track {self.target_track_id} (use overwrite semantics "
                    f"to replace it)"
                )

        # Store old position for undo
        old_start = clip.timeline_start
        # R14（第七轮审计）：旧注册入口 create() 允许省略 old_start，apply
        # 已知真实原位置却不上填——undo 在缺省时回 0（100→300→Undo 变
        # 0 而非 100）。序列化重放（from_dict 后 old 为 None）同理。
        if self.old_timeline_start is None:
            object.__setattr__(self, "old_timeline_start", old_start)

        # Create moved clip with new track_id and position
        # C02：动画关键帧随时间位置平移（与 MoveClipCommand/MoveClipToTrack
        # 同一语义；旧代码只改 timeline_start，关键帧留在原绝对帧位）
        clamped_start = max(0, self.new_timeline_start)
        temporal_delta = clamped_start - old_start
        if self._temporal_delta is None:
            object.__setattr__(self, "_temporal_delta", temporal_delta)
        moved_clip = (
            clip.with_temporal_shift(temporal_delta)
                .with_track(self.target_track_id)
                .with_timeline_start(clamped_start)
        )

        # C03：快照两轨转场；单边淡入/淡出随片段迁移重锚，双边丢弃
        if self._source_transitions_before is None:
            object.__setattr__(
                self, "_source_transitions_before",
                _copy.deepcopy(source_track.transitions))
        if self._target_transitions_before is None:
            object.__setattr__(
                self, "_target_transitions_before",
                _copy.deepcopy(target_track.transitions))
        migrated_transitions, dropped_transitions = _split_clip_transitions_for_move(
            source_track.transitions, self.clip_id, self.target_track_id,
            clamped_start, clip.timeline_duration)
        if dropped_transitions:
            logger.info(
                "MoveClipToExistingTrackCommand: %d 个双边转场随跨轨移动失效（undo 恢复）",
                len(dropped_transitions))

        # Add clip to target track
        new_target = target_track.with_clip_added(moved_clip)
        if migrated_transitions:
            new_target = new_target._rebuild(
                transitions=new_target.transitions + tuple(migrated_transitions))

        # Remove clip from source track
        # 审计修复：with_clip_removed 走 _rebuild 保留全部轨道字段
        # （手工 EditTrack 重建丢 is_sync_locked/is_visible/strip_eq/
        # effect_chain/color/automation/is_bus/output_bus_id/sends/
        # stem_group/caption_metadata/track_role 等 14 个字段）
        new_source = source_track.with_clip_removed(self.clip_id)

        # Update timeline with both tracks
        new_timeline = timeline.with_track_updated(new_target)
        new_timeline = new_timeline.with_track_updated(new_source)
        new_timeline = _move_scoped_markers_with_clip(
            new_timeline,
            self.clip_id,
            old_start,
            clamped_start,
            self.target_track_id,
        )

        logger.debug(
            f"MoveClipToExistingTrackCommand applied: {self.clip_id} "
            f"from {self.source_track_id} to {self.target_track_id}, "
            f"position {old_start} to {self.new_timeline_start}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo: move clip back to source track."""
        # Get current location (target track)
        target_track = timeline.get_track_by_id(self.target_track_id)
        if target_track is None:
            raise ValueError(f"Target track not found: {self.target_track_id}")

        clip = target_track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found in target track: {self.clip_id}")

        # Restore clip to source track with original position
        restored_start = (
            self.old_timeline_start if self.old_timeline_start is not None else 0
        )
        current_start = clip.timeline_start
        # C02：动画关键帧对称平移回原位
        restored_clip = clip.with_track(self.source_track_id).with_timeline_start(
            restored_start
        )
        if self._temporal_delta:
            restored_clip = restored_clip.with_temporal_shift(-self._temporal_delta)

        source_track = timeline.get_track_by_id(self.source_track_id)
        if source_track is None:
            raise ValueError(f"Source track not found: {self.source_track_id}")

        restored_source = source_track.with_clip_added(restored_clip)
        # C03：源轨转场按快照恢复（with_clip_removed 顺带删除了引用片段
        # 的转场，此前 undo 后转场数 1→0→undo 仍 0）
        if self._source_transitions_before is not None:
            restored_source = restored_source._rebuild(
                transitions=self._source_transitions_before)

        # Remove from target track（审计修复：同上，保留全部轨道字段）
        new_target = target_track.with_clip_removed(self.clip_id)
        # C03：目标轨转场恢复 apply 前快照（去掉迁移进来的单边转场）
        if self._target_transitions_before is not None:
            new_target = new_target._rebuild(
                transitions=self._target_transitions_before)

        # Update timeline
        new_timeline = timeline.with_track_updated(restored_source)
        new_timeline = new_timeline.with_track_updated(new_target)
        new_timeline = _move_scoped_markers_with_clip(
            new_timeline,
            self.clip_id,
            current_start,
            restored_start,
            self.source_track_id,
        )

        logger.debug(
            f"MoveClipToExistingTrackCommand undone: {self.clip_id} "
            f"restored to {self.source_track_id}"
        )
        return new_timeline

    @staticmethod
    def create(
        source_track_id: str,
        target_track_id: str,
        clip_id: str,
        new_start: int,
        old_start: int | None = None
    ) -> MoveClipToExistingTrackCommand:
        """Factory method for cross-track move to existing track."""
        return MoveClipToExistingTrackCommand(
            command_id=str(uuid.uuid4()),
            label="Move Clip to Track",
            source_track_id=source_track_id,
            target_track_id=target_track_id,
            clip_id=clip_id,
            new_timeline_start=new_start,
            old_timeline_start=old_start,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveClipToExistingTrackCommand",
            "label": self.label,
            "source_track_id": self.source_track_id,
            "target_track_id": self.target_track_id,
            "clip_id": self.clip_id,
            "new_timeline_start": self.new_timeline_start,
            "old_timeline_start": self.old_timeline_start,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveClipToExistingTrackCommand:
        """Deserialize command."""
        return MoveClipToExistingTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Clip to Track"),
            source_track_id=data.get("source_track_id", ""),
            target_track_id=data.get("target_track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_timeline_start=data.get("new_timeline_start", 0),
            old_timeline_start=data.get("old_timeline_start"),
        )


# ================================================================
# MoveClipsOverwriteCommand
# ================================================================

@dataclass(frozen=True)
class MoveClipsOverwriteCommand:
    """
    Move one or more clips, overwriting material on existing target tracks.

    This matches common NLE drag behavior: dropping a clip onto occupied track
    space removes the covered region, trimming or splitting existing target
    clips as needed. All moving clips are removed before overwrite is applied,
    so selected groups can move atomically without cutting each other.
    """

    command_id: str
    label: str
    moves: tuple[dict[str, Any], ...]
    _before_timeline: TimelineSequence | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        if not self.moves:
            return timeline

        moving_ids = {str(move["clip_id"]) for move in self.moves}
        moving_clips: dict[str, ClipInstance] = {}
        target_ranges: dict[str, list[tuple[int, int]]] = {}

        for move in self.moves:
            clip_id = str(move["clip_id"])
            source_track_id = str(move["source_track_id"])
            target_track_id = str(move["target_track_id"])
            new_start = max(0, int(move["new_start"]))

            source_track = timeline.get_track_by_id(source_track_id)
            target_track = timeline.get_track_by_id(target_track_id)
            if source_track is None:
                raise ValueError(f"Source track not found: {source_track_id}")
            if target_track is None:
                raise ValueError(f"Target track not found: {target_track_id}")
            if getattr(source_track, "is_locked", False):
                raise ValueError(f"Source track is locked: {source_track_id}")
            if getattr(target_track, "is_locked", False):
                raise ValueError(f"Target track is locked: {target_track_id}")
            if source_track.track_type != target_track.track_type:
                raise ValueError(
                    f"Cannot move {source_track.track_type} clip to {target_track.track_type} track"
                )

            clip = source_track.get_clip_by_id(clip_id)
            if clip is None:
                raise ValueError(f"Clip not found: {clip_id}")
            moving_clips[clip_id] = clip
            target_ranges.setdefault(target_track_id, []).append(
                (new_start, new_start + clip.timeline_duration)
            )

        working = timeline
        for clip_id in moving_ids:
            working = working.with_clip_removed(clip_id)

        overwritten_clip_ids: set[str] = set()
        for target_track_id, ranges in target_ranges.items():
            track = working.get_track_by_id(target_track_id)
            if track is None:
                raise ValueError(f"Target track not found after source removal: {target_track_id}")
            new_clips: list[ClipInstance] = []
            for existing in track.clips:
                retained = self._subtract_overwrite_ranges(existing, ranges)
                # ClipInstance equality is identity-by-clip_id.  A partial
                # overwrite keeps that id while returning a trimmed replacement,
                # so value equality cannot tell an unchanged clip from a retained
                # fragment.  _subtract_overwrite_ranges returns the original
                # object only for the no-overlap case.
                if len(retained) != 1 or retained[0] is not existing:
                    overwritten_clip_ids.add(existing.clip_id)
                new_clips.extend(retained)
            rebuilt = replace(
                track,
                clips=tuple(sorted(new_clips, key=lambda c: (c.timeline_start, c.clip_id))),
            )
            working = working.with_track_updated(rebuilt)

        working = self._clear_links_to_overwritten_clips(working, overwritten_clip_ids)

        for move in self.moves:
            clip_id = str(move["clip_id"])
            target_track_id = str(move["target_track_id"])
            new_start = max(0, int(move["new_start"]))
            clip = moving_clips[clip_id]
            moved_clip = replace(
                clip,
                track_id=target_track_id,
                timeline_start=new_start,
            )
            track = working.get_track_by_id(target_track_id)
            if track is None:
                raise ValueError(f"Target track not found while adding moved clip: {target_track_id}")
            working = working.with_track_updated(track.with_clip_added(moved_clip))

            working = _move_scoped_markers_with_clip(
                working,
                clip_id,
                clip.timeline_start,
                new_start,
                target_track_id,
            )

        updated_cmd = MoveClipsOverwriteCommand(
            command_id=self.command_id,
            label=self.label,
            moves=self.moves,
            _before_timeline=timeline,
        )
        logger.debug(
            f"MoveClipsOverwriteCommand applied: moves={len(self.moves)}, "
            f"overwritten={len(overwritten_clip_ids)}"
        )
        return working, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._before_timeline is None:
            raise ValueError("Cannot undo overwrite move: previous timeline not stored")
        return self._before_timeline

    @staticmethod
    def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        clean = sorted((max(0, s), max(0, e)) for s, e in ranges if e > s)
        merged: list[tuple[int, int]] = []
        for start, end in clean:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                prev_start, prev_end = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end))
        return merged

    @classmethod
    def _subtract_overwrite_ranges(
        cls,
        clip: ClipInstance,
        ranges: list[tuple[int, int]],
    ) -> list[ClipInstance]:
        clip_start = clip.timeline_start
        clip_end = clip.timeline_end
        overlaps = [
            (max(clip_start, start), min(clip_end, end))
            for start, end in cls._merge_ranges(ranges)
            if start < clip_end and end > clip_start
        ]
        if not overlaps:
            return [clip]

        retained_intervals: list[tuple[int, int]] = []
        cursor = clip_start
        for start, end in overlaps:
            if start > cursor:
                retained_intervals.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < clip_end:
            retained_intervals.append((cursor, clip_end))

        retained: list[ClipInstance] = []
        for index, (start, end) in enumerate(retained_intervals):
            duration = end - start
            # AUD-16209: 旧实现用 int((start - clip_start) * speed) 截断量化
            # 时间线→源帧换算，分数速度（如 0.5）下每段最多丢 1 帧且误差以
            # source_start 累加，反复覆盖-修剪同一变速片段会让保留段源窗口
            # 单调漂移。改用 clip 的真实帧映射（速度点/反转/冻结帧/
            # 有理 fps 元数据感知，与 SplitClipCommand 同一入口）计算两个
            # 边界源帧：段内 duration 用两边界源帧差，相邻保留段共享边界
            # 映射从而无缝衔接，不再有截断累积误差。
            first_source = clip.timeline_to_source_frame(start)
            last_source = clip.timeline_to_source_frame(end - 1)
            window_lo = min(first_source, last_source)
            window_hi = max(first_source, last_source)
            source_start = window_lo
            source_duration = min(
                window_hi - window_lo + 1,
                clip.source_end - window_lo + 1,
            )
            if duration <= 0 or source_duration <= 0:
                continue
            retained.append(
                replace(
                    clip,
                    clip_id=clip.clip_id if index == 0 else str(uuid.uuid4()),
                    timeline_start=start,
                    timeline_duration=duration,
                    source_start=source_start,
                    source_duration=source_duration,
                    linked_clip_id=None,
                    fade_in_duration=min(clip.fade_in_duration, duration),
                    fade_out_duration=min(clip.fade_out_duration, duration),
                )
            )
        return retained

    @staticmethod
    def _clear_links_to_overwritten_clips(
        timeline: TimelineSequence,
        overwritten_clip_ids: set[str],
    ) -> TimelineSequence:
        if not overwritten_clip_ids:
            return timeline
        current = timeline
        for track in current.all_tracks:
            for clip in track.clips:
                if clip.linked_clip_id in overwritten_clip_ids:
                    current = current.with_clip_updated(clip.with_linked_clip_id(None))
        return current

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "MoveClipsOverwriteCommand",
            "label": self.label,
            "moves": list(self.moves),
        }

    @staticmethod
    def create(
        moves: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        label: str = "Move clips with overwrite",
    ) -> MoveClipsOverwriteCommand:
        normalized = tuple(
            {
                "clip_id": str(move["clip_id"]),
                "source_track_id": str(move["source_track_id"]),
                "target_track_id": str(move["target_track_id"]),
                "new_start": int(move["new_start"]),
                "old_start": int(move.get("old_start", 0)),
            }
            for move in moves
        )
        return MoveClipsOverwriteCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            moves=normalized,
        )


# ================================================================
# TrimClipCommand
# ================================================================

@dataclass(frozen=True)
class TrimClipCommand:
    """
    Command to trim clip in/out points.

    Supports trimming either left (in) or right (out) edge.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to trim
        trim_side: Which side to trim ('left' or 'right')
        new_timeline_start: New timeline start (for left trim)
        new_timeline_duration: New timeline duration (for right trim)
        new_source_start: New source start (for left trim)
        new_source_duration: New source duration (for right trim)
        old_timeline_start: Old timeline start (for undo)
        old_timeline_duration: Old timeline duration (for undo)
        old_source_start: Old source start (for undo)
        old_source_duration: Old source duration (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    trim_side: str  # 'left' or 'right'
    new_timeline_start: int | None = None
    new_timeline_duration: int | None = None
    new_source_start: int | None = None
    new_source_duration: int | None = None
    old_timeline_start: int | None = None
    old_timeline_duration: int | None = None
    old_source_start: int | None = None
    old_source_duration: int | None = None
    # R05：apply 期间钳制了 fade/转场，undo 必须恢复原 envelope，
    # 不能从钳制后的状态逆推。
    _old_fade_in: int | None = None
    _old_fade_out: int | None = None
    _before_transitions: tuple = None
    _before_clip: ClipInstance | None = None
    _before_markers: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: trim clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip trimmed
        """

        # F2.9：同步时序的图形 clip 由字幕 cue 驱动（sync_timing），直接
        # trim 会与 cue 失步；已 detach（快照语义）的允许修剪。
        _binding = timeline.get_caption_graphic_binding_by_clip(self.clip_id)
        if _binding is not None and getattr(_binding, "sync_timing", False):
            raise ValueError(
                f"Caption graphic clip {self.clip_id} is driven by its "
                "subtitle cue: trim the cue instead, or detach first"
            )

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # R01：锁定轨道拒绝修剪（守卫先于旧值回写，拒绝实例无残留）
        assert_track_editable(timeline, self.track_id, "Trim clip")

        # Store old values for undo
        old_start = clip.timeline_start
        old_duration = clip.timeline_duration
        old_source_start = clip.source_start
        old_source_duration = clip.source_duration
        # AUD-16205：旧值必须持久化（此前仅局部变量，undo 抛
        # "Cannot undo: old values not stored"）。object.__setattr__ 回写
        # 是本文件既有机制；工厂显式传入的旧值优先，不覆盖。
        if self.old_timeline_start is None:
            object.__setattr__(self, "old_timeline_start", old_start)
        if self.old_timeline_duration is None:
            object.__setattr__(self, "old_timeline_duration", old_duration)
        if self.old_source_start is None:
            object.__setattr__(self, "old_source_start", old_source_start)
        if self.old_source_duration is None:
            object.__setattr__(self, "old_source_duration", old_source_duration)
        # R05：fade 原值与整轨转场快照——缩短路径会钳制两者，undo 逐字段
        # 还原而不是保留钳制结果（审计实测 fade 40→20→undo 仍 20）。
        object.__setattr__(self, "_old_fade_in", clip.fade_in_duration)
        object.__setattr__(self, "_old_fade_out", clip.fade_out_duration)
        object.__setattr__(self, "_before_transitions", track.transitions)

        object.__setattr__(self, "_before_clip", clip)
        object.__setattr__(self, "_before_markers", timeline.markers)

        # Apply trim
        if self.trim_side == "left":
            if self.new_timeline_start is None or self.new_source_start is None:
                raise ValueError("Left trim requires new_timeline_start and new_source_start")

            if self.new_source_duration is not None:
                # C06：方向感知左修——反向片段时间线左缘收缩 source
                # duration 而 start 不变，with_trim_left 的 delta 推导表达
                # 不了；显式 (start, duration) 直接构造。R05：显式路径同样
                # 钳制 fade（raw replace 会因 fade>duration 在
                # __post_init__ 抛 ValueError）。
                timeline_delta = self.new_timeline_start - clip.timeline_start
                new_duration = clip.timeline_duration - timeline_delta
                trimmed_clip = replace(
                    clip,
                    timeline_start=self.new_timeline_start,
                    timeline_duration=new_duration,
                    source_start=self.new_source_start,
                    source_duration=self.new_source_duration,
                    fade_in_duration=min(clip.fade_in_duration, new_duration),
                    fade_out_duration=min(clip.fade_out_duration, new_duration),
                )
            else:
                trimmed_clip = clip.with_trim_left(
                    self.new_timeline_start,
                    self.new_source_start
                )
        elif self.trim_side == "right":
            if self.new_timeline_duration is None or self.new_source_duration is None:
                raise ValueError("Right trim requires new_timeline_duration and new_source_duration")

            if self.new_source_start is not None:
                # C06/R02：方向感知右修——反向片段右缘收缩低端源帧
                # （source_start 前移）。new_source_start 单独即可生效（此前
                # 还要求 new_timeline_start 同时有值，真实 UI 工厂只传
                # 后者，反向右修的新源入点被静默丢弃）；右修不移动时间线
                # 起点，保持原 clip.timeline_start。R05：钳制 fade。
                trimmed_clip = replace(
                    clip,
                    timeline_start=clip.timeline_start,
                    timeline_duration=self.new_timeline_duration,
                    source_start=self.new_source_start,
                    source_duration=self.new_source_duration,
                    fade_in_duration=min(
                        clip.fade_in_duration, self.new_timeline_duration
                    ),
                    fade_out_duration=min(
                        clip.fade_out_duration, self.new_timeline_duration
                    ),
                )
            else:
                trimmed_clip = clip.with_trim_right(
                    self.new_timeline_duration,
                    self.new_source_duration
                )
        else:
            raise ValueError(f"Invalid trim_side: {self.trim_side}")

        trimmed_clip = replace(trimmed_clip, **clip.source_window_mapping_updates(
            trimmed_clip.source_start, trimmed_clip.source_duration,
            trimmed_clip.timeline_start - clip.timeline_start
            if self.trim_side == "left" else 0))

        # Check for overlap with other clips
        other_clips = [c for c in track.clips if c.clip_id != self.clip_id]
        for existing in other_clips:
            if (trimmed_clip.timeline_start < existing.timeline_end and
                trimmed_clip.timeline_end > existing.timeline_start):
                raise ValueError(
                    f"Trimmed clip overlaps with {existing.clip_id}"
                )

        new_track = track.with_clip_updated(trimmed_clip)
        new_timeline = timeline.with_track_updated(new_track)
        new_timeline = new_timeline._rebuild(markers=tuple(
            marker for marker in timeline.markers
            if marker.clip_id != clip.clip_id
            or trimmed_clip.timeline_start <= marker.frame < trimmed_clip.timeline_end))

        logger.debug(
            f"TrimClipCommand applied: {self.clip_id} "
            f"{self.trim_side} trim"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original clip bounds.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip restored
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Restore old values
        if self.trim_side == "left":
            if self.old_timeline_start is None or self.old_source_start is None:
                raise ValueError("Cannot undo: old values not stored")

            # Undo left trim: extend left edge back.
            # replace() keeps every other field (fades/color/blend/gain/
            # freeze/fps metadata...) — the manual reconstruction previously
            # silently reset them on undo.  R05：fade 在 apply 被钳制，
            # undo 用快照原值恢复 envelope。
            restored_clip = replace(
                clip,
                timeline_start=self.old_timeline_start,
                timeline_duration=self.old_timeline_duration,
                source_start=self.old_source_start,
                source_duration=self.old_source_duration,
                fade_in_duration=self._old_fade_in
                if self._old_fade_in is not None else clip.fade_in_duration,
                fade_out_duration=self._old_fade_out
                if self._old_fade_out is not None else clip.fade_out_duration,
            )
        elif self.trim_side == "right":
            if self.old_timeline_duration is None or self.old_source_duration is None:
                raise ValueError("Cannot undo: old values not stored")

            # Same replace()-based restore as the left branch: preserve all
            # fields the manual reconstruction used to drop on undo.
            # R02：反向右修改了 source_start，undo 必须一并还原（此前
            # 只还 duration，源入点停在 110）。R05：同上恢复 fade。
            restored_clip = replace(
                clip,
                timeline_duration=self.old_timeline_duration,
                source_start=self.old_source_start
                if self.old_source_start is not None else clip.source_start,
                source_duration=self.old_source_duration,
                fade_in_duration=self._old_fade_in
                if self._old_fade_in is not None else clip.fade_in_duration,
                fade_out_duration=self._old_fade_out
                if self._old_fade_out is not None else clip.fade_out_duration,
            )
        else:
            raise ValueError(f"Invalid trim_side: {self.trim_side}")

        if self._before_clip is not None:
            restored_clip = self._before_clip
        new_track = track.with_clip_updated(restored_clip)
        new_timeline = timeline.with_track_updated(new_track)
        # R05：恢复修剪（可能钳制时长）前的整轨转场快照——reanchor 只会
        # 收紧转场，不会重新放开，undo 必须回快照（审计实测转场 40→20
        # →undo 仍 20）。
        if self._before_transitions is not None:
            track = new_timeline.get_track_by_id(self.track_id)
            if track is not None:
                new_timeline = new_timeline.with_track_updated(
                    track._rebuild(transitions=self._before_transitions)
                )

        if self._before_markers is not None:
            new_timeline = new_timeline._rebuild(markers=self._before_markers)

        logger.debug(f"TrimClipCommand undone: {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "TrimClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "trim_side": self.trim_side,
            "new_timeline_start": self.new_timeline_start,
            "new_timeline_duration": self.new_timeline_duration,
            "new_source_start": self.new_source_start,
            "new_source_duration": self.new_source_duration,
            "old_timeline_start": self.old_timeline_start,
            "old_timeline_duration": self.old_timeline_duration,
            "old_source_start": self.old_source_start,
            "old_source_duration": self.old_source_duration,
            # R09：新增 fade/转场快照完整往返（None 与空 tuple 区分：
            # 无快照存 None，空转场存 []）
            "before_markers": ([m.to_dict() for m in self._before_markers]
                               if self._before_markers is not None else None),
            "before_clip": self._before_clip.to_dict() if self._before_clip else None,
            "old_fade_in": self._old_fade_in,
            "old_fade_out": self._old_fade_out,
            "before_transitions": (
                None
                if self._before_transitions is None
                else [t.to_dict() for t in self._before_transitions]
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TrimClipCommand:
        """Deserialize command."""
        return TrimClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Trim Clip"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            trim_side=data.get("trim_side", "right"),
            new_timeline_start=data.get("new_timeline_start"),
            new_timeline_duration=data.get("new_timeline_duration"),
            new_source_start=data.get("new_source_start"),
            new_source_duration=data.get("new_source_duration"),
            old_timeline_start=data.get("old_timeline_start"),
            old_timeline_duration=data.get("old_timeline_duration"),
            old_source_start=data.get("old_source_start"),
            old_source_duration=data.get("old_source_duration"),
            _before_markers=(tuple(TimelineMarker.from_dict(m)
                                   for m in data["before_markers"])
                             if data.get("before_markers") is not None else None),
            _before_clip=(ClipInstance.from_dict(data["before_clip"])
                          if data.get("before_clip") else None),
            _old_fade_in=data.get("old_fade_in"),
            _old_fade_out=data.get("old_fade_out"),
            _before_transitions=_transitions_from_dicts(
                data.get("before_transitions")
            ),
        )

    @staticmethod
    def create_left_trim(
        track_id: str,
        clip_id: str,
        new_start: int,
        new_source_start: int,
        old_start: int | None = None,
        old_source_start: int | None = None,
        old_duration: int | None = None,
        old_source_duration: int | None = None,
        new_source_duration: int | None = None,
    ) -> TrimClipCommand:
        """Factory method for left trim.

        C06：``new_source_duration`` 为方向感知修剪的显式源时长（反向片
        段左缘收缩 duration、start 不变）；缺省时按 with_trim_left 的
        delta 推导（正向兼容）。
        """
        return TrimClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Trim Clip In to {new_start}",
            track_id=track_id,
            clip_id=clip_id,
            trim_side="left",
            new_timeline_start=new_start,
            new_source_start=new_source_start,
            new_source_duration=new_source_duration,
            old_timeline_start=old_start,
            old_source_start=old_source_start,
            old_timeline_duration=old_duration,
            old_source_duration=old_source_duration,
        )

    @staticmethod
    def create_right_trim(
        track_id: str,
        clip_id: str,
        new_duration: int,
        new_source_duration: int,
        old_duration: int | None = None,
        old_source_duration: int | None = None,
        new_source_start: int | None = None,
    ) -> TrimClipCommand:
        """Factory method for right trim.

        C06：``new_source_start`` 为方向感知修剪的显式源入点（反向片段
        右缘收缩低端帧时前移）；缺省按 with_trim_right 推导（正向兼容）。
        """
        return TrimClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Trim Clip Out to duration {new_duration}",
            track_id=track_id,
            clip_id=clip_id,
            trim_side="right",
            new_timeline_duration=new_duration,
            new_source_duration=new_source_duration,
            new_source_start=new_source_start,
            old_timeline_duration=old_duration,
            old_source_duration=old_source_duration,
        )


# ================================================================
# SlipClipCommand (P4.4)
# ================================================================

@dataclass(frozen=True)
class SlipClipCommand:
    """
    Command to slip a clip's source range.

    Slip: clip在timeline的位置和duration不变，只改变source范围。
    source_start和source_end同时移动delta_frames。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to slip
        delta_frames: Source offset (正数向后，负数向前)
        max_source_frames: Maximum source frames for boundary check
        old_source_start: Old source start (for undo)
        old_source_duration: Old source duration (stored for verification)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    delta_frames: int
    max_source_frames: int | None = None
    old_source_start: int | None = None  # Set during apply
    old_source_duration: int | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: slip clip source range.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip slipped
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old values for undo
        old_source_start = clip.source_start
        # N04/C3-04：锁轨拒绝（Slip 是内容写入口，此前可绕锁）
        assert_track_editable(timeline, self.track_id, "Slip clip")

        old_source_duration = clip.source_duration

        # Import slip_clip operation
        from src.features.edit.core.timeline_ops import slip_clip

        # Apply slip operation
        new_timeline = slip_clip(
            timeline,
            self.clip_id,
            self.delta_frames,
            self.max_source_frames
        )

        logger.debug(
            f"SlipClipCommand applied: {self.clip_id} "
            f"source_start {old_source_start} -> {old_source_start + self.delta_frames}"
        )

        # Return updated command with stored values for undo
        updated_cmd = SlipClipCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            delta_frames=self.delta_frames,
            max_source_frames=self.max_source_frames,
            old_source_start=old_source_start,
            old_source_duration=old_source_duration,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original source range.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original source range
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        if self.old_source_start is None:
            raise ValueError("Cannot undo: old_source_start not stored")

        # Restore original source range (timeline position unchanged).
        # replace() preserves fades/color/blend/gain/freeze/fps metadata that
        # the manual reconstruction silently reset.
        restored_clip = replace(clip, source_start=self.old_source_start)

        new_track = track.with_clip_updated(restored_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"SlipClipCommand undone: {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "SlipClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "delta_frames": self.delta_frames,
            "max_source_frames": self.max_source_frames,
            "old_source_start": self.old_source_start,
            "old_source_duration": self.old_source_duration,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SlipClipCommand:
        """Deserialize command."""
        return SlipClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Slip Clip"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            delta_frames=data.get("delta_frames", 0),
            max_source_frames=data.get("max_source_frames"),
            old_source_start=data.get("old_source_start"),
            old_source_duration=data.get("old_source_duration"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        delta_frames: int,
        max_source_frames: int | None = None,
        old_source_start: int | None = None,
        old_source_duration: int | None = None
    ) -> SlipClipCommand:
        """Factory method to create command."""
        return SlipClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Slip Clip {delta_frames} frames",
            track_id=track_id,
            clip_id=clip_id,
            delta_frames=delta_frames,
            max_source_frames=max_source_frames,
            old_source_start=old_source_start,
            old_source_duration=old_source_duration,
        )


# ================================================================
# SplitClipCommand
# ================================================================

@dataclass(frozen=True)
class SplitClipCommand:
    """
    Command to split a clip at a position (blade/razor tool).

    Creates two clips from one, split at the specified frame.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to split
        split_frame: Frame to split at (absolute timeline frame)
        _left_clip_id: ID of left clip after split
        _right_clip_id: ID of right clip after split
        _original_clip: Original clip for undo
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    split_frame: int
    _left_clip_id: str | None = None
    _right_clip_id: str | None = None
    _original_clip: ClipInstance | None = None
    # C11：切分前整轨快照。with_clip_removed(原片段) 会把引用它的转场
    # 一并删除且 undo 不恢复（审计实测 blade 后转场 1→0→undo 仍 0）。
    # 快照 + 引用迁移（头/左邻转场挂左段，尾/右邻转场挂右段）保证切分
    # 保留转场，undo 逐字节复原。
    _before_clips: tuple[ClipInstance, ...] | None = None
    _before_transitions: tuple = None
    # C15：切分前序列级 marker 快照。clip-scoped marker 按内容位置迁移
    # 到所属半段（审计实测 marker75 在 blade50 后仍指向 timeline_end=50
    # 的左段，单独移动左段时 marker 错误跟随）。
    _before_markers: tuple = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: split clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip split into two
        """

        # F2.9：绑定字幕 cue 的图形 clip 不支持刀片切分——split 会产生无
        # 绑定的孤儿半段（同 caption-graphic 资产 id），文本/动画关系断裂。
        # 修剪/切分请作用于字幕 cue，或先 Convert Back to Subtitle。
        if timeline.get_caption_graphic_binding_by_clip(self.clip_id) is not None:
            raise ValueError(
                f"Caption graphic clip {self.clip_id} cannot be split: "
                "split the subtitle cue instead (or convert back to subtitle)"
            )

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C09/INT-04：局部刀片曾绕过轨道锁；菜单/快捷键入口同样经此拒绝
        assert_track_editable(timeline, self.track_id, "Split clip")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Validate split position
        if not (clip.timeline_start < self.split_frame < clip.timeline_end):
            raise ValueError(
                f"Split frame {self.split_frame} must be inside clip "
                f"[{clip.timeline_start}, {clip.timeline_end})"
            )

        # Calculate split
        left_timeline_duration = self.split_frame - clip.timeline_start
        right_timeline_duration = clip.timeline_end - self.split_frame

        # Convert to source frames using the real frame mapping (speed
        # points / reverse aware).  effective_speed() only covers the base
        # speed and used to produce negative or misaligned segment ranges on
        # retime-control clips.
        from src.features.edit.core.timeline_ops import split_source_partition

        (left_source_start, left_source_duration,
         right_source_start, right_source_duration) = split_source_partition(
            clip, self.split_frame)
        left_mapping = clip.source_window_mapping_updates(
            left_source_start, left_source_duration, 0)
        right_mapping = clip.source_window_mapping_updates(
            right_source_start, right_source_duration, left_timeline_duration)

        # Keep the original ID on the left segment.  Any reciprocal partner
        # still points to this ID, so a one-sided blade operation cannot leave
        # a stale link behind.  Only the right segment needs a new identity.
        left_clip_id = clip.clip_id
        right_clip_id = self._right_clip_id or str(uuid.uuid4())

        # Store the produced IDs and original data on the frozen command so
        # direct callers and CommandStack undo/redo use the exact segments.
        object.__setattr__(self, "_original_clip", clip)
        object.__setattr__(self, "_left_clip_id", left_clip_id)
        object.__setattr__(self, "_right_clip_id", right_clip_id)

        # Create left/right segments via replace(): keeps blend mode, audio
        # gain, freeze frame, label color, waveform flags and fps metadata.
        # Only the split-specific fields differ from the original clip.
        # Fades are clamped to the shortened segments so the
        # fade <= duration invariant cannot reject the split.
        left_clip = replace(
            clip,
            timeline_duration=left_timeline_duration,
            source_start=left_source_start,
            source_duration=left_source_duration,
            **left_mapping,
            fade_in_duration=min(clip.fade_in_duration, left_timeline_duration),
            # C10：左段不继承超出新时长的尾部淡出（审计实测 blade20 抛
            # fade_out (40) exceeds clip duration (20)）
            fade_out_duration=min(clip.fade_out_duration, left_timeline_duration),
        )

        # The right segment is linked later by MacroCommand when both members
        # of an A/V pair are split in the same atomic operation.
        # Fade in/out: right clip gets fade_out from original, no fade_in
        right_clip = replace(
            clip,
            clip_id=right_clip_id,
            timeline_start=self.split_frame,
            timeline_duration=right_timeline_duration,
            source_start=right_source_start,
            source_duration=right_source_duration,
            **right_mapping,
            linked_clip_id=None,  # Right clip is not linked after split
            fade_in_duration=0,  # Right clip starts fresh
            fade_out_duration=min(clip.fade_out_duration, right_timeline_duration),
        )

        # C11：先记录整轨快照（切分是可逆事务，undo 精确回滚）
        object.__setattr__(self, "_before_clips", copy.deepcopy(track.clips))
        object.__setattr__(self, "_before_transitions", track.transitions)
        # C15：记录 marker 快照
        object.__setattr__(self, "_before_markers", timeline.markers)

        # Remove original clip and add two new clips
        track_without_original = track.with_clip_removed(clip.clip_id)
        track_with_left = track_without_original.with_clip_added(left_clip)
        track_with_both = track_with_left.with_clip_added(right_clip)

        # C11：迁移引用原片段的转场——头转场/左邻切点转场挂左段，尾转
        # 场/右邻切点转场挂右段；随后按新边缘重锚（center/duration 由
        # C12 的 reanchor 规则收紧）。
        migrated_transitions = []
        for trans in track.transitions:
            if trans.right_clip_id == clip.clip_id:
                migrated_transitions.append(
                    replace(trans, right_clip_id=left_clip_id)
                )
            elif trans.left_clip_id == clip.clip_id:
                migrated_transitions.append(
                    replace(trans, left_clip_id=right_clip_id)
                )
            else:
                migrated_transitions.append(trans)
        track_with_both = track_with_both._rebuild(
            transitions=tuple(migrated_transitions)
        ).reanchor_transitions()

        new_timeline = timeline.with_track_updated(track_with_both)

        # C15/R10：迁移 clip-scoped markers 到内容所在的半段（frame 保持
        # 绝对时间）。clip 为半开区间 [start, end)：切点帧属于右段——
        # ``<=`` 会把 seam marker 归左半，validator 报 marker outside
        # owning clip [start, split)。
        migrated_markers = []
        markers_changed = False
        for marker in timeline.markers:
            if marker.clip_id == clip.clip_id:
                target = (
                    left_clip_id
                    if marker.frame < self.split_frame
                    else right_clip_id
                )
                if target != marker.clip_id:
                    markers_changed = True
                migrated_markers.append(replace(marker, clip_id=target))
            else:
                migrated_markers.append(marker)
        if markers_changed:
            new_timeline = new_timeline._rebuild(
                markers=tuple(migrated_markers)
            )

        logger.debug(
            f"SplitClipCommand applied: {self.clip_id} split at {self.split_frame} "
            f"into {left_clip_id} and {right_clip_id}"
        )

        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: merge split clips back.

        C11：有整轨快照时一次原子恢复 clips+transitions（含被迁移/删除
        的转场），不做反向推导。

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original single clip
        """
        if self._before_clips is not None:
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")
            restored = track._rebuild(
                clips=self._before_clips,
                transitions=self._before_transitions,
            )
            restored_timeline = timeline.with_track_updated(restored)
            # C15：marker 归属一并精确恢复
            if self._before_markers is not None:
                restored_timeline = restored_timeline._rebuild(
                    markers=self._before_markers
                )
            logger.debug(f"SplitClipCommand undone (snapshot): {self.clip_id}")
            return restored_timeline

        if self._original_clip is None:
            # Find the clips at split position and merge
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")

            # Find clips adjacent to split point
            left_clip = track.get_clip_at(self.split_frame - 1)
            right_clip = track.get_clip_at(self.split_frame)

            if left_clip is None or right_clip is None:
                raise ValueError("Cannot undo: split clips not found")

            # Create merged clip from the left segment via replace(): restores
            # the original span plus the right segment's fade_out while
            # keeping every other attribute (blend/gain/freeze/color/fps...).
            merged_clip = replace(
                left_clip,
                clip_id=self.clip_id,
                timeline_duration=(
                    left_clip.timeline_duration + right_clip.timeline_duration
                ),
                source_duration=(
                    left_clip.source_duration + right_clip.source_duration
                ),
                fade_out_duration=right_clip.fade_out_duration,  # Restore original fade_out
            )

            # Remove both split clips and add merged
            track_without_left = track.with_clip_removed(left_clip.clip_id)
            track_without_both = track_without_left.with_clip_removed(right_clip.clip_id)
            track_with_merged = track_without_both.with_clip_added(merged_clip)

            new_timeline = timeline.with_track_updated(track_with_merged)
        else:
            # Use stored original clip
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")

            # Remove clips created by split
            if self._left_clip_id and self._right_clip_id:
                track_without_left = track.with_clip_removed(self._left_clip_id)
                track_without_both = track_without_left.with_clip_removed(self._right_clip_id)
            else:
                # Generic undo: remove clips around split point
                left_clip = track.get_clip_at(self.split_frame - 1)
                right_clip = track.get_clip_at(self.split_frame)
                if left_clip:
                    track = track.with_clip_removed(left_clip.clip_id)
                if right_clip:
                    track = track.with_clip_removed(right_clip.clip_id)
                track_without_both = track

            track_with_original = track_without_both.with_clip_added(self._original_clip)
            new_timeline = timeline.with_track_updated(track_with_original)

        logger.debug(f"SplitClipCommand undone: merged back to {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "SplitClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "before_clips": (
                [c.to_dict() for c in self._before_clips]
                if self._before_clips else None
            ),
            "before_transitions": (
                [t.to_dict() for t in self._before_transitions]
                if self._before_transitions else None
            ),
            # R09：C15 marker 快照随载荷往返（此前 marker 归右半后
            # loaded-undo 恢复原单片却留孤儿 marker）
            "before_markers": (
                [m.to_dict() for m in self._before_markers]
                if self._before_markers else None
            ),
            "clip_id": self.clip_id,
            "split_frame": self.split_frame,
            "left_clip_id": self._left_clip_id,
            "right_clip_id": self._right_clip_id,
            "original_clip": self._original_clip.to_dict() if self._original_clip else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SplitClipCommand:
        """Deserialize command."""
        original_data = data.get("original_clip")
        original_clip = ClipInstance.from_dict(original_data) if original_data else None

        return SplitClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Split Clip"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            split_frame=data.get("split_frame", 0),
            _left_clip_id=data.get("left_clip_id"),
            _right_clip_id=data.get("right_clip_id"),
            _original_clip=original_clip,
            _before_clips=_clips_from_dicts(data.get("before_clips")),
            _before_transitions=_transitions_from_dicts(
                data.get("before_transitions")
            ),
            _before_markers=tuple(
                TimelineMarker.from_dict(m)
                for m in (data.get("before_markers") or [])
            ),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        split_frame: int,
        right_clip_id: str | None = None,
    ) -> SplitClipCommand:
        """Factory method to create command.

        Args:
            track_id: Track containing the clip.
            clip_id: Clip to split.
            split_frame: Timeline frame to split at.
            right_clip_id: Optional pre-generated ID for the right segment.
                Composite operations (range Lift/Extract) must reference the
                right half in later sub-commands, so the ID has to be known
                before apply() runs — same pattern as AddEffectCommand's
                pre-generated effect_id.
        """
        return SplitClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Split Clip at frame {split_frame}",
            track_id=track_id,
            clip_id=clip_id,
            split_frame=split_frame,
            _right_clip_id=right_clip_id,
        )


@dataclass(frozen=True)
class SlideClipCommand:
    """
    Command to slide a clip along the timeline, adjusting adjacent clips.

    Slide operation:
    - The selected clip moves to a new timeline position
    - The previous clip's duration is extended/shortened
    - The next clip's start position is moved forward/backward
    - Overall gap between clips remains unchanged
    - The clip's source range remains unchanged (only timeline position changes)

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to slide
        delta_frames: Number of frames to slide (positive = right, negative = left)
        _original_clip_start: Original timeline start (for undo)
        _prev_clip_id: Previous clip ID (if exists)
        _next_clip_id: Next clip ID (if exists)
        _prev_original_state: Previous clip original state (for undo)
        _next_original_state: Next clip original state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    delta_frames: int
    prev_max_source_frames: int | None = None
    next_max_source_frames: int | None = None
    _before_markers: tuple | None = None
    _after_markers: tuple | None = None
    _original_clip_start: int | None = None
    _prev_clip_id: str | None = None
    _next_clip_id: str | None = None
    _prev_original_state: dict[str, Any] | None = None  # {timeline_start, timeline_duration, source_start, source_duration}
    _next_original_state: dict[str, Any] | None = None
    # C14：slide 前后整轨快照。旧 undo 用 slide(-delta) 反向推导，审计
    # 实测两类失败：原 A/B/C 间 10 帧 gap 被 apply 吞掉后反向 slide 复
    # 原：邻居 fade 在 apply 中被 clamp 到缩短后的时长（A 淡出 80→50），
    # 反向 slide 只回几何不回 fade。快照式 undo 原子恢复全部 payload。
    _before_clips: tuple[ClipInstance, ...] | None = None
    _before_transitions: tuple = None
    _after_clips: tuple[ClipInstance, ...] | None = None
    _after_transitions: tuple = None

    def _restore_track(
        self,
        timeline: TimelineSequence,
        clips: tuple[ClipInstance, ...],
        transitions: tuple,
    ) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(
            track._rebuild(clips=clips, transitions=transitions)
        )

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: slide clip and adjust adjacent clips.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip slid and adjacent clips adjusted
        """
        # N04/C3-04：锁轨拒绝置于 redo 快路径之前（与 Insert 同序修正）
        assert_track_editable(timeline, self.track_id, "Slide clip")

        # C14 redo 快路径：回放 after 快照，ID/参数稳定
        if self._after_clips is not None:
            return (
                self._restore_track(
                    timeline, self._after_clips, self._after_transitions
                )._rebuild(markers=(self._after_markers if self._after_markers is not None
                                    else timeline.markers)),
                self,
            )

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        before_clips = copy.deepcopy(track.clips)  # R01：快照深拷贝
        before_transitions = copy.deepcopy(track.transitions)

        # Store original state for undo
        original_start = clip.timeline_start

        # Get adjacent clips
        prev_clip, next_clip = track.get_adjacent_clips(self.clip_id)

        # Store adjacent clip states for undo
        prev_original_state = None
        next_original_state = None

        if prev_clip is not None:
            prev_original_state = {
                "clip_id": prev_clip.clip_id,
                "timeline_start": prev_clip.timeline_start,
                "timeline_duration": prev_clip.timeline_duration,
                "source_start": prev_clip.source_start,
                "source_duration": prev_clip.source_duration,
            }

        if next_clip is not None:
            next_original_state = {
                "clip_id": next_clip.clip_id,
                "timeline_start": next_clip.timeline_start,
                "timeline_duration": next_clip.timeline_duration,
                "source_start": next_clip.source_start,
                "source_duration": next_clip.source_duration,
            }

        # Apply slide using timeline_ops
        from src.features.edit.core.timeline_ops import slide_clip
        new_timeline = slide_clip(
            timeline, self.clip_id, self.delta_frames,
            self.prev_max_source_frames, self.next_max_source_frames)

        new_track = new_timeline.get_track_by_id(self.track_id)
        updated_command = SlideClipCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            delta_frames=self.delta_frames,
            prev_max_source_frames=self.prev_max_source_frames,
            next_max_source_frames=self.next_max_source_frames,
            _before_markers=copy.deepcopy(timeline.markers),
            _after_markers=copy.deepcopy(new_timeline.markers),
            _original_clip_start=original_start,
            _prev_clip_id=self._prev_clip_id,
            _next_clip_id=self._next_clip_id,
            _prev_original_state=prev_original_state,
            _next_original_state=next_original_state,
            _before_clips=before_clips,
            _before_transitions=before_transitions,
            _after_clips=new_track.clips if new_track is not None else None,
            _after_transitions=new_track.transitions if new_track is not None else None,
        )

        logger.debug(
            f"SlideClipCommand applied: {self.clip_id} "
            f"slid by {self.delta_frames} frames"
        )
        return new_timeline, updated_command

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original positions.

        C14：快照优先，一次原子恢复整轨（几何+源区间+fade+转场）；旧
        命令无快照时回退反向 slide。
        """
        if self._before_clips is not None:
            new_timeline = self._restore_track(
                timeline, self._before_clips, self._before_transitions
            )
            logger.debug(
                f"SlideClipCommand undone (snapshot): {self.clip_id}"
            )
            if self._before_markers is not None:
                new_timeline = new_timeline._rebuild(markers=self._before_markers)
            return new_timeline

        # Restore by sliding back with negative delta
        from src.features.edit.core.timeline_ops import slide_clip

        # Simply slide back by the negative delta
        new_timeline = slide_clip(timeline, self.clip_id, -self.delta_frames)

        logger.debug(
            f"SlideClipCommand undone: {self.clip_id} "
            f"slid back by {-self.delta_frames} frames"
        )
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "SlideClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "delta_frames": self.delta_frames,
            "prev_max_source_frames": self.prev_max_source_frames,
            "next_max_source_frames": self.next_max_source_frames,
            "before_markers": (None if self._before_markers is None else
                               [m.to_dict() for m in self._before_markers]),
            "after_markers": (None if self._after_markers is None else
                              [m.to_dict() for m in self._after_markers]),
            "original_clip_start": self._original_clip_start,
            "prev_clip_id": self._prev_clip_id,
            "next_clip_id": self._next_clip_id,
            "prev_original_state": self._prev_original_state,
            "next_original_state": self._next_original_state,
            # R09：C14 前后快照（含转场）随载荷往返——JSON 加载后的
            # undo 与 live 路径 payload 一致（审计实测 gap 变形）
            "before_clips": (
                None if self._before_clips is None
                else [c.to_dict() for c in self._before_clips]
            ),
            "after_clips": (
                None if self._after_clips is None
                else [c.to_dict() for c in self._after_clips]
            ),
            "before_transitions": (
                None
                if self._before_transitions is None
                else [t.to_dict() for t in self._before_transitions]
            ),
            "after_transitions": (
                None
                if self._after_transitions is None
                else [t.to_dict() for t in self._after_transitions]
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SlideClipCommand:
        """Deserialize command."""
        return SlideClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Slide Clip"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            delta_frames=data.get("delta_frames", 0),
            prev_max_source_frames=data.get("prev_max_source_frames"),
            next_max_source_frames=data.get("next_max_source_frames"),
            _before_markers=(None if data.get("before_markers") is None else
                             tuple(TimelineMarker.from_dict(m) for m in data["before_markers"])),
            _after_markers=(None if data.get("after_markers") is None else
                            tuple(TimelineMarker.from_dict(m) for m in data["after_markers"])),
            _original_clip_start=data.get("original_clip_start"),
            _prev_clip_id=data.get("prev_clip_id"),
            _next_clip_id=data.get("next_clip_id"),
            _prev_original_state=data.get("prev_original_state"),
            _next_original_state=data.get("next_original_state"),
            _before_clips=_clips_from_dicts(data.get("before_clips")),
            _after_clips=_clips_from_dicts(data.get("after_clips")),
            _before_transitions=_transitions_from_dicts(
                data.get("before_transitions")
            ),
            _after_transitions=_transitions_from_dicts(
                data.get("after_transitions")
            ),
        )

    @staticmethod
    def create(
        timeline: TimelineSequence,
        track_id: str,
        clip_id: str,
        delta_frames: int,
        prev_max_source_frames: int | None = None,
        next_max_source_frames: int | None = None,
    ) -> SlideClipCommand:
        """
        Factory method to create slide command with stored original states.

        Args:
            timeline: Current timeline (for storing original states)
            track_id: Track containing the clip
            clip_id: Clip ID to slide
            delta_frames: Number of frames to slide

        Returns:
            SlideClipCommand with stored data for undo
        """
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        clip = track.get_clip_by_id(clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {clip_id}")

        # Store original states
        original_start = clip.timeline_start

        # Get adjacent clips
        prev_clip, next_clip = track.get_adjacent_clips(clip_id)

        # Store adjacent clip states for undo
        prev_clip_id = prev_clip.clip_id if prev_clip else None
        next_clip_id = next_clip.clip_id if next_clip else None

        prev_original_state = None
        next_original_state = None

        if prev_clip is not None:
            prev_original_state = {
                "clip_id": prev_clip.clip_id,
                "timeline_start": prev_clip.timeline_start,
                "timeline_duration": prev_clip.timeline_duration,
                "source_start": prev_clip.source_start,
                "source_duration": prev_clip.source_duration,
            }

        if next_clip is not None:
            next_original_state = {
                "clip_id": next_clip.clip_id,
                "timeline_start": next_clip.timeline_start,
                "timeline_duration": next_clip.timeline_duration,
                "source_start": next_clip.source_start,
                "source_duration": next_clip.source_duration,
            }

        return SlideClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Slide Clip {clip_id} by {delta_frames} frames",
            track_id=track_id,
            clip_id=clip_id,
            delta_frames=delta_frames,
            prev_max_source_frames=prev_max_source_frames,
            next_max_source_frames=next_max_source_frames,
            _original_clip_start=original_start,
            _prev_clip_id=prev_clip_id,
            _next_clip_id=next_clip_id,
            _prev_original_state=prev_original_state,
            _next_original_state=next_original_state,
        )


# ================================================================
# InsertClipCommand (P3.2)
# ================================================================

@dataclass(frozen=True)
class InsertClipCommand:
    """
    Command to insert a clip at playhead position with ripple.

    Insert operation:
    1. Create new clip at playhead
    2. Ripple: move all clips after playhead to the right
    3. Preserve relative positions of existing clips

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Source asset ID
        in_frame: Source start frame
        out_frame: Source end frame (inclusive)
        playhead: Timeline insert position
        track_id: Target track ID
        speed: Playback speed (default 1.0)
        _inserted_clip_id: ID of the inserted clip (set during apply)
        _affected_clip_ids: IDs of clips that were rippled (set during apply)
        _ripple_offset: Amount of ripple offset (set during apply)
    """

    command_id: str
    label: str
    asset_id: str
    in_frame: int
    out_frame: int
    playhead: int
    track_id: str
    speed: float = 1.0
    _source_fps_num: int | None = None
    _source_fps_den: int | None = None
    _timeline_fps_num: int | None = None
    _timeline_fps_den: int | None = None
    # F5.4：VFR PTS 索引（None=CFR 走既有数学）
    _source_pts_index: tuple | None = None
    _inserted_clip_id: str | None = None
    _affected_clip_ids: tuple[str, ...] | None = None
    _ripple_offset: int | None = None
    _sync_lock_moves: tuple[dict[str, Any], ...] = ()  # P1-05 跨轨跟随 undo 快照
    # C02：目标轨完整 before/after 快照（含转场）。insert 现在会切分跨
    # 播放头的既有片段，旧 undo 只删新片段+回移 start>=playhead 的
    # follower，跨点片段被永久劈成左右两半并留下 10 帧洞。快照使 undo
    # 一次原子恢复整轨，redo 直接回放 after 状态并复用既有 clip IDs。
    _before_clips: tuple[ClipInstance, ...] | None = None
    _after_clips: tuple[ClipInstance, ...] | None = None
    _before_transitions: tuple = None
    _after_transitions: tuple = None
    # R08：目标轨 scoped markers 随插入右移，undo 恢复快照
    _before_markers: tuple = None
    # N05/C3-05：after 快照供 redo 恢复（此前 redo 只回 clips/transitions
    # /sync，目标轨 marker 停在 undo 后的旧帧）
    _after_markers: tuple = None

    def _restore_track_state(
        self,
        timeline: TimelineSequence,
        clips: tuple[ClipInstance, ...],
        transitions: tuple,
    ) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        rebuilt = track._rebuild(clips=clips, transitions=transitions)
        return timeline.with_track_updated(rebuilt)

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: insert clip with ripple.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with clip inserted
        """
        # R01/N04（C3-04）：锁定轨道拒绝插入。守卫必须位于 redo 快路径
        # 之前——此前快路径先 return，锁轨后 redo 仍写入（注释宣称
        # "redo 快路径之上"，实际顺序相反）。
        assert_track_editable(timeline, self.track_id, "Insert clip")

        # C02 redo 快路径：快照存在说明该命令已成功 apply 过（undo 后重
        # 做）。直接回放 after 状态，插入片段沿用原 ID，不重新切分。
        if self._after_clips is not None:
            new_timeline = self._restore_track_state(
                timeline, self._after_clips, self._after_transitions
            )
            if self._sync_lock_moves:
                from src.features.edit.core.timeline_ops import (
                    reapply_sync_lock_shifts,
                )
                new_timeline = reapply_sync_lock_shifts(
                    new_timeline, self._sync_lock_moves
                )
            # N05/C3-05：redo 用 after 快照恢复 markers（目标轨+跨轨一次
            # 到位，此前 redo 后两个 marker 都落在 owner 范围外）
            if self._after_markers is not None:
                new_timeline = new_timeline._rebuild(
                    markers=self._after_markers
                )
            return new_timeline, self

        from src.features.edit.core.timeline_ops import insert_clip_at_playhead

        # Find affected clips before applying
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C02：完整 before 快照（含跨播放头将被切分的片段与转场）
        before_clips = copy.deepcopy(track.clips)  # R01：快照深拷贝
        before_transitions = copy.deepcopy(track.transitions)
        # R08：目标轨 scoped markers 现随插入右移，undo 需要快照还原
        before_markers = copy.deepcopy(timeline.markers)

        affected_clip_ids = tuple(
            c.clip_id for c in track.clips
            if c.timeline_start >= self.playhead
        )

        # Calculate ripple offset
        # R03：跨轨波纹 delta 必须与实际插入时长一致——统一走
        # timeline_duration_for_source（30fps 源 30 帧插 24fps 序列实际
        # 24 帧，旧 int(source/speed)=30 使 cross-track follower 错位）。
        source_duration = self.out_frame - self.in_frame + 1
        from src.features.edit.core.timeline_ops import timeline_duration_for_source

        ripple_offset = timeline_duration_for_source(
            source_duration,
            self.speed,
            self._source_fps_num,
            self._source_fps_den,
            self._timeline_fps_num,
            self._timeline_fps_den,
        )

        # Apply insert operation
        new_timeline = insert_clip_at_playhead(
            timeline,
            self.asset_id,
            self.in_frame,
            self.out_frame,
            self.playhead,
            self.track_id,
            self.speed,
            source_fps_num=self._source_fps_num,
            source_fps_den=self._source_fps_den,
            timeline_fps_num=self._timeline_fps_num,
            timeline_fps_den=self._timeline_fps_den,
            source_pts_index=self._source_pts_index,
        )

        # Find the newly inserted clip
        new_track = new_timeline.get_track_by_id(self.track_id)
        if new_track is not None:
            # Find clip at playhead position
            inserted_clip = None
            for clip in new_track.clips:
                if clip.timeline_start == self.playhead and clip.asset_id == self.asset_id:
                    inserted_clip = clip
                    break
            inserted_clip_id = inserted_clip.clip_id if inserted_clip else None
        else:
            inserted_clip_id = None

        # P1-05 Sync Lock：插入把时间线内容右推，其余同步锁轨道上位于
        # 插入点及之后的片段跟随右移，跨轨下游保持对齐。
        from src.features.edit.core.timeline_ops import shift_sync_lock_followers
        new_timeline, sync_lock_moves = shift_sync_lock_followers(
            new_timeline,
            exclude_track_ids=[self.track_id],
            from_frame=self.playhead,
            delta=ripple_offset,
        )

        # Return updated command with stored values for undo
        return new_timeline, InsertClipCommand(
            command_id=self.command_id,
            label=self.label,
            asset_id=self.asset_id,
            in_frame=self.in_frame,
            out_frame=self.out_frame,
            playhead=self.playhead,
            track_id=self.track_id,
            speed=self.speed,
            _source_fps_num=self._source_fps_num,
            _source_fps_den=self._source_fps_den,
            _timeline_fps_num=self._timeline_fps_num,
            _timeline_fps_den=self._timeline_fps_den,
            _inserted_clip_id=inserted_clip_id,
            _affected_clip_ids=affected_clip_ids,
            _ripple_offset=ripple_offset,
            _sync_lock_moves=sync_lock_moves,
            _before_clips=before_clips,
            _before_transitions=before_transitions,
            _before_markers=before_markers,
            _after_markers=new_timeline.markers,
            _after_clips=(
                new_timeline.get_track_by_id(self.track_id).clips
                if new_timeline.get_track_by_id(self.track_id) is not None
                else None
            ),
            _after_transitions=(
                new_timeline.get_track_by_id(self.track_id).transitions
                if new_timeline.get_track_by_id(self.track_id) is not None
                else None
            ),
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove inserted clip and ripple back.

        C02：优先用完整 before 快照一次原子恢复目标轨（含被切分的跨点
        片段、follower 位置与转场）；旧序列化命令无快照时回退旧行为。
        """
        if self._before_clips is not None:
            new_timeline = self._restore_track_state(
                timeline, self._before_clips, self._before_transitions
            )
            # N05/C3-05：sync 逆变换在前，markers 用 before 快照一次覆盖
            # ——此前先覆盖再 restore_sync_lock_shifts，跨轨 marker 被逆
            # 变换两次（am110 → undo 变 90 而非 110）。
            from src.features.edit.core.timeline_ops import restore_sync_lock_shifts
            new_timeline = restore_sync_lock_shifts(
                new_timeline, self._sync_lock_moves
            )
            if self._before_markers is not None:
                new_timeline = new_timeline._rebuild(
                    markers=self._before_markers
                )
            logger.debug(
                f"InsertClipCommand undone (snapshot): {self._inserted_clip_id}"
            )
            return new_timeline

        if self._inserted_clip_id is None:
            raise ValueError("Cannot undo: inserted clip ID not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Remove the inserted clip
        new_track = track.with_clip_removed(self._inserted_clip_id)

        # Ripple back: move affected clips to original positions
        if self._affected_clip_ids and self._ripple_offset:
            for clip_id in self._affected_clip_ids:
                clip = new_track.get_clip_by_id(clip_id)
                if clip is not None:
                    restored_clip = clip.with_timeline_start(
                        clip.timeline_start - self._ripple_offset
                    )
                    new_track = new_track.with_clip_updated(restored_clip)

        new_timeline = timeline.with_track_updated(new_track)

        # P1-05：还原跨轨同步锁跟随位移
        from src.features.edit.core.timeline_ops import restore_sync_lock_shifts
        new_timeline = restore_sync_lock_shifts(
            new_timeline, self._sync_lock_moves
        )

        logger.debug(f"InsertClipCommand undone: removed {self._inserted_clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "InsertClipCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "playhead": self.playhead,
            "track_id": self.track_id,
            "speed": self.speed,
            "source_fps_num": self._source_fps_num,
            "source_fps_den": self._source_fps_den,
            "timeline_fps_num": self._timeline_fps_num,
            "timeline_fps_den": self._timeline_fps_den,
            "inserted_clip_id": self._inserted_clip_id,
            "affected_clip_ids": list(self._affected_clip_ids) if self._affected_clip_ids else None,
            "ripple_offset": self._ripple_offset,
            "sync_lock_moves": list(self._sync_lock_moves),
            # C02：完整快照随命令持久化，undo/redo 跨会话一致
            "before_clips": [c.to_dict() for c in self._before_clips] if self._before_clips else None,
            "after_clips": [c.to_dict() for c in self._after_clips] if self._after_clips else None,
            "before_transitions": [t.to_dict() for t in self._before_transitions] if self._before_transitions else None,
            "after_transitions": [t.to_dict() for t in self._after_transitions] if self._after_transitions else None,
            # N05/C3-05：marker 快照随载荷往返（空 tuple 与 None 语义区分）
            "before_markers": [m.to_dict() for m in self._before_markers] if self._before_markers else None,
            "after_markers": [m.to_dict() for m in self._after_markers] if self._after_markers else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> InsertClipCommand:
        """Deserialize command."""
        affected_ids = data.get("affected_clip_ids")
        return InsertClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Insert Clip"),
            asset_id=data.get("asset_id", ""),
            in_frame=data.get("in_frame", 0),
            out_frame=data.get("out_frame", 0),
            playhead=data.get("playhead", 0),
            track_id=data.get("track_id", ""),
            speed=data.get("speed", 1.0),
            _source_fps_num=data.get("source_fps_num"),
            _source_fps_den=data.get("source_fps_den"),
            _timeline_fps_num=data.get("timeline_fps_num"),
            _timeline_fps_den=data.get("timeline_fps_den"),
            _inserted_clip_id=data.get("inserted_clip_id"),
            _affected_clip_ids=tuple(affected_ids) if affected_ids else None,
            _ripple_offset=data.get("ripple_offset"),
            _sync_lock_moves=tuple(data.get("sync_lock_moves", [])),
            _before_clips=_clips_from_dicts(data.get("before_clips")),
            _after_clips=_clips_from_dicts(data.get("after_clips")),
            _before_transitions=_transitions_from_dicts(data.get("before_transitions")),
            _after_transitions=_transitions_from_dicts(data.get("after_transitions")),
            _before_markers=tuple(
                TimelineMarker.from_dict(m)
                for m in (data.get("before_markers") or [])
            ),
            _after_markers=tuple(
                TimelineMarker.from_dict(m)
                for m in (data.get("after_markers") or [])
            ),
        )

    @staticmethod
    def create(
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
        source_pts_index=None,
    ) -> InsertClipCommand:
        """Factory method to create command."""
        return InsertClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Insert Clip at {playhead}",
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            track_id=track_id,
            speed=speed,
            _source_fps_num=source_fps_num,
            _source_fps_den=source_fps_den,
            _timeline_fps_num=timeline_fps_num,
            _timeline_fps_den=timeline_fps_den,
            _source_pts_index=tuple(source_pts_index)
            if source_pts_index else None,
        )


# ================================================================
# OverwriteClipCommand (P3.2)
# ================================================================

@dataclass(frozen=True)
class OverwriteClipCommand:
    """
    Command to overwrite a clip at playhead position.

    Overwrite operation:
    1. Create new clip at playhead
    2. If overlapping with existing clips:
       - Fully covered clips are deleted
       - Partially covered clips are trimmed/split
    3. No clips are moved

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Source asset ID
        in_frame: Source start frame
        out_frame: Source end frame (inclusive)
        playhead: Timeline insert position
        track_id: Target track ID
        speed: Playback speed (default 1.0)
        _inserted_clip_id: ID of the inserted clip (set during apply)
        _modified_clips: Clips that were modified/deleted (set during apply)
        _original_clips: Original state of modified clips (for undo)
        _split_clip_ids: IDs of clips newly created by overwrite-induced
            splits (set during apply); undo removes exactly these
    """

    command_id: str
    label: str
    asset_id: str
    in_frame: int
    out_frame: int
    playhead: int
    track_id: str
    speed: float = 1.0
    _inserted_clip_id: str | None = None
    _modified_clips: tuple[str, ...] | None = None  # IDs of modified/deleted clips
    _original_clips: tuple[ClipInstance, ...] | None = None  # Original clip data
    _split_clip_ids: tuple[str, ...] | None = None  # AUD-16203: split residue whitelist
    # R22/F-R03：fps 元数据（48fps 源 48 帧覆盖 24fps 序列应占 24 帧，
    # 此前 int(source/speed)=48 使覆盖范围与映射分叉）
    _source_fps_num: int | None = None
    _source_fps_den: int | None = None
    # W07 修复：apply() 引用但字段未声明（覆盖命令 apply 即崩）——
    # 与 InsertLinkedAssetRippleCommand 同因同修
    _source_pts_index: tuple | None = None
    _timeline_fps_num: int | None = None
    _timeline_fps_den: int | None = None
    # N08/C3-06：整轨转场与序列 marker 的 before 快照（undo 此前只回
    # clips，中覆盖/迁移后的转场与 marker 永久丢失）
    _before_transitions: tuple = None
    _before_markers: tuple = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: overwrite clip at playhead.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with clip overwritten
        """
        from src.features.edit.core.timeline_ops import (
            overwrite_clip_at_playhead,
            timeline_duration_for_source,
        )

        # Find affected clips before applying
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # R01：锁定轨道拒绝覆盖写入
        assert_track_editable(timeline, self.track_id, "Overwrite clip")

        # Calculate overwrite range
        source_duration = self.out_frame - self.in_frame + 1
        timeline_duration = timeline_duration_for_source(
            source_duration,
            self.speed,
            self._source_fps_num,
            self._source_fps_den,
            self._timeline_fps_num,
            self._timeline_fps_den,
        )
        new_clip_end = self.playhead + timeline_duration

        # Collect clips that will be modified
        affected_clips: list[ClipInstance] = []
        for clip in track.clips:
            # Check if overlaps with new clip
            if clip.timeline_start >= new_clip_end:
                continue
            if clip.timeline_end <= self.playhead:
                continue
            affected_clips.append(clip)

        affected_clip_ids = tuple(c.clip_id for c in affected_clips)
        pre_apply_ids = {c.clip_id for c in track.clips}
        # N08/C3-08：整轨转场 + 序列 marker before 快照
        before_transitions = copy.deepcopy(track.transitions)
        before_markers = copy.deepcopy(timeline.markers)

        # Apply overwrite operation
        new_timeline = overwrite_clip_at_playhead(
            timeline,
            self.asset_id,
            self.in_frame,
            self.out_frame,
            self.playhead,
            self.track_id,
            self.speed,
            source_fps_num=self._source_fps_num,
            source_fps_den=self._source_fps_den,
            timeline_fps_num=self._timeline_fps_num,
            timeline_fps_den=self._timeline_fps_den,
            source_pts_index=self._source_pts_index,
        )

        # AUD-16203：用 apply 前后的片段 ID 差集精确识别本次操作新建的
        # 片段——即插入片段与分裂产生的左右半段。旧实现按
        # "timeline_start == playhead 且 asset_id 相同" 启发式找插入片段，
        # undo 再按 "不在 original/modified 集合且 asset 不同" 启发式删
        # "分裂残留"，会把同轨上未受影响的其他素材片段整体误删。
        new_track = new_timeline.get_track_by_id(self.track_id)
        inserted_clip_id: str | None = None
        split_clip_ids: tuple[str, ...] = ()
        if new_track is not None:
            created_ids = {
                c.clip_id for c in new_track.clips
                if c.clip_id not in pre_apply_ids
            }
            for clip in new_track.clips:
                if clip.clip_id in created_ids and (
                    clip.timeline_start == self.playhead
                    and clip.asset_id == self.asset_id
                ):
                    inserted_clip_id = clip.clip_id
                    break
            if inserted_clip_id is None and created_ids:
                # 防御回退：新 ID 中取第一个（正常不会走到——
                # overwrite_clip_at_playhead 总是新建插入片段）
                inserted_clip_id = sorted(created_ids)[0]
            split_clip_ids = tuple(
                sorted(created_ids - {inserted_clip_id})
            )

        # Return updated command with stored values for undo
        return new_timeline, OverwriteClipCommand(
            command_id=self.command_id,
            label=self.label,
            asset_id=self.asset_id,
            in_frame=self.in_frame,
            out_frame=self.out_frame,
            playhead=self.playhead,
            track_id=self.track_id,
            speed=self.speed,
            _inserted_clip_id=inserted_clip_id,
            _modified_clips=affected_clip_ids,
            _original_clips=tuple(affected_clips),
            _split_clip_ids=split_clip_ids,
            _source_fps_num=self._source_fps_num,
            _source_fps_den=self._source_fps_den,
            _timeline_fps_num=self._timeline_fps_num,
            _timeline_fps_den=self._timeline_fps_den,
            _before_transitions=before_transitions,
            _before_markers=before_markers,
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove inserted clip and restore modified clips.

        Args:
            timeline: Current timeline

        Returns:
            Timeline restored to original state
        """
        if self._inserted_clip_id is None:
            raise ValueError("Cannot undo: inserted clip ID not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Remove the inserted clip
        new_track = track.with_clip_removed(self._inserted_clip_id)

        # AUD-16203：先按 apply 记录的白名单删除分裂产生的残留片段
        # （左右半段等新建 ID），再还原原片段。顺序很重要：残留片段与
        # 被分裂的原片段时间区间重叠，先还原后删除会被 with_clip_added
        # 的重叠校验拒绝。
        # 注意：只删白名单内的 ID。旧启发式 "不在 original/modified 集合
        # 且 asset 不同即删除" 会把同轨未受影响的其他素材片段整体误删。
        # 白名单为 None（旧序列化历史）时不删任何片段，宁可留下残留
        # 也不误删用户内容。
        for split_id in (self._split_clip_ids or ()):
            if split_id != self._inserted_clip_id:
                new_track = new_track.with_clip_removed(split_id)

        # Restore original clips
        if self._original_clips:
            for original_clip in self._original_clips:
                # Check if clip still exists (might have been split)
                existing = new_track.get_clip_by_id(original_clip.clip_id)
                if existing is not None:
                    # Update to original state
                    new_track = new_track.with_clip_updated(original_clip)
                else:
                    # Clip was deleted/split, add it back
                    new_track = new_track.with_clip_added(original_clip)

        new_timeline = timeline.with_track_updated(new_track)

        # N08/C3-08：恢复整轨转场与序列 marker 快照（迁移/删除的关联
        # 状态随 Undo 一并复原，不只回 clips）
        if self._before_transitions is not None:
            new_timeline = new_timeline.with_track_updated(
                new_track._rebuild(transitions=self._before_transitions)
            )
        if self._before_markers is not None:
            new_timeline = new_timeline._rebuild(
                markers=self._before_markers
            )

        logger.debug(f"OverwriteClipCommand undone: removed {self._inserted_clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "OverwriteClipCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "playhead": self.playhead,
            "track_id": self.track_id,
            "speed": self.speed,
            "inserted_clip_id": self._inserted_clip_id,
            "modified_clips": list(self._modified_clips) if self._modified_clips else None,
            "original_clips": [c.to_dict() for c in self._original_clips] if self._original_clips else None,
            "split_clip_ids": list(self._split_clip_ids) if self._split_clip_ids else None,
            # N06/C3-06：fps 时基字段必须随载荷往返（30fps 源 30 帧在
            # 24fps 序列 apply 24 帧；丢字段回放变 30 帧）
            "source_fps_num": self._source_fps_num,
            "source_fps_den": self._source_fps_den,
            "timeline_fps_num": self._timeline_fps_num,
            "timeline_fps_den": self._timeline_fps_den,
            # N08/C3-08：转场/marker 快照随载荷往返
            "before_transitions": [t.to_dict() for t in self._before_transitions] if self._before_transitions else None,
            "before_markers": [m.to_dict() for m in self._before_markers] if self._before_markers else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> OverwriteClipCommand:
        """Deserialize command."""
        modified_ids = data.get("modified_clips")
        original_clips_data = data.get("original_clips")
        original_clips = tuple(
            ClipInstance.from_dict(c) for c in original_clips_data
        ) if original_clips_data else None
        split_ids = data.get("split_clip_ids")

        return OverwriteClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Overwrite Clip"),
            asset_id=data.get("asset_id", ""),
            in_frame=data.get("in_frame", 0),
            out_frame=data.get("out_frame", 0),
            playhead=data.get("playhead", 0),
            track_id=data.get("track_id", ""),
            speed=data.get("speed", 1.0),
            _inserted_clip_id=data.get("inserted_clip_id"),
            _modified_clips=tuple(modified_ids) if modified_ids else None,
            _original_clips=original_clips,
            _split_clip_ids=tuple(split_ids) if split_ids else None,
            _source_fps_num=data.get("source_fps_num"),
            _source_fps_den=data.get("source_fps_den"),
            _timeline_fps_num=data.get("timeline_fps_num"),
            _timeline_fps_den=data.get("timeline_fps_den"),
            _before_transitions=_transitions_from_dicts(data.get("before_transitions")),
            _before_markers=tuple(
                TimelineMarker.from_dict(m)
                for m in (data.get("before_markers") or [])
            ),
        )

    @staticmethod
    def create(
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
    ) -> OverwriteClipCommand:
        """Factory method to create command."""
        return OverwriteClipCommand(
            command_id=str(uuid.uuid4()),
            label=f"Overwrite Clip at {playhead}",
            asset_id=asset_id,
            in_frame=in_frame,
            out_frame=out_frame,
            playhead=playhead,
            track_id=track_id,
            speed=speed,
            _source_fps_num=source_fps_num,
            _source_fps_den=source_fps_den,
            _timeline_fps_num=timeline_fps_num,
            _timeline_fps_den=timeline_fps_den,
        )


# ================================================================
# RollTrimCommand (P4.3)
# ================================================================

@dataclass(frozen=True)
class RollTrimCommand:
    """
    Command to perform roll trim on adjacent clips.

    Roll trim moves the cut point between two adjacent clips:
    - Left clip's right edge moves (duration changes)
    - Right clip's left edge moves (source_start and duration change opposite)
    - Timeline total duration remains unchanged

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing both clips
        left_clip_id: Left (earlier) clip ID
        right_clip_id: Right (later) clip ID
        delta_frames: Frames to move cut point (positive = right, negative = left)
        _original_left_clip: Original left clip state (for undo)
        _original_right_clip: Original right clip state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    left_clip_id: str
    right_clip_id: str
    delta_frames: int
    _original_left_clip: ClipInstance | None = None
    _original_right_clip: ClipInstance | None = None
    # R04：整轨转场快照（中间态重锚曾删合法 seam 转场，undo 完整回滚）
    _before_transitions: tuple = None
    _before_markers: tuple | None = None
    left_max_source_frames: int | None = None
    right_max_source_frames: int | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: perform roll trim.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with roll trim applied
        """
        from src.features.edit.core.timeline_ops import roll_trim_clips

        # N04/C3-04：锁轨拒绝（Roll 是内容写入口，此前可绕锁）
        assert_track_editable(timeline, self.track_id, "Roll trim")

        # Store original clips for undo
        left_clip = timeline.get_clip_by_id(self.left_clip_id)
        right_clip = timeline.get_clip_by_id(self.right_clip_id)

        if left_clip is None:
            raise ValueError(f"Left clip not found: {self.left_clip_id}")
        if right_clip is None:
            raise ValueError(f"Right clip not found: {self.right_clip_id}")

        # Apply roll trim operation
        new_timeline = roll_trim_clips(
            timeline,
            self.left_clip_id,
            self.right_clip_id,
            self.delta_frames,
            self.left_max_source_frames,
            self.right_max_source_frames,
        )

        # Return updated command with stored originals for undo
        return new_timeline, RollTrimCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            left_clip_id=self.left_clip_id,
            right_clip_id=self.right_clip_id,
            delta_frames=self.delta_frames,
            left_max_source_frames=self.left_max_source_frames,
            right_max_source_frames=self.right_max_source_frames,
            _original_left_clip=left_clip,
            _original_right_clip=right_clip,
            _before_markers=timeline.markers,
            _before_transitions=copy.deepcopy(
                timeline.get_track_by_id(self.track_id).transitions
            ),
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original clip states.

        C01：向左 Roll 后撤销曾按顺序 with_clip_updated 恢复两片——先恢复
        的左片（延长回原时长）立即撞上仍在新位置的右片，抛 "Updated clip
        overlaps"，历史从此卡死。改为用快照建立替换映射，一次
        ``track._rebuild`` 原子恢复整组 clips（重叠校验只看最终状态）。
        """
        if self._original_left_clip is None or self._original_right_clip is None:
            raise ValueError("Cannot undo: original clips not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Restore original clips atomically
        replacement = {
            self._original_left_clip.clip_id: self._original_left_clip,
            self._original_right_clip.clip_id: self._original_right_clip,
        }
        restored_clips = tuple(
            replacement.get(c.clip_id, c) for c in track.clips
        )
        new_track = track._rebuild(clips=restored_clips)
        new_timeline = timeline.with_track_updated(new_track)
        # R04：恢复 roll 前整轨转场快照
        if self._before_transitions is not None:
            track = new_timeline.get_track_by_id(self.track_id)
            if track is not None:
                new_timeline = new_timeline.with_track_updated(
                    track._rebuild(transitions=self._before_transitions)
                )

        if self._before_markers is not None:
            new_timeline = new_timeline._rebuild(markers=self._before_markers)
        logger.debug(
            f"RollTrimCommand undone: restored {self.left_clip_id} and {self.right_clip_id}"
        )
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RollTrimCommand",
            "label": self.label,
            "track_id": self.track_id,
            "left_clip_id": self.left_clip_id,
            "right_clip_id": self.right_clip_id,
            "delta_frames": self.delta_frames,
            "left_max_source_frames": self.left_max_source_frames,
            "right_max_source_frames": self.right_max_source_frames,
            "before_markers": ([m.to_dict() for m in self._before_markers]
                               if self._before_markers is not None else None),
            "before_transitions": ([t.to_dict() for t in self._before_transitions]
                                   if self._before_transitions is not None else None),
            "original_left_clip": self._original_left_clip.to_dict() if self._original_left_clip else None,
            "original_right_clip": self._original_right_clip.to_dict() if self._original_right_clip else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RollTrimCommand:
        """Deserialize command."""
        left_clip_data = data.get("original_left_clip")
        right_clip_data = data.get("original_right_clip")
        original_left = ClipInstance.from_dict(left_clip_data) if left_clip_data else None
        original_right = ClipInstance.from_dict(right_clip_data) if right_clip_data else None

        return RollTrimCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Roll Trim"),
            track_id=data.get("track_id", ""),
            left_clip_id=data.get("left_clip_id", ""),
            right_clip_id=data.get("right_clip_id", ""),
            delta_frames=data.get("delta_frames", 0),
            left_max_source_frames=data.get("left_max_source_frames"),
            right_max_source_frames=data.get("right_max_source_frames"),
            _before_transitions=_transitions_from_dicts(data.get("before_transitions")),
            _before_markers=(tuple(TimelineMarker.from_dict(m) for m in data["before_markers"])
                             if data.get("before_markers") is not None else None),
            _original_left_clip=original_left,
            _original_right_clip=original_right,
        )

    @staticmethod
    def create(
        track_id: str,
        left_clip_id: str,
        right_clip_id: str,
        delta_frames: int,
        left_max_source_frames: int | None = None,
        right_max_source_frames: int | None = None,
    ) -> RollTrimCommand:
        """Factory method to create command."""
        direction = "right" if delta_frames > 0 else "left"
        return RollTrimCommand(
            command_id=str(uuid.uuid4()),
            label=f"Roll Trim {direction} by {abs(delta_frames)} frames",
            track_id=track_id,
            left_clip_id=left_clip_id,
            right_clip_id=right_clip_id,
            delta_frames=delta_frames,
            left_max_source_frames=left_max_source_frames,
            right_max_source_frames=right_max_source_frames,
        )


# ================================================================
# RippleTrimCommand (P4.2)
# ================================================================

@dataclass(frozen=True)
class RippleTrimCommand:
    """
    Command to ripple trim a clip (P4.2).

    Ripple trim: When trimming a clip edge, all subsequent clips shift
    to fill the gap (no gap created).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to trim
        trim_side: Which side to trim ('left' or 'right')
        new_timeline_start: New timeline start (for left trim)
        new_timeline_end: New timeline end (for right trim)
        _affected_clips: Clips affected by ripple (for undo)
        _original_clip: Original clip before trim
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    trim_side: str  # 'left' or 'right'
    new_timeline_start: int | None = None  # For left trim
    new_timeline_end: int | None = None  # For right trim
    _affected_clips: tuple[ClipInstance, ...] | None = None  # Clips shifted by ripple
    _original_clip: ClipInstance | None = None  # Original clip before trim
    # P1-05：跨轨 sync-lock 跟随。链接 A/V 波纹 trim 由视图组成两条命令的
    # 宏——第二条命令置 apply_sync_lock=False，避免其他轨道双重位移。
    apply_sync_lock: bool = True
    _sync_lock_moves: tuple[dict[str, Any], ...] = ()
    # R08：目标轨跟随片段的 scoped marker 现随波纹位移，undo 用快照还原
    _before_markers: tuple = None
    _before_transitions: tuple | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: ripple trim clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip ripple trimmed and subsequent clips shifted
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # R01：锁定轨道拒绝波纹修剪
        assert_track_editable(timeline, self.track_id, "Ripple trim clip")

        # Store original clip and affected clips for undo（R01 深拷贝）
        original_clip = copy.deepcopy(clip)
        affected_clips = copy.deepcopy(tuple(
            c for c in track.clips
            if c.timeline_start > clip.timeline_start
        ))

        # Import ripple trim operations
        from src.features.edit.core.timeline_ops import ripple_trim_clip_left, ripple_trim_clip_right

        # Apply ripple trim
        if self.trim_side == "left":
            if self.new_timeline_start is None:
                raise ValueError("new_timeline_start required for left ripple trim")
            new_timeline = ripple_trim_clip_left(
                timeline,
                self.clip_id,
                self.new_timeline_start
            )
        elif self.trim_side == "right":
            if self.new_timeline_end is None:
                raise ValueError("new_timeline_end required for right ripple trim")
            new_timeline = ripple_trim_clip_right(
                timeline,
                self.clip_id,
                self.new_timeline_end
            )
        else:
            raise ValueError(f"Invalid trim_side: {self.trim_side}")

        # P1-05 Sync Lock：其余同步锁轨道跟随与本轨 followers 相同的位移。
        # 波纹点 = 被移除/插入区间的终点（与 ripple-delete 等价语义一致）：
        # - 左缩短 n：等价删除头部 [S, S+n) → 其余轨 start >= S+n 左移 n；
        #   左延伸不动 followers（入点锚定语义），其余轨也不动。
        # - 右侧：等价删除尾部 [new_E, E) 或在 E 插入 n 帧 → 其余轨
        #   start >= E 按 (new_E - E) 位移。
        sync_lock_moves: tuple[dict[str, Any], ...] = ()
        if self.apply_sync_lock:
            sync_delta = 0
            sync_point = 0
            if self.trim_side == "left":
                assert self.new_timeline_start is not None
                if self.new_timeline_start > clip.timeline_start:
                    sync_delta = -(self.new_timeline_start - clip.timeline_start)
                    sync_point = self.new_timeline_start
            else:
                assert self.new_timeline_end is not None
                sync_delta = self.new_timeline_end - clip.timeline_end
                sync_point = clip.timeline_end

            if sync_delta != 0:
                exclude = [self.track_id]
                linked = timeline.get_reciprocal_linked_clip(clip)
                if linked is not None:
                    exclude.append(linked.track_id)
                from src.features.edit.core.timeline_ops import shift_sync_lock_followers
                new_timeline, sync_lock_moves = shift_sync_lock_followers(
                    new_timeline,
                    exclude_track_ids=exclude,
                    from_frame=sync_point,
                    delta=sync_delta,
                )

        logger.debug(
            f"RippleTrimCommand applied: {self.clip_id} "
            f"{self.trim_side} trim, {len(affected_clips)} clips shifted"
        )

        return new_timeline, replace(
            self,
            _sync_lock_moves=sync_lock_moves,
            _original_clip=self._original_clip or original_clip,
            _affected_clips=(
                self._affected_clips
                if self._affected_clips is not None
                else affected_clips
            ),
            _before_transitions=(self._before_transitions if self._before_transitions is not None
                                 else timeline.get_track_by_id(self.track_id).transitions),
            _before_markers=self._before_markers
            if self._before_markers is not None
            else copy.deepcopy(timeline.markers),
        )

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original clip and affected clips.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original clip and clip positions restored
        """
        # P1-05：先还原跨轨同步锁跟随位移（独立于本轨快照，顺序无关）
        from src.features.edit.core.timeline_ops import restore_sync_lock_shifts
        timeline = restore_sync_lock_shifts(timeline, self._sync_lock_moves)
        # R08：还原目标轨 scoped marker（apply 已随波纹位移）
        if self._before_markers is not None:
            timeline = timeline._rebuild(markers=self._before_markers)

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Restore original clip
        if self._original_clip is None:
            raise ValueError("Cannot undo: original clip not stored")

        current_clip = track.get_clip_by_id(self.clip_id)
        if current_clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # P1-04 审计修复：undo 用快照原子还原。
        # _affected_clips 本就是 apply 前的原位快照——直接各归各位即可，
        # 不需要用 delta 反推（旧反推在入点锚定语义下方向错误，且
        # 逐个 with_clip_updated 会因瞬时重叠误抛）。
        snapshot_by_id = {c.clip_id: c for c in (self._affected_clips or ())}
        new_clips = tuple(
            self._original_clip if c.clip_id == self.clip_id
            else snapshot_by_id.get(c.clip_id, c)
            for c in track.clips
        )
        new_track = track._rebuild(clips=new_clips, **(
            {"transitions": self._before_transitions}
            if self._before_transitions is not None else {}))

        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"RippleTrimCommand undone: {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RippleTrimCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "trim_side": self.trim_side,
            "new_timeline_start": self.new_timeline_start,
            "new_timeline_end": self.new_timeline_end,
            "affected_clips": [c.to_dict() for c in self._affected_clips] if self._affected_clips else None,
            "original_clip": self._original_clip.to_dict() if self._original_clip else None,
            "apply_sync_lock": self.apply_sync_lock,
            "sync_lock_moves": list(self._sync_lock_moves),
            "before_markers": ([m.to_dict() for m in self._before_markers]
                               if self._before_markers is not None else None),
            "before_transitions": ([t.to_dict() for t in self._before_transitions]
                                   if self._before_transitions is not None else None),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RippleTrimCommand:
        """Deserialize command."""
        affected_data = data.get("affected_clips")
        affected_clips = tuple(ClipInstance.from_dict(c) for c in affected_data) if affected_data else None

        original_data = data.get("original_clip")
        original_clip = ClipInstance.from_dict(original_data) if original_data else None

        return RippleTrimCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Ripple Trim"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            trim_side=data.get("trim_side", "right"),
            new_timeline_start=data.get("new_timeline_start"),
            new_timeline_end=data.get("new_timeline_end"),
            _affected_clips=affected_clips,
            _original_clip=original_clip,
            apply_sync_lock=data.get("apply_sync_lock", True),
            _before_markers=(tuple(TimelineMarker.from_dict(m) for m in data["before_markers"])
                             if data.get("before_markers") is not None else None),
            _before_transitions=_transitions_from_dicts(data.get("before_transitions")),
            _sync_lock_moves=tuple(data.get("sync_lock_moves", [])),
        )

    @staticmethod
    def create_left_trim(
        track_id: str,
        clip_id: str,
        new_start: int,
        original_clip: ClipInstance | None = None,
        affected_clips: tuple[ClipInstance, ...] | None = None,
        apply_sync_lock: bool = True,
    ) -> RippleTrimCommand:
        """Factory method for left ripple trim."""
        return RippleTrimCommand(
            command_id=str(uuid.uuid4()),
            label=f"Ripple Trim In to {new_start}",
            track_id=track_id,
            clip_id=clip_id,
            trim_side="left",
            new_timeline_start=new_start,
            _original_clip=original_clip,
            _affected_clips=affected_clips,
            apply_sync_lock=apply_sync_lock,
        )

    @staticmethod
    def create_right_trim(
        track_id: str,
        clip_id: str,
        new_end: int,
        original_clip: ClipInstance | None = None,
        affected_clips: tuple[ClipInstance, ...] | None = None,
        apply_sync_lock: bool = True,
    ) -> RippleTrimCommand:
        """Factory method for right ripple trim."""
        return RippleTrimCommand(
            command_id=str(uuid.uuid4()),
            label=f"Ripple Trim Out to {new_end}",
            track_id=track_id,
            clip_id=clip_id,
            trim_side="right",
            new_timeline_end=new_end,
            _original_clip=original_clip,
            _affected_clips=affected_clips,
            apply_sync_lock=apply_sync_lock,
        )


# ================================================================
# SetClipLinkedCommand
# ================================================================

class SetClipLinkedCommand:
    """Set or clear the linked_clip_id between two clips."""

    def __init__(self, command_id: str, label: str,
                 clip_a_id: str, clip_a_track_id: str,
                 clip_b_id: str, clip_b_track_id: str,
                 link: bool) -> None:
        self.command_id = command_id
        self.label = label
        self.clip_a_id = clip_a_id
        self.clip_a_track_id = clip_a_track_id
        self.clip_b_id = clip_b_id
        self.clip_b_track_id = clip_b_track_id
        self.link = link
        self._previous_links: tuple[tuple[str, str | None], ...] | None = None

    def _capture_previous_links(self, timeline: TimelineSequence) -> None:
        if self._previous_links is not None:
            return
        affected_ids = {self.clip_a_id, self.clip_b_id}
        for clip_id in (self.clip_a_id, self.clip_b_id):
            clip = timeline.get_clip_by_id(clip_id)
            if clip is not None and clip.linked_clip_id:
                partner = timeline.get_clip_by_id(clip.linked_clip_id)
                if partner is not None:
                    affected_ids.add(partner.clip_id)
        self._previous_links = tuple(
            (clip_id, timeline.get_clip_by_id(clip_id).linked_clip_id)
            for clip_id in sorted(affected_ids)
            if timeline.get_clip_by_id(clip_id) is not None
        )

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        # C09/INT-05：链接/解链改动两条轨（含被替换的旧伙伴），全部必须可编辑
        assert_track_editable(timeline, self.clip_a_track_id, "Change clip link")
        assert_track_editable(timeline, self.clip_b_track_id, "Change clip link")
        self._capture_previous_links(timeline)
        for clip_id, _prev in (self._previous_links or ()):
            linked = timeline.get_clip_by_id(clip_id)
            if linked is not None:
                assert_track_editable(
                    timeline, linked.track_id, "Change clip link"
                )
        if self.link:
            clip_a = timeline.get_clip_by_id(self.clip_a_id)
            clip_b = timeline.get_clip_by_id(self.clip_b_id)
            if clip_a is None or clip_b is None:
                raise ValueError("Both clips must exist before linking")
            if self.clip_a_id == self.clip_b_id:
                raise ValueError("A clip cannot be linked to itself")

            track_a = timeline.get_track_by_id(clip_a.track_id)
            track_b = timeline.get_track_by_id(clip_b.track_id)
            if track_a is None or track_b is None:
                raise ValueError("Both clips must belong to existing tracks")
            if {track_a.track_type, track_b.track_type} != {"video", "audio"}:
                raise ValueError("Only one video clip and one audio clip can be linked")

            for old_link_id, owner_id in (
                (clip_a.linked_clip_id, self.clip_a_id),
                (clip_b.linked_clip_id, self.clip_b_id),
            ):
                if old_link_id and old_link_id not in (self.clip_a_id, self.clip_b_id):
                    old_partner = timeline.get_clip_by_id(old_link_id)
                    if old_partner is not None and old_partner.linked_clip_id == owner_id:
                        timeline = timeline.with_clip_updated(
                            old_partner.with_linked_clip_id(None)
                        )

            clip_a = timeline.get_clip_by_id(self.clip_a_id)
            clip_b = timeline.get_clip_by_id(self.clip_b_id)
            if clip_a is None or clip_b is None:
                return timeline
            timeline = timeline.with_clip_updated(
                clip_a.with_linked_clip_id(self.clip_b_id))
            clip_b = timeline.get_clip_by_id(self.clip_b_id)
            timeline = timeline.with_clip_updated(
                clip_b.with_linked_clip_id(self.clip_a_id))
        else:
            clip_a = timeline.get_clip_by_id(self.clip_a_id)
            clip_b = timeline.get_clip_by_id(self.clip_b_id)
            if clip_a:
                timeline = timeline.with_clip_updated(
                    clip_a.with_linked_clip_id(None))
            clip_b = timeline.get_clip_by_id(self.clip_b_id)
            if clip_b:
                timeline = timeline.with_clip_updated(
                    clip_b.with_linked_clip_id(None))
        return timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous_links is None:
            return timeline
        current = timeline
        for clip_id, linked_id in self._previous_links:
            clip = current.get_clip_by_id(clip_id)
            if clip is not None:
                current = current.with_clip_updated(clip.with_linked_clip_id(linked_id))
        return current

    @staticmethod
    def create_link(clip_a_id: str, clip_a_track_id: str,
                    clip_b_id: str, clip_b_track_id: str) -> SetClipLinkedCommand:
        return SetClipLinkedCommand(
            command_id=str(uuid.uuid4()),
            label="Link clips",
            clip_a_id=clip_a_id,
            clip_a_track_id=clip_a_track_id,
            clip_b_id=clip_b_id,
            clip_b_track_id=clip_b_track_id,
            link=True,
        )

    @staticmethod
    def create_unlink(clip_a_id: str, clip_a_track_id: str,
                      clip_b_id: str, clip_b_track_id: str) -> SetClipLinkedCommand:
        return SetClipLinkedCommand(
            command_id=str(uuid.uuid4()),
            label="Unlink clips",
            clip_a_id=clip_a_id,
            clip_a_track_id=clip_a_track_id,
            clip_b_id=clip_b_id,
            clip_b_track_id=clip_b_track_id,
            link=False,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured during apply)."""
        return {
            "command_id": self.command_id,
            "command_type": "SetClipLinkedCommand",
            "label": self.label,
            "clip_a_id": self.clip_a_id,
            "clip_a_track_id": self.clip_a_track_id,
            "clip_b_id": self.clip_b_id,
            "clip_b_track_id": self.clip_b_track_id,
            "link": self.link,
            "previous_links": (
                [list(pair) for pair in self._previous_links]
                if self._previous_links is not None
                else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetClipLinkedCommand:
        """Deserialize command."""
        previous_links = data.get("previous_links")
        command = SetClipLinkedCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set clip link"),
            clip_a_id=data.get("clip_a_id", ""),
            clip_a_track_id=data.get("clip_a_track_id", ""),
            clip_b_id=data.get("clip_b_id", ""),
            clip_b_track_id=data.get("clip_b_track_id", ""),
            link=bool(data.get("link", False)),
        )
        if previous_links is not None:
            command._previous_links = tuple(
                (str(pair[0]), pair[1]) for pair in previous_links
            )
        return command
