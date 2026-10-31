"""Relight node — re-light a render from its NORMAL/POSITION AOVs.

Consumes the world-space NORMAL (encoded to [0,1]) and POSITION (HDR float)
AOVs that Scene3D already renders (``render3d/aov.py``) — or the equivalent
channels read back from a multi-layer EXR — and evaluates a Lambert diffuse
term against a point or directional light in the linear domain:

    out = image * (max(dot(N, L), 0) * intensity * light_color + ambient)

MVP is diffuse-only; specular/roughness parameter slots are reserved but
hidden (phase 2).  The operation is purely per-pixel: no spatial sampling,
so tile padding is 0.
"""

import logging
from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

logger = logging.getLogger(__name__)

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Light types — order defines the combo dropdown order.
LIGHT_TYPES = ["point", "directional"]

# Normal decode conventions.  "encoded" matches this project's AOV encode
# (n * 0.5 + 0.5, see _MESH_AOV_FRAGMENT); "signed" consumes raw signed
# floats such as some DCC EXR exports.
NORMAL_DECODES = ["encoded", "signed"]


class RelightNode(Node):
    """Re-light an image using its NORMAL/POSITION AOVs (Lambert MVP)."""

    node_type = "relight"
    node_title = "Relight"
    i18n_title_key = "nodes.relight.title"
    header_color = "#8a7a5a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "relight", "re-light", "relighting", "重光照", "补光", "重新打光",
        "AOV 光照", "lambert",
    ]
    DESCRIPTION = "用 NORMAL/POSITION AOV 在 2D 图上重新打光（点光/平行光）"
    TAGS = ["filter", "lighting", "aov", "3d"]

    PARAM_DEFS = [
        ParamDef("light_type", "option", "directional", labels=LIGHT_TYPES,
                 group="Light", animated=True),
        # Point light position in world units.
        ParamDef("light_position_x", "float", 1.0, min_val=-100.0,
                 max_val=100.0, step=0.1, group="Light", animated=True,
                 expression_hint="light_position_x"),
        ParamDef("light_position_y", "float", 1.0, min_val=-100.0,
                 max_val=100.0, step=0.1, group="Light", animated=True),
        ParamDef("light_position_z", "float", 2.0, min_val=-100.0,
                 max_val=100.0, step=0.1, group="Light", animated=True),
        # Directional light direction (shader negates it into L).
        ParamDef("light_direction_x", "float", 0.0, min_val=-1.0,
                 max_val=1.0, step=0.01, group="Light", animated=True),
        ParamDef("light_direction_y", "float", 0.0, min_val=-1.0,
                 max_val=1.0, step=0.01, group="Light", animated=True),
        ParamDef("light_direction_z", "float", -1.0, min_val=-1.0,
                 max_val=1.0, step=0.01, group="Light", animated=True),
        ParamDef("intensity", "float", 1.0, min_val=0.0, max_val=10.0,
                 step=0.01, group="Light", animated=True),
        ParamDef("color", "color", (1.0, 1.0, 1.0, 1.0),
                 group="Light", animated=True),
        ParamDef("ambient", "float", 0.0, min_val=0.0, max_val=4.0,
                 step=0.01, group="Light", animated=True),

        ParamDef("normal_decode", "option", "encoded", labels=NORMAL_DECODES,
                 group="Normal"),
        ParamDef("normal_flip_y", "bool", False, group="Normal"),

        # Phase-2 reserved slots (specular highlights).  Hidden from the UI
        # and forced to no-op values in the MVP shader.
        ParamDef("specular", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Light", hidden=True),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
        ParamDef("roughness", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01, group="Light", hidden=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("normal", "input", "image"),
            Port("position", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "light_type": "directional",
            "light_position_x": 1.0,
            "light_position_y": 1.0,
            "light_position_z": 2.0,
            "light_direction_x": 0.0,
            "light_direction_y": 0.0,
            "light_direction_z": -1.0,
            "intensity": 1.0,
            "color": (1.0, 1.0, 1.0, 1.0),
            "ambient": 0.0,
            "normal_decode": "encoded",
            "normal_flip_y": False,
            "specular": 0.0,
            "roughness": 0.5,
            "cache_to_disk": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Per-pixel effect: the output bbox mirrors the image input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        """Per-pixel sampling — no spatial expansion for any input."""
        return {"image": output_roi, "normal": output_roi,
                "position": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    def _read_color_rgb(self) -> tuple:
        """Read the ``color`` parameter as a validated RGB triple.

        AUD-7208: ``color`` arrives from persisted project data (from_dict
        merges parameters verbatim for undeclared shapes) and from
        programmatic ``set_parameter`` writes.  A None / short / non-numeric
        value used to raise TypeError/IndexError straight out of
        ``_do_evaluate`` and blank the node.  Fall back to neutral white per
        channel instead; every fallback is logged so the bad value is
        traceable, never silent.
        """
        raw = self.parameters.get("color", (1.0, 1.0, 1.0, 1.0))
        if not isinstance(raw, (tuple, list)) or len(raw) < 3:
            logger.warning(
                "RelightNode %s: 'color' has invalid shape %r — using "
                "neutral white", self.id, raw)
            return (1.0, 1.0, 1.0)
        channels: list = []
        for index in range(3):
            try:
                channels.append(float(raw[index]))
            except (TypeError, ValueError):
                logger.warning(
                    "RelightNode %s: 'color' channel %d is %r — using 1.0",
                    self.id, index, raw[index])
                channels.append(1.0)
        return (channels[0], channels[1], channels[2])

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Emit the renderer-neutral Relight effect marker."""
        image = inputs.get("image")
        normal = inputs.get("normal")
        position = inputs.get("position")
        # Without a normal AOV there is nothing to light from; emit no output
        # rather than silently passing the image through unlit.
        if image is None or not image.is_valid:
            return {}
        if normal is None or not normal.is_valid:
            return {}

        light_type = self.parameters.get("light_type", "directional")
        if light_type not in LIGHT_TYPES:
            light_type = "directional"
        intensity = max(0.0, float(self.parameters.get("intensity", 1.0)))
        ambient = max(0.0, float(self.parameters.get("ambient", 0.0)))
        color_rgb = self._read_color_rgb()

        marker: Dict[str, Any] = {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "relight",
            "_effect_input": image,
            "_effect_normal": normal,
            "_effect_params": {
                "light_type": light_type,
                "light_position": (
                    float(self.parameters.get("light_position_x", 1.0)),
                    float(self.parameters.get("light_position_y", 1.0)),
                    float(self.parameters.get("light_position_z", 2.0)),
                ),
                "light_direction": (
                    float(self.parameters.get("light_direction_x", 0.0)),
                    float(self.parameters.get("light_direction_y", 0.0)),
                    float(self.parameters.get("light_direction_z", -1.0)),
                ),
                "intensity": intensity,
                "color": color_rgb,
                "ambient": ambient,
                "normal_decode": self.parameters.get(
                    "normal_decode", "encoded"),
                "normal_flip_y": bool(
                    self.parameters.get("normal_flip_y", False)),
            },
        }
        # Position is optional: without it the point light treats its
        # position parameter as a direction from the world origin.
        if position is not None and position.is_valid:
            marker["_effect_position"] = position
        return marker
