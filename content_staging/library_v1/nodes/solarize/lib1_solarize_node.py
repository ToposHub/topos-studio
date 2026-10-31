"""C04 lib1_solarize 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/solarize_ref.py
（F17 共享核，单一事实源直接复用——阈值折返/软过渡/通道档/mix）。
节点参数表与 solarize_ref.DEFAULTS 逐项一致。
纯逐像素：tile_padding = 0；amount=0 恒等。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_solarize"
SOLARIZE_CHANNEL_MODES = ["Luma", "RGB"]

_EFFECT_PARAM_NAMES = ("threshold", "softness", "channel_mode", "amount")


class Lib1SolarizeNode(Node):
    """C04：阈值折返太阳化（F17 共享核的 Composite 入口）。"""

    node_type = EFFECT_KEY
    node_title = "Solarize (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["solarize", "太阳化", "折返", "foldback", "tone invert"]
    DESCRIPTION = "亮度/RGB 阈值折返太阳化（软过渡，线性域无 clamp）"
    TAGS = ["filter", "color"]

    PARAM_DEFS = [
        ParamDef("threshold", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("softness", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("channel_mode", "option", "Luma", labels=SOLARIZE_CHANNEL_MODES),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "threshold": 1.0,
            "softness": 0.0,
            "channel_mode": "Luma",
            "amount": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """纯逐像素折返：零 padding。"""
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": EFFECT_KEY,
            "_effect_input": image_input,
            "_effect_params": {
                "threshold": self.parameters.get("threshold", 1.0),
                "softness": self.parameters.get("softness", 0.0),
                "channel_mode": self.parameters.get("channel_mode", "Luma"),
                "amount": self.parameters.get("amount", 1.0),
            },
        }
