"""C03 lib1_despill 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线（dispatch/GLSL/registry）由集成者做；接线前该
marker 属"未知 lib1_*"显式拒绝——不发布。

算法事实源：content_staging/library_v1/references/despill_ref.py
（绿幕去溢色：Subtract=削峰减法 / Scale=乘性双向拉向 max(r,b)，
limit 封顶、strength=0 恒等；语义从 keyer despill 提取）。
纯逐像素：tile_padding = 0。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_despill"
DESPILL_METHODS = ["Subtract", "Scale"]

_EFFECT_PARAM_NAMES = ("method", "limit", "strength")


class Lib1DespillNode(Node):
    """C03：独立绿幕去溢色（Subtract/Scale，limit 封顶）。"""

    node_type = EFFECT_KEY
    node_title = "Despill (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["despill", "去溢色", "spill suppression", "绿溢", "unspill"]
    DESCRIPTION = "绿幕去溢色：Subtract 削峰 / Scale 乘性，limit 封顶（strength=0 恒等）"
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("method", "option", "Subtract", labels=DESPILL_METHODS),
        ParamDef("limit", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "method": "Subtract",
            "limit": 1.0,
            "strength": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """纯逐像素通道运算：零 padding。"""
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
                "method": self.parameters.get("method", "Subtract"),
                "limit": self.parameters.get("limit", 1.0),
                "strength": self.parameters.get("strength", 1.0),
            },
        }
