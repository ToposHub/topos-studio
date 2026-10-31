"""
Time Offset Node - Shift frames forward or backward in time.

Offsets the input video by a specified number of frames:
  - Positive offset: shift forward (show future frames)
  - Negative offset: shift backward (show past frames)
  - Zero offset: pass through unchanged

Commonly used for:
  - Frame-accurate timing adjustments
  - Syncing audio/video
  - Creating simple time-based effects
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class TimeOffsetNode(Node):
    """Offsets input frames by a specified frame count."""

    node_type = "time_offset"
    node_title = "Time Offset"
    header_color = "#c47a3a"  # Orange-brown

    PARAM_DEFS = [
        ParamDef("frame_offset", "int", 0, min_val=-1000, max_val=1000),
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
            "frame_offset": 0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Time offset doesn't change the bbox."""
        return input_bboxes.get("input")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Input needs the same ROI as output."""
        return {"input": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate time offset effect.

        Short-term implementation: passthrough + frame remap metadata.
        The frame_offset parameter is stored in metadata for future frame source control.
        When connected to image/video sources, the scheduler will use this metadata
        to request the correct frame from the source.

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
        frame_offset = self.parameters.get("frame_offset", 0)

        if input_img is None or not input_img.is_valid:
            return {}

        # Return time offset marker for passthrough processing
        # Uses standard _effect_input field for compatibility with _dispatch_effect
        # _effect_params stores frame_offset for future frame source integration
        return {
            "result": TextureResult(
                texture_id=-1,
                width=input_img.width,
                height=input_img.height,
            ),
            "_effect_type": "time_offset",
            "_effect_input": input_img,
            "_effect_params": {"frame_offset": frame_offset},
        }
