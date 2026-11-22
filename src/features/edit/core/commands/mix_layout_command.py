"""Undoable Track, Bus and Master speaker layout edits."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from src.features.edit.core.native_mix_layout import mix_layout, validate_native_layouts
from src.features.edit.core.timeline_v2 import TimelineSequence


@dataclass(frozen=True)
class SetMixLayoutCommand:
    command_id: str
    label: str
    track_id: str | None
    layout: str
    previous: str = ""

    @classmethod
    def create(cls, track_id: str | None, layout: str) -> SetMixLayoutCommand:
        return cls(str(uuid.uuid4()), "Set Mix Layout", track_id, layout)

    def _set(self, sequence: TimelineSequence, name: str) -> tuple[TimelineSequence, str]:
        if name:
            mix_layout(name)
        if self.track_id is None:
            old = sequence.master_audio_layout
            return (replace(sequence, master_audio_layout=name) if old != name else sequence), old
        track = sequence.get_track_by_id(self.track_id)
        if track is None or track.track_type != "audio" or track.is_locked:
            raise ValueError("Select an unlocked audio channel")
        if track.is_bus:
            old = track.bus_layout
            updated = track.with_bus_layout(name)
        else:
            old = track.audio_layout
            updated = replace(track, audio_layout=name)
        return (sequence.with_track_updated(updated) if old != name else sequence), old

    def apply(self, timeline: TimelineSequence) -> tuple[TimelineSequence, SetMixLayoutCommand]:
        result, previous = self._set(timeline, self.layout)
        validate_native_layouts(result, edited_bus=self.track_id)
        return result, replace(self, previous=previous)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return self._set(timeline, self.previous)[0]

    def to_dict(self) -> dict[str, Any]:
        return {"command_type": type(self).__name__, "command_id": self.command_id,
                "label": self.label, "track_id": self.track_id, "layout": self.layout,
                "previous": self.previous}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetMixLayoutCommand:
        return cls(data["command_id"], data["label"], data["track_id"],
                   data["layout"], data["previous"])
