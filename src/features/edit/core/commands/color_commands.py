"""Color stack & grade mode commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: color cluster (project-level color
stack create/update, per-clip color stack switch, shared-grade use and
local grade copy). Class sources are byte-identical to the original
module; to_dict/from_dict keys are a persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import copy
import uuid

from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.edit.core.project import EditProject

logger = logging.getLogger(__name__)


# ================================================================
# ColorStack Commands - Project-level Color Stack Operations (P9.1)
# ================================================================



@dataclass(frozen=True)
class CreateColorStackCommand:
    """
    Command to create a new color stack in the project.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Associated asset ID (optional)
        name: Color stack name
        _created_stack_id: Internal: ID of created stack (set during apply)
    """

    command_id: str
    label: str
    asset_id: str | None = None
    name: str = "Default"
    _created_stack_id: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: create color stack.

        Args:
            project: Current project

        Returns:
            New project with color stack created
        """
        new_project, stack_id = project.create_color_stack(
            asset_id=self.asset_id,
            name=self.name
        )

        # Store created stack ID for undo
        object.__setattr__(self, '_created_stack_id', stack_id)

        logger.debug(f"CreateColorStackCommand applied: {self.name} (id={stack_id})")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: delete the color stack.

        Args:
            project: Current project

        Returns:
            Project without the color stack
        """
        if self._created_stack_id is None:
            logger.warning("CreateColorStackCommand undo: no stack ID stored")
            return project

        new_project = project.delete_color_stack(self._created_stack_id)
        logger.debug(f"CreateColorStackCommand undone: removed stack {self._created_stack_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "CreateColorStackCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "name": self.name,
            "created_stack_id": self._created_stack_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CreateColorStackCommand:
        """Deserialize command."""
        return CreateColorStackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Create Color Stack"),
            asset_id=data.get("asset_id"),
            name=data.get("name", "Default"),
            _created_stack_id=data.get("created_stack_id"),
        )

    @staticmethod
    def create(asset_id: str | None = None, name: str = "Default") -> CreateColorStackCommand:
        """Factory method to create command."""
        return CreateColorStackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Create Color Stack '{name}'",
            asset_id=asset_id,
            name=name,
        )


@dataclass(frozen=True)
class UpdateColorStackCommand:
    """
    Command to update a color stack's parameters.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        stack_id: Color stack ID to update
        grading_params: New grading parameters (optional)
        curves_data: New curve data (optional)
        hsl_curve_data: New HSL curve data (optional)
        lut_path: New LUT path (optional)
        lut_intensity: New LUT intensity (optional)
        _old_grading_params: Old grading params for undo
        _old_curves_data: Old curves data for undo
        _old_hsl_curve_data: Old HSL curves data for undo
        _old_lut_path: Old LUT path for undo
        _old_lut_intensity: Old LUT intensity for undo
    """

    command_id: str
    label: str
    stack_id: str
    grading_params: dict[str, Any] | None = None
    curves_data: dict[str, Any] | None = None
    hsl_curve_data: dict[str, Any] | None = None
    mixer_params: dict[str, Any] | None = None
    soft_clip_params: dict[str, Any] | None = None
    lut_path: str | None = None
    lut_intensity: float | None = None
    # 完整 Layer 快照（schema v2 权威状态；None = 不更新）
    layers: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None
    active_layer_index: int | None = None
    raw_params: dict[str, Any] | None = None
    trackings: dict[str, Any] | None = None
    video_denoise: dict[str, Any] | None = None
    video_stabilization: dict[str, Any] | None = None
    # Old values for undo
    _old_grading_params: dict[str, Any] | None = None
    _old_curves_data: dict[str, Any] | None = None
    _old_hsl_curve_data: dict[str, Any] | None = None
    _old_mixer_params: dict[str, Any] | None = None
    _old_soft_clip_params: dict[str, Any] | None = None
    _old_lut_path: str | None = None
    _old_lut_intensity: float | None = None
    _old_layers: tuple[dict[str, Any], ...] | None = None
    _old_active_layer_index: int | None = None
    _old_raw_params: dict[str, Any] | None = None
    _old_trackings: dict[str, Any] | None = None
    _old_video_denoise: dict[str, Any] | None = None
    _old_video_stabilization: dict[str, Any] | None = None

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: update color stack.

        Args:
            project: Current project

        Returns:
            New project with updated color stack
        """
        stack = project.color_stacks.get(self.stack_id)
        if stack is None:
            logger.warning(f"UpdateColorStackCommand: stack not found: {self.stack_id}")
            return project

        # F4.2: 共享 look 锁定——被锁栈拒绝直接改参数（防误改全组），
        # 解锁经 SetStackLockCommand 或 MakeLocalGradeCopy 复制独立版本。
        try:
            from src.features.edit.core.color_versions import is_stack_locked
            if is_stack_locked(project, self.stack_id):
                raise ValueError(
                    f"共享调色栈 {self.stack_id} 已锁定（防误改）："
                    "请先显式解锁，或复制为独立版本后再修改。")
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001 — 锁表异常不阻断既有编辑路径
            logger.warning(
                f"UpdateColorStackCommand: shared-lock check failed for "
                f"{self.stack_id}, edit proceeds unlocked: {e}")

        # Store old values for undo
        object.__setattr__(self, '_old_grading_params', dict(stack.grading_params))
        object.__setattr__(self, '_old_curves_data', dict(stack.curves_data))
        object.__setattr__(self, '_old_hsl_curve_data', dict(stack.hsl_curve_data))
        object.__setattr__(self, '_old_mixer_params', dict(stack.mixer_params) if stack.mixer_params else None)
        object.__setattr__(self, '_old_soft_clip_params', dict(stack.soft_clip_params) if stack.soft_clip_params else None)
        object.__setattr__(self, '_old_lut_path', stack.lut_path)
        object.__setattr__(self, '_old_lut_intensity', stack.lut_intensity)
        object.__setattr__(self, '_old_layers', stack.layers)
        object.__setattr__(self, '_old_active_layer_index', stack.active_layer_index)
        object.__setattr__(self, '_old_raw_params', dict(stack.raw_params) if stack.raw_params else None)
        object.__setattr__(self, '_old_trackings', dict(stack.trackings) if stack.trackings is not None else {})
        object.__setattr__(self, '_old_video_stabilization', copy.deepcopy(stack.video_stabilization))
        object.__setattr__(self, '_old_video_denoise', dict(stack.video_denoise) if stack.video_denoise else None)

        new_project = project.update_color_stack(
            stack_id=self.stack_id,
            grading_params=self.grading_params,
            curves_data=self.curves_data,
            hsl_curve_data=self.hsl_curve_data,
            mixer_params=self.mixer_params,
            soft_clip_params=self.soft_clip_params,
            lut_path=self.lut_path,
            lut_intensity=self.lut_intensity,
            layers=self.layers,
            active_layer_index=self.active_layer_index,
            raw_params=self.raw_params,
            trackings=self.trackings,
            video_denoise=self.video_denoise,
            video_stabilization=self.video_stabilization,
        )

        logger.debug(f"UpdateColorStackCommand applied: {self.stack_id}")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: restore old color stack parameters.

        Args:
            project: Current project

        Returns:
            Project with restored color stack
        """
        if self._old_grading_params is None:
            logger.warning("UpdateColorStackCommand undo: no old values stored")
            return project

        new_project = project.update_color_stack(
            stack_id=self.stack_id,
            grading_params=self._old_grading_params,
            curves_data=self._old_curves_data,
            hsl_curve_data=self._old_hsl_curve_data,
            mixer_params=self._old_mixer_params,
            soft_clip_params=self._old_soft_clip_params,
            lut_path=self._old_lut_path,
            lut_intensity=self._old_lut_intensity,
            layers=self._old_layers,
            active_layer_index=self._old_active_layer_index,
            raw_params=self._old_raw_params,
            trackings=self._old_trackings,
            video_stabilization=self._old_video_stabilization,
            video_denoise=self._old_video_denoise if self._old_video_denoise is not None else {},
        )

        logger.debug(f"UpdateColorStackCommand undone: {self.stack_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateColorStackCommand",
            "label": self.label,
            "stack_id": self.stack_id,
            "grading_params": self.grading_params,
            "curves_data": self.curves_data,
            "hsl_curve_data": self.hsl_curve_data,
            "mixer_params": self.mixer_params,
            "soft_clip_params": self.soft_clip_params,
            "lut_path": self.lut_path,
            "lut_intensity": self.lut_intensity,
            "old_grading_params": self._old_grading_params,
            "old_curves_data": self._old_curves_data,
            "old_hsl_curve_data": self._old_hsl_curve_data,
            "old_mixer_params": self._old_mixer_params,
            "old_soft_clip_params": self._old_soft_clip_params,
            "old_lut_path": self._old_lut_path,
            "old_lut_intensity": self._old_lut_intensity,
            "video_stabilization": copy.deepcopy(self.video_stabilization),
            "old_video_stabilization": copy.deepcopy(self._old_video_stabilization),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateColorStackCommand:
        """Deserialize command."""
        return UpdateColorStackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Color Stack"),
            stack_id=data.get("stack_id", ""),
            grading_params=data.get("grading_params"),
            curves_data=data.get("curves_data"),
            hsl_curve_data=data.get("hsl_curve_data"),
            mixer_params=data.get("mixer_params"),
            soft_clip_params=data.get("soft_clip_params"),
            lut_path=data.get("lut_path"),
            lut_intensity=data.get("lut_intensity"),
            video_stabilization=data.get("video_stabilization"),
            _old_video_stabilization=data.get("old_video_stabilization"),
            _old_grading_params=data.get("old_grading_params"),
            _old_curves_data=data.get("old_curves_data"),
            _old_hsl_curve_data=data.get("old_hsl_curve_data"),
            _old_mixer_params=data.get("old_mixer_params"),
            _old_soft_clip_params=data.get("old_soft_clip_params"),
            _old_lut_path=data.get("old_lut_path"),
            _old_lut_intensity=data.get("old_lut_intensity"),
        )

    @staticmethod
    def create(
        stack_id: str,
        grading_params: dict[str, Any] | None = None,
        curves_data: dict[str, Any] | None = None,
        hsl_curve_data: dict[str, Any] | None = None,
        mixer_params: dict[str, Any] | None = None,
        soft_clip_params: dict[str, Any] | None = None,
        lut_path: str | None = None,
        lut_intensity: float | None = None,
        layers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        active_layer_index: int | None = None,
        raw_params: dict[str, Any] | None = None,
        trackings: dict[str, Any] | None = None,
        video_denoise: dict[str, Any] | None = None,
        video_stabilization: dict[str, Any] | None = None,
    ) -> UpdateColorStackCommand:
        """Factory method to create command."""
        return UpdateColorStackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Color Stack {stack_id[:8]}",
            stack_id=stack_id,
            grading_params=grading_params,
            curves_data=curves_data,
            hsl_curve_data=hsl_curve_data,
            mixer_params=mixer_params,
            soft_clip_params=soft_clip_params,
            lut_path=lut_path,
            lut_intensity=lut_intensity,
            layers=layers,
            active_layer_index=active_layer_index,
            raw_params=raw_params,
            trackings=trackings,
            video_denoise=video_denoise,
            video_stabilization=copy.deepcopy(video_stabilization),
        )


@dataclass(frozen=True)
class UpdateClipColorStackCommand:
    """
    Command to update a clip's color stack reference.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to update
        new_color_stack_id: New color stack ID
        new_color_shared: New color shared flag
        old_color_stack_id: Old color stack ID (for undo)
        old_color_shared: Old color shared flag (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    new_color_stack_id: str | None
    new_color_shared: bool = False
    old_color_stack_id: str | None = None
    old_color_shared: bool = False

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: update clip's color stack reference.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with updated clip
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old values for undo
        object.__setattr__(self, 'old_color_stack_id', clip.color_stack_id)
        object.__setattr__(self, 'old_color_shared', clip.color_shared)

        # Update clip
        updated_clip = clip.with_color_stack(
            color_stack_id=self.new_color_stack_id,
            color_shared=self.new_color_shared
        )
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"UpdateClipColorStackCommand applied: {self.clip_id} "
            f"→ stack={self.new_color_stack_id}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore old color stack reference.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with restored clip
        """
        # Create reverse command
        undo_command = UpdateClipColorStackCommand(
            command_id=self.command_id,
            label=f"Undo {self.label}",
            track_id=self.track_id,
            clip_id=self.clip_id,
            new_color_stack_id=self.old_color_stack_id,
            new_color_shared=self.old_color_shared,
            old_color_stack_id=self.new_color_stack_id,
            old_color_shared=self.new_color_shared,
        )

        return undo_command.apply(timeline)

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateClipColorStackCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_color_stack_id": self.new_color_stack_id,
            "new_color_shared": self.new_color_shared,
            "old_color_stack_id": self.old_color_stack_id,
            "old_color_shared": self.old_color_shared,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateClipColorStackCommand:
        """Deserialize command."""
        return UpdateClipColorStackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Clip Color Stack"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_color_stack_id=data.get("new_color_stack_id"),
            new_color_shared=data.get("new_color_shared", False),
            old_color_stack_id=data.get("old_color_stack_id"),
            old_color_shared=data.get("old_color_shared", False),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        new_color_stack_id: str | None,
        new_color_shared: bool = False,
    ) -> UpdateClipColorStackCommand:
        """Factory method to create command."""
        return UpdateClipColorStackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Clip {clip_id[:8]} Color Stack",
            track_id=track_id,
            clip_id=clip_id,
            new_color_stack_id=new_color_stack_id,
            new_color_shared=new_color_shared,
        )


# ================================================================
# Grade Mode Commands (P9.1)
# ================================================================

@dataclass(frozen=True)
class UseSharedGradeCommand:
    """
    Command to switch clip to use shared asset grade.

    This command:
    1. Gets or creates the asset's default color stack
    2. Updates clip's color_stack_id to reference the asset stack
    3. Sets color_shared=True

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip to update
        asset_id: Asset ID for the shared grade
        _previous_stack_id: Internal: previous stack ID (for undo)
        _previous_shared: Internal: previous shared status (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    asset_id: str
    _previous_stack_id: str | None = None
    _previous_shared: bool = False

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: switch clip to shared asset grade.

        Note: This command only updates the clip's color_stack_id.
        The actual color stack creation/lookup is done by EditProject.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with updated clip (color_shared=True, stack_id needs to be set externally)
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old values for undo
        object.__setattr__(self, '_previous_stack_id', clip.color_stack_id)
        object.__setattr__(self, '_previous_shared', clip.color_shared)

        # The stack_id will be set by the project layer when handling this command
        # Here we just mark color_shared=True and keep existing stack_id
        # (actual asset stack lookup happens at EditPage level)
        updated_clip = clip.with_color_stack(
            color_stack_id=clip.color_stack_id,  # Keep existing, will be replaced externally
            color_shared=True
        )
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"UseSharedGradeCommand applied: {self.clip_id} → shared=True (asset={self.asset_id})"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore previous grade settings."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        updated_clip = clip.with_color_stack(
            color_stack_id=self._previous_stack_id,
            color_shared=self._previous_shared
        )
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"UseSharedGradeCommand undone: {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UseSharedGradeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "_previous_stack_id": self._previous_stack_id,
            "_previous_shared": self._previous_shared,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UseSharedGradeCommand:
        """Deserialize command."""
        return UseSharedGradeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Use Shared Asset Grade"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            asset_id=data.get("asset_id", ""),
            _previous_stack_id=data.get("_previous_stack_id"),
            _previous_shared=data.get("_previous_shared", False),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        asset_id: str,
    ) -> UseSharedGradeCommand:
        """Factory method to create command."""
        return UseSharedGradeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Use Shared Grade for {clip_id[:8]}",
            track_id=track_id,
            clip_id=clip_id,
            asset_id=asset_id,
        )


