"""
NoOp Node - No-operation pass-through utility node.

A NoOp (No Operation) node is a simple pass-through that can be used for:
  - Labeling/organizing sections of the node graph
  - Debugging (breakpoints in the node chain)
  - Placeholder for future operations
  - Adding comments/notes to the graph

The node passes the input directly to the output without modification.
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class NoOpNode(Node):
    """Pass-through utility node for organization and labeling."""

    HELP_TEXT = """透传工具节点。直接传递输入到输出，支持 label 注释。
输入：image（图像）
输出：相同的图像
参数：
  - label: 节点标签文本
注意：适合添加注释、组织画布、调试管线。"""
    DISPLAY_NAME = "Noop"

    node_type = "noop"
    # CP-044: 路由/组织节点不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "NoOp"
    header_color = "#6e6e6e"  # Dark gray

    PARAM_DEFS = [
        ParamDef("label", "string", ""),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "label": "",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """ROI passes through directly."""
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input and image_input.is_valid:
            return {"image": image_input}
        return {}
