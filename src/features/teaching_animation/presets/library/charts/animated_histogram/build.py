"""动画直方图 — staggered grow-in bar chart with value labels."""

from src.features.teaching_animation.presets.builders import (
    key, linear_gradient, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "animated_histogram",
    "name": "动画直方图",
    "name_i18n": {"en_US": "Animated Histogram"},
    "category": "charts",
    "description": "7 根渐变柱依次长出，带数值标签与基线，入场即动画",
    "description_i18n": {"en_US": "Seven gradient bars grow in sequence with value labels and a baseline"},
    "tags": ["chart", "histogram", "bar", "图表", "直方图", "柱状"],
}

_VALUES = [0.38, 0.56, 0.44, 0.80, 0.62, 0.92, 0.72]
_BAR_COLORS = ["#38bdf8", "#22d3ee", "#34d399", "#facc15", "#fb923c", "#f87171", "#a78bfa"]
_BASE_Y = 800.0
_MAX_H = 460.0


def build() -> dict:
    elements = [
        text_element("title", 960, 120, "Monthly Active Users", size=64, weight="700",
                     color="#f8fafc", anchor="middle", z=30),
        text_element("subtitle", 960, 196, "单位：万人 · 2026 H1", size=26,
                     color="#94a3b8", anchor="middle", z=31),
        shape_element("baseline", 320, _BASE_Y, 1280, 3, kind="rect", z=32, fill="#334155"),
    ]
    for index, value in enumerate(_VALUES):
        final_h = value * _MAX_H
        x = 340 + index * 172
        color = _BAR_COLORS[index]
        start = 6 + index * 4
        end = start + 18
        y_final = _BASE_Y - final_h
        elements.append(shape_element(
            f"bar_{index}", x, _BASE_Y, 116, 0.0, kind="rect", z=40 + index,
            fill=color, corner_radius=8.0,
            gradient=linear_gradient((0.0, color), (1.0, "#0f172a"), angle=90.0),
            tracks=(
                track("height", key(start, 0.0), key(end, final_h, "ease_out")),
                track("y", key(start, _BASE_Y), key(end, y_final, "ease_out")),
            )))
        elements.append(text_element(
            f"label_{index}", x + 58, y_final - 56, f"{value * 100:.0f}", size=34,
            weight="700", color=color, anchor="middle", z=60 + index,
            tracks=(track("opacity", key(end, 0.0), key(end + 8, 1.0, "ease_out")),)))
    return scene_document(elements, name="Animated Histogram", duration_frames=72).to_dict()
