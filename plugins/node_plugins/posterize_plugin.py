"""
Posterize Plugin - Example node plugin for Topos Color.

This plugin demonstrates the NodePlugin architecture:
- A NodePlugin subclass that declares the plugin metadata
- A Node subclass that implements the node logic
- Integration with the node graph evaluation system

Posterize reduces the number of colors in an image by quantizing
each channel to a specified number of levels.
"""

import math
from typing import Dict, List, Optional, Type

# Import from parent package
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# CP-002: src.composite → src.features.composite（模块迁移后的正确路径，
# 旧路径自迁移起一直 ImportError，导致本目录插件从未加载成功）
from src.features.composite.core.node_plugin import NodePlugin, PluginInfo
from src.features.composite.nodes.base_node import Node, Port, TextureResult, ParamDef


class PosterizeNode(Node):
    """Posterize effect - reduces color levels per channel.

    This node quantizes each color channel to a specified number of levels,
    creating a poster-like effect with limited colors.

    Parameters:
        levels: Number of color levels per channel (2-64)
        channel_mode: Which channels to affect ("rgb", "rgba", "luminance")
        dither: Enable dithering to reduce banding
    """

    node_type = "posterize"
    node_title = "Posterize"
    header_color = "#8B5CF6"  # Purple

    # Metadata for search and documentation
    SEARCH_ALIASES = ["posterize", "色调分离", "poster", "quantize", "量化"]
    DESCRIPTION = "色调分离效果，减少颜色层级"
    TAGS = ["filter", "color", "image"]

    # Parameter definitions
    PARAM_DEFS = [
        ParamDef("levels", "int", 8, min_val=2, max_val=64),
        ParamDef("channel_mode", "option", "rgb", labels=["rgb", "rgba", "luminance"]),
        ParamDef("dither", "bool", False),
    ]

    def __init__(self, node_id: Optional[str] = None):
        super().__init__(node_id)

        # Define ports
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]

        # Default parameters
        self.parameters = {
            "levels": 8,
            "channel_mode": "rgb",
            "dither": False,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
        """Evaluate the posterize effect.

        Returns an effect marker that will be processed by the renderer.
        """
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Get parameters with validation
        levels = self._get_param_int("levels", 8)
        levels = max(2, min(64, levels))  # Clamp to valid range

        channel_mode = self.parameters.get("channel_mode", "rgb")
        dither = self._get_param_bool("dither", False)

        # Return effect marker for GPU processing
        # The renderer will pick up _effect_type and process accordingly
        return {
            "result": TextureResult(
                texture_id=-1,  # Placeholder, renderer will create actual texture
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "posterize",
            "_effect_input": image,
            "_effect_params": {
                "levels": levels,
                "channel_mode": channel_mode,
                "dither": dither,
            },
        }

    def compute_output_bbox(self, input_bboxes, params) -> Optional["BBox"]:
        """Posterize doesn't change the data window."""
        if not input_bboxes:
            return None
        return input_bboxes.get("image")


class PosterizePlugin(NodePlugin):
    """Plugin providing the Posterize node."""

    @classmethod
    def plugin_info(cls) -> PluginInfo:
        return PluginInfo(
            name="posterize",
            version="1.0.0",
            author="Topos Color Team",
            description="色调分离效果节点 - Posterize effect node"
        )

    @classmethod
    def node_types(cls) -> List[Type[Node]]:
        return [PosterizeNode]

    @classmethod
    def category(cls) -> str:
        return "Filter"

    @classmethod
    def dependencies(cls) -> List[str]:
        # No external dependencies
        return []


# Plugin metadata for display
__plugin_info__ = {
    "name": "posterize",
    "version": "1.0.0",
    "description": "色调分离效果插件",
}