@dataclass(frozen=True)
class MakeLocalGradeCopyCommand:
    """
    Command to create a local grade copy for clip.

    This command:
    1. Creates a new color stack (copy of current or asset default)
    2. Updates clip's color_stack_id to the new stack
    3. Sets color_shared=False

    Note: The actual stack creation is done by EditProject.
    This command just marks the clip as local and sets up for new stack_id.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip to update
        source_stack_id: Source stack to copy from (current or asset default)
        new_stack_name: Name for the new local stack
        _previous_stack_id: Internal: previous stack ID (for undo)
        _previous_shared: Internal: previous shared status (for undo)
        _created_stack_id: Internal: ID of newly created stack (set externally)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    source_stack_id: str | None = None  # Stack to copy from
    new_stack_name: str = "Local Grade"
    _previous_stack_id: str | None = None
    _previous_shared: bool = False
    _created_stack_id: str | None = None  # Set by project layer

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: create local grade copy.

        The actual stack creation happens at EditProject level.
        This command prepares the clip for local grade mode.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with updated clip (color_shared=False, awaiting new stack_id)
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old values for undo
        object.__setattr__(self, '_previous_stack_id', clip.color_stack_id)
        object.__setattr__(self, '_previous_shared', clip.color_shared)

        # Mark as local grade (stack_id will be set externally by project)
        updated_clip = clip.with_color_stack(
            color_stack_id=self._created_stack_id or clip.color_stack_id,
            color_shared=False
        )
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"MakeLocalGradeCopyCommand applied: {self.clip_id} → local copy"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore previous grade settings."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        updated_clip = clip.with_color_stack(
            color_stack_id=self._previous_stack_id,
            color_shared=self._previous_shared
        )
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"MakeLocalGradeCopyCommand undone: {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MakeLocalGradeCopyCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "source_stack_id": self.source_stack_id,
            "new_stack_name": self.new_stack_name,
            "_previous_stack_id": self._previous_stack_id,
            "_previous_shared": self._previous_shared,
            "_created_stack_id": self._created_stack_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MakeLocalGradeCopyCommand:
        """Deserialize command."""
        return MakeLocalGradeCopyCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Make Local Grade Copy"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            source_stack_id=data.get("source_stack_id"),
            new_stack_name=data.get("new_stack_name", "Local Grade"),
            _previous_stack_id=data.get("_previous_stack_id"),
            _previous_shared=data.get("_previous_shared", False),
            _created_stack_id=data.get("_created_stack_id"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        source_stack_id: str | None = None,
        new_stack_name: str = "Local Grade",
    ) -> MakeLocalGradeCopyCommand:
        """Factory method to create command."""
        return MakeLocalGradeCopyCommand(
            command_id=str(uuid.uuid4()),
            label=f"Make Local Grade for {clip_id[:8]}",
            track_id=track_id,
            clip_id=clip_id,
            source_stack_id=source_stack_id,
            new_stack_name=new_stack_name,
        )
