"""CDL Node — ASC Color Decision List (CP-015).

Applies the ASC CDL primary grade: per-channel
``out = pow(in * slope + offset, power)`` followed by Rec.709-luma
saturation. Supports importing/exporting the standard ``.cdl``
(ColorDecision XML) interchange format used between on-set and DI
systems (slope="1 1 1" power="1 1 1" offset="0 0 0" saturation="1").

Generic mask/mix (CP-044) applies for free.
"""

import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional, Tuple

from .base_node import Node, ParamDef, Port, TextureResult


def _parse_triple(text: str) -> Tuple[float, float, float]:
    parts = [float(v) for v in text.replace(",", " ").split()]
    while len(parts) < 3:
        parts.append(1.0 if len(parts) == 0 and "saturation" not in text else 0.0)
    return (parts[0], parts[1], parts[2])


def parse_cdl_file(path: str) -> Optional[Dict[str, Any]]:
    """Parse an ASC CDL (.cdl) ColorDecision XML into CDL values.

    Returns {"slope": (r,g,b), "offset": (r,g,b), "power": (r,g,b),
    "saturation": float} or None when the file is unreadable/not CDL.
    """
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    # Tolerate <ColorDecisionList> wrappers of ANY namespace (ASC urn or
    # bare) — match by local tag name.
    def _find_local(local: str):
        for el in root.iter():
            if el.tag.rsplit('}', 1)[-1] == local:
                return el
        return None
    sop = _find_local("SOPNode")
    sat_node = _find_local("SaturationNode")
    if sop is None and sat_node is None:
        return None
    values: Dict[str, Any] = {
        "slope": (1.0, 1.0, 1.0),
        "offset": (0.0, 0.0, 0.0),
        "power": (1.0, 1.0, 1.0),
        "saturation": 1.0,
    }
    if sop is not None:
        for tag in ("slope", "offset", "power"):
            el = next((e for e in sop
                       if e.tag.rsplit('}', 1)[-1].lower() == tag), None)
            if el is not None and el.text:
                try:
                    values[tag] = _parse_triple(el.text)
                except ValueError:
                    continue
    # <SaturationNode><Saturation>0.8</Saturation></SaturationNode> — the
    # wrapper's text is whitespace; the value lives in the child element
    # (some writers inline it on the wrapper, handle both).
    sat_value_el = None
    if sat_node is not None:
        sat_value_el = next(
            (e for e in sat_node
             if e.tag.rsplit('}', 1)[-1].lower() == "saturation"), None)
        if sat_value_el is None and sat_node.text and sat_node.text.strip():
            sat_value_el = sat_node
    if sat_value_el is not None and sat_value_el.text:
        try:
            values["saturation"] = float(sat_value_el.text)
        except ValueError:
            pass
    return values


def write_cdl_file(path: str, slope: Tuple[float, float, float],
                   offset: Tuple[float, float, float],
                   power: Tuple[float, float, float],
                   saturation: float) -> bool:
    """Write an ASC CDL (.cdl) file with one ColorDecision."""
    def _fmt(t: Tuple[float, float, float]) -> str:
        return f"{t[0]:.6f} {t[1]:.6f} {t[2]:.6f}"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ColorDecisionList xmlns="urn:ASC:CDL:v1.01">\n'
        '  <ColorDecision>\n'
        '    <ColorCorrection>\n'
        '      <SOPNode>\n'
        f'        <Slope>{_fmt(slope)}</Slope>\n'
        f'        <Offset>{_fmt(offset)}</Offset>\n'
        f'        <Power>{_fmt(power)}</Power>\n'
        '      </SOPNode>\n'
        '      <SaturationNode>\n'
        f'        <Saturation>{saturation:.6f}</Saturation>\n'
        '      </SaturationNode>\n'
        '    </ColorCorrection>\n'
        '  </ColorDecision>\n'
        '</ColorDecisionList>\n')
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        return True
    except OSError:
        return False


class CDLNode(Node):
    """ASC CDL primary grade (slope/power/offset/saturation)."""

    node_type = "cdl"
    node_title = "CDL"
    i18n_title_key = "nodes.cdl.title"
    header_color = "#7d8a5c"

    SEARCH_ALIASES = ["cdl", "asc cdl", "color decision list",
                      "slope", "power", "offset", "调色决策", "CDL调色"]
    DESCRIPTION = "ASC CDL 主调色（slope/power/offset/saturation，.cdl 导入导出）"
    TAGS = ["color", "grade"]

    PARAM_DEFS = [
        ParamDef("slope_r", "float", 1.0, min_val=0.0, max_val=8.0, step=0.01),
        ParamDef("slope_g", "float", 1.0, min_val=0.0, max_val=8.0, step=0.01),
        ParamDef("slope_b", "float", 1.0, min_val=0.0, max_val=8.0, step=0.01),
        ParamDef("offset_r", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("offset_g", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("offset_b", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("power_r", "float", 1.0, min_val=0.05, max_val=8.0, step=0.01),
        ParamDef("power_g", "float", 1.0, min_val=0.05, max_val=8.0, step=0.01),
        ParamDef("power_b", "float", 1.0, min_val=0.05, max_val=8.0, step=0.01),
        ParamDef("saturation", "float", 1.0, min_val=0.0, max_val=4.0,
                 step=0.01),
        # CP-015: .cdl 交换（str 参数，属性面板文本编辑）
        ParamDef("cdl_path", "str", ""),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {p.name: p.default for p in self.PARAM_DEFS}
        self._init_port_revisions()

    def load_cdl(self, path: str) -> bool:
        """Import CDL values from a .cdl file into this node's parameters."""
        values = parse_cdl_file(path)
        if values is None:
            return False
        for key in ("slope", "offset", "power"):
            r, g, b = values[key]
            self.set_parameter(f"{key}_r", float(r))
            self.set_parameter(f"{key}_g", float(g))
            self.set_parameter(f"{key}_b", float(b))
        self.set_parameter("saturation", float(values["saturation"]))
        self.set_parameter("cdl_path", path)
        return True

    def save_cdl(self, path: str) -> bool:
        """Export this node's CDL values to a .cdl file."""
        return write_cdl_file(
            path,
            (self.parameters["slope_r"], self.parameters["slope_g"],
             self.parameters["slope_b"]),
            (self.parameters["offset_r"], self.parameters["offset_g"],
             self.parameters["offset_b"]),
            (self.parameters["power_r"], self.parameters["power_g"],
             self.parameters["power_b"]),
            float(self.parameters["saturation"]))

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        return {
            "result": TextureResult(
                texture_id=-1, width=image.width, height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space),
            "_effect_type": "cdl",
            "_effect_input": image,
            "_effect_params": {
                "slope": (
                    self._get_param_clamped("slope_r", 0.0, 8.0),
                    self._get_param_clamped("slope_g", 0.0, 8.0),
                    self._get_param_clamped("slope_b", 0.0, 8.0)),
                "offset": (
                    self._get_param_clamped("offset_r", -1.0, 1.0),
                    self._get_param_clamped("offset_g", -1.0, 1.0),
                    self._get_param_clamped("offset_b", -1.0, 1.0)),
                "power": (
                    self._get_param_clamped("power_r", 0.05, 8.0),
                    self._get_param_clamped("power_g", 0.05, 8.0),
                    self._get_param_clamped("power_b", 0.05, 8.0)),
                "saturation": self._get_param_clamped("saturation", 0.0, 4.0),
            },
        }
