"""
Shuffle Node - Rearrange / remap RGBA channels (NUKE-style Shuffle).

Each output channel (R, G, B, A) can be sourced from any input channel
or from fixed values (zero, one, luma).

Common use cases:
  - Alpha from luma: a_from = "luma"
  - Remove alpha: a_from = "one"
  - Swap channels: r_from = "a", a_from = "r"
  - Monochrome preview: r_from = g_from = b_from = "luma"
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef

# Channel source options — index must match shader switch indices
CHANNEL_SOURCES = ["r", "g", "b", "a", "zero", "one", "luma"]


class ShuffleNode(Node):
    """Shuffle / remap RGBA channels from a single input."""

    HELP_TEXT = """RGBA 通道重映射。每个输出通道可指定来源（r/g/b/a/zero/one/luma）。
输入：image（图像）
输出：重映射后的图像（RGBA）
参数：
  - r_source, g_source, b_source, a_source: 通道来源
注意：适合通道提取、重组、复制。例如将 alpha 复制到 RGB 创建灰度图。"""
    DISPLAY_NAME = "Shuffle"

    node_type = "shuffle"
    node_title = "Shuffle"
    header_color = "#7b68ae"  # Purple

    # i18n title key
    i18n_title_key = "nodes.shuffle.title"

    # Metadata
    SEARCH_ALIASES = ["shuffle", "channel", "通道", "重排", "rgba", "remap"]
    DESCRIPTION = "重排RGBA通道（NUKE风格）"
    TAGS = ["channel", "utility"]

    # Bypass mapping: when disabled, pass input through unchanged
    BYPASS_MAPPING = {"image": "image"}

    PARAM_DEFS = [
        ParamDef("r_from", "option", "r", labels=CHANNEL_SOURCES),
        ParamDef("g_from", "option", "g", labels=CHANNEL_SOURCES),
        ParamDef("b_from", "option", "b", labels=CHANNEL_SOURCES),
        ParamDef("a_from", "option", "a", labels=CHANNEL_SOURCES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image")
        ]
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "r_from": "r",
            "g_from": "g",
            "b_from": "b",
            "a_from": "a",
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if not image_input or not image_input.is_valid:
            return {}

        # Check if this is an identity shuffle (no-op)
        p = self.parameters
        if (p.get("r_from") == "r" and p.get("g_from") == "g"
                and p.get("b_from") == "b" and p.get("a_from") == "a"):
            return {"image": image_input}

        # Return effect marker for GPU dispatch
        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "shuffle",
            "_effect_input": image_input,
            "_effect_params": {
                # Use safe lookup with fallback defaults (prevent ValueError crash)
                "r_from": CHANNEL_SOURCES.index(p.get("r_from", "r")) if p.get("r_from", "r") in CHANNEL_SOURCES else 0,
                "g_from": CHANNEL_SOURCES.index(p.get("g_from", "g")) if p.get("g_from", "g") in CHANNEL_SOURCES else 1,
                "b_from": CHANNEL_SOURCES.index(p.get("b_from", "b")) if p.get("b_from", "b") in CHANNEL_SOURCES else 2,
                "a_from": CHANNEL_SOURCES.index(p.get("a_from", "a")) if p.get("a_from", "a") in CHANNEL_SOURCES else 3,
            },
        }
