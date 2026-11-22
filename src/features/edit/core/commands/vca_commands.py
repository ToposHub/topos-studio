"""VCA group edits through the shared timeline history."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from typing import Any

from src.features.edit.core.timeline_v2 import TimelineSequence, VcaGroup


@dataclass(frozen=True)
class SetVcaGroupCommand:
    """Create, replace or delete a group as a single reversible edit."""

    command_id: str
    label: str
    vca_id: str
    group: VcaGroup | None
    _old_groups: tuple[VcaGroup, ...] | None = None

    def apply(self, timeline: TimelineSequence) -> tuple[TimelineSequence, SetVcaGroupCommand]:
        if not self.vca_id:
            raise ValueError("VCA identity cannot be empty")
        if self.group is not None:
            if self.group.vca_id != self.vca_id:
                raise ValueError("VCA identity mismatch")
            if isinstance(self.group.gain_db, bool) or not math.isfinite(self.group.gain_db):
                raise ValueError("VCA gain must be finite")
            if not -120.0 <= self.group.gain_db <= 24.0:
                raise ValueError("VCA gain outside supported range [-120, 24] dB")
            members = self.group.member_track_ids
            if len(set(members)) != len(members):
                raise ValueError("Duplicate VCA members")
            valid_ids = {track.track_id for track in timeline.audio_tracks}
            if set(members) - valid_ids:
                raise ValueError("VCA member audio track not found")
            result = timeline.with_vca_group(self.group)
        else:
            result = replace(timeline, vcas=tuple(g for g in timeline.vcas if g.vca_id != self.vca_id))
        updated = replace(self, _old_groups=timeline.vcas)
        return (timeline if result.vcas == timeline.vcas else result), updated

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return timeline if self._old_groups is None else replace(timeline, vcas=self._old_groups)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_type": type(self).__name__, "command_id": self.command_id,
            "label": self.label, "vca_id": self.vca_id,
            "group": None if self.group is None else self.group.to_dict(),
            "old_groups": None if self._old_groups is None else [g.to_dict() for g in self._old_groups],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetVcaGroupCommand:
        group = data.get("group")
        old = data.get("old_groups")
        return cls(data["command_id"], data["label"], data["vca_id"],
                   None if group is None else VcaGroup.from_dict(group),
                   None if old is None else tuple(VcaGroup.from_dict(g) for g in old))

    @classmethod
    def create(cls, vca_id: str, group: VcaGroup | None) -> SetVcaGroupCommand:
        return cls(str(uuid.uuid4()), "Set VCA Group", vca_id, group)
