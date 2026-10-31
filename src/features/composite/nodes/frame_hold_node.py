"""
Frame Hold Node - Hold a specific frame from the input.

Freezes the input video at a specified frame number:
  - hold_frame: the frame number to hold (0-based)
  - Output is the same frame repeated for all output frames

Commonly used for:
  - Freeze frame effects
  - Holding on important moments
  - Creating still images from video
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class FrameHoldNode(Node):
    """Holds a specific frame from the input."""

    node_type = "frame_hold"
    node_title = "Frame Hold"
    header_color = "#3a8bc4"  # Blue

    PARAM_DEFS = [
        ParamDef("hold_frame", "int", 0, min_val=0, max_val=1000000),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("input", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "hold_frame": 0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Frame hold doesn't change the bbox."""
        return input_bboxes.get("input")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Input needs the same ROI as output."""
        return {"input": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate frame hold effect.

        Short-term implementation: passthrough + freeze frame metadata.
        The hold_frame parameter is stored in metadata for future frame source control.
        When connected to image/video sources, the scheduler will use this metadata
        to request the specified frame repeatedly (freeze frame effect).

        Args:
            inputs: Input textures from upstream nodes.
            ctx: ModernGL context.
            fb_manager: Framebuffer manager.
            resource_pool: GPU resource pool.
            render_mode: Render mode ("preview" or "full").

        Returns:
            Dict with effect marker for passthrough processing.
        """
        input_img = inputs.get("input")
        hold_frame = self.parameters.get("hold_frame", 0)

        if input_img is None or not input_img.is_valid:
            return {}

        # Return frame hold marker for passthrough processing
        # Uses standard _effect_input field for compatibility with _dispatch_effect
        # _effect_params stores hold_frame for future frame source integration
        return {
            "result": TextureResult(
                texture_id=-1,
                width=input_img.width,
                height=input_img.height,
            ),
            "_effect_type": "frame_hold",
            "_effect_input": input_img,
            "_effect_params": {"hold_frame": hold_frame},
        }
