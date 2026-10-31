"""Keymix Node — mask-driven A/B blend (CP-011).

Nuke Keymix counterpart: uses a matte ``M`` to mix foreground ``A`` over
background ``B`` — ``out = B + (A - B) * M`` in premultiplied-linear
space (alpha identically lerped). The classic use is keyer-repair chains:
keyed foreground + clean plate + a garbage/edge matte decides where the
keyed result is trusted.

Unlike blend+mask (which composites A over B and gates with the mask),
Keymix is a pure three-input lerp — no alpha-compositing term of its own —
which is why it exists as a dedicated node in every professional
compositor.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

MASK_CHANNELS = ["luma", "r", "g", "b", "a"]


class KeymixNode(Node):
    """Mask-driven A/B blend: out = B + (A - B) * mask * mix."""

    node_type = "keymix"
    node_title = "Keymix"
    i18n_title_key = "nodes.keymix.title"
    header_color = "#4a7d6b"

    SEARCH_ALIASES = [
        "keymix", "key mix", "key mix blend", "matte mix",
        "键混", "遮罩混合", "抠像混合",
    ]
    DESCRIPTION = "用遮罩在 A/B 两路输入间混合（键控返修/垃圾遮罩常用）"
    TAGS = ["merge", "mask", "utility"]

    PARAM_DEFS = [
        ParamDef("mask_channel", "option", "luma", labels=MASK_CHANNELS,
                 group="Matte"),
        ParamDef("mask_invert", "bool", False, group="Matte"),
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="Matte"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("A", "input", "image"),
            Port("B", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "mask_channel": "luma",
            "mask_invert": False,
            "mix": 1.0,
        }
        self._init_port_revisions()

    def compute_output_bbox(self, input_bboxes, params):
        """Union of A/B bboxes (mask is a gate, not a geometry source)."""
        bbox_a = input_bboxes.get("A") if input_bboxes else None
        bbox_b = input_bboxes.get("B") if input_bboxes else None
        if bbox_a is None and bbox_b is None:
            return None
        if bbox_a is None:
            return bbox_b
        if bbox_b is None:
            return bbox_a
        return bbox_a.union(bbox_b)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        input_a = inputs.get("A")
        input_b = inputs.get("B")
        has_a = input_a is not None and input_a.is_valid
        has_b = input_b is not None and input_b.is_valid
        input_mask = inputs.get("mask")
        mask_valid = input_mask is not None and input_mask.is_valid

        # Degradations: single input passes through (nothing to mix with);
        # missing mask behaves as a fully-open gate.
        if has_a and not has_b:
            return {"result": input_a, "_passthrough": True}
        if has_b and not has_a:
            return {"result": input_b, "_passthrough": True}
        if not has_a and not has_b:
            return {}

        ref = input_a if input_a.width * input_a.height >= \
            input_b.width * input_b.height else input_b

        return {
            "result": TextureResult(
                texture_id=-1, width=ref.width, height=ref.height,
                alpha_premultiplied=True, color_space="linear"),
            "_effect_type": "keymix",
            "_effect_input_a": input_a,
            "_effect_input_b": input_b,
            "_effect_mask": input_mask if mask_valid else None,
            "_effect_params": {
                "mask_channel": self.parameters.get("mask_channel", "luma"),
                "mask_invert": bool(self.parameters.get("mask_invert", False)),
                "mix": self._get_param_clamped("mix", 0.0, 1.0),
            },
        }
