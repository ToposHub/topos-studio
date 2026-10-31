"""Atomic timeline settings snapshots on the sequence editing history."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from .timeline_v2 import TimelineSequence


@dataclass(frozen=True)
class TimelineSettingsCommand:
    """Keep timebase changes ordered with clip edits on the same timeline.

    Project references, renderer state and publication revisions follow the
    normal timeline_changed commit path. Content snapshots preserve unknown
    payloads and all frame domains already transformed by the controller.
    """

    command_id: str
    label: str
    before_sequence: TimelineSequence
    after_sequence: TimelineSequence

    def __post_init__(self) -> None:
        if self.before_sequence.timeline_id != self.after_sequence.timeline_id:
            raise ValueError("Timeline settings cannot replace another timeline")

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        self._validate_target(timeline)
        if timeline.to_dict() == self.after_sequence.to_dict():
            return timeline
        return self.after_sequence

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        self._validate_target(timeline)
        return self.before_sequence

    def _validate_target(self, timeline: TimelineSequence) -> None:
        if timeline.timeline_id != self.before_sequence.timeline_id:
            raise ValueError("Timeline settings history belongs to a different timeline")

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_type": "TimelineSettingsCommand",
            "command_id": self.command_id,
            "label": self.label,
            "before_sequence": self.before_sequence.to_dict(),
            "after_sequence": self.after_sequence.to_dict(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineSettingsCommand:
        return TimelineSettingsCommand(
            command_id=data["command_id"], label=data["label"],
            before_sequence=TimelineSequence.from_dict(data["before_sequence"]),
            after_sequence=TimelineSequence.from_dict(data["after_sequence"]),
        )

    @staticmethod
    def create(before: TimelineSequence, after: TimelineSequence) -> TimelineSettingsCommand:
        if before.timeline_id != after.timeline_id:
            raise ValueError("Timeline settings cannot replace another timeline")
        return TimelineSettingsCommand(
            command_id=str(uuid.uuid4()), label="Update timeline settings",
            before_sequence=before, after_sequence=after,
        )
