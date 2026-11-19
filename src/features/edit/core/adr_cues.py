"""Timeline ADR cues and their undoable updates."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence


_KEY = "adr_cue_sheet"
_VERSION = 1


@dataclass(frozen=True)
class AdrCue:
    cue_id: str
    in_frame: int
    out_frame: int
    character: str = ""
    dialog: str = ""

    def __post_init__(self) -> None:
        if not self.cue_id or self.in_frame < 0 or self.out_frame <= self.in_frame:
            raise ValueError("ADR cue requires an ID and a nonempty time range")

    @property
    def scene_name(self) -> str:
        """Keep takes grouped together when a cue's dialog or timing changes."""
        return f"ADR_{self.cue_id}"


def read_adr_cues(timeline: TimelineSequence) -> tuple[AdrCue, ...]:
    sheet = timeline.format_settings.get(_KEY)
    if not isinstance(sheet, dict) or sheet.get("version") != _VERSION:
        return ()
    fps = sheet.get("fps")
    rows = sheet.get("cues")
    if not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        return ()
    if not isinstance(rows, list):
        return ()
    scale = float(timeline.fps) / fps
    cues: dict[str, AdrCue] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cue_id, start, end = (row.get(key) for key in ("cue_id", "in_frame", "out_frame"))
        if (not isinstance(cue_id, str) or not cue_id or type(start) is not int
                or type(end) is not int or start < 0 or end <= start):
            continue
        in_frame = round(start * scale)
        cues[cue_id] = AdrCue(
            cue_id, in_frame, max(in_frame + 1, round(end * scale)),
            str(row.get("character") or ""), str(row.get("dialog") or ""),
        )
    return tuple(sorted(cues.values(), key=lambda cue: (cue.in_frame, cue.cue_id)))


@dataclass(frozen=True)
class SetAdrCuesCommand:
    command_id: str
    label: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    @classmethod
    def create(
        cls, timeline: TimelineSequence, cues: tuple[AdrCue, ...],
    ) -> SetAdrCuesCommand:
        before = timeline.format_settings.get(_KEY)
        if before is not None and (not isinstance(before, dict) or before.get("version") != _VERSION):
            raise ValueError("Unsupported ADR cue sheet version")
        # Keep unfamiliar fields from compatible versions of the cue sheet.
        previous = {
            entry["cue_id"]: entry for entry in (before or {}).get("cues", [])
            if isinstance(entry, dict) and isinstance(entry.get("cue_id"), str)
        } if isinstance((before or {}).get("cues"), list) else {}
        after = {
            **(before or {}),
            "version": _VERSION,
            "fps": float(timeline.fps),
            "cues": [{**previous.get(cue.cue_id, {}), **asdict(cue)} for cue in cues],
        }
        return cls(str(uuid.uuid4()), "Edit ADR cues", before, after)

    @staticmethod
    def _with_sheet(
        timeline: TimelineSequence, sheet: dict[str, Any] | None,
    ) -> TimelineSequence:
        settings = dict(timeline.format_settings)
        if sheet is None:
            settings.pop(_KEY, None)
        else:
            settings[_KEY] = sheet
        return timeline if settings == timeline.format_settings else replace(timeline, format_settings=settings)

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        return self._with_sheet(timeline, self.after)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return self._with_sheet(timeline, self.before)

    def to_dict(self) -> dict[str, Any]:
        return {"command_type": "SetAdrCuesCommand", **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SetAdrCuesCommand:
        return cls(data["command_id"], data["label"], data.get("before"), data.get("after"))
