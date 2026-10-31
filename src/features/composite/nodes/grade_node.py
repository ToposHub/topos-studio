"""
Grade Node - Color correction (NUKE-style).

Adjusts lift, gamma, gain, and offset for R/G/B channels independently.
Mathematical formula: output = (input * gain + offset) ^ (1/gamma) + lift

Upgraded features:
- Blackpoint/whitepoint controls
- Multiply/Add operations
- Channel masking (R/G/B/A selector)
- Mask input support
- Mix control (blend between original and graded)
- HDR-safe operations (no violent clamp)
- Negative gamma value handling
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef


class GradeNode(Node):
    """Color correction node with lift/gamma/gain/offset controls."""

    HELP_TEXT = """专业色彩校正节点。独立的 R/G/B lift/gamma/gain/offset 控制。
输入：image（图像）、mask（可选）
输出：校正后的图像（RGBA）
参数：
  - lift_r/g/b: 暗部偏移（-1.0 到 1.0）
  - gamma_r/g/b: 中间调伽马（实现按正 gamma 求幂，非正值会被下钳到
    0.001——AUD-6808：帮助文本原声称支持负 gamma，与派发着色器不符）
  - gain_r/g/b: 亮部增益（0.0 到 4.0）
  - offset_r/g/b: 全局偏移（-1.0 到 1.0）
  - blackpoint, whitepoint: 黑/白点
  - multiply, add: 乘法/加法操作
  - mix: 混合比例（0.0-1.0，原图与校正图混合）
  - mask_channels: 通道掩码（着色器仅对 rgb 三通道生效，alpha 不参与
    ——AUD-6808：帮助文本原声称支持 rgba，与派发着色器不符）
公式：output = (input × gain + offset) ^ (1/gamma) + lift
注意：
  - HDR 安全：不会暴力钳制，支持 >1.0 的值
  - mask_channels: 指定应用校正的通道（rgb/rgba 等）
  - 适合调色、曝光校正、色彩匹配"""
    DISPLAY_NAME = "Grade"

    node_type = "grade"
    node_title = "Grade"
    header_color = "#b87333"  # Copper

    # Metadata
    SEARCH_ALIASES = ["grade", "cc", "colorcorrect", "调色", "校色", "色彩", "lift", "gamma", "gain"]
    DESCRIPTION = "NUKE风格的色彩校正节点"
    TAGS = ["color", "correction"]

    PARAM_DEFS = [
        # Primary controls
        ParamDef("lift_r", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("lift_g", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("lift_b", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("gamma_r", "float", 1.0, min_val=-4.0, max_val=4.0, step=0.01),
        ParamDef("gamma_g", "float", 1.0, min_val=-4.0, max_val=4.0, step=0.01),
        ParamDef("gamma_b", "float", 1.0, min_val=-4.0, max_val=4.0, step=0.01),
        ParamDef("gain_r", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("gain_g", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("gain_b", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("offset_r", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("offset_g", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("offset_b", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        # Blackpoint/Whitepoint
        ParamDef("blackpoint", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("whitepoint", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        # Multiply/Add
        ParamDef("multiply", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("add", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        # Channel mask
        ParamDef("mask_channels", "string", "rgb"),
        # Mix control
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),  # Optional mask input
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "lift_r": 0.0, "lift_g": 0.0, "lift_b": 0.0,
            "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
            "gain_r": 1.0, "gain_g": 1.0, "gain_b": 1.0,
            "offset_r": 0.0, "offset_g": 0.0, "offset_b": 0.0,
            "blackpoint": 0.0,
            "whitepoint": 1.0,
            "multiply": 1.0,
            "add": 0.0,
            "mask_channels": "rgb",
            "mix": 1.0,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        mask_image = inputs.get("mask")  # Optional mask input

        # Check if any parameter actually differs from default
        has_changes = any(
            self.parameters.get(k, v) != v
            for k, v in {
                "lift_r": 0.0, "lift_g": 0.0, "lift_b": 0.0,
                "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
                "gain_r": 1.0, "gain_g": 1.0, "gain_b": 1.0,
                "offset_r": 0.0, "offset_g": 0.0, "offset_b": 0.0,
                "blackpoint": 0.0,
                "whitepoint": 1.0,
                "multiply": 1.0,
                "add": 0.0,
                "mix": 1.0,
            }.items()
        )

        if not has_changes:
            return {"result": image}

        # Build channel mask (which channels to apply grade to)
        mask_channels = self.parameters.get("mask_channels", "rgb")
        channel_mask = [
            1.0 if "r" in mask_channels else 0.0,
            1.0 if "g" in mask_channels else 0.0,
            1.0 if "b" in mask_channels else 0.0,
        ]

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "grade",
            "_effect_input": image,
            "_effect_mask": mask_image,  # Optional mask
            "_effect_params": {
                "lift": [self.parameters.get(f"lift_{c}", 0.0) for c in "rgb"],
                "gamma": [self.parameters.get(f"gamma_{c}", 1.0) for c in "rgb"],
                "gain": [self.parameters.get(f"gain_{c}", 1.0) for c in "rgb"],
                "offset": [self.parameters.get(f"offset_{c}", 0.0) for c in "rgb"],
                "blackpoint": self.parameters.get("blackpoint", 0.0),
                "whitepoint": self.parameters.get("whitepoint", 1.0),
                "multiply": self.parameters.get("multiply", 1.0),
                "add": self.parameters.get("add", 0.0),
                "channel_mask": channel_mask,
                "mix": self.parameters.get("mix", 1.0),
            },
        }
