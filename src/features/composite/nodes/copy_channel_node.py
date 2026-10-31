"""
CopyChannel Node - Copy a single channel from input B into input A (NUKE-style Copy).

Takes two image inputs:
  - A: main image (destination — all channels preserved except the target)
  - B: source image (provides the channel to copy)

Parameters:
  - src_channel: which channel to read from B (r/g/b/a)
  - dst_channel: which channel to replace in A (r/g/b/a)

Common use cases:
  - Copy B's red channel into A's alpha
  - Copy B's alpha into A's red for matte visualization
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef

CHANNELS = ["r", "g", "b", "a"]


class CopyChannelNode(Node):
    """Copy a channel from input B into input A."""

    HELP_TEXT = """将输入 B 的单个通道复制到输入 A 的指定通道。
输入：A（目标图像）、B（源图像）
输出：复制后的图像（RGBA）
参数：
  - source_channel: 源通道（r/g/b/a）
  - dest_channel: 目标通道（r/g/b/a）
注意：适合从一张图像提取通道到另一张。例如将亮度图的 alpha 复制到彩色图。"""
    DISPLAY_NAME = "Copy Channel"

    node_type = "copy_channel"
    node_title = "CopyChannel"
    header_color = "#6a7b8e"  # Slate

    # Metadata
    SEARCH_ALIASES = ["copychannel", "copy", "channel", "复制通道", "拷贝", "拷贝通道"]
    DESCRIPTION = "从输入B复制通道到输入A"
    TAGS = ["channel", "utility"]

    # Bypass mapping: when disabled, pass through input A (main input)
    BYPASS_MAPPING = {"image": "A"}

    PARAM_DEFS = [
        ParamDef("src_channel", "option", "a", labels=CHANNELS),
        ParamDef("dst_channel", "option", "a", labels=CHANNELS),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("A", "input", "image"),
            Port("B", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "src_channel": "a",
            "dst_channel": "a",
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        input_a = inputs.get("A")
        input_b = inputs.get("B")

        if not input_a or not input_a.is_valid:
            return {}
        if not input_b or not input_b.is_valid:
            return {"image": input_a}

        # Check if src == dst channel from the same source — identity if same texture
        p = self.parameters
        if p.get("src_channel") == p.get("dst_channel") and input_a.texture_id == input_b.texture_id:
            return {"image": input_a}

        # Use A's dimensions for output
        return {
            "image": TextureResult(
                texture_id=-1,
                width=input_a.width,
                height=input_a.height,
            ),
            "_effect_type": "copy_channel",
            "_effect_input": input_a,
            "_effect_params": {
                "input_b": input_b,
                # Use safe lookup with fallback defaults (prevent ValueError crash)
                "src_channel": CHANNELS.index(p.get("src_channel", "a")) if p.get("src_channel", "a") in CHANNELS else 3,  # default to 'a'
                "dst_channel": CHANNELS.index(p.get("dst_channel", "a")) if p.get("dst_channel", "a") in CHANNELS else 3,  # default to 'a'
            },
        }
