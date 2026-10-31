"""
Normalize Node - Value range normalization.

Normalizes input image values to a specified output range.
Used in keyer pipelines to unify value ranges from different sources.

Modes:
  - range: Linear remapping from input_min/input_max to output_min/output_max
  - percentile: Remapping based on histogram percentile analysis
                Automatically finds input_min/max from image histogram.
                Uses GPU compute shader for efficient histogram computation.
  - clip: Simple clamp + remap combination

Mathematical formula (range mode):
  output = (input - input_min) / (input_max - input_min) * (output_max - output_min) + output_min

Percentile mode process:
  1. Downsample image to manageable size (max 512x512)
  2. Compute histogram using GPU compute shader with atomic counters
  3. Compute CDF (cumulative distribution function)
  4. Find values at low/high percentiles from CDF
  5. Apply linear remapping using found values as input range

If clamp_output is True, the result is clamped to [output_min, output_max].
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Normalize modes
NORMALIZE_MODES = ["range", "percentile", "clip"]


class NormalizeNode(Node):
    """Value range normalization node."""

    node_type = "normalize"
    node_title = "Normalize"
    header_color = "#6b8e7a"  # Sage green - matches utility/color nodes

    # Metadata
    SEARCH_ALIASES = ["normalize", "norm", "归一化", "值域", "range", "remap"]
    DESCRIPTION = "值域归一化节点，将输入图像的值域归一化到指定范围"
    TAGS = ["color", "utility"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "range", labels=NORMALIZE_MODES),
        # Input range (for range mode)
        ParamDef("input_min", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("input_max", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        # Output range
        ParamDef("output_min", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("output_max", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        # Percentile mode parameters - percentiles in 0-100 range
        # Default: exclude extreme 1% at both ends to handle outliers
        ParamDef("percentile_low", "float", 1.0, min_val=0.0, max_val=50.0, step=0.1),
        ParamDef("percentile_high", "float", 99.0, min_val=50.0, max_val=100.0, step=0.1),
        # Clamp control
        ParamDef("clamp_output", "bool", True),
        # Channel selection for histogram analysis
        ParamDef("channels", "string", "rgb"),
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
            "mode": "range",
            "input_min": 0.0,
            "input_max": 1.0,
            "output_min": 0.0,
            "output_max": 1.0,
            "percentile_low": 1.0,  # Exclude 1% outliers at low end
            "percentile_high": 99.0,  # Exclude 1% outliers at high end
            "clamp_output": True,
            "channels": "rgb",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """ROI passes through directly."""
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}

        # Check if normalization is needed (default values = identity transform)
        mode = self.parameters.get("mode", "range")
        input_min = self.parameters.get("input_min", 0.0)
        input_max = self.parameters.get("input_max", 1.0)
        output_min = self.parameters.get("output_min", 0.0)
        output_max = self.parameters.get("output_max", 1.0)
        clamp_output = self.parameters.get("clamp_output", True)
        channels = self.parameters.get("channels", "rgb")

        # For percentile mode, use histogram analysis to find input range
        percentile_low = self.parameters.get("percentile_low", 1.0)
        percentile_high = self.parameters.get("percentile_high", 99.0)

        # Check if this is an identity transform (no actual normalization needed)
        # Identity: input range equals output range with no clamp (range mode only)
        is_identity = (
            mode == "range" and
            input_min == output_min and
            input_max == output_max and
            not clamp_output
        )

        if is_identity:
            return {"result": image_input}

        # Build channel mask
        channel_mask = [
            1.0 if "r" in channels else 0.0,
            1.0 if "g" in channels else 0.0,
            1.0 if "b" in channels else 0.0,
            1.0 if "a" in channels else 0.0,
        ]

        # For percentile mode, the actual input range (percentile values)
        # will be computed by the evaluator using HistogramManager.
        # We pass percentile parameters and let evaluator handle the computation.
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "normalize",
            "_effect_input": image_input,
            "_effect_params": {
                "mode": mode,
                "input_min": input_min,
                "input_max": input_max,
                "output_min": output_min,
                "output_max": output_max,
                "percentile_low": percentile_low,
                "percentile_high": percentile_high,
                "clamp_output": clamp_output,
                "channel_mask": channel_mask,
            },
        }