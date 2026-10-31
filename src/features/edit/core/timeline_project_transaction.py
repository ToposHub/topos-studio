"""Key-scoped project payloads committed on a timeline's ordered history."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping
import time
import uuid

from .project import EditProject, MediaAsset, TimelineSequenceRef
from .timeline_v2 import TimelineSequence


def _payload(value: Any) -> Any:
    if value is None:
        return None
    serialize = getattr(value, "to_dict", None)
    return serialize() if callable(serialize) else asdict(value)


def _sequence_content(sequence: TimelineSequence) -> dict[str, Any]:
    data = sequence.to_dict()
    data.pop("revision", None)
    return data


@dataclass(frozen=True)
class TimelineProjectTransactionCommand:
    """An active sequence plus only the project/child keys it changes.

    The command is pure. TimelineView checks and merges its payload before
    changing history, then publishes the merged project and sequence together.
    Unrelated project keys survive both Undo and Redo. Conflicting changes to
    a touched key are rejected instead of restoring an old whole project.
    """

    command_id: str
    label: str
    project_id: str
    before_sequence: TimelineSequence
    after_sequence: TimelineSequence
    asset_changes: dict[str, tuple[MediaAsset | None, MediaAsset | None]]
    timeline_ref_changes: dict[str, tuple[TimelineSequenceRef | None, TimelineSequenceRef | None]]
    child_sequence_changes: dict[str, tuple[TimelineSequence | None, TimelineSequence | None]]

    def __post_init__(self) -> None:
        if self.before_sequence.timeline_id != self.after_sequence.timeline_id:
            raise ValueError("Project transaction cannot change the active timeline identity")
        if self.before_sequence.timeline_id in self.child_sequence_changes:
            raise ValueError("Active timeline must not also be a child payload")
        for changes, identity_name in ((self.asset_changes, "asset_id"),
                                       (self.timeline_ref_changes, "timeline_id"),
                                       (self.child_sequence_changes, "timeline_id")):
            for key, pair in changes.items():
                if len(pair) != 2 or any(
                    value is not None and getattr(value, identity_name) != key for value in pair
                ):
                    raise ValueError("Transaction payload key does not match its object identity")

    @property
    def is_noop(self) -> bool:
        return (not self.asset_changes and not self.timeline_ref_changes
                and not self.child_sequence_changes
                and _sequence_content(self.before_sequence) == _sequence_content(self.after_sequence))

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        self._validate_sequence(timeline, self.before_sequence)
        if self.is_noop:
            return timeline
        return replace(self.after_sequence, revision=timeline.revision)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        self._validate_sequence(timeline, self.after_sequence)
        if self.is_noop:
            return timeline
        return replace(self.before_sequence, revision=timeline.revision)

    def _validate_sequence(self, actual: TimelineSequence, expected: TimelineSequence) -> None:
        if actual.timeline_id != expected.timeline_id:
            raise ValueError("Project transaction belongs to a different timeline")
        if _sequence_content(actual) != _sequence_content(expected):
            raise ValueError("Timeline changed outside this transaction's ordered history")

    def prepare_project(
        self, project: EditProject, sequences: Mapping[str, TimelineSequence], *, undo: bool = False,
    ) -> tuple[EditProject, dict[str, TimelineSequence | None]]:
        """Preflight all touched keys, then merge without modifying inputs."""
        if project.project_id != self.project_id:
            raise ValueError("Project transaction belongs to a different project")
        if self.is_noop:
            return project, {}
        expected_index, target_index = (1, 0) if undo else (0, 1)
        new_maps: dict[str, dict[str, Any]] = {}
        for field, changes in (("assets", self.asset_changes),
                               ("timelines", self.timeline_ref_changes)):
            current = getattr(project, field)
            merged = dict(current)
            for key, values in changes.items():
                if _payload(current.get(key)) != _payload(values[expected_index]):
                    raise ValueError(f"Cannot change transaction: {field} key {key} was edited separately")
                target = values[target_index]
                if target is None:
                    merged.pop(key, None)
                else:
                    merged[key] = target
            new_maps[field] = merged
        child_updates: dict[str, TimelineSequence | None] = {}
        for key, values in self.child_sequence_changes.items():
            actual, expected = sequences.get(key), values[expected_index]
            if ((actual is None) != (expected is None)
                    or (actual is not None and _sequence_content(actual) != _sequence_content(expected))):
                raise ValueError(f"Cannot change transaction: child timeline {key} was edited separately")
            child_updates[key] = values[target_index]

        # Removing a newly created nested asset/sequence must not orphan a
        # subsequent edit made in another timeline.
        removed_assets = {key for key, values in self.asset_changes.items()
                          if values[target_index] is None}
        removed_children = {key for key, child in child_updates.items() if child is None}
        target_parent = self.before_sequence if undo else self.after_sequence
        remaining = dict(sequences)
        remaining[target_parent.timeline_id] = target_parent
        for key, child in child_updates.items():
            if child is None:
                remaining.pop(key, None)
            else:
                remaining[key] = child
        for sequence in remaining.values():
            for track in sequence.all_tracks:
                if any(clip.asset_id in removed_assets for clip in track.clips):
                    raise ValueError("Cannot remove a transaction asset used by another timeline")
        if any(asset.nested_timeline_id in removed_children
               for asset in new_maps["assets"].values()
               if getattr(asset, "is_nested_sequence", False)):
            raise ValueError("Cannot remove a child timeline used by another nested asset")
        merged_project = replace(
            project, **new_maps, revision=project.revision + 1, modified_at=time.time())
        return merged_project, child_updates

    @staticmethod
    def create(
        before_sequence: TimelineSequence, after_sequence: TimelineSequence, *,
        before_project: EditProject, after_project: EditProject,
        before_child_sequences: Mapping[str, TimelineSequence | None] | None = None,
        after_child_sequences: Mapping[str, TimelineSequence | None] | None = None,
        label: str = "Edit project and timeline",
    ) -> TimelineProjectTransactionCommand:
        if before_project.project_id != after_project.project_id:
            raise ValueError("Project transaction cannot change the project identity")
        def delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
            return {key: (deepcopy(before.get(key)), deepcopy(after.get(key)))
                    for key in before.keys() | after.keys()
                    if _payload(before.get(key)) != _payload(after.get(key))}
        return TimelineProjectTransactionCommand(
            command_id=str(uuid.uuid4()), label=label, project_id=before_project.project_id,
            before_sequence=deepcopy(before_sequence), after_sequence=deepcopy(after_sequence),
            asset_changes=delta(before_project.assets, after_project.assets),
            timeline_ref_changes=delta(before_project.timelines, after_project.timelines),
            child_sequence_changes=delta(dict(before_child_sequences or {}),
                                         dict(after_child_sequences or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        def encode(changes: dict[str, tuple[Any, Any]]) -> dict[str, list[Any]]:
            return {key: [deepcopy(_payload(before)), deepcopy(_payload(after))]
                    for key, (before, after) in changes.items()}
        return {
            "command_type": "TimelineProjectTransactionCommand", "command_id": self.command_id,
            "label": self.label, "project_id": self.project_id,
            "before_sequence": self.before_sequence.to_dict(),
            "after_sequence": self.after_sequence.to_dict(),
            "asset_changes": encode(self.asset_changes),
            "timeline_ref_changes": encode(self.timeline_ref_changes),
            "child_sequence_changes": encode(self.child_sequence_changes),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineProjectTransactionCommand:
        data = deepcopy(data)
        def asset(payload: dict[str, Any]) -> MediaAsset:
            values = deepcopy(payload)
            values["keywords"] = tuple(values.get("keywords", ()))
            return MediaAsset(**values)
        def decode(name: str, factory: Any) -> dict[str, tuple[Any, Any]]:
            return {key: tuple(factory(value) if value is not None else None for value in pair)
                    for key, pair in data.get(name, {}).items()}
        return TimelineProjectTransactionCommand(
            command_id=data["command_id"], label=data["label"], project_id=data["project_id"],
            before_sequence=TimelineSequence.from_dict(data["before_sequence"]),
            after_sequence=TimelineSequence.from_dict(data["after_sequence"]),
            asset_changes=decode("asset_changes", asset),
            timeline_ref_changes=decode("timeline_ref_changes", lambda value: TimelineSequenceRef(**value)),
            child_sequence_changes=decode("child_sequence_changes", TimelineSequence.from_dict),
        )
