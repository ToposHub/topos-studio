"""Frame-sequence path resolution (shared convention).

Supports the two widespread sequence notations:

* ``####`` runs — ``plate.####.exr`` with padding = the number of ``#``
* printf patterns — ``plate.%04d.exr``

``image_input`` (Render-in-Place bake products) and ``volume3d`` (VDB
sequences) both resolve through this module so one convention serves the
whole graph.
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"(#+)|(%0\d+d)")


def has_frame_pattern(path: str) -> bool:
    """True when the path carries a frame placeholder."""
    return bool(path) and ("#" in path or "%0" in path)


def resolve_frame_pattern_path(path: str, frame: int,
                               padding: int = 4) -> str:
    """Substitute the frame number into ``####`` / ``%04d`` patterns.

    Paths without a pattern are returned unchanged.
    """

    def _sub(match: "re.Match") -> str:
        if match.group(1):
            return str(int(frame)).zfill(len(match.group(1)))
        return match.group(2) % int(frame)

    if not path or not has_frame_pattern(path):
        return path
    return _PATTERN.sub(_sub, path)
