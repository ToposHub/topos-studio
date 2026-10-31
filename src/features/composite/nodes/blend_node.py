"""
Merge Node - Professional compositing merge operations (NUKE-style).

Composites two inputs (A=foreground, B=background) with optional mask.
Supports standard NUKE blend modes, format/bbox policies, and alpha handling.

In NUKE's convention:
  A = foreground (top layer)
  B = background (bottom layer)
  Mask = optional per-pixel modulation of the merge effect

S2-B: Alpha/Premultiplied Policy
---------------------------------
The merge shader internally operates in premultiplied space for correct
alpha compositing semantics. The alpha_policy parameter controls how
input and output alpha is interpreted:

  - "auto" (default): Read alpha_premultiplied from input metadata.
      If input.alpha_premultiplied is False (straight), shader premultiplies
      on-the-fly. Output is always premultiplied.

  - "premult_force": Assume all inputs are premultiplied regardless of
      metadata. Useful when metadata is missing or incorrect.

  - "straight_force": Assume all inputs are straight alpha regardless of
      metadata. Shader will premultiply before blend.

  - "output_straight": Same as auto, but also unpremultiply the output
      to produce straight alpha result. Useful for downstream nodes
      that expect straight alpha (e.g., certain filters, output nodes).

Internal Processing (S2-B):
--------------------------
1. Sample input textures
2. If input is straight (alpha_premultiplied=False), premultiply:
   rgb' = rgb * alpha
3. Apply blend operation in premultiplied space
4. Output is premultiplied by default (alpha_premultiplied=True)
5. If alpha_policy="output_straight", unpremultiply output:
   rgb_out = blended_rgb / out_alpha (where alpha > 0)

Add/Subtract HDR Behavior (S2-B):
---------------------------------
For add/subtract modes, color channels are not clamped to preserve HDR.
Alpha is clamped to [0.0, 1.0] by default to prevent invalid alpha values.
To disable alpha clamping for HDR alpha compositing, use alpha_clamp=False.
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# All supported blend modes in order (matches shader switch indices).
# Indices 0-14: original modes, 15-18: Porter-Duff matte, 19-23: photographic,
# 24-27: HSL.
BLEND_MODES = [
    # ── Basic composite ──
    "over",        # 0:  Standard A-over-B alpha compositing
    "under",       # 1:  B-over-A (swap roles)
    "add",         # 2:  A + B (additive, HDR-aware)
    "subtract",    # 3:  B.rgb - A.rgb, alpha keeps background (NUKE minus)
    "multiply",    # 4:  A * B
    "screen",      # 5:  1 - (1-A)(1-B)
    "overlay",     # 6:  Hard light mix
    "difference",  # 7:  |A - B|
    "divide",      # 8:  B / A
    "min",         # 9:  min(A, B) per channel
    "max",         # 10: max(A, B) per channel
    "average",     # 11: (A + B) / 2
    "copy",        # 12: Copy A, ignore B
    "stencil",     # 13: Use A's alpha to cut B
    "mask",        # 14: Use B's alpha to mask A
    # ── Porter-Duff matte (S3-B) ──
    "in",          # 15: A only where B is opaque (Porter-Duff A in B)
    "out",         # 16: A only where B is transparent (A held out by B)
    "atop",        # 17: A in B, plus B outside A
    "xor",         # 18: A or B, but not both
    # ── Photographic (Photoshop/AE, S3-B) ──
    "color-dodge", # 19: Lighten bg by fg (dodge)
    "color-burn",  # 20: Darken bg by fg (burn)
    "soft-light",  # 21: W3C soft-light
    "hard-light",  # 22: Overlay with fg/bg swapped
    "linear-light",# 23: Burn/dodge combined
    # ── HSL (Photoshop/AE, S3-B) ──
    "hue",         # 24: Use fg hue, bg sat & value
    "saturation",  # 25: Use bg hue, fg sat, bg value
    "color",       # 26: Use fg hue & sat, bg value
    "luminosity",  # 27: Use bg hue & sat, fg value
    # ── CP-012: Nuke 补齐 ──
    "from",        # 28: A.rgb - B.rgb（subtract 的镜像，Nuke from）
    "exclusion",   # 29: A + B - 2AB（软差值）
    "conjoint",    # 30: 覆盖率和封顶为 1 的并集（重叠区按可见比例贡献）
    "disjoint",    # 31: 不重叠并集（alpha 和 ≤1 时直接相加）
]

# Format policy options
FORMAT_POLICIES = ["from_b", "from_a", "larger", "smaller"]

# Bbox policy options
BBOX_POLICIES = ["union", "intersection", "a", "b"]

# S2-B: Alpha policy options
ALPHA_POLICIES = [
    "auto",           # Read from input metadata, output premultiplied
    "premult_force",  # Force assume inputs are premultiplied
    "straight_force", # Force assume inputs are straight
    "output_straight", # Auto input, but output straight alpha
]

# S3-A: Mask channel selection options (indices match shader u_mask_channel)
#   0=luma (Rec.709), 1=R, 2=G, 3=B, 4=A
MASK_CHANNELS = ["luma", "red", "green", "blue", "alpha"]


class MergeNode(Node):
    """Merges two inputs with optional mask (NUKE-style Merge).

    S2-B Alpha Policy:
    - Internal shader operates in premultiplied space
    - Input straight alpha is converted to premultiplied on-the-fly
    - Output is premultiplied by default (unless alpha_policy="output_straight")
    - Add/subtract modes preserve HDR color (no clamping)
    - Alpha clamping is controlled by alpha_clamp parameter
    """

    node_type = "merge"
    node_title = "Merge"
    i18n_title_key = "nodes.merge.title"
    header_color = "#8b5e3c"  # Brown

    # Metadata
    SEARCH_ALIASES = ["merge", "blend", "over", "plus", "add", "合并", "混合", "叠加", "comp"]
    DESCRIPTION = "NUKE风格的合并节点，支持多种混合模式和alpha策略"
    TAGS = ["merge", "composite"]

    # AUD-64010: explicit disabled/bypass semantics. NUKE convention — a
    # disabled Merge outputs the B (background) input. Without this the
    # base class default passes through whichever input the evaluator's
    # dict happens to iterate first (A or B), silently dropping the other.
    BYPASS_MAPPING = {"result": "B"}

    PARAM_DEFS = [
        ParamDef("blend_mode", "option", "over", labels=BLEND_MODES),
        ParamDef("opacity", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("format_policy", "option", "from_b", labels=FORMAT_POLICIES),
        ParamDef("bbox_policy", "option", "union", labels=BBOX_POLICIES),
        # S2-B: Alpha policy for premultiplied/straight handling
        ParamDef("alpha_policy", "option", "auto", labels=ALPHA_POLICIES),
        # S2-B: Alpha clamping for add/subtract HDR modes
        ParamDef("alpha_clamp", "bool", True),
        # S3-A: Mask channel selection (luma/R/G/B/A) and invert
        ParamDef("mask_channel", "option", "luma", labels=MASK_CHANNELS),
        ParamDef("mask_invert", "bool", False),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("A", "input", "image"),
            Port("B", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "blend_mode": "over",
            "opacity": 1.0,
            "mix": 1.0,                 # S3-A: 1.0 = full merge result
            "format_policy": "from_b",
            "bbox_policy": "union",
            "alpha_policy": "auto",      # S2-B
            "alpha_clamp": True,         # S2-B
            "mask_channel": "luma",      # S3-A
            "mask_invert": False,        # S3-A
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Merge output bbox depends on bbox_policy."""
        bbox_a = input_bboxes.get("A")
        bbox_b = input_bboxes.get("B")

        # Handle missing inputs
        if bbox_a is None or bbox_a.is_empty:
            return bbox_b
        if bbox_b is None or bbox_b.is_empty:
            return bbox_a

        bbox_policy = params.get("bbox_policy", "union")

        if bbox_policy == "intersection":
            return bbox_a.intersection(bbox_b)
        elif bbox_policy == "a":
            return bbox_a
        elif bbox_policy == "b":
            return bbox_b
        else:  # "union" (default)
            return bbox_a.union(bbox_b)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Compute per-input ROI for merge (BUG-3 fix: respect bbox_policy).

        Merge is per-pixel, but each input's data window (bbox) may cover a
        different sub-region of the output canvas. The shader's `sampleLayer()`
        maps each input's bbox-relative pixels into the output, so an input
        needs to produce pixels covering the union of its own bbox and the
        requested output_roi (mapped into the output coordinate space).

        Without this, when bbox_policy != the natural extents of A/B, upstream
        nodes would under-fetch and the shader would sample out-of-bounds
        (transparent) pixels.

        Args:
            output_roi: The region of interest in output canvas pixel space.
            params: Node parameters (bbox_policy is read but the conservative
                    union approach is bbox-policy-agnostic — requesting the
                    union is always safe and only affects how much upstream
                    renders).
            input_bboxes: Optional dict of input port name -> input bbox.
                When provided, each input's ROI is the union of its bbox and
                the output_roi. When None, all inputs get the output_roi.
        """
        result = {}
        if input_bboxes:
            for name, ibbox in input_bboxes.items():
                if ibbox is None or ibbox.is_empty:
                    result[name] = output_roi
                else:
                    # Conservative: request the union so upstream provides
                    # enough data for the shader to sample from.
                    result[name] = ibbox.union(output_roi)
        else:
            # Fallback when input bboxes are not known
            result["A"] = output_roi
            result["B"] = output_roi
            result["mask"] = output_roi

        return result

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Return merge marker for GPU dispatch.

        Uses the new `_effect_type` protocol (S1-C):
        - `_effect_type`: "merge" — dispatcher key for _dispatch_effect()
        - `_effect_input`: Primary foreground input (input_b) — unified effect entry
        - `_effect_input_a`: Background input (A)
        - `_effect_input_b`: Foreground input (B)
        - `_effect_input_mask`: Optional mask input
        - `_effect_params`: Node parameters for blend operation

        S2-B: Added alpha_policy and alpha_clamp parameters:
        - alpha_policy: Controls how input/output alpha is interpreted
        - alpha_clamp: Whether to clamp alpha in add/subtract modes

        S3-A: Added mix, mask_channel, mask_invert parameters:
        - mix: Overall effect strength (0=background, 1=full merge)
        - mask_channel: Which mask channel to use (luma/R/G/B/A)
        - mask_invert: Invert the mask value

        The actual GPU rendering is handled by BaseEvaluator._dispatch_effect()
        which dispatches to _dispatch_merge_effect() internally.
        """
        input_a = inputs.get("A")
        input_b = inputs.get("B")
        input_mask = inputs.get("mask")

        if input_a is None and input_b is None:
            return {}

        # Pass through if only one input
        if input_a is None or not input_a.is_valid:
            return {"result": input_b} if input_b else {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        # S2-B: Determine output alpha_premultiplied based on alpha_policy
        alpha_policy = self.parameters.get("alpha_policy", "auto")
        output_premult = alpha_policy != "output_straight"

        # S3-A: Resolve mask_channel string → index (must match shader u_mask_channel)
        mask_channel_str = self.parameters.get("mask_channel", "luma")
        mask_channel_idx = MASK_CHANNELS.index(mask_channel_str) if mask_channel_str in MASK_CHANNELS else 0

        # Return new `_effect_type` marker protocol (S1-C + S2-B + S3-A)
        # GPU blend operation is handled by BaseEvaluator._dispatch_effect()
        # which reads the blend_mode, opacity, format_policy, bbox_policy from _effect_params
        return {
            "result": TextureResult(
                texture_id=-1,
                width=input_b.width if input_b.is_valid else input_a.width,
                height=input_b.height if input_b.is_valid else input_a.height,
                alpha_premultiplied=output_premult,  # S2-B: Set output alpha type
            ),
            "_effect_type": "merge",
            "_effect_input": input_b,  # Primary foreground input (unified effect entry)
            "_effect_input_a": input_a,
            "_effect_input_b": input_b,
            "_effect_input_mask": input_mask,
            "_effect_params": {
                "blend_mode": self.parameters.get("blend_mode", "over"),
                "opacity": self._get_param_clamped("opacity", min_val=0.0, max_val=1.0),
                "mix": self._get_param_clamped("mix", min_val=0.0, max_val=1.0),
                "format_policy": self.parameters.get("format_policy", "from_b"),
                "bbox_policy": self.parameters.get("bbox_policy", "union"),
                # S2-B: Alpha policy parameters
                "alpha_policy": alpha_policy,
                "alpha_clamp": self.parameters.get("alpha_clamp", True),
                # S3-A: Mask parameters
                "mask_channel": mask_channel_idx,
                "mask_invert": self.parameters.get("mask_invert", False),
            },
        }


# Backward-compatible alias
BlendNode = MergeNode
