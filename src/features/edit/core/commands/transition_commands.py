"""Transition commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: transitions cluster (add/remove/
update transitions between adjacent clips). Class sources are
byte-identical to the original module; to_dict/from_dict keys are a
persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import uuid

from src.features.edit.core.timeline_v2 import TimelineSequence, TransitionInstance

logger = logging.getLogger(__name__)


# ================================================================
# Transition Commands (转场命令)
# ================================================================


@dataclass(frozen=True)
class AddTransitionCommand:
    """
    Command to add a transition between two adjacent clips.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        transition: TransitionInstance to add
        _track_id: Track ID containing the clips
    """

    command_id: str
    label: str
    transition: TransitionInstance
    _track_id: str | None = None  # Set during apply for undo
    _replaced_transition: TransitionInstance | None = None  # Set when replacing an existing cut transition

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: add transition to track.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with transition added

        Raises:
            ValueError: If track not found or clips not adjacent
        """
        track = timeline.get_track_by_id(self.transition.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.transition.track_id}")

        # Verify a two-sided cut transition or a one-sided clip-edge
        # transition.  Keeping this validation in the command prevents UI and
        # non-UI callers from creating a card in the middle of a clip.
        left_clip = (
            track.get_clip_by_id(self.transition.left_clip_id)
            if self.transition.left_clip_id else None
        )
        right_clip = (
            track.get_clip_by_id(self.transition.right_clip_id)
            if self.transition.right_clip_id else None
        )
        if left_clip is None and right_clip is None:
            raise ValueError("Transition must reference at least one existing clip")
        if self.transition.left_clip_id and left_clip is None:
            raise ValueError(f"Left clip not found: {self.transition.left_clip_id}")
        if self.transition.right_clip_id and right_clip is None:
            raise ValueError(f"Right clip not found: {self.transition.right_clip_id}")

        # A repeated add at the same logical edge is a replacement.  The
        # incoming command is only an effect selector in that case; the
        # existing position/duration must be preserved.
        existing_transition = next(
            (
                transition
                for transition in track.transitions
                if transition.left_clip_id == self.transition.left_clip_id
                and transition.right_clip_id == self.transition.right_clip_id
            ),
            None,
        )

        if existing_transition is None and left_clip is not None and right_clip is not None:
            _, next_ = track.get_adjacent_clips(self.transition.left_clip_id)
            if next_ is None or next_.clip_id != self.transition.right_clip_id:
                raise ValueError(
                    f"Clips {self.transition.left_clip_id} and "
                    f"{self.transition.right_clip_id} are not adjacent"
                )
        elif existing_transition is None and right_clip is not None:
            if (
                self.transition.alignment != "start_on_cut"
                or self.transition.center_frame != right_clip.timeline_start
                or self.transition.start_frame < right_clip.timeline_start
                or self.transition.end_frame > right_clip.timeline_end
            ):
                raise ValueError("Head transition must be fully contained at the clip start")
        elif existing_transition is None:
            if (
                self.transition.alignment != "end_on_cut"
                or self.transition.center_frame != left_clip.timeline_end
                or self.transition.start_frame < left_clip.timeline_start
                or self.transition.end_frame > left_clip.timeline_end
            ):
                raise ValueError("Tail transition must be fully contained at the clip end")

        # A cut can own only one transition. If one already exists for this
        # clip pair, replace its effect parameters but keep the existing ID,
        # duration, center frame and alignment. This also protects non-UI
        # callers (for example AI editing commands) from stacking transitions.
        if existing_transition is not None:
            replacement = TransitionInstance(
                transition_id=existing_transition.transition_id,
                transition_type=self.transition.transition_type,
                track_id=existing_transition.track_id,
                left_clip_id=existing_transition.left_clip_id,
                right_clip_id=existing_transition.right_clip_id,
                center_frame=existing_transition.center_frame,
                duration=existing_transition.duration,
                alignment=existing_transition.alignment,
                params=dict(self.transition.params),
            )
            object.__setattr__(self, "_replaced_transition", existing_transition)
            new_transitions = tuple(
                replacement if transition.transition_id == existing_transition.transition_id else transition
                for transition in track.transitions
            )
            logger.debug(
                "AddTransitionCommand replaced %s at cut %s -> %s",
                existing_transition.transition_id,
                existing_transition.left_clip_id,
                replacement.transition_type,
            )
        else:
            new_transitions = track.transitions + (self.transition,)

        # 审计修复 AUD-16201：_rebuild 保留全部轨道字段（手工 EditTrack
        # 重建会丢 strip_eq/effect_chain/automation/sends/output_bus_id/
        # is_sync_locked/color/subtitle_clips/caption_metadata 等 14+ 字段）
        new_track = track._rebuild(transitions=new_transitions)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"AddTransitionCommand applied: {self.transition.transition_id} "
            f"between {self.transition.left_clip_id} and {self.transition.right_clip_id}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove the transition.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the transition
        """
        track = timeline.get_track_by_id(self.transition.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.transition.track_id}")

        if self._replaced_transition is not None:
            # Restore the original transition at the same cut.
            new_transitions = tuple(
                self._replaced_transition
                if transition.transition_id == self._replaced_transition.transition_id
                else transition
                for transition in track.transitions
            )
        else:
            # Remove a newly added transition from track.
            new_transitions = tuple(
                transition
                for transition in track.transitions
                if transition.transition_id != self.transition.transition_id
            )

        # 审计修复 AUD-16201：_rebuild 保留全部轨道字段（见 apply 内注释）
        new_track = track._rebuild(transitions=new_transitions)

        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"AddTransitionCommand undone: removed {self.transition.transition_id}"
        )
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "AddTransitionCommand",
            "label": self.label,
            "transition": self.transition.to_dict(),
            "track_id": self._track_id,
            "replaced_transition": (
                self._replaced_transition.to_dict()
                if self._replaced_transition else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddTransitionCommand:
        """Deserialize command."""
        return AddTransitionCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Transition"),
            transition=TransitionInstance.from_dict(data.get("transition", {})),
            _track_id=data.get("track_id"),
            _replaced_transition=(
                TransitionInstance.from_dict(data["replaced_transition"])
                if data.get("replaced_transition") else None
            ),
        )

    @staticmethod
    def create(
        track_id: str,
        left_clip_id: str,
        right_clip_id: str,
        duration: int,
        transition_type: str = "cross_dissolve",
        alignment: str = "centered",
        params: dict[str, Any] | None = None,
    ) -> AddTransitionCommand:
        """
        Factory method to create AddTransitionCommand.

        Args:
            track_id: Track ID containing the clips
            left_clip_id: Left (outgoing) clip ID
            right_clip_id: Right (incoming) clip ID
            duration: Transition duration in frames
            transition_type: Type of transition from the shared transition catalogue.
            alignment: Alignment mode (centered, start_on_cut, end_on_cut)
            params: Additional parameters for the transition

        Returns:
            AddTransitionCommand instance
        """
        transition = TransitionInstance(
            transition_id=str(uuid.uuid4()),
            transition_type=transition_type,
            track_id=track_id,
            left_clip_id=left_clip_id,
            right_clip_id=right_clip_id,
            center_frame=0,  # Will be set based on clip positions
            duration=duration,
            alignment=alignment,
            params=params or {},
        )

        return AddTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add {transition_type} transition ({duration} frames)",
            transition=transition,
            _track_id=track_id,
        )

    @staticmethod
    def create_from_clip_gap(
        timeline: TimelineSequence,
        track_id: str,
        left_clip_id: str,
        right_clip_id: str,
        duration: int,
        transition_type: str = "cross_dissolve",
        alignment: str = "centered",
        params: dict[str, Any] | None = None,
    ) -> AddTransitionCommand:
        """
        Factory method to create AddTransitionCommand with correct center_frame.

        Calculates the center_frame based on the actual clip positions.

        Args:
            timeline: Current timeline (to get clip positions)
            track_id: Track ID containing the clips
            left_clip_id: Left (outgoing) clip ID
            right_clip_id: Right (incoming) clip ID
            duration: Transition duration in frames
            transition_type: Type of transition
            alignment: Alignment mode
            params: Additional parameters

        Returns:
            AddTransitionCommand instance with calculated center_frame
        """
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        left_clip = track.get_clip_by_id(left_clip_id)
        right_clip = track.get_clip_by_id(right_clip_id)

        if left_clip is None or right_clip is None:
            raise ValueError(f"Clips not found: {left_clip_id}, {right_clip_id}")

        # Calculate center_frame based on the cut point
        # The cut point is where left clip ends and right clip starts
        cut_frame = left_clip.timeline_end

        # 审计修复：转场必须落在两片段并集内——切点两侧可用帧数钳制
        # 时长（旧代码不钳制：右片段 4 帧 + 12 帧居中转场会越出片段
        # 终点存进模型，无报错无提示）
        max_duration = min(
            2 * (cut_frame - left_clip.timeline_start),
            2 * (right_clip.timeline_start + right_clip.timeline_duration
                 - cut_frame),
        )
        duration = max(1, min(int(duration), max_duration))

        # For centered alignment, center_frame is the cut point
        # For start_on_cut, center_frame is also the cut point
        # For end_on_cut, center_frame is cut point (transition ends at cut)
        center_frame = cut_frame

        transition = TransitionInstance(
            transition_id=str(uuid.uuid4()),
            transition_type=transition_type,
            track_id=track_id,
            left_clip_id=left_clip_id,
            right_clip_id=right_clip_id,
            center_frame=center_frame,
            duration=duration,
            alignment=alignment,
            params=params or {},
        )

        return AddTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add {transition_type} transition ({duration} frames)",
            transition=transition,
            _track_id=track_id,
        )

    @staticmethod
    def create_from_clip_edge(
        timeline: TimelineSequence,
        track_id: str,
        clip_id: str,
        edge: str,
        duration: int,
        transition_type: str = "cross_dissolve",
        params: dict[str, Any] | None = None,
    ) -> AddTransitionCommand:
        """Create a one-sided transition anchored to a clip edge."""
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")
        clip = track.get_clip_by_id(clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {clip_id}")
        if edge not in ("left", "right"):
            raise ValueError(f"Invalid clip edge: {edge}")

        duration = max(1, min(int(duration), clip.timeline_duration))
        if edge == "left":
            left_clip_id, right_clip_id = "", clip_id
            center_frame = clip.timeline_start
            alignment = "start_on_cut"
        else:
            left_clip_id, right_clip_id = clip_id, ""
            center_frame = clip.timeline_end
            alignment = "end_on_cut"

        transition = TransitionInstance(
            transition_id=str(uuid.uuid4()),
            transition_type=transition_type,
            track_id=track_id,
            left_clip_id=left_clip_id,
            right_clip_id=right_clip_id,
            center_frame=center_frame,
            duration=duration,
            alignment=alignment,
            params=params or {},
        )
        return AddTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add {transition_type} transition ({duration} frames)",
            transition=transition,
            _track_id=track_id,
        )


@dataclass(frozen=True)
class RemoveTransitionCommand:
    """
    Command to remove a transition.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        transition_id: Transition ID to remove
        track_id: Track ID containing the transition
        _deleted_transition: Internal: transition data for undo
    """

    command_id: str
    label: str
    transition_id: str
    track_id: str
    _deleted_transition: TransitionInstance | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: remove transition.

        Args:
            timeline: Current timeline

        Returns:
            New timeline without the transition

        Raises:
            ValueError: If track or transition not found
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Find and store transition for undo
        transition = None
        for t in track.transitions:
            if t.transition_id == self.transition_id:
                transition = t
                break

        if transition is None:
            raise ValueError(f"Transition not found: {self.transition_id}")

        # Remove transition
        new_transitions = tuple(
            t for t in track.transitions
            if t.transition_id != self.transition_id
        )

        # 审计修复 AUD-16201：_rebuild 保留全部轨道字段（见
        # AddTransitionCommand.apply 内注释）
        new_track = track._rebuild(transitions=new_transitions)

        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"RemoveTransitionCommand applied: removed {self.transition_id}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore the transition.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with transition restored

        Raises:
            ValueError: If deleted transition not stored
        """
        if self._deleted_transition is None:
            raise ValueError("Cannot undo: deleted transition not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Add transition back
        new_track = track.with_transition_added(self._deleted_transition)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"RemoveTransitionCommand undone: restored {self.transition_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RemoveTransitionCommand",
            "label": self.label,
            "transition_id": self.transition_id,
            "track_id": self.track_id,
            "deleted_transition": (
                self._deleted_transition.to_dict()
                if self._deleted_transition else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RemoveTransitionCommand:
        """Deserialize command."""
        deleted_data = data.get("deleted_transition")
        deleted_transition = (
            TransitionInstance.from_dict(deleted_data)
            if deleted_data else None
        )

        return RemoveTransitionCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Transition"),
            transition_id=data.get("transition_id", ""),
            track_id=data.get("track_id", ""),
            _deleted_transition=deleted_transition,
        )

    @staticmethod
    def create(
        timeline: TimelineSequence,
        transition_id: str,
        track_id: str,
    ) -> RemoveTransitionCommand:
        """
        Factory method to create RemoveTransitionCommand with stored transition.

        Args:
            timeline: Current timeline (to find and store transition)
            transition_id: Transition ID to remove
            track_id: Track ID containing the transition

        Returns:
            RemoveTransitionCommand instance with stored transition for undo
        """
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        transition = None
        for t in track.transitions:
            if t.transition_id == transition_id:
                transition = t
                break

        if transition is None:
            raise ValueError(f"Transition not found: {transition_id}")

        return RemoveTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Remove transition {transition_id}",
            transition_id=transition_id,
            track_id=track_id,
            _deleted_transition=transition,
        )


