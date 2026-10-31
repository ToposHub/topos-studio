"""
RemoveChannels Node - Remove specified RGBA channels and replace with fill value.

Each channel (R, G, B, A) can be independently marked for removal.
Removed channels are replaced with the specified fill value.

Common use cases:
  - Remove alpha channel (set alpha to 1.0 for full opacity)
  - Create RGB-only image (remove alpha)
  - Isolate specific channels (remove others)
  - Generate solid color overlays (remove R, G, or B)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class RemoveChannelsNode(Node):
    """Remove specified RGBA channels and replace with fill value."""

    node_type = "remove_channels"
    node_title = "Remove Channels"
    header_color = "#8b5a5a"  # Dusty rose

    # Metadata
    SEARCH_ALIASES = ["removechannels", "remove", "channel", "移除通道", "删除通道", "delete"]
    DESCRIPTION = "移除指定通道并用填充值替换"
    TAGS = ["channel", "utility"]

    # Bypass mapping: when disabled, pass input through unchanged
    BYPASS_MAPPING = {"result": "image"}

    PARAM_DEFS = [
        ParamDef("remove_r", "bool", False),
        ParamDef("remove_g", "bool", False),
        ParamDef("remove_b", "bool", False),
        ParamDef("remove_a", "bool", False),
        ParamDef("fill_value", "float", 0.0, min_val=0.0, max_val=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image")
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "remove_r": False,
            "remove_g": False,
            "remove_b": False,
            "remove_a": False,
            "fill_value": 0.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Input needs the same ROI as output."""
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        # Get parameters using helper methods for type safety
        remove_r = self._get_param_bool("remove_r", False)
        remove_g = self._get_param_bool("remove_g", False)
        remove_b = self._get_param_bool("remove_b", False)
        remove_a = self._get_param_bool("remove_a", False)
        fill_value = self._get_param_clamped("fill_value", min_val=0.0, max_val=1.0)

        # If no channels are being removed, pass through
        if not (remove_r or remove_g or remove_b or remove_a):
            return {"result": image_input}

        # Return effect marker for GPU dispatch
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "remove_channels",
            "_effect_input": image_input,
            "_effect_params": {
                "remove_r": remove_r,
                "remove_g": remove_g,
                "remove_b": remove_b,
                "remove_a": remove_a,
                "fill_value": fill_value,
            },
        }