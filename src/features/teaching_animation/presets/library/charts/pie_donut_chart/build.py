"""环形饼图 — arc slices swinging into place around a labelled centre."""

import math

from src.features.teaching_animation.presets.builders import (
    key, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "pie_donut_chart",
    "name": "环形饼图",
    "name_i18n": {"en_US": "Pie Donut Chart"},
    "category": "charts",
    "description": "四段扇形绕圆心依次旋入，中心镂空显示总量，附图例",
    "description_i18n": {"en_US": "Four slices spin in around the center with a total label and legend"},
    "tags": ["chart", "pie", "donut", "图表", "饼图"],
}

_SLICES = [
    (0, 132, "#22d3ee", "Desktop 37%"),
    (132, 250, "#f472b6", "Mobile 33%"),
    (250, 320, "#facc15", "Tablet 19%"),
    (320, 360, "#a78bfa", "Other 11%"),
]
_RADIUS = 290.0
_CENTER = (960.0, 540.0)


def _arc_path(start_deg: float, end_deg: float, radius: float) -> str:
    x0 = radius * math.cos(math.radians(start_deg))
    y0 = radius * math.sin(math.radians(start_deg))
    x1 = radius * math.cos(math.radians(end_deg))
    y1 = radius * math.sin(math.radians(end_deg))
    large = 1 if (end_deg - start_deg) > 180.0 else 0
    return (f"M 0 0 L {x0:.2f} {y0:.2f} "
            f"A {radius:.2f} {radius:.2f} 0 {large} 1 {x1:.2f} {y1:.2f} Z")


def build() -> dict:
    cx, cy = _CENTER
    elements = []
    for index, (start_deg, end_deg, color, _label) in enumerate(_SLICES):
        span = end_deg - start_deg
        appear = 4 + index * 5
        elements.append(shape_element(
            f"slice_{index}", cx, cy, _RADIUS * 2.0, _RADIUS * 2.0, kind="path",
            z=10 + index, fill=color, d=_arc_path(start_deg, end_deg, _RADIUS),
            tracks=(
                track("rotation", key(appear, -28.0 - span * 0.2), key(appear + 16, 0.0, "ease_out")),
                track("opacity", key(appear, 0.0), key(appear + 10, 1.0)),
            )))
    elements.extend([
        shape_element("hole", cx - 118, cy - 118, 236, 236, kind="circle", z=20,
                      fill="#0e1526", stroke="#1e293b", stroke_width=2.0),
        text_element("center_value", cx, cy - 26, "128k", size=76, weight="700",
                     color="#f8fafc", anchor="middle", z=21,
                     tracks=(track("opacity", key(24, 0.0), key(34, 1.0)),)),
        text_element("center_caption", cx, cy + 62, "Total Sessions", size=24,
                     color="#94a3b8", anchor="middle", z=22,
                     tracks=(track("opacity", key(28, 0.0), key(38, 1.0)),)),
        text_element("title", 960, 128, "Traffic by Device", size=58, weight="700",
                     color="#f8fafc", anchor="middle", z=30,
                     tracks=(track("opacity", key(0, 0.0), key(12, 1.0)),)),
    ])
    for index, (_start, _end, color, label) in enumerate(_SLICES):
        lx = 560 + (index % 2) * 420
        ly = 912 + (index // 2) * 52
        elements.append(shape_element(f"legend_dot_{index}", lx, ly + 8, 22, 22,
                                      kind="rect", z=31, fill=color, corner_radius=6.0))
        elements.append(text_element(f"legend_{index}", lx + 36, ly, label, size=26,
                                     color="#cbd5f5", z=32))
    return scene_document(elements, name="Pie Donut Chart", duration_frames=72).to_dict()