@dataclass(frozen=True)
class UpdateTransitionCommand:
    """
    Command to update transition parameters (duration, alignment, params).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        transition_id: Transition ID to update
        track_id: Track ID containing the transition
        new_duration: New duration (frames), or None to keep existing
        new_alignment: New alignment, or None to keep existing
        new_params: New params dict, or None to keep existing
        _old_transition: Internal: original transition for undo
    """

    command_id: str
    label: str
    transition_id: str
    track_id: str
    new_duration: int | None = None
    new_center_frame: int | None = None
    new_transition_type: str | None = None
    new_alignment: str | None = None
    new_params: dict[str, Any] | None = None
    _old_transition: TransitionInstance | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: update transition.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with updated transition
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Find transition
        old_transition = None
        for t in track.transitions:
            if t.transition_id == self.transition_id:
                old_transition = t
                break

        if old_transition is None:
            raise ValueError(f"Transition not found: {self.transition_id}")

        # Create updated transition
        new_duration = (
            self.new_duration if self.new_duration is not None
            else old_transition.duration
        )
        new_center_frame = (
            self.new_center_frame if self.new_center_frame is not None
            else old_transition.center_frame
        )
        new_transition_type = (
            self.new_transition_type if self.new_transition_type is not None
            else old_transition.transition_type
        )
        new_alignment = (
            self.new_alignment if self.new_alignment is not None
            else old_transition.alignment
        )
        new_params = (
            self.new_params if self.new_params is not None
            else old_transition.params
        )

        updated_transition = TransitionInstance(
            transition_id=old_transition.transition_id,
            transition_type=new_transition_type,
            track_id=old_transition.track_id,
            left_clip_id=old_transition.left_clip_id,
            right_clip_id=old_transition.right_clip_id,
            center_frame=new_center_frame,
            duration=new_duration,
            alignment=new_alignment,
            params=new_params,
        )

        # Replace transition in track
        new_transitions = tuple(
            updated_transition if t.transition_id == self.transition_id else t
            for t in track.transitions
        )

        # 审计修复 AUD-16201：_rebuild 保留全部轨道字段（见
        # AddTransitionCommand.apply 内注释）
        new_track = track._rebuild(transitions=new_transitions)

        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"UpdateTransitionCommand applied: {self.transition_id}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original transition.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original transition restored
        """
        if self._old_transition is None:
            raise ValueError("Cannot undo: old transition not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # Restore original transition
        new_transitions = tuple(
            self._old_transition if t.transition_id == self.transition_id else t
            for t in track.transitions
        )

        # 审计修复 AUD-16201：_rebuild 保留全部轨道字段（见
        # AddTransitionCommand.apply 内注释）
        new_track = track._rebuild(transitions=new_transitions)

        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"UpdateTransitionCommand undone: {self.transition_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateTransitionCommand",
            "label": self.label,
            "transition_id": self.transition_id,
            "track_id": self.track_id,
            "new_duration": self.new_duration,
            "new_center_frame": self.new_center_frame,
            "new_transition_type": self.new_transition_type,
            "new_alignment": self.new_alignment,
            "new_params": self.new_params,
            "old_transition": (
                self._old_transition.to_dict()
                if self._old_transition else None
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateTransitionCommand:
        """Deserialize command."""
        old_data = data.get("old_transition")
        old_transition = (
            TransitionInstance.from_dict(old_data)
            if old_data else None
        )

        return UpdateTransitionCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Transition"),
            transition_id=data.get("transition_id", ""),
            track_id=data.get("track_id", ""),
            new_duration=data.get("new_duration"),
            new_center_frame=data.get("new_center_frame"),
            new_transition_type=data.get("new_transition_type"),
            new_alignment=data.get("new_alignment"),
            new_params=data.get("new_params"),
            _old_transition=old_transition,
        )

    @staticmethod
    def create_duration(
        timeline: TimelineSequence,
        transition_id: str,
        track_id: str,
        new_duration: int,
    ) -> UpdateTransitionCommand:
        """
        Factory method for duration update.

        Args:
            timeline: Current timeline
            transition_id: Transition ID
            track_id: Track ID
            new_duration: New duration in frames

        Returns:
            UpdateTransitionCommand instance
        """
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        old_transition = None
        for t in track.transitions:
            if t.transition_id == transition_id:
                old_transition = t
                break

        if old_transition is None:
            raise ValueError(f"Transition not found: {transition_id}")

        return UpdateTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change transition duration to {new_duration} frames",
            transition_id=transition_id,
            track_id=track_id,
            new_duration=new_duration,
            _old_transition=old_transition,
        )

    @staticmethod
    def create_center_frame(
        timeline: TimelineSequence,
        transition_id: str,
        track_id: str,
        new_center_frame: int,
    ) -> UpdateTransitionCommand:
        """Create an undoable transition-position update."""
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")
        old_transition = next(
            (t for t in track.transitions if t.transition_id == transition_id),
            None,
        )
        if old_transition is None:
            raise ValueError(f"Transition not found: {transition_id}")
        return UpdateTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Move transition to frame {new_center_frame}",
            transition_id=transition_id,
            track_id=track_id,
            new_center_frame=int(new_center_frame),
            _old_transition=old_transition,
        )

    @staticmethod
    def create_type(
        timeline: TimelineSequence,
        transition_id: str,
        track_id: str,
        new_transition_type: str,
    ) -> UpdateTransitionCommand:
        """Create an undoable native transition type update."""
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")
        old_transition = next(
            (t for t in track.transitions if t.transition_id == transition_id),
            None,
        )
        if old_transition is None:
            raise ValueError(f"Transition not found: {transition_id}")
        return UpdateTransitionCommand(
            command_id=str(uuid.uuid4()),
            label=f"Change transition type to {new_transition_type}",
            transition_id=transition_id,
            track_id=track_id,
            new_transition_type=new_transition_type,
            _old_transition=old_transition,
        )

    @staticmethod
    def create_params(
        timeline: TimelineSequence,
        transition_id: str,
        track_id: str,
        new_params: dict[str, Any],
    ) -> UpdateTransitionCommand:
        """
        Factory method for params update.

        Args:
            timeline: Current timeline
            transition_id: Transition ID
            track_id: Track ID
            new_params: New parameters dict

        Returns:
            UpdateTransitionCommand instance
        """
        track = timeline.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")

        old_transition = None
        for t in track.transitions:
            if t.transition_id == transition_id:
                old_transition = t
                break

        if old_transition is None:
            raise ValueError(f"Transition not found: {transition_id}")

        return UpdateTransitionCommand(
            command_id=str(uuid.uuid4()),
            label="Update transition parameters",
            transition_id=transition_id,
            track_id=track_id,
            new_params=new_params,
            _old_transition=old_transition,
        )
