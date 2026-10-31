"""
Edge Detect Node - Detects edges in images using various methods.

Provides Sobel, Laplacian, and Canny edge detection with configurable
thresholds and output options.

Supported methods:
  - sobel: Gradient-based edge detection using Sobel kernels
  - laplacian: Second-derivative edge detection using Laplacian kernel
  - prewitt: Similar to Sobel but with different kernel weights
  - canny: Multi-stage edge detection with noise reduction and thinning

Uses GPU shader for real-time edge detection.
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Edge detection methods
EDGE_METHODS = ["sobel", "laplacian", "prewitt", "canny"]

# Direction modes
DIRECTION_MODES = ["both", "horizontal", "vertical"]

# Output modes
OUTPUT_MODES = ["edges", "gradient", "direction"]


class EdgeDetectNode(Node):
    """Detects edges in images using GPU shader."""

    node_type = "edge_detect"
    node_title = "Edge Detect"
    header_color = "#5a6a8a"  # Blue-ish

    # Metadata
    SEARCH_ALIASES = ["edge", "detect", "边缘", "检测", "sobel", "laplacian", "canny", "outline", "轮廓"]
    DESCRIPTION = "检测图像边缘（Sobel/Laplacian/Canny方法）"
    TAGS = ["filter", "image", "edge"]

    PARAM_DEFS = [
        ParamDef("method", "option", "sobel", labels=EDGE_METHODS),
        ParamDef("threshold", "float", 0.1, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("threshold_high", "float", 0.3, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("direction", "option", "both", labels=DIRECTION_MODES),
        ParamDef("output_mode", "option", "edges", labels=OUTPUT_MODES),
        ParamDef("invert", "bool", False),
        ParamDef("output_strength", "float", 1.0, min_val=0.0, max_val=10.0, step=0.01),
        ParamDef("kernel_size", "int", 3, min_val=3, max_val=7, step=2),
        ParamDef("preblur", "float", 0.0, min_val=0.0, max_val=10.0, step=0.1),
        ParamDef("channel", "option", "luminance", labels=["luminance", "r", "g", "b", "a"]),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "method": "sobel",
            "threshold": 0.1,
            "threshold_high": 0.3,
            "direction": "both",
            "output_mode": "edges",
            "invert": False,
            "output_strength": 1.0,
            "kernel_size": 3,
            "preblur": 0.0,
            "channel": "luminance",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Edge detection preserves input bbox."""
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for edge detection.

        Edge detection uses convolution kernels (3x3 or larger).
        Padding must account for kernel size plus optional preblur.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels (kernel_size/2 + preblur_radius).
        """
        kernel_size = params.get("kernel_size", 3)
        preblur = params.get("preblur", 0.0)
        kernel_pad = int(kernel_size // 2)
        blur_pad = int(preblur * 2) if preblur > 0.0 else 0
        return max(1, kernel_pad + blur_pad)

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Detect edges using GPU shader.

        Returns a marker dict for the renderer to execute the shader.
        """
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Get parameters
        method = self.parameters.get("method", "sobel")
        threshold = float(self.parameters.get("threshold", 0.1))
        threshold_high = float(self.parameters.get("threshold_high", 0.3))
        direction = self.parameters.get("direction", "both")
        output_mode = self.parameters.get("output_mode", "edges")
        invert = self.parameters.get("invert", False)
        output_strength = float(self.parameters.get("output_strength", 1.0))
        kernel_size = int(self.parameters.get("kernel_size", 3))
        preblur = float(self.parameters.get("preblur", 0.0))
        channel = self.parameters.get("channel", "luminance")

        # Preview mode: reduce kernel size for faster response
        if render_mode == "preview" and kernel_size > 3:
            kernel_size = 3
            preblur = max(0.0, preblur * 0.5)

        # Return effect marker for GPU renderer
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "edge_detect",
            "_effect_input": image,
            "_effect_params": {
                "method": method,
                "threshold": threshold,
                "threshold_high": threshold_high,
                "direction": direction,
                "output_mode": output_mode,
                "invert": invert,
                "output_strength": output_strength,
                "kernel_size": kernel_size,
                "preblur": preblur,
                "channel": channel,
            },
        }