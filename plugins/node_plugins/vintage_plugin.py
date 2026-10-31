"""
Vintage Film Plugin - Multi-node example plugin for Topos Color.

This plugin demonstrates how a single plugin can provide multiple node types:
- VintageToneNode: Color grading with vintage/film look
- FilmGrainNode: Realistic film grain simulation

Shows advanced plugin features:
- Multiple node types from one plugin
- Custom category organization
- Metadata for UI and documentation
"""

from typing import Dict, List, Optional, Type

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# CP-002: src.composite → src.features.composite（模块迁移后的正确路径）
from src.features.composite.core.node_plugin import NodePlugin, PluginInfo
from src.features.composite.nodes.base_node import Node, Port, TextureResult, ParamDef


class VintageToneNode(Node):
    """Vintage tone color grading.

    Applies a classic film/vintage color tone to the image.
    Supports different film emulsion types (Kodak, Fuji, Agfa).

    Parameters:
        film_type: Film emulation type (kodak_portra, fuji_pro, agfa_optima)
        intensity: Effect strength (0.0-1.0)
        warm_shift: Additional warm color shift
        contrast_boost: Contrast enhancement
    """

    node_type = "vintage_tone"
    node_title = "Vintage Tone"
    header_color = "#D2691E"  # Chocolate brown

    SEARCH_ALIASES = ["vintage", "film", "复古", "胶片", "kodak", "fuji", "film look"]
    DESCRIPTION = "复古胶片色调调色"
    TAGS = ["color", "filter", "film"]

    FILM_TYPES = ["kodak_portra", "fuji_pro", "agfa_optima", "ilford_hp5"]

    PARAM_DEFS = [
        ParamDef("film_type", "option", "kodak_portra", labels=FILM_TYPES),
        ParamDef("intensity", "float", 0.8, min_val=0.0, max_val=1.0, step=0.05),
        ParamDef("warm_shift", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.1),
        ParamDef("contrast_boost", "float", 0.1, min_val=-0.5, max_val=0.5, step=0.05),
        ParamDef("saturation_adjust", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.1),
    ]

    def __init__(self, node_id: Optional[str] = None):
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "film_type": "kodak_portra",
            "intensity": 0.8,
            "warm_shift": 0.0,
            "contrast_boost": 0.1,
            "saturation_adjust": 0.0,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        intensity = self._get_param_clamped("intensity", 0.0, 1.0)
        warm_shift = self._get_param_clamped("warm_shift", -1.0, 1.0)
        contrast = self._get_param_clamped("contrast_boost", -0.5, 0.5)
        saturation = self._get_param_clamped("saturation_adjust", -1.0, 1.0)
        film_type = self.parameters.get("film_type", "kodak_portra")

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "vintage_tone",
            "_effect_input": image,
            "_effect_params": {
                "film_type": film_type,
                "intensity": intensity,
                "warm_shift": warm_shift,
                "contrast_boost": contrast,
                "saturation_adjust": saturation,
            },
        }


class FilmGrainNode(Node):
    """Realistic film grain simulation.

    Adds natural-looking film grain to the image.
    Simulates different grain patterns and sizes.

    Parameters:
        grain_size: Grain particle size (0.5-4.0)
        grain_intensity: Grain visibility strength
        grain_type: Grain pattern type (fine, medium, coarse)
        color_grain: Enable colored grain (RGB variation)
        luminance_only: Apply grain only to luminance
    """

    node_type = "film_grain"
    node_title = "Film Grain"
    header_color = "#696969"  # Dim gray

    SEARCH_ALIASES = ["grain", "film grain", "颗粒", "噪点", "noise", "film"]
    DESCRIPTION = "胶片颗粒效果"
    TAGS = ["filter", "film", "texture"]

    GRAIN_TYPES = ["fine", "medium", "coarse", "analog"]

    PARAM_DEFS = [
        ParamDef("grain_size", "float", 1.5, min_val=0.5, max_val=4.0, step=0.25),
        ParamDef("grain_intensity", "float", 0.3, min_val=0.0, max_val=1.0, step=0.05),
        ParamDef("grain_type", "option", "medium", labels=GRAIN_TYPES),
        ParamDef("color_grain", "bool", True),
        ParamDef("luminance_only", "bool", False),
    ]

    def __init__(self, node_id: Optional[str] = None):
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "grain_size": 1.5,
            "grain_intensity": 0.3,
            "grain_type": "medium",
            "color_grain": True,
            "luminance_only": False,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        grain_size = self._get_param_positive("grain_size", 1.5)
        intensity = self._get_param_clamped("grain_intensity", 0.0, 1.0)
        grain_type = self.parameters.get("grain_type", "medium")
        color_grain = self._get_param_bool("color_grain", True)
        luminance_only = self._get_param_bool("luminance_only", False)

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "film_grain",
            "_effect_input": image,
            "_effect_params": {
                "grain_size": grain_size,
                "grain_intensity": intensity,
                "grain_type": grain_type,
                "color_grain": color_grain,
                "luminance_only": luminance_only,
            },
        }


class VintageFilmPlugin(NodePlugin):
    """Plugin providing vintage film effect nodes."""

    @classmethod
    def plugin_info(cls) -> PluginInfo:
        return PluginInfo(
            name="vintage_film",
            version="1.0.0",
            author="Topos Color Team",
            description="复古胶片效果套件 - Vintage film effect suite with tone and grain"
        )

    @classmethod
    def node_types(cls) -> List[Type[Node]]:
        return [VintageToneNode, FilmGrainNode]

    @classmethod
    def category(cls) -> str:
        return "Film"

    @classmethod
    def on_load(cls) -> bool:
        """Initialize plugin - could load film preset data here."""
        return True

    @classmethod
    def on_unload(cls) -> None:
        """Cleanup - release any loaded resources."""
        pass


__plugin_info__ = {
    "name": "vintage_film",
    "version": "1.0.0",
    "nodes": ["vintage_tone", "film_grain"],
    "description": "复古胶片效果插件套件",
}