"""Small, dependency-free IES LM-63 profile reader.

The renderer uses four normalized angular samples. This is intentionally a
stable approximation rather than a full photometric database: it preserves
the important beam shape, keeps profiles serializable, and avoids uploading
large CPU-side tables for every light.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class IESProfile:
    """Normalized vertical photometric distribution for one light."""

    path: str
    vertical_angles: Tuple[float, ...]
    candela: Tuple[float, ...]

    def samples(self, count: int = 4) -> Tuple[float, ...]:
        """Return normalized samples from 0° to 90° for a shader lookup."""

        if count < 2 or not self.candela:
            return (1.0,)
        peak = max(max(self.candela), 1e-6)
        result = []
        for index in range(count):
            angle = 90.0 * index / float(count - 1)
            result.append(max(0.0, min(1.0, sample_profile(self, angle) / peak)))
        return tuple(result)


def sample_profile(profile: IESProfile, angle_deg: float) -> float:
    """Linearly sample candela at a vertical profile angle."""

    if not profile.candela:
        return 1.0
    if not profile.vertical_angles:
        return profile.candela[0]
    angle = max(profile.vertical_angles[0],
                min(float(angle_deg), profile.vertical_angles[-1]))
    for index in range(1, len(profile.vertical_angles)):
        right = profile.vertical_angles[index]
        if angle <= right:
            left = profile.vertical_angles[index - 1]
            lv = profile.candela[index - 1]
            rv = profile.candela[min(index, len(profile.candela) - 1)]
            span = max(right - left, 1e-6)
            return lv + (rv - lv) * (angle - left) / span
    return profile.candela[-1]


def load_ies_profile(path: str) -> IESProfile:
    """Parse the common ``TILT=NONE`` LM-63 numeric section.

    Unsupported tilt blocks or malformed files raise ``ValueError`` so the
    node can report a structured warning and render without the profile.
    """

    source = str(Path(path).expanduser().resolve())
    if not Path(source).is_file():
        raise ValueError(f"IES file does not exist: {path}")
    text = Path(source).read_text(encoding="utf-8", errors="replace")
    marker = re.search(r"TILT\s*=\s*NONE", text, flags=re.IGNORECASE)
    if marker is None:
        raise ValueError("only TILT=NONE IES profiles are supported")
    tokens = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?",
                        text[marker.end():])
    values = [float(token) for token in tokens]
    if len(values) < 13:
        raise ValueError("IES numeric section is incomplete")
    lamp_count = max(1, int(values[0]))
    candela_multiplier = values[2]
    vertical_count = int(values[3])
    horizontal_count = int(values[4])
    if vertical_count < 2 or horizontal_count < 1:
        raise ValueError("IES angle counts are invalid")
    start = 13
    end_vertical = start + vertical_count
    end_horizontal = end_vertical + horizontal_count
    end_candela = end_horizontal + vertical_count * horizontal_count
    if end_candela > len(values):
        raise ValueError("IES candela table is incomplete")
    angles = tuple(values[start:end_vertical])
    table = values[end_horizontal:end_candela]
    # Use the first horizontal slice. Most production profiles are symmetric;
    # averaging all slices is more stable for asymmetric files.
    candela = []
    for vertical_index in range(vertical_count):
        samples = [table[h * vertical_count + vertical_index]
                   for h in range(horizontal_count)]
        candela.append(sum(samples) / max(1, len(samples)))
    scale = candela_multiplier / float(lamp_count)
    return IESProfile(source, angles, tuple(max(0.0, value * scale)
                                             for value in candela))
