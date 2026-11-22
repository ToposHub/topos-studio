"""One validated routing edit, including send identity and disabled state."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from typing import Any

from src.features.edit.core.timeline_v2 import Send, TimelineSequence


@dataclass(frozen=True)
class SetTrackRoutingCommand:
    """Validate the complete candidate graph before publishing one model revision."""

    command_id: str
    label: str
    track_id: str
    output_bus_id: str | None
    sends: tuple[Send, ...]
    _old_output_bus_id: str | None = None
    _old_sends: tuple[Send, ...] | None = None

    def apply(self, timeline: TimelineSequence) -> tuple[TimelineSequence, SetTrackRoutingCommand]:
        from src.features.edit.core.audio_playback import validate_routing_graph

        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "audio":
            raise ValueError(f"Audio track not found: {self.track_id}")
        ids = [send.send_id for send in self.sends]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise ValueError("Routing send identities must be unique and nonempty")
        for send in self.sends:
            if isinstance(send.level, bool) or not math.isfinite(send.level):
                raise ValueError("Send level must be finite")
        candidate = replace(track, output_bus_id=self.output_bus_id, sends=self.sends)
        buses = [item.track_id for item in timeline.audio_tracks if item.is_bus]
        routing = {
            item.track_id: {
                "is_bus": item.is_bus,
                "output_bus_id": item.output_bus_id,
                "sends": [(s.target_bus_id, s.level, s.tap, s.muted)
                          for s in item.sends if s.enabled],
            }
            for item in (candidate if t.track_id == self.track_id else t
                         for t in timeline.audio_tracks)
        }
        valid, errors = validate_routing_graph(buses, routing)
        if not valid:
            raise ValueError("Invalid routing: " + "; ".join(errors))
        # Disabled sends still need valid endpoints: enabling later must not
        # turn an apparently valid persisted configuration into an orphan.
        if any(send.target_bus_id not in buses for send in self.sends):
            raise ValueError("Unknown send bus target")
        updated = replace(self, _old_output_bus_id=track.output_bus_id, _old_sends=track.sends)
        if track.output_bus_id == self.output_bus_id and track.sends == self.sends:
            return timeline, updated
        return timeline.with_track_updated(candidate), updated

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_sends is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Audio track not found: {self.track_id}")
        return timeline.with_track_updated(replace(
            track, output_bus_id=self._old_output_bus_id, sends=self._old_sends))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_type": type(self).__name__, "command_id": self.command_id,
            "label": self.label, "track_id": self.track_id,
            "output_bus_id": self.output_bus_id,
            "sends": [s.to_dict() for s in self.sends],
            "old_output_bus_id": self._old_output_bus_id,
            "old_sends": None if self._old_sends is None else [s.to_dict() for s in self._old_sends],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetTrackRoutingCommand:
        old_sends = data.get("old_sends")
        return cls(
            command_id=data["command_id"], label=data["label"], track_id=data["track_id"],
            output_bus_id=data.get("output_bus_id"),
            sends=tuple(Send.from_dict(s) for s in data.get("sends", ())),
            _old_output_bus_id=data.get("old_output_bus_id"),
            _old_sends=None if old_sends is None else tuple(Send.from_dict(s) for s in old_sends),
        )

    @classmethod
    def create(cls, track_id: str, output_bus_id: str | None,
               sends: tuple[Send, ...]) -> SetTrackRoutingCommand:
        return cls(str(uuid.uuid4()), "Set Track Routing", track_id, output_bus_id, tuple(sends))
