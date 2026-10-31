"""Precomposition boundary node for nested Composite layer stacks."""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult


class PrecompNode(Node):
    """Pass-through boundary whose input is compiled by a child LayerStack.

    The node deliberately does not render anything itself.  Its child stack
    uses this node as its output target, so normal graph evaluation can feed
    the resulting image into the parent stack without a second render graph.
    """

    node_type = "precomp"
    # CP-044: 嵌套合成边界节点不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Precomp"
    header_color = "#8a6bb8"

    PARAM_DEFS = [
        ParamDef("child_stack_id", "str", ""),
        ParamDef("precomp_name", "str", "Precomp"),
        # Nested compositions share the parent playhead by default. These
        # optional controls are consumed by core.time_remap so a precomp's
        # child sources, cache keys, preview and export all agree.
        ParamDef("frame_offset", "int", 0, min_val=-100000, max_val=100000),
        ParamDef("speed", "float", 1.0, min_val=0.01, max_val=100.0),
        # -1 means disabled; non-negative values hold the child at a frame.
        ParamDef("hold_frame", "int", -1, min_val=-1, max_val=100000),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "child_stack_id": "",
            "precomp_name": "Precomp",
            "frame_offset": 0,
            "speed": 1.0,
            "hold_frame": -1,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any],
                            params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: Any, params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, Any]] = None
                          ) -> Dict[str, Any]:
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is not None and image.is_valid:
            return {"result": image}
        return {}
