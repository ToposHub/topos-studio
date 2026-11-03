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
    # B06 保存边界图一致化：layers 提交时若栈携带可投影 color_graph，
    # 同步重导出图（否则 E07 图优先会静默丢掉 legacy 通道编辑）；
    # undo 以首次快照字节等价恢复。
    _pre_reconcile_graph: dict[str, Any] | None = None

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

        # B06：layers 提交 + 栈携带图 → 图与层快照一致化（legacy 写通道
        # 的编辑进图；无变化不动 revision；复杂图保持权威不同步）
        reconciled_graph = None
        if self.layers is not None and stack.color_graph is not None:
            from src.features.color.core.color_graph_migration import (
                reconcile_projectable_graph,
            )
            try:
                reconciled_graph, changed = reconcile_projectable_graph(
                    stack.color_graph, self.layers,
                    active_layer_index=(self.active_layer_index
                                        if self.active_layer_index is not None
                                        else 0))
            except Exception:  # noqa: BLE001 —— 一致化失败不阻断层保存
                logger.debug(
                    "UpdateColorStackCommand graph reconcile failed",
                    exc_info=True)
                reconciled_graph, changed = None, False
            if changed:
                if self._pre_reconcile_graph is None:
                    object.__setattr__(
                        self, '_pre_reconcile_graph',
                        stack.color_graph.to_dict())
                logger.debug(
                    "UpdateColorStackCommand reconciled color graph for "
                    "stack %s (rev %s -> %s)", self.stack_id[:8],
                    stack.color_graph.graph_revision,
                    reconciled_graph.graph_revision)

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
            color_graph=reconciled_graph,
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
            color_graph=(
                _graph_from_dict(self._pre_reconcile_graph)
                if self._pre_reconcile_graph is not None else None),
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


# ================================================================
# B05: Color Graph Commands —— 调色节点图的权威工程事务
# ================================================================


