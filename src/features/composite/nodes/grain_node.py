"""Grain Node — film grain generator overlay (CP-019).

First-class counterpart to Nuke Grains / AE Add Grain. Applies the
``film_grain`` shader (value-noise grain, resolution-independent,
scene-linear multiplicative modulation) with an ANIMATABLE seed: when
``animate`` is on the effective seed advances with the playhead, giving
per-frame live grain; ``seed`` itself stays keyframable for controlled
re-seeds.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

GRAIN_TYPES = ["fine", "medium", "coarse"]


class GrainNode(Node):
    """Film grain overlay with animatable per-frame seed."""

    node_type = "grain"
    node_title = "Grain"
    i18n_title_key = "nodes.grain.title"
    header_color = "#8a7d5c"

    SEARCH_ALIASES = [
        "grain", "film grain", "颗粒", "胶片颗粒", "噪点", "noise overlay",
    ]
    DESCRIPTION = "胶片颗粒叠加（分辨率无关、线性空间乘性调制、可动画种子）"
    TAGS = ["filter", "film", "texture"]

    PARAM_DEFS = [
        ParamDef("grain_size", "float", 1.5, min_val=0.5, max_val=4.0,
                 step=0.25, group="Grain"),
        ParamDef("intensity", "float", 0.3, min_val=0.0, max_val=1.0,
                 step=0.05, group="Grain"),
        ParamDef("grain_type", "option", "medium", labels=GRAIN_TYPES,
                 group="Grain"),
        ParamDef("color_grain", "bool", True, group="Grain"),
        ParamDef("luminance_only", "bool", False, group="Grain"),
        ParamDef("seed", "float", 0.0, min_val=0.0, max_val=10000.0,
                 step=1.0, group="Grain"),
        ParamDef("animate", "bool", True, group="Grain"),
        ParamDef("animation_speed", "float", 1.0, min_val=0.0, max_val=10.0,
                 step=0.1, group="Grain"),
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
            "grain_size": 1.5,
            "intensity": 0.3,
            "grain_type": "medium",
            "color_grain": True,
            "luminance_only": False,
            "seed": 0.0,
            "animate": True,
            "animation_speed": 1.0,
        }
        self._init_port_revisions()

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, object]) -> bool:
        """Animated grain is frame-dependent (CP-019)."""
        return bool(params.get("animate", True))

    def effective_seed(self, frame_number: int) -> float:
        """Seed advanced by the playhead when animate is on."""
        seed = self._get_param_clamped("seed", 0.0, 10000.0)
        if self._get_param_bool("animate", True):
            speed = self._get_param_clamped("animation_speed", 0.0, 10.0)
            seed = seed + float(frame_number) * speed * 7.31
        return seed

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        has_image = image is not None and image.is_valid
        if not has_image:
            return {}
        width = max(1, int(image.width))
        height = max(1, int(image.height))
        frame_number = int(getattr(ctx, "frame_number", 0) or 0)
        return {
            "result": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space),
            "_effect_type": "film_grain",
            "_effect_input": image,
            "_effect_params": {
                "grain_size": self._get_param_clamped("grain_size", 0.5, 4.0),
                "grain_intensity": self._get_param_clamped("intensity", 0.0, 1.0),
                "grain_type": self.parameters.get("grain_type", "medium"),
                "color_grain": self._get_param_bool("color_grain", True),
                "luminance_only": self._get_param_bool("luminance_only", False),
                "seed": self.effective_seed(frame_number),
            },
        }
