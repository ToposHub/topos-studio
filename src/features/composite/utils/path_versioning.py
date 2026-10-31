"""Read/Write path version binning (C5 version switching).

Nuke-style version tokens — ``v001``, ``V2``, ``ver_03``, ``version-4`` —
are recognised anywhere in the path (file name or directory); the LAST one
wins so ``shot010/v003/plate_v02.exr`` bins on the file's own v02.
Padding is preserved when bumping (``v003`` → ``v002`` / ``v004``).
Paths without a version token are untouched by design (control hides).
"""

import re
import glob as _globmod
from glob import glob
from typing import List, Optional, Tuple

# v / ver / version + optional separator + digits (case-insensitive).
# The lookbehind requires a non-alphanumeric boundary so ordinary words
# don't bin as versions: "preview_v2" yes, "av1_master"/"rev2" no.
_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:v|ver|version)[._\- ]?(?P<num>\d+)",
    re.IGNORECASE)


def find_version_span(path: str) -> Optional[Tuple[int, int, str]]:
    """Locate the LAST version token: (num_start, num_end, num_text)."""
    matches = list(_VERSION_RE.finditer(path or ""))
    if not matches:
        return None
    m = matches[-1]
    return (m.start("num"), m.end("num"), m.group("num"))


def parse_version(path: str) -> Optional[int]:
    """Numeric value of the path's version token (None = no version)."""
    span = find_version_span(path)
    return int(span[2]) if span else None


def bump_version(path: str, delta: int) -> Optional[str]:
    """Step the version by ``delta``, preserving zero padding.

    Returns None for paths without a version token (the caller keeps the
    path unchanged).  The number never goes below zero.
    """
    span = find_version_span(path)
    if not span:
        return None
    start, end, num = span
    new_num = max(0, int(num) + int(delta))
    return path[:start] + str(new_num).zfill(len(num)) + path[end:]


def list_versions(path: str) -> List[str]:
    """Existing sibling versions of ``path``, sorted ascending by version.

    Globs the same directory with the version token replaced by ``*`` and
    keeps only results that still parse as the same binning.
    """
    span = find_version_span(path)
    if not span:
        return []
    start, end, _ = span
    # Escape glob metacharacters in the prefix/suffix (e.g. "plate[v2].exr")
    # — only the version token itself is a wildcard.
    pattern = (_globmod.escape(path[:start]) + "*"
               + _globmod.escape(path[end:]))
    found: List[Tuple[int, str]] = []
    for candidate in glob(pattern):
        cspan = find_version_span(candidate)
        if cspan:
            found.append((int(cspan[2]), candidate))
    found.sort()
    return [p for _, p in found]


def stepped_version(path: str, delta: int) -> Optional[str]:
    """Switch to the NEAREST existing version in the step direction.

    Nuke behaviour: ▲ jumps the current version, skipping gaps (v001 ▲
    with v002 missing lands on v003).  Returns None when there is no
    version token or no neighbour on disk in that direction.
    """
    span = find_version_span(path)
    if span is None:
        return None
    current = int(span[2])
    direction = 1 if delta > 0 else -1
    candidates = [
        p for p in list_versions(path)
        if (parse_version(p) - current) * direction > 0
    ]
    if not candidates:
        return None
    return (min if direction > 0 else max)(
        candidates, key=lambda p: parse_version(p))


def latest_version(path: str) -> Optional[str]:
    """Highest EXISTING version of this path (None = keep path)."""
    versions = list_versions(path)
    return versions[-1] if versions else None
