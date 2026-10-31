"""
Dot Node - Utility node for connection management.

A Dot is a purely organizational node that passes the image through unchanged.
It can be used to:
  - Break up visual connections in the node graph
  - Create cleaner routing
  - Organize complex node graphs
  - Add visual waypoints for better graph readability

The Dot has one input and one output port, passing the image through unchanged.
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class DotNode(Node):
    """Utility dot for connection management - passes image through unchanged."""

    node_type = "dot"
    # CP-044: 路由/组织节点不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Dot"
    header_color = "#757575"  # Medium gray (compact visual style)

    # Metadata
    SEARCH_ALIASES = ["dot", "reroute", "pass", "passthrough", "点", "中转", "路由", "relay"]
    DESCRIPTION = "线缆整理节点，不改变数据"
    TAGS = ["utility", "organize"]

    PARAM_DEFS = []

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("input", "input", "image"),
        ]
        self.outputs = [
            Port("output", "output", "image")
        ]

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the input."""
        return input_bboxes.get("input")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """ROI passes through directly."""
        return {"input": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Pass through input to output unchanged."""
        input_texture = inputs.get("input")
        if input_texture and input_texture.is_valid:
            return {"output": input_texture}
        return {}
