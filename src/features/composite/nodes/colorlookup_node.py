"""ColorLookup Node — per-channel curve lookup (CP-014).

Nuke ColorLookup / AE Curves counterpart: master / R / G / B curves remap
channel values. Curves are stored as JSON control-point lists
(``[[x, y], ...]``, x/y in the node's input range, x strictly ascending
after parsing); the evaluator bakes them into a 256×4 float LUT texture
(row 0=R, 1=G, 2=B, 3=master) and applies per-channel
``out = master(curve_ch(value))`` in one pass.

Honest boundary (MVP): control points interpolate linearly; the in-panel
curve editor hook-up is the follow-up (the JSON param is editable as
text meanwhile). Generic mask/mix (CP-044) applies for free.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from .base_node import Node, ParamDef, Port, TextureResult

_DEFAULT_CURVE = "[[0.0, 0.0], [1.0, 1.0]]"


def parse_curve_points(value: Any) -> List[Tuple[float, float]]:
    """Parse a curve JSON into sorted, de-duplicated control points.

    Malformed payloads fall back to the identity curve (never raise) — a
    hand-edited project file must not break evaluation.
    """
    try:
        raw = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = None
    points: List[Tuple[float, float]] = []
    if isinstance(raw, list):
        for pt in raw:
            try:
                x = float(pt[0])
                y = float(pt[1])
            except (TypeError, IndexError, ValueError):
                continue
            points.append((x, y))
    if not points:
        points = [(0.0, 0.0), (1.0, 1.0)]
    points.sort(key=lambda p: p[0])
    # De-duplicate identical x (keep first) so segments never invert.
    dedup: List[Tuple[float, float]] = []
    for x, y in points:
        if dedup and x - dedup[-1][0] < 1e-6:
            continue
        dedup.append((x, y))
    if len(dedup) == 1:
        dedup.append((dedup[0][0] + 1.0, dedup[0][1]))
    return dedup


def bake_curve_lut(points: List[Tuple[float, float]], input_min: float,
                   input_max: float, size: int = 256) -> List[float]:
    """Bake control points into a ``size``-entry LUT over the input range.

    Linear interpolation between control points; outside the control span
    the nearest end value is held (Nuke behaviour).
    """
    out: List[float] = []
    dom = input_max - input_min
    if dom <= 1e-9:
        return [points[0][1]] * size
    for i in range(size):
        # 端点对齐（i/(size-1)）：shader 直接以 uv.x=v 线性采样即可命中
        v = input_min + dom * i / (size - 1)
        if v <= points[0][0]:
            out.append(points[0][1])
            continue
        if v >= points[-1][0]:
            out.append(points[-1][1])
            continue
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= v <= x1:
                t = (v - x0) / max(x1 - x0, 1e-9)
                out.append(y0 + (y1 - y0) * t)
                break
    return out


class ColorLookupNode(Node):
    """Master/R/G/B curve colour lookup."""

    node_type = "colorlookup"
    node_title = "ColorLookup"
    i18n_title_key = "nodes.colorlookup.title"
    header_color = "#8a5c7d"

    SEARCH_ALIASES = [
        "colorlookup", "curves", "color curves", "rgb curves",
        "曲线", "颜色曲线", "RGB曲线",
    ]
    DESCRIPTION = "主/R/G/B 曲线查表调色（JSON 控制点，256 级 LUT 烘焙）"
    TAGS = ["color", "filter"]

    PARAM_DEFS = [
        ParamDef("master_curve", "str", _DEFAULT_CURVE, group="Curves"),
        ParamDef("red_curve", "str", _DEFAULT_CURVE, group="Curves"),
        ParamDef("green_curve", "str", _DEFAULT_CURVE, group="Curves"),
        ParamDef("blue_curve", "str", _DEFAULT_CURVE, group="Curves"),
        ParamDef("input_min", "float", 0.0, min_val=-4.0, max_val=0.0,
                 step=0.01, group="Range"),
        ParamDef("input_max", "float", 1.0, min_val=0.01, max_val=16.0,
                 step=0.01, group="Range"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "master_curve": _DEFAULT_CURVE,
            "red_curve": _DEFAULT_CURVE,
            "green_curve": _DEFAULT_CURVE,
            "blue_curve": _DEFAULT_CURVE,
            "input_min": 0.0,
            "input_max": 1.0,
        }
        self._init_port_revisions()

    def get_curve(self, name: str) -> List[Tuple[float, float]]:
        return parse_curve_points(self.parameters.get(
            name, _DEFAULT_CURVE))

    def set_curve(self, name: str, points: List[Tuple[float, float]]) -> None:
        self.set_parameter(name, json.dumps(
            [[float(x), float(y)] for x, y in points]))

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        has_image = image is not None and image.is_valid
        if not has_image:
            return {}
        width = max(1, int(image.width))
        height = max(1, int(image.height))
        return {
            "result": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space),
            "_effect_type": "colorlookup",
            "_effect_input": image,
            "_effect_params": {
                "master_curve": self.get_curve("master_curve"),
                "red_curve": self.get_curve("red_curve"),
                "green_curve": self.get_curve("green_curve"),
                "blue_curve": self.get_curve("blue_curve"),
                "input_min": self._get_param_clamped("input_min", -4.0, 0.0),
                "input_max": self._get_param_clamped("input_max", 0.01, 16.0),
            },
        }
