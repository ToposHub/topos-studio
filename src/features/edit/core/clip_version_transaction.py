"""Atomic clip reference and grade activation on the project command history."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping
from uuid import uuid4

from src.features.edit.core.color_versions import ActivateColorVersionCommand
from src.features.edit.core.commands import ProjectTransactionResult, consume_project_result
from src.features.edit.core.timeline_v2 import TimelineSequence


@dataclass(frozen=True)
class SequenceColorBindingUpdate:
    """Key-scoped publication; later trims/moves/other clip edits survive Undo."""

    timeline_id: str
    clip_id: str
    asset_id: str
    expected_stack_id: str | None
    expected_shared: bool
    stack_id: str | None
    shared: bool

    def apply_to(self, sequence: TimelineSequence) -> TimelineSequence:
        if sequence.timeline_id != self.timeline_id:
            raise ValueError("版本操作的时间线已变化。")
        for track in sequence.video_tracks:
            for clip in track.clips:
                if clip.clip_id != self.clip_id:
                    continue
                if (clip.asset_id != self.asset_id
                        or clip.color_stack_id != self.expected_stack_id
                        or bool(clip.color_shared) != self.expected_shared):
                    raise ValueError("片段调色绑定已被其他操作修改，无法应用版本历史。")
                updated = clip.with_color_stack(color_stack_id=self.stack_id,
                                                color_shared=self.shared)
                return sequence.with_track_updated(track.with_clip_updated(updated))
        raise ValueError("版本操作的目标片段已不存在。")

    def reversed(self) -> SequenceColorBindingUpdate:
        return replace(self, expected_stack_id=self.stack_id,
                       expected_shared=self.shared, stack_id=self.expected_stack_id,
                       shared=self.expected_shared)


@dataclass(frozen=True)
class LoadClipVersionCommand:
    """One project history entry; both grade and reference publish atomically."""

    command_id: str
    label: str
    project_id: str
    binding_update: SequenceColorBindingUpdate
    activation: Any

    def validate_sequences(self, sequences: Mapping[str, TimelineSequence],
                           *, undo: bool = False) -> None:
        update = self.binding_update.reversed() if undo else self.binding_update
        sequence = sequences.get(update.timeline_id)
        if sequence is None:
            raise ValueError("版本操作的时间线已不存在。")
        update.apply_to(sequence)

    def _project_sequence(self, project: Any, update: SequenceColorBindingUpdate) -> Any:
        # Small hosts may embed full sequences; production keeps light refs.
        stored = project.timelines.get(update.timeline_id)
        if isinstance(stored, TimelineSequence):
            sequence = update.apply_to(stored)
            return replace(project, timelines={**project.timelines,
                                               update.timeline_id: sequence})
        return project

    def apply(self, project: Any) -> ProjectTransactionResult:
        if project.project_id != self.project_id:
            raise ValueError("版本命令属于另一工程。")
        updated, activation = consume_project_result(
            self.activation, self.activation.apply(project))
        updated = self._project_sequence(updated, self.binding_update)
        return ProjectTransactionResult(
            updated, {self.binding_update.timeline_id: self.binding_update},
            replace(self, activation=activation))

    def undo(self, project: Any) -> ProjectTransactionResult:
        if project.project_id != self.project_id:
            raise ValueError("版本命令属于另一工程。")
        updated, _ = consume_project_result(
            self.activation, self.activation.undo(project))
        inverse = self.binding_update.reversed()
        updated = self._project_sequence(updated, inverse)
        return ProjectTransactionResult(updated, {inverse.timeline_id: inverse})

    @staticmethod
    def create(before: TimelineSequence, after: TimelineSequence,
               stack_id: str, version_name: str, *, project_id: str) -> LoadClipVersionCommand:
        if before.timeline_id != after.timeline_id:
            raise ValueError("Version load cannot change the timeline identity")
        changes = []
        after_clips = {clip.clip_id: clip for track in after.video_tracks for clip in track.clips}
        for track in before.video_tracks:
            for clip in track.clips:
                target = after_clips.get(clip.clip_id)
                if target is not None and (clip.color_stack_id, clip.color_shared) != (target.color_stack_id, target.color_shared):
                    changes.append(SequenceColorBindingUpdate(
                        before.timeline_id, clip.clip_id, clip.asset_id,
                        clip.color_stack_id, bool(clip.color_shared),
                        target.color_stack_id, bool(target.color_shared)))
        if len(changes) != 1:
            raise ValueError("Version load must switch exactly one clip binding")
        return LoadClipVersionCommand(
            str(uuid4()), f"Load Color Version「{version_name}」", project_id, changes[0],
            ActivateColorVersionCommand.create(stack_id, version_name))
