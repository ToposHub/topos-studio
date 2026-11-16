"""Stable track identity colors shared by timeline and mixer, without Qt."""

from __future__ import annotations

import colorsys
import hashlib
from collections.abc import Iterable


TRACK_COLORS: tuple[str, ...] = (
    "#65ad92", "#739bc5", "#c68d70", "#aa85be",
    "#b3ad69", "#65aeb4", "#c47e92", "#8aab73",
    "#858cc7", "#c4a16b", "#77a8a3", "#b58d9e",
    "#92a7c2", "#a6b47b", "#bc9290", "#9d96b9",
)


def resolve_track_color(track_id: str, color: str | None) -> str:
    """Respect saved colors; give legacy uncolored tracks a stable identity."""
    if color is not None:
        return color
    digest = hashlib.blake2b(track_id.encode("utf-8"), digest_size=8).digest()
    return TRACK_COLORS[int.from_bytes(digest, "big") % len(TRACK_COLORS)]


def next_track_color(existing_colors: Iterable[str]) -> str:
    """Choose an unused muted color, extending the palette for large sessions."""
    used = {color.casefold() for color in existing_colors}
    for color in TRACK_COLORS:
        if color not in used:
            return color
    # A golden-angle hue sequence gives additional tracks distinct colors
    # without cycling back to an already-used base palette entry.
    index = 0
    while True:
        hue = (index * 0.618033988749895 + 0.11) % 1.0
        lightness = (0.54, 0.61, 0.47)[index % 3]
        red, green, blue = colorsys.hls_to_rgb(hue, lightness, 0.34)
        color = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        if color not in used:
            return color
        index += 1