class GraphCommandConflict(Exception):
    """图 command 冲突（stale revision / 栈锁 / service 拒绝）。

    冲突时工程**完全不变**且命令不进入 undo 历史；诊断携带稳定 code：
    - ``stale_revision``：expected_graph_revision 与当前不符
    - ``stack_locked``：栈锁生效
    - ``service_rejected``：N02 领域校验拒绝（code 见 GraphSchemaError）
    """

    def __init__(self, code: str, message: str,
                 diagnostics: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.diagnostics = diagnostics


def _graph_service():
    from src.features.color.core.color_graph_service import ColorGraphService
    return ColorGraphService()


def _graph_from_dict(payload: dict[str, Any]):
    from src.shared.project.color_graph import ColorGraphData
    return ColorGraphData.from_dict(payload)


@dataclass(frozen=True)
class ApplyColorGraphCommand:
    """把一次 N02 图事务提交为工程 command（B05.1）。

    两种载荷：
    - **operation 模式**：``operation`` + ``arguments``，apply 时经
      ColorGraphService 在当前图上执行（服务端重新验证一切）。
    - **snapshot 模式**：``operation="apply_snapshot"`` +
      ``arguments={"graph": <to_dict>}``——事务合并（同一 transaction_id
      的参数拖动）flush 出的**最终权威图**，一次 command、一次 undo。

    ``expected_graph_revision`` 乐观并发：与当前图 revision 不符抛
    ``GraphCommandConflict("stale_revision")``，工程不变。
    undo 恢复前图（字节等价，含 shared 定义/资源引用/revision）。
    """

    command_id: str
    label: str
    stack_id: str
    operation: str
    arguments: dict[str, Any]
    expected_graph_revision: int | None = None
    transaction_id: str | None = None
    # undo/redo 快照（首次 apply 时填充；redo 以快照重放保证字节等价，
    # 不再重跑 operation 分配新随机 ID）
    _old_graph: dict[str, Any] | None = None
    _new_graph: dict[str, Any] | None = None
    _id_mapping: dict[str, str] | None = None

    # ── apply ──

    def apply(self, project: EditProject) -> EditProject:
        stack = project.color_stacks.get(self.stack_id)
        if stack is None:
            raise GraphCommandConflict(
                "stack_missing", f"调色栈不存在: {self.stack_id}")
        if self._new_graph is not None:
            # redo 重放：以快照恢复（与首次执行字节等价）
            return self._apply_snapshot(project, stack,
                                        _graph_from_dict(self._new_graph),
                                        dict(self._id_mapping or {}))
        if stack.graph_locked and self.operation not in (
                "unlock_stack", "set_edit_lock"):
            raise GraphCommandConflict(
                "stack_locked",
                f"栈 {self.stack_id[:8]} 已锁定，拒绝图编辑 "
                f"{self.operation}")

        from src.features.color.core.color_graph_migration import (
            migrate_legacy_stack,
        )
        graph = stack.color_graph
        if graph is None:
            # 首次建图：从 legacy 状态迁移（显式、幂等；迁移失败=冲突）
            graph = migrate_legacy_stack(stack)

        if (self.expected_graph_revision is not None
                and graph.graph_revision != self.expected_graph_revision):
            raise GraphCommandConflict(
                "stale_revision",
                f"期望 revision {self.expected_graph_revision} != 当前 "
                f"{graph.graph_revision}")

        if self.operation == "apply_snapshot":
            from src.shared.project.color_graph import GraphSchemaError
            try:
                new_graph = _graph_from_dict(dict(self.arguments["graph"]))
            except GraphSchemaError as exc:
                raise GraphCommandConflict(
                    "service_rejected", f"快照图非法: {exc}",
                    diagnostics=[exc]) from None
            id_mapping = dict(self.arguments.get("id_mapping") or {})
        else:
            result = _graph_service().apply_intent(
                graph, self.operation, self.arguments)
            if not result.ok:
                raise GraphCommandConflict(
                    "service_rejected",
                    f"{self.operation} 被拒绝: "
                    f"{[str(d) for d in result.diagnostics[:2]]}",
                    diagnostics=result.diagnostics)
            new_graph = result.graph
            id_mapping = dict(result.id_mapping)
            object.__setattr__(self, "_new_graph", new_graph.to_dict())
            object.__setattr__(self, "_id_mapping", id_mapping)
        return self._apply_snapshot(project, stack, new_graph, id_mapping)

    def _apply_snapshot(self, project: EditProject, stack,
                        new_graph, id_mapping: dict) -> EditProject:
        from src.shared.project.color_graph import GraphSchemaError
        errors = new_graph.validate()
        if errors:
            raise GraphCommandConflict(
                "service_rejected", f"目标图非法: {errors[0]}",
                diagnostics=errors)
        old_graph = stack.color_graph
        if old_graph is not None and self._old_graph is None:
            object.__setattr__(self, "_old_graph", old_graph.to_dict())
        new_project = project.update_color_stack(
            self.stack_id, color_graph=new_graph)
        logger.debug(
            "ApplyColorGraphCommand applied: %s (rev %s -> %d)",
            self.operation,
            getattr(old_graph, "graph_revision", "?"),
            new_graph.graph_revision)
        return new_project

    def get_id_mapping(self) -> dict[str, str]:
        """首次执行分配的稳定 ID 映射（redo 重放同一份）。"""
        return dict(self._id_mapping or
                    self.arguments.get("id_mapping") or {})

    # ── undo / redo 语义 ──

    def undo(self, project: EditProject) -> EditProject:
        if self._old_graph is None:
            logger.warning("ApplyColorGraphCommand undo: no snapshot")
            return project
        restored = _graph_from_dict(self._old_graph)
        return project.update_color_stack(self.stack_id, color_graph=restored)

    def get_projection(self, project: EditProject):
        """命令应用后的权威图（N08 ack 投影用）。"""
        stack = project.color_stacks.get(self.stack_id)
        return stack.color_graph if stack else None

    # ── 序列化（持久化契约：键不重命名） ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ApplyColorGraphCommand",
            "label": self.label,
            "stack_id": self.stack_id,
            "operation": self.operation,
            "arguments": self.arguments,
            "expected_graph_revision": self.expected_graph_revision,
            "transaction_id": self.transaction_id,
            "old_graph": self._old_graph,
            "new_graph": self._new_graph,
            "id_mapping": self._id_mapping,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ApplyColorGraphCommand:
        return ApplyColorGraphCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Apply Color Graph"),
            stack_id=data["stack_id"],
            operation=data["operation"],
            arguments=dict(data.get("arguments") or {}),
            expected_graph_revision=data.get("expected_graph_revision"),
            transaction_id=data.get("transaction_id"),
            _old_graph=data.get("old_graph"),
            _new_graph=data.get("new_graph"),
            _id_mapping=data.get("id_mapping"),
        )

    @staticmethod
    def create(stack_id: str, operation: str,
               arguments: dict[str, Any] | None = None,
               expected_graph_revision: int | None = None,
               transaction_id: str | None = None,
               label: str | None = None) -> ApplyColorGraphCommand:
        return ApplyColorGraphCommand(
            command_id=str(uuid.uuid4()),
            label=label or f"Color Graph: {operation}",
            stack_id=stack_id,
            operation=operation,
            arguments=dict(arguments or {}),
            expected_graph_revision=expected_graph_revision,
            transaction_id=transaction_id)


