"""
Invert Node - Inverts image channels.

Subtracts each channel from 1.0, producing a photographic negative effect.

HDR semantics (AUD-6909): the inversion is defined as ``1.0 - c`` with the
pivot FIXED at 1.0 and no clamping — this is the documented contract, not
an accident.  For HDR (float16/32) pixels with ``c > 1.0`` the result is a
NEGATIVE channel value.  Downstream consumers (screen/add merges, display
transforms) must define their handling of negative RGB; this node
deliberately does not clamp HDR data (project rule: no unjustified HDR
clamping).  Display-referred [0,1] content maps to [0,1] as usual.
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef


class InvertNode(Node):
    """Inverts image channels (1.0 - color)."""

    node_type = "invert"
    node_title = "Invert"
    header_color = "#a05050"  # Muted red

    HELP_TEXT = """\
**Invert（反相）**

反转图像通道（1.0 - color）。

**用途：** 创建负片效果、反转遮罩

**输入：**
- image：输入图像

**输出：**
- result：反相后的图像

**关键参数：**
- invert_r：反转红色通道
- invert_g：反转绿色通道
- invert_b：反转蓝色通道
- invert_a：反转 alpha 通道

**HDR 语义（AUD-6909）：**
反相固定以 1.0 为 pivot（1.0 - c），不做任何钳制。HDR（float16/32）
像素 c > 1.0 反相后为**负值**——这是文档化的设计行为：下游的
screen/add 混合与显示转换需自行定义对负 RGB 的处理；本节点遵守
"HDR 数据不得无理由钳制"的项目铁律。显示域 [0,1] 内容不受影响。
"""

    PARAM_DEFS = [
        ParamDef("invert_r", "bool", True),
        ParamDef("invert_g", "bool", True),
        ParamDef("invert_b", "bool", True),
        ParamDef("invert_a", "bool", False),
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
            "invert_r": True,
            "invert_g": True,
            "invert_b": True,
            "invert_a": False,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # If nothing is inverted, pass through
        if not any(self.parameters.get(f"invert_{c}", False) for c in "rgba"):
            return {"result": image}

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "invert",
            "_effect_input": image,
            "_effect_params": {
                "channels": [
                    1.0 if self.parameters.get("invert_r", True) else 0.0,
                    1.0 if self.parameters.get("invert_g", True) else 0.0,
                    1.0 if self.parameters.get("invert_b", True) else 0.0,
                    1.0 if self.parameters.get("invert_a", False) else 0.0,
                ],
            },
        }
