"""Reference source registration uses the shared timeline command history."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from src.features.edit.core.reference_mix import (
    ReferenceMixRecord, ReferenceMixRef, decode_reference_mix_record,
)
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.shared.i18n.i18n import I18N


@dataclass(frozen=True)
class SetReferenceMixCommand:
    command_id: str
    label: str
    sequence_id: str
    reference_id: str
    reference: ReferenceMixRef | None
    _previous: tuple[ReferenceMixRecord, ...] | None = None

    def apply(self, timeline: TimelineSequence) -> tuple[TimelineSequence, SetReferenceMixCommand]:
        if timeline.timeline_id != self.sequence_id:
            raise ValueError("Reference mix belongs to another timeline")
        if not self.reference_id or (self.reference and self.reference.version_id != self.reference_id):
            raise ValueError("Reference mix identity mismatch")
        remaining = list(timeline.reference_mixes)
        for index, entry in enumerate(remaining):
            if isinstance(entry, dict) and entry.get("version_id") == self.reference_id:
                raise ValueError("Reference mix is from an unsupported schema")
            if isinstance(entry, ReferenceMixRef) and entry.version_id == self.reference_id:
                if self.reference is None:
                    remaining.pop(index)
                else:
                    if entry.mix_version is not None and entry.mix_version != self.reference.mix_version:
                        raise ValueError("A registered mix version cannot replace its source evidence")
                    remaining[index] = self.reference
                break
        else:
            if self.reference is not None:
                remaining.append(self.reference)
        result = tuple(remaining)
        if result == timeline.reference_mixes:
            return timeline, self
        return (replace(timeline, reference_mixes=result),
                replace(self, _previous=timeline.reference_mixes))

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return (replace(timeline, reference_mixes=self._previous)
                if self._previous is not None else timeline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_type": type(self).__name__,
            "command_id": self.command_id,
            "label": self.label,
            "sequence_id": self.sequence_id,
            "reference_id": self.reference_id,
            "reference": self.reference.to_dict() if self.reference is not None else None,
            "previous": None if self._previous is None else [
                entry.to_dict() if isinstance(entry, ReferenceMixRef) else deepcopy(entry)
                for entry in self._previous
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetReferenceMixCommand:
        reference = data.get("reference")
        previous = data.get("previous")
        return cls(
            data["command_id"], data["label"], data["sequence_id"], data["reference_id"],
            None if reference is None else ReferenceMixRef.from_dict(reference),
            None if previous is None else tuple(decode_reference_mix_record(entry) for entry in previous),
        )

    @classmethod
    def create(cls, sequence_id: str, reference_id: str,
               reference: ReferenceMixRef | None) -> SetReferenceMixCommand:
        label = (I18N.tr("mixer.reference.save", "Save reference")
                 if reference else I18N.tr("mixer.reference.remove", "Remove reference"))
        return cls(str(uuid4()), label, sequence_id, reference_id, reference)