class GraphTransactionBuffer:
    """同 transaction_id 的连续图事务合并为一个 command（B05.2）。

    参数拖动（50 次）、move+insert、branch 创建、compound 成员更新
    等手势共用一个 transaction_id：buffer 记录**首个**操作的 before 图
    与**每个**操作后的图，flush 时以 ``apply_snapshot`` 一次提交——
    一个 command、一次 revision 推进链、一次 undo。
    """

    def __init__(self) -> None:
        self._first_graph: dict | None = None
        self._last_graph: dict | None = None
        self._transaction_id: str | None = None
        self._operations: list[str] = []
        self._id_mapping: dict[str, str] = {}

    @property
    def active(self) -> bool:
        return self._transaction_id is not None

    def add(self, graph_payload_after: dict[str, Any],
            transaction_id: str, operation: str,
            id_mapping: dict[str, str] | None = None) -> None:
        if self._transaction_id not in (None, transaction_id):
            raise RuntimeError(
                f"事务缓冲冲突: {self._transaction_id} vs {transaction_id}")
        if self._first_graph is None:
            self._first_graph = None  # 由调用方在 begin 时提供
        self._transaction_id = transaction_id
        self._last_graph = graph_payload_after
        self._operations.append(operation)
        if id_mapping:
            self._id_mapping.update(id_mapping)

    def begin(self, first_graph_payload: dict[str, Any],
              transaction_id: str) -> None:
        if self.active and self._transaction_id != transaction_id:
            raise RuntimeError("上一个事务未 flush")
        if self._first_graph is None or self._transaction_id != transaction_id:
            self._first_graph = first_graph_payload
            self._transaction_id = transaction_id

    def cancel(self) -> bool:
        """R04：丢弃当前事务（无 command、无 revision 变化）。

        取消拾色/显式取消手势用；返回是否有在途事务被丢弃。
        """
        had = self.active
        self._first_graph = None
        self._last_graph = None
        self._transaction_id = None
        self._operations.clear()
        self._id_mapping.clear()
        return had

    def flush(self, stack_id: str) -> ApplyColorGraphCommand | None:
        """产出合并 command；无累积返回 None。"""
        if self._last_graph is None or self._first_graph is None:
            return None
        operations = "+".join(self._operations[:3]) + (
            f"+{len(self._operations) - 3}ops"
            if len(self._operations) > 3 else "")
        command = ApplyColorGraphCommand(
            command_id=str(uuid.uuid4()),
            label=f"Color Graph: {operations}",
            stack_id=stack_id,
            operation="apply_snapshot",
            arguments={"graph": self._last_graph,
                       "id_mapping": dict(self._id_mapping)},
            expected_graph_revision=None,
            transaction_id=self._transaction_id,
            _old_graph=self._first_graph,
        )
        self._first_graph = None
        self._last_graph = None
        self._transaction_id = None
        self._operations = []
        self._id_mapping = {}
        return command


@dataclass(frozen=True)
class SetColorGraphLockCommand:
    """栈锁元信息 command（B05.4）：锁定/解锁，可 undo，不改像素。"""

    command_id: str
    label: str
    stack_id: str
    locked: bool
    _old_locked: bool | None = None

    def apply(self, project: EditProject) -> EditProject:
        stack = project.color_stacks.get(self.stack_id)
        if stack is None:
            raise GraphCommandConflict(
                "stack_missing", f"调色栈不存在: {self.stack_id}")
        object.__setattr__(self, "_old_locked", stack.graph_locked)
        new_stack = stack.with_graph_locked(self.locked)
        new_stacks = dict(project.color_stacks)
        new_stacks[self.stack_id] = new_stack
        from dataclasses import replace as _replace
        return _replace(project, color_stacks=new_stacks)

    def undo(self, project: EditProject) -> EditProject:
        if self._old_locked is None:
            return project
        stack = project.color_stacks.get(self.stack_id)
        if stack is None:
            return project
        new_stacks = dict(project.color_stacks)
        new_stacks[self.stack_id] = stack.with_graph_locked(self._old_locked)
        from dataclasses import replace as _replace
        return _replace(project, color_stacks=new_stacks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetColorGraphLockCommand",
            "label": self.label,
            "stack_id": self.stack_id,
            "locked": self.locked,
            "old_locked": self._old_locked,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetColorGraphLockCommand:
        return SetColorGraphLockCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Color Graph Lock"),
            stack_id=data["stack_id"],
            locked=bool(data["locked"]),
            _old_locked=data.get("old_locked"),
        )

    @staticmethod
    def create(stack_id: str, locked: bool) -> SetColorGraphLockCommand:
        return SetColorGraphLockCommand(
            command_id=str(uuid.uuid4()),
            label=("Lock" if locked else "Unlock") + " Color Graph",
            stack_id=stack_id, locked=locked)
