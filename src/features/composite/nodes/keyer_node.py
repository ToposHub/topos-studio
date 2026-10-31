"""
Keyer Node - Unified keying entry point (Keylight 1.2-style chroma keyer).

Acts as a unified interface for various keying modes:
- Chroma (default): Keylight 1.2-style color-difference keying using
  screen_color + screen_balance + clip black/white + gain + despill.
- Luma: luminance-based keying (black/white points + softness).
- Difference: compare two images to generate a matte.
- Auto: picks chroma/luma/difference based on inputs.

Parameters (chroma mode mirrors AE Keylight 1.2):
- keyer_mode: auto / chroma / luma / difference
- screen_color_r/g/b: screen colour (typically pure green or blue)
- screen_balance: weights Cb vs Cr of the screen axis (0..1)
- clip_black / clip_white: matte input black/white points
- gain: matte multiplier
- softness: matte edge softness (0..1)
- despill_strength: 0..1
- despill_method: none / subtract / scale
- alpha_offset: matte lift
- invert: invert the final matte
- output_mode: result (premultiplied) / matte (alpha only)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Keyer modes
KEYER_MODES = ["auto", "chroma", "luma", "difference"]

# Output modes
OUTPUT_MODES = ["result", "matte"]

# Despill methods — indices match the shader u_despill_method values
DESILL_METHODS = ["none", "subtract", "scale"]


class KeyerNode(Node):
    """Unified keying entry point with Keylight 1.2-style chroma keying."""

    node_type = "keyer"
    node_title = "Keyer"
    header_color = "#5a9a5a"

    # i18n title key
    i18n_title_key = "nodes.keyer.title"

    # Metadata
    SEARCH_ALIASES = ["keyer", "key", "keylight", "抠像", "抠图", "绿幕", "蓝幕", "chroma", "luma", "difference"]
    DESCRIPTION = "通用抠像节点（Keylight 1.2 风格色度抠像）"
    TAGS = ["keyer", "matte"]
    IS_PLACEHOLDER = False

    # Bypass mapping: when disabled, pass image input through
    BYPASS_MAPPING = {"result": "image"}

    PARAM_DEFS = [
        ParamDef("keyer_mode", "option", "auto", labels=KEYER_MODES,
                 group="Keying", allow_expression=False),

        # Screen colour (chroma mode)
        ParamDef("screen_color_r", "float", 0.0, min_val=0.0, max_val=1.0,
                 group="Screen", hidden=True),
        ParamDef("screen_color_g", "float", 1.0, min_val=0.0, max_val=1.0,
                 group="Screen", hidden=True),
        ParamDef("screen_color_b", "float", 0.0, min_val=0.0, max_val=1.0,
                 group="Screen", hidden=True),
        # Hex string mirrors the RGBA swatch (for the colour picker round-trip),
        # exactly like ConstantNode.color_hex. Kept hidden so the generic UI
        # builder never renders a raw text field for it.
        ParamDef("screen_color_hex", "str", "#00ff00", group="Screen", hidden=True),
        ParamDef("screen_balance", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01,
                 group="Screen", hidden=True),

        # Matte shaping (chroma + luma)
        ParamDef("clip_black", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="Matte"),
        ParamDef("clip_white", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="Matte"),
        ParamDef("gain", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01,
                 group="Matte"),
        ParamDef("softness", "float", 0.1, min_val=0.0, max_val=1.0, step=0.01,
                 group="Matte"),
        ParamDef("alpha_offset", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01,
                 group="Matte", hidden=True),

        # Despill (chroma mode)
        ParamDef("despill_strength", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01,
                 group="Despill", hidden=True),
        ParamDef("despill_method", "option", "subtract", labels=DESILL_METHODS,
                 group="Despill", hidden=True),

        # Common
        ParamDef("invert", "bool", False, group="Keying"),
        ParamDef("output_mode", "option", "result", labels=OUTPUT_MODES,
                 group="Output"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("reference", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "keyer_mode": "auto",
            "screen_color_r": 0.0,
            "screen_color_g": 1.0,
            "screen_color_b": 0.0,
            "screen_color_hex": "#00ff00",
            "screen_balance": 0.5,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "gain": 1.0,
            "softness": 0.1,
            "alpha_offset": 0.0,
            "despill_strength": 0.5,
            "despill_method": "subtract",
            "invert": False,
            "output_mode": "result",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the image input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI for difference keying."""
        result = {"image": output_roi}
        keyer_mode = params.get("keyer_mode", "auto")
        if keyer_mode == "difference" or (input_bboxes and "reference" in input_bboxes):
            result["reference"] = output_roi
        return result

    @staticmethod
    def keyer_mode_param_visibility(keyer_mode: str) -> set:
        """Return the set of param keys that should be HIDDEN for the given mode.

        Used by the UI panel to toggle per-mode parameters.
        """
        # Always-visible params
        # Per-mode params
        chroma_params = {
            "screen_color_r", "screen_color_g", "screen_color_b",
            "screen_balance", "clip_black", "clip_white", "gain", "softness",
            "alpha_offset", "despill_strength", "despill_method",
        }
        luma_params = {
            "clip_black", "clip_white", "gain", "softness", "alpha_offset",
        }
        if keyer_mode == "chroma":
            hidden = set()
        elif keyer_mode == "luma":
            hidden = chroma_params - luma_params
        elif keyer_mode == "difference":
            hidden = chroma_params
        else:  # auto — show all chroma-style params (auto picks chroma by default)
            hidden = set()
        return hidden

    def _detect_best_keyer_mode(self, inputs: Dict[str, TextureResult]) -> str:
        """Auto-detect the best keying mode based on input analysis.

        Detection logic:
        1. If reference input exists → difference_keyer
        2. Default → chroma_keyer (most common keying scenario)

        Args:
            inputs: Dict of input port name -> TextureResult

        Returns:
            Detected keyer mode string ("chroma", "luma", "difference")
        """
        # Rule 1: If reference input exists → difference_keyer
        reference = inputs.get("reference")
        if reference is not None and reference.is_valid:
            return "difference"

        # Rule 2-4: Default to chroma_keyer
        # Full color analysis would require GPU sampling, which is expensive.
        # For now, we default to chroma_keyer as the most common keying scenario.
        return "chroma"

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate keyer with automatic mode detection.

        Dispatches to specific keyer type based on keyer_mode parameter.
        In auto mode, analyzes inputs to choose best keying strategy.
        """
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        # Get parameters with validation
        keyer_mode = self.parameters.get("keyer_mode", "auto")

        # Auto-detect mode if needed
        if keyer_mode == "auto":
            keyer_mode = self._detect_best_keyer_mode(inputs)

        # Get common parameters
        clip_black = self._get_param_clamped("clip_black", 0.0, 1.0)
        clip_white = self._get_param_clamped("clip_white", 0.0, 1.0)
        gain = max(0.0, float(self.parameters.get("gain", 1.0)))
        softness = self._get_param_clamped("softness", 0.0, 1.0)
        alpha_offset = float(self.parameters.get("alpha_offset", 0.0))
        invert = self._get_param_bool("invert", False)
        output_mode = self.parameters.get("output_mode", "result")

        # Prepare effect params based on keyer mode
        effect_params = {
            "clip_black": clip_black,
            "clip_white": clip_white,
            "gain": gain,
            "softness": softness,
            "alpha_offset": alpha_offset,
            "invert": invert,
            "output_mode": output_mode,
        }

        # Build result based on keyer mode
        result = {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "keyer",
            "_effect_input": image_input,
            "_effect_params": effect_params,
        }

        if keyer_mode == "chroma":
            # Chroma keyer: Keylight-style
            screen_color_r = self._get_param_clamped("screen_color_r", 0.0, 1.0)
            screen_color_g = self._get_param_clamped("screen_color_g", 0.0, 1.0)
            screen_color_b = self._get_param_clamped("screen_color_b", 0.0, 1.0)
            screen_balance = self._get_param_clamped("screen_balance", 0.0, 1.0)
            despill_strength = self._get_param_clamped("despill_strength", 0.0, 1.0)
            despill_method = self.parameters.get("despill_method", "subtract")
            if despill_method not in DESILL_METHODS:
                despill_method = "subtract"
            effect_params["screen_color"] = (screen_color_r, screen_color_g, screen_color_b)
            effect_params["screen_balance"] = screen_balance
            effect_params["despill_strength"] = despill_strength
            effect_params["despill_method"] = DESILL_METHODS.index(despill_method)
            effect_params["keyer_mode"] = "chroma"

        elif keyer_mode == "luma":
            # Luma keyer: use clip_black/white + softness directly
            effect_params["keyer_mode"] = "luma"

        elif keyer_mode == "difference":
            # Difference keyer: needs reference input
            reference = inputs.get("reference")
            if reference is None or not reference.is_valid:
                # No reference - return input unchanged
                return {"result": image_input}
            result["_effect_input_a"] = image_input
            result["_effect_input_b"] = reference
            effect_params["keyer_mode"] = "difference"
            # Remove _effect_input for difference mode (uses input_a/input_b)
            del result["_effect_input"]

        return result
