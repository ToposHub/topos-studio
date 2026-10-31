"""曲线图 — smooth bezier line chart with popping data points."""

from src.features.teaching_animation.presets.builders import (
    key, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "line_curve_chart",
    "name": "平滑曲线图",
    "name_i18n": {"en_US": "Line Curve Chart"},
    "category": "charts",
    "description": "贝塞尔平滑曲线 + 数据点依次弹出 + 渐变面积，适合趋势展示",
    "description_i18n": {"en_US": "Bezier-smoothed curve with popping data points and a gradient fill area"},
    "tags": ["chart", "line", "curve", "图表", "曲线", "趋势"],
}

_POINTS = [(400, 760), (632, 636), (864, 668), (1096, 500), (1328, 540), (1540, 384)]
_BASE_Y = 820.0


def _smooth_path(points) -> str:
    parts = [f"M {points[0][0]} {points[0][1]}"]
    for index in range(1, len(points)):
        x0, y0 = points[index - 1]
        x1, y1 = points[index]
        dx = x1 - x0
        c1x, c1y = x0 + dx * 0.42, y0 + (y1 - y0) * 0.08
        c2x, c2y = x1 - dx * 0.42, y1 - (y1 - y0) * 0.08
        parts.append(f"C {c1x:.2f} {c1y:.2f} {c2x:.2f} {c2y:.2f} {x1:.2f} {y1:.2f}")
    return " ".join(parts)


def build() -> dict:
    line_path = _smooth_path(_POINTS)
    last_x, last_y = _POINTS[-1]
    first_x, first_y = _POINTS[0]
    area_path = (f"{line_path} L {last_x:.2f} {_BASE_Y:.2f} "
                 f"L {first_x:.2f} {_BASE_Y:.2f} Z")
    elements = [
        text_element("title", 960, 120, "Render Throughput", size=60, weight="700",
                     color="#f8fafc", anchor="middle", z=40,
                     tracks=(track("opacity", key(0, 0.0), key(10, 1.0)),)),
        shape_element("axis_x", 360, _BASE_Y, 1240, 3, kind="rect", z=30, fill="#334155"),
        shape_element("axis_y", 360, 300, 3, _BASE_Y - 300, kind="rect", z=30, fill="#334155"),
        shape_element("area", 0, 0, 1920, 1080, kind="path", z=31, fill="#22d3ee",
                      fill_opacity=0.16, d=area_path,
                      tracks=(track("opacity", key(8, 0.0), key(26, 1.0, "ease_out")),)),
        shape_element("line", 0, 0, 1920, 1080, kind="path", z=32, fill="#22d3ee",
                      fill_opacity=0.0, stroke="#22d3ee", stroke_width=7.0, d=line_path,
                      tracks=(track("opacity", key(4, 0.0), key(22, 1.0, "ease_out")),)),
    ]
    for index, (px, py) in enumerate(_POINTS):
        appear = 10 + index * 4
        elements.append(shape_element(
            f"dot_{index}", px - 13, py - 13, 26, 26, kind="circle", z=33,
            fill="#e0f2fe", stroke="#22d3ee", stroke_width=4.0,
            tracks=(
                track("opacity", key(appear, 0.0), key(appear + 6, 1.0)),
                track("scale_x", key(appear, 0.2), key(appear + 8, 1.0, "ease_out")),
                track("scale_y", key(appear, 0.2), key(appear + 8, 1.0, "ease_out")),
            )))
    for index, label in enumerate(["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]):
        px = _POINTS[index][0]
        elements.append(text_element(f"xlabel_{index}", px, 852, label, size=24,
                                     color="#64748b", anchor="middle", z=34))
    return scene_document(elements, name="Smooth Line Chart", duration_frames=72).to_dict()
