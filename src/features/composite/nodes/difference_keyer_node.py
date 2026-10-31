"""
DifferenceKeyer Node - Keying based on image difference.

Generates a matte by comparing the difference between two images.
Commonly used for:
  - Static background keying (alternative to bluescreen/greenscreen)
  - Motion detection
  - Frame difference analysis
  - Clean plate extraction

Algorithm:
  diff = abs(A - B)
  matte = smoothstep(threshold - softness, threshold + softness, diff * gain)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Channel comparison modes
CHANNEL_MODES = ["luma", "rgb", "individual"]

# Output modes
OUTPUT_MODES = ["result", "matte"]


class DifferenceKeyerNode(Node):
    """Keying based on image difference between two inputs."""

    node_type = "difference_keyer"
    node_title = "DifferenceKeyer"
    header_color = "#6a4a8e"  # Purple

    # Metadata
    SEARCH_ALIASES = ["difference", "diff", "差值", "差值键", "抠像", "difference key", "motion detect"]
    DESCRIPTION = "基于两图像差值的抠像"
    TAGS = ["keyer", "matte", "difference"]

    # Bypass mapping: when disabled, pass input_a through
    BYPASS_MAPPING = {"result": "input_a"}

    # Input aliases for compatibility with standard keyer tests
    # Maps: alias -> actual port name
    INPUT_ALIASES = {
        "image": "input_a",      # Foreground image
        "reference": "input_b",  # Reference background (clean plate)
    }

    PARAM_DEFS = [
        ParamDef("threshold", "float", 0.1, min_val=0.0, max_val=1.0, decimals=3,
                 group="Keying", allow_expression=True,
                 expression_hint="this.width * 0.001"),
        ParamDef("softness", "float", 0.05, min_val=0.0, max_val=0.5, decimals=3,
                 group="Keying"),
        ParamDef("gain", "float", 1.0, min_val=0.1, max_val=10.0, decimals=2,
                 group="Keying"),
        ParamDef("channel_mode", "option", "luma", labels=CHANNEL_MODES,
                 group="Keying"),
        ParamDef("invert", "bool", False, group="Keying"),
        ParamDef("output_mode", "option", "result", labels=OUTPUT_MODES,
                 group="Output"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("input_a", "input", "image"),  # Foreground image
            Port("input_b", "input", "image"),  # Reference background (clean plate)
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "threshold": 0.1,
            "softness": 0.05,
            "gain": 1.0,
            "channel_mode": "luma",
            "invert": False,
            "output_mode": "result",
        }

    def evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                 resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Override evaluate to handle input aliases before processing.

        This ensures both bypass mode and normal evaluation work with
        standard keyer test inputs (image/reference aliases).
        """
        # Handle input aliases for compatibility with standard keyer tests
        # Map aliases to actual port names if not already present
        resolved_inputs = dict(inputs)  # Create a copy to avoid modifying the original
        for alias, actual_port in self.INPUT_ALIASES.items():
            if alias in resolved_inputs and actual_port not in resolved_inputs:
                resolved_inputs[actual_port] = resolved_inputs[alias]

        # Call parent evaluate with resolved inputs
        return super().evaluate(resolved_inputs, ctx, fb_manager, resource_pool, render_mode)

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches input_a (foreground)."""
        return input_bboxes.get("input_a")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI for difference comparison."""
        return {"input_a": output_roi, "input_b": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        # Input aliases already resolved by evaluate()
        input_a = inputs.get("input_a")
        input_b = inputs.get("input_b")

        # If only one input is valid, return it as-is
        if input_a is None or not input_a.is_valid:
            return {}
        if input_b is None or not input_b.is_valid:
            # No reference background - return input_a unchanged
            return {"result": input_a}

        # Get parameters with validation
        threshold = self._get_param_clamped("threshold", 0.0, 1.0)
        softness = self._get_param_clamped("softness", 0.0, 0.5)
        gain = self._get_param_clamped("gain", 0.1, 10.0)
        channel_mode = self.parameters.get("channel_mode", "luma")
        invert = self._get_param_bool("invert", False)
        output_mode = self.parameters.get("output_mode", "result")

        return {
            "result": TextureResult(
                texture_id=-1,
                width=input_a.width,
                height=input_a.height,
            ),
            "_effect_type": "difference_keyer",
            "_effect_input_a": input_a,
            "_effect_input_b": input_b,
            "_effect_params": {
                "threshold": threshold,
                "softness": softness,
                "gain": gain,
                "channel_mode": channel_mode,
                "invert": invert,
                "output_mode": output_mode,
            },
        }