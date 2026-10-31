"""
Retime Node - Speed up or slow down footage.

Changes the playback speed of the input video:
  - speed > 1.0: speed up (e.g., 2.0 = 2x faster)
  - speed < 1.0: slow down (e.g., 0.5 = half speed)
  - speed = 1.0: normal speed (pass through)

Implementation status (AUD-7209 — documented honestly, no over-promising):
the node performs SOURCE-SIDE time remapping (core/time_remap.py resolves
which source frame each output frame maps to) and passes the frame
through.  The speed remap itself already applies at the source, so fast /
slow playback works; without interpolation, large speed changes show
frame-skipping.  ``use_optical_flow`` is RESERVED METADATA for a future
resampling backend — setting it today does NOT enable optical-flow
interpolation or frame blending (the dispatcher returns the input
unchanged; see base_evaluator ``_dispatch_effect`` case "retime").

Commonly used for:
  - Slow motion effects (source-frame remapping; may skip frames)
  - Time-lapse
  - Speed ramping
  - Reverse / loop playback (reverse / loop_mode parameters)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class RetimeNode(Node):
    """Source-side time remapping + passthrough (optical flow: reserved).

    AUD-7209: the speed/remap parameters take effect at the video source
    (frame picking); this node's evaluation is a passthrough that only
    attaches timing metadata.  ``use_optical_flow`` does not yet enable
    any interpolation — it is stored for a future resampling backend.
    """

    node_type = "retime"
    node_title = "Retime"
    header_color = "#5bc43a"  # Green

    PARAM_DEFS = [
        ParamDef("speed", "float", 1.0, min_val=0.1, max_val=10.0, step=0.1),
        ParamDef("use_optical_flow", "bool", True),
        # B5: reverse / loop modes (post-affine, see core/time_remap.py)
        ParamDef("reverse", "bool", False, group="Mode"),
        ParamDef("loop_mode", "option", "off", labels=["off", "loop"],
                 group="Mode"),
        ParamDef("loop_in", "int", 0, min_val=0, max_val=1000000, step=1,
                 group="Mode"),
        ParamDef("loop_out", "int", 100, min_val=0, max_val=1000000, step=1,
                 group="Mode"),
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
            "speed": 1.0,
            "use_optical_flow": True,
            "reverse": False,
            "loop_mode": "off",
            "loop_in": 0,
            "loop_out": 100,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Retime doesn't change the bbox."""
        return input_bboxes.get("input")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Input needs the same ROI as output."""
        return {"input": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate retime effect.

        AUD-7209 (honest docstring): this is a passthrough + timing
        metadata marker.  The speed remap happens at the source
        (core/time_remap.py picks which source frame each output frame
        requests); ``use_optical_flow`` is reserved metadata for a future
        resampling backend and does NOT enable motion interpolation or
        frame blending today — the dispatcher returns the input unchanged
        (see base_evaluator ``_dispatch_effect`` case "retime").

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
        speed = self.parameters.get("speed", 1.0)
        use_optical_flow = self.parameters.get("use_optical_flow", True)
        reverse = self.parameters.get("reverse", False)
        loop_mode = self.parameters.get("loop_mode", "off")
        loop_in = self.parameters.get("loop_in", 0)
        loop_out = self.parameters.get("loop_out", 100)

        if input_img is None or not input_img.is_valid:
            return {}

        # Return retime marker for passthrough processing
        # Uses standard _effect_input field for compatibility with _dispatch_effect
        # _effect_params stores speed and optical_flow for future frame source integration
        return {
            "result": TextureResult(
                texture_id=-1,
                width=input_img.width,
                height=input_img.height,
            ),
            "_effect_type": "retime",
            "_effect_input": input_img,
            "_effect_params": {
                "speed": speed,
                "use_optical_flow": use_optical_flow,
                "reverse": reverse,
                "loop_mode": loop_mode,
                "loop_in": loop_in,
                "loop_out": loop_out,
            },
        }
