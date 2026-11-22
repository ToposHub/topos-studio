"""Editable Alpha-derived styles for compositor layers."""

from __future__ import annotations

import math
from typing import Any

from .base_node import Node, ParamDef, Port, TextureResult
from ..core.format import BBox

_STYLE_PREFIXES = ("overlay", "shadow", "glow")


class LayerStyleNode(Node):
    """Apply color overlay, outer glow and drop shadow using one source Alpha."""

    node_type = "layer_style"
    node_title = "Layer Style"
    i18n_title_key = "nodes.layer_style.title"
    header_color = "#824c8a"
    SEARCH_ALIASES = ["layer style", "color overlay", "drop shadow", "outer glow", "图层样式", "投影"]
    PARAM_DEFS = [
        ParamDef(f"{prefix}_{channel}", "float", color, min_val=0.0, max_val=8.0, step=0.01)
        for prefix, color in (("overlay", 1.0), ("shadow", 0.0), ("glow", 1.0))
        for channel in "rgb"
    ] + [
        ParamDef("overlay_opacity", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("shadow_opacity", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("glow_opacity", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("shadow_radius", "float", 5.0, min_val=0.0, max_val=128.0, step=0.1),
        ParamDef("glow_radius", "float", 5.0, min_val=0.0, max_val=128.0, step=0.1),
        ParamDef("glow_range", "float", 1.0, min_val=0.01, max_val=1.0, step=0.01),
        ParamDef("shadow_offset_x", "float", 4.0, min_val=-512.0, max_val=512.0, step=0.1),
        ParamDef("shadow_offset_y", "float", 4.0, min_val=-512.0, max_val=512.0, step=0.1),
    ]

    def __init__(self, node_id: str | None = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {definition.name: definition.default for definition in self.PARAM_DEFS}
        self.parameters["psd_styles"] = []
        self.parameters["psd_srgb"] = False

    def _tile_padding(self, params: dict[str, Any]) -> int:
        shadow = float(params.get("shadow_radius", 5.0)) * 2.0 + max(
            abs(float(params.get("shadow_offset_x", 4.0))),
            abs(float(params.get("shadow_offset_y", 4.0))),
        )
        glow = float(params.get("glow_radius", 5.0)) * 2.0
        return math.ceil(max(shadow if params.get("shadow_opacity") else 0.0,
                             glow if params.get("glow_opacity") else 0.0)) + 1

    def compute_input_roi(self, output_roi: BBox,
                          params: dict[str, Any],
                          input_bboxes: dict[str, BBox] | None = None) -> dict[str, BBox]:
        return {"image": output_roi.expanded(self._tile_padding(params))}

    def _do_evaluate(self, inputs: dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> dict[str, Any]:
        source = inputs.get("image")
        if source is None or not source.is_valid:
            return {}
        if not any(float(self.parameters[f"{prefix}_opacity"]) > 0.0 for prefix in _STYLE_PREFIXES):
            return {"result": source}
        uniforms = self.style_uniforms(self.parameters, source.alpha_premultiplied)
        return {"_effect_type": "layer_style", "_effect_input": source, "_effect_params": uniforms}

    @staticmethod
    def style_uniforms(parameters: dict[str, Any], premultiplied: bool) -> dict[str, Any]:
        """Pack the editable controls for both GPU backends."""
        uniforms = {
            f"u_{prefix}_color": tuple(float(parameters[f"{prefix}_{channel}"])
                                      for channel in "rgb")
            for prefix in _STYLE_PREFIXES
        }
        uniforms.update({f"u_{key}": float(parameters[key]) for key in (
            "overlay_opacity", "shadow_opacity", "glow_opacity", "shadow_radius", "glow_radius", "glow_range",
        )})
        uniforms["u_shadow_offset"] = (float(parameters["shadow_offset_x"]),
                                       -float(parameters["shadow_offset_y"]))
        uniforms["u_premultiplied"] = premultiplied
        uniforms["u_psd_srgb"] = bool(parameters.get("psd_srgb"))
        return uniforms
