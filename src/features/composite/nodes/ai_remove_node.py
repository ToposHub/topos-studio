"""AI Remove node — erase the masked region's content via LaMa inpainting.

B6 wiring: the node is a real graph node now. Evaluation runs through the
renderer's async AI branch (``_evaluate_ai_node``): the masked input is sent
to the shared ``AIComputeManager`` (LaMa ONNX), the result lands in the
EXR disk cache keyed by content, and the graph re-evaluates when the
compute finishes.  Pixels outside the mask are preserved unchanged
(mask composite happens on the read-back side).

The ``mask`` input expects a matte (white = region to remove).  Pair it
with a roto shape or, later, the Paint node's mask output (C2 hook).
"""

from typing import Dict, Optional, Any

from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class AIRemoveNode(Node):
    """Remove masked content from the input image (LaMa inpaint)."""

    node_type = "ai_remove"
    node_title = "AI Remove"
    header_color = "#9B59B6"

    DESCRIPTION = "AI 擦除：移除遮罩区域内的物体（LaMa 修补模型，遮罩外像素保持不变）"
    TAGS = ["ai", "inpaint", "remove"]
    SEARCH_ALIASES = ["ai remove", "remove", "erase", "inpaint",
                      "AI擦除", "擦除", "移除", "修补"]

    PARAM_DEFS = [
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=1.0, step=0.05),
        ParamDef("model", "str", "default"),
        ParamDef("use_tiling", "bool", True, group="Tiling"),
        ParamDef("tile_size", "int", 1024, min_val=256, max_val=4096, step=64,
                 group="Tiling"),
        ParamDef("overlap", "int", 64, min_val=0, max_val=512, step=8,
                 group="Tiling"),
        ParamDef("blend", "float", 1.0, min_val=0.0, max_val=1.0, step=0.05,
                 group="Tiling"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image"),
        ]
        self.parameters = {
            "strength": 1.0,
            "model": "default",
            "use_tiling": True,
            "tile_size": 1024,
            "overlap": 64,
            "blend": 1.0,
        }
        self._init_port_revisions()

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output keeps the input's data window."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the full output ROI."""
        return {"image": output_roi, "mask": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Pass-through fallback.

        The real evaluation is the renderer's async AI branch (cache-first
        LaMa compute).  This fallback keeps the graph alive when that branch
        is unavailable (no AI managers, direct node.evaluate in tests):
        the input passes through unchanged instead of going black.
        """
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        return {"image": image}
