"""
ChannelMerge Node - Merge multiple single-channel inputs into RGBA output.

Takes up to 4 image inputs (R, G, B, A) and merges them into a single RGBA output.
Each output channel can be sourced from any input channel or a constant value.

Common use cases:
  - Rebuild RGBA from separate R/G/B/A files
  - Replace specific channels (e.g., replace alpha from another source)
  - Channel remapping (e.g., R→G, G→B)
  - Create RGBA from grayscale inputs

Parameters:
  - r_source: Source for R channel (input_r, input_g, input_b, input_a, constant)
  - g_source: Source for G channel
  - b_source: Source for B channel
  - a_source: Source for A channel
  - r_channel: Which channel to read from the R input (r, g, b, a, luma)
  - g_channel: Which channel to read from the G input
  - b_channel: Which channel to read from the B input
  - a_channel: Which channel to read from the A input
  - r_constant: Constant value for R when source=constant
  - g_constant: Constant value for G when source=constant
  - b_constant: Constant value for B when source=constant
  - a_constant: Constant value for A when source=constant
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Channel source options — which input to use for each output channel
CHANNEL_SOURCES = ["input_r", "input_g", "input_b", "input_a", "constant"]

# Which channel to extract from each input (for multi-channel inputs)
EXTRACT_CHANNELS = ["r", "g", "b", "a", "luma"]


class ChannelMergeNode(Node):
    """Merge multiple single-channel inputs into RGBA output."""

    node_type = "channel_merge"
    node_title = "Channel Merge"
    header_color = "#6b8e6b"  # Forest green

    # Metadata
    SEARCH_ALIASES = ["channelmerge", "merge", "channel", "通道合并", "合并", "rgba", "combine"]
    DESCRIPTION = "将多个单通道输入合并成RGBA输出"
    TAGS = ["channel", "utility", "merge"]

    # Bypass mapping: when disabled, use input_r if available, else empty
    BYPASS_MAPPING = {"result": "input_r"}

    PARAM_DEFS = [
        # Source selection for each output channel
        ParamDef("r_source", "option", "input_r", labels=CHANNEL_SOURCES),
        ParamDef("g_source", "option", "input_g", labels=CHANNEL_SOURCES),
        ParamDef("b_source", "option", "input_b", labels=CHANNEL_SOURCES),
        ParamDef("a_source", "option", "input_a", labels=CHANNEL_SOURCES),
        # Which channel to extract from each input
        ParamDef("r_channel", "option", "r", labels=EXTRACT_CHANNELS),
        ParamDef("g_channel", "option", "g", labels=EXTRACT_CHANNELS),
        ParamDef("b_channel", "option", "b", labels=EXTRACT_CHANNELS),
        ParamDef("a_channel", "option", "a", labels=EXTRACT_CHANNELS),
        # Constant values when source=constant
        ParamDef("r_constant", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("g_constant", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("b_constant", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("a_constant", "float", 1.0, min_val=0.0, max_val=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("input_r", "input", "image"),
            Port("input_g", "input", "image"),
            Port("input_b", "input", "image"),
            Port("input_a", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "r_source": "input_r",
            "g_source": "input_g",
            "b_source": "input_b",
            "a_source": "input_a",
            "r_channel": "r",
            "g_channel": "g",
            "b_channel": "b",
            "a_channel": "a",
            "r_constant": 0.0,
            "g_constant": 0.0,
            "b_constant": 0.0,
            "a_constant": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the first connected input.

        Uses input_r if connected, else falls back to other connected inputs.
        """
        # Priority order: input_r, input_g, input_b, input_a
        for port_name in ["input_r", "input_g", "input_b", "input_a"]:
            bbox = input_bboxes.get(port_name)
            if bbox and not bbox.is_empty:
                return bbox
        return None

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """All connected inputs need the same ROI."""
        result = {}
        for port_name in ["input_r", "input_g", "input_b", "input_a"]:
            if input_bboxes and port_name in input_bboxes:
                result[port_name] = output_roi
        return result

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Return effect marker for GPU dispatch.

        The marker contains all connected inputs and channel source parameters.
        The actual GPU merge is handled by _apply_channel_merge in GraphEvaluatorMixin.
        """
        # Get all inputs
        input_r = inputs.get("input_r")
        input_g = inputs.get("input_g")
        input_b = inputs.get("input_b")
        input_a = inputs.get("input_a")

        # Determine output dimensions from first connected input
        # Priority: input_r, input_g, input_b, input_a
        first_valid_input: Optional[TextureResult] = None
        w: int = 0
        h: int = 0
        for tex in [input_r, input_g, input_b, input_a]:
            if tex and tex.is_valid:
                first_valid_input = tex
                w = tex.width
                h = tex.height
                break

        # No valid inputs — return empty
        if first_valid_input is None:
            return {}

        # Get parameters
        p = self.parameters

        # Convert source options to indices for GPU
        def safe_source_index(source: str) -> int:
            """Convert source string to index, with safe fallback."""
            if source in CHANNEL_SOURCES:
                return CHANNEL_SOURCES.index(source)
            return 0  # default to input_r

        def safe_channel_index(channel: str) -> int:
            """Convert channel string to index, with safe fallback."""
            if channel in EXTRACT_CHANNELS:
                return EXTRACT_CHANNELS.index(channel)
            return 0  # default to 'r'

        # Check if all outputs use the same source and channel (identity case)
        r_src = p.get("r_source", "input_r")
        g_src = p.get("g_source", "input_g")
        b_src = p.get("b_source", "input_b")
        a_src = p.get("a_source", "input_a")

        r_ch = p.get("r_channel", "r")
        g_ch = p.get("g_channel", "g")
        b_ch = p.get("b_channel", "b")
        a_ch = p.get("a_channel", "a")

        # Identity case: R from input_r.r, G from input_g.g, B from input_b.b, A from input_a.a
        # AND all inputs are the same texture — return first input as passthrough
        all_inputs_same = (
            input_r and input_g and input_b and input_a and
            input_r.texture_id == input_g.texture_id and
            input_g.texture_id == input_b.texture_id and
            input_b.texture_id == input_a.texture_id
        )
        if all_inputs_same and r_src == "input_r" and g_src == "input_g" and b_src == "input_b" and a_src == "input_a" and r_ch == "r" and g_ch == "g" and b_ch == "b" and a_ch == "a":
            return {"result": first_valid_input}

        # Return effect marker for GPU dispatch
        return {
            "result": TextureResult(
                texture_id=-1,
                width=w,
                height=h,
            ),
            "_effect_type": "channel_merge",
            "_effect_params": {
                # Source indices: 0=input_r, 1=input_g, 2=input_b, 3=input_a, 4=constant
                "r_source": safe_source_index(r_src),
                "g_source": safe_source_index(g_src),
                "b_source": safe_source_index(b_src),
                "a_source": safe_source_index(a_src),
                # Channel indices: 0=r, 1=g, 2=b, 3=a, 4=luma
                "r_channel": safe_channel_index(r_ch),
                "g_channel": safe_channel_index(g_ch),
                "b_channel": safe_channel_index(b_ch),
                "a_channel": safe_channel_index(a_ch),
                # Constant values (used when source=constant)
                "r_constant": float(p.get("r_constant", 0.0)),
                "g_constant": float(p.get("g_constant", 0.0)),
                "b_constant": float(p.get("b_constant", 0.0)),
                "a_constant": float(p.get("a_constant", 1.0)),
                # Input textures (may be None if not connected)
                "input_r": input_r,
                "input_g": input_g,
                "input_b": input_b,
                "input_a": input_a,
            },
        }