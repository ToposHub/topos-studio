"""Lens Flare node (B4) — procedural ghosts + halo + anamorphic streak.

Screen-space additive/screen overlay driven by a light position:

* **ghosts** — mirrored points along the line centre↔light at
  ``pos_i = center + (light - center) · (spacing · i)`` with per-ghost
  falloff ``ghost_decay^i``;
* **halo** — a ring of configurable radius around the light;
* **streak** — horizontal anamorphic bar through the light.

Occlusion awareness (attenuating by input alpha at the light) is an
optional enhancement switch.  Purely screen-space: padding 0, full-frame.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

MAX_GHOSTS = 8


class LensFlareNode(Node):
    """Procedural lens flare (ghosts / halo / streak MVP)."""

    node_type = "lens_flare"
    node_title = "Lens Flare"
    i18n_title_key = "nodes.lens_flare.title"
    header_color = "#8a8a5a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "lens flare", "flare", "ghosts", "halo", "streak", "anamorphic",
        "光晕", "镜头光斑", "眩光", "鬼影", "拉丝光",
    ]
    DESCRIPTION = "程序化镜头光晕（ghost 序列 + halo 环 + 变形拉丝），screen 叠加"
    TAGS = ["filter", "lens", "lighting"]

    PARAM_DEFS = [
        ParamDef("light_x", "float", 0.3, min_val=-0.5, max_val=1.5,
                 step=0.005, group="Light", animated=True),
        ParamDef("light_y", "float", 0.4, min_val=-0.5, max_val=1.5,
                 step=0.005, group="Light", animated=True),
        ParamDef("color", "color", (1.0, 0.9, 0.75, 1.0), group="Light",
                 animated=True),
        ParamDef("intensity", "float", 0.6, min_val=0.0, max_val=3.0,
                 step=0.01, group="Light", animated=True),
        ParamDef("ghost_count", "int", 4, min_val=0, max_val=MAX_GHOSTS,
                 step=1, group="Ghosts", animated=True),
        ParamDef("ghost_spacing", "float", 0.5, min_val=-2.0, max_val=2.0,
                 step=0.01, group="Ghosts", animated=True),
        ParamDef("ghost_decay", "float", 0.7, min_val=0.1, max_val=1.0,
                 step=0.01, group="Ghosts", animated=True),
        ParamDef("ghost_size", "float", 0.06, min_val=0.005, max_val=0.5,
                 step=0.005, group="Ghosts", animated=True),
        ParamDef("halo_radius", "float", 0.25, min_val=0.01, max_val=1.0,
                 step=0.005, group="Halo", animated=True),
        ParamDef("halo_strength", "float", 0.2, min_val=0.0, max_val=2.0,
                 step=0.01, group="Halo", animated=True),
        ParamDef("streak_strength", "float", 0.3, min_val=0.0, max_val=2.0,
                 step=0.01, group="Streak", animated=True),
        ParamDef("streak_width", "float", 0.01, min_val=0.001, max_val=0.2,
                 step=0.001, group="Streak", animated=True),
        ParamDef("use_occlusion", "bool", False, group="Light"),
        ParamDef("blend_mode", "option", "screen",
                 labels=["screen", "add"], group="Light"),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "light_x": 0.3,
            "light_y": 0.4,
            "color": (1.0, 0.9, 0.75, 1.0),
            "intensity": 0.6,
            "ghost_count": 4,
            "ghost_spacing": 0.5,
            "ghost_decay": 0.7,
            "ghost_size": 0.06,
            "halo_radius": 0.25,
            "halo_strength": 0.2,
            "streak_strength": 0.3,
            "streak_width": 0.01,
            "use_occlusion": False,
            "blend_mode": "screen",
            "cache_to_disk": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        """Screen-space overlay: full-frame like God Rays."""
        if input_bboxes:
            image_bbox = input_bboxes.get("image")
            if image_bbox is not None and not image_bbox.is_empty:
                return {"image": image_bbox.union(output_roi)}
        return {"image": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}

        ghost_count = min(MAX_GHOSTS, max(0, int(
            self.parameters.get("ghost_count", 4))))
        if render_mode == "preview":
            # Draft downgrade: fewer ghosts (plan §2-14).
            ghost_count = max(0, ghost_count // 2)

        color = self.parameters.get("color", (1.0, 0.9, 0.75, 1.0))

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "lens_flare",
            "_effect_input": image,
            "_effect_params": {
                "light_pos": (
                    min(1.5, max(-0.5,
                        float(self.parameters.get("light_x", 0.3)))),
                    min(1.5, max(-0.5,
                        float(self.parameters.get("light_y", 0.4)))),
                ),
                "light_color": (float(color[0]), float(color[1]),
                                float(color[2])),
                "intensity": min(3.0, max(0.0, float(
                    self.parameters.get("intensity", 0.6)))),
                "ghost_count": ghost_count,
                "ghost_spacing": min(2.0, max(-2.0, float(
                    self.parameters.get("ghost_spacing", 0.5)))),
                "ghost_decay": min(1.0, max(0.1, float(
                    self.parameters.get("ghost_decay", 0.7)))),
                "ghost_size": min(0.5, max(0.005, float(
                    self.parameters.get("ghost_size", 0.06)))),
                "halo_radius": min(1.0, max(0.01, float(
                    self.parameters.get("halo_radius", 0.25)))),
                "halo_strength": min(2.0, max(0.0, float(
                    self.parameters.get("halo_strength", 0.2)))),
                "streak_strength": min(2.0, max(0.0, float(
                    self.parameters.get("streak_strength", 0.3)))),
                "streak_width": min(0.2, max(0.001, float(
                    self.parameters.get("streak_width", 0.01)))),
                "use_occlusion": bool(
                    self.parameters.get("use_occlusion", False)),
                "blend_mode": 1 if self.parameters.get(
                    "blend_mode", "screen") == "add" else 0,
            },
        }
