"""Composition-owned markers (B04).

Markers belong to the composition (the NodeGraph), not to any layer: they
carry a stable UUID, an integer frame, a name, an optional duration (in
frames; ``None`` is a point marker) and a color.  Several markers may share
a frame; every edit addresses markers by UUID so same-frame markers stay
independently editable.  Markers are pure data (no Qt) so export and
headless consumers can read them from the serialized graph.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MARKER_COLOR = "#f2c94c"


@dataclass(frozen=True)
class CompositeMarker:
    """One composition marker at an integer frame."""

    marker_id: str
    frame: int
    name: str = ""
    duration: Optional[int] = None  # frames; None = point marker
    color: str = DEFAULT_MARKER_COLOR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "marker_id": self.marker_id,
            "frame": int(self.frame),
            "name": str(self.name),
            "duration": (int(self.duration)
                         if self.duration is not None else None),
            "color": str(self.color),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["CompositeMarker"]:
        """Parse one marker, returning None for malformed entries."""
        if not isinstance(data, dict):
            return None
        marker_id = data.get("marker_id")
        if not isinstance(marker_id, str) or not marker_id:
            return None
        try:
            frame = int(data.get("frame"))
        except (TypeError, ValueError, OverflowError):
            return None
        name = data.get("name", "")
        if not isinstance(name, str):
            name = ""
        duration = data.get("duration")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError, OverflowError):
                duration = None
            if duration is not None and duration < 0:
                duration = None
        color = data.get("color", DEFAULT_MARKER_COLOR)
        if not isinstance(color, str) or not color:
            color = DEFAULT_MARKER_COLOR
        return cls(marker_id=marker_id, frame=frame, name=name,
                   duration=duration, color=color)

    @classmethod
    def new_id(cls) -> str:
        return f"marker_{uuid.uuid4().hex[:12]}"


def parse_markers(raw: Any) -> Dict[str, CompositeMarker]:
    """Tolerantly parse the serialized marker list (migration-safe).

    Malformed entries are dropped and duplicate ids keep the first
    occurrence, so a corrupt file cannot break project load.
    """
    markers: Dict[str, CompositeMarker] = {}
    if not isinstance(raw, list):
        return markers
    for item in raw:
        marker = CompositeMarker.from_dict(item)
        if marker is None or marker.marker_id in markers:
            continue
        markers[marker.marker_id] = marker
    return markers


def markers_report(markers: Dict[str, CompositeMarker],
                   duration_frames: Optional[int]
                   ) -> Tuple[List[CompositeMarker], List[CompositeMarker]]:
    """Split markers into visible and out-of-bounds after a comp change.

    Out-of-bounds markers are never deleted: their positions survive so a
    later re-lengthening of the composition restores them (B04 §2).
    ``duration_frames`` None / non-positive means unbounded.
    """
    if duration_frames is None or int(duration_frames) <= 0:
        return list(markers.values()), []
    limit = int(duration_frames)
    visible: List[CompositeMarker] = []
    outside: List[CompositeMarker] = []
    for marker in markers.values():
        if 0 <= marker.frame < limit:
            visible.append(marker)
        else:
            outside.append(marker)
    return visible, outside
