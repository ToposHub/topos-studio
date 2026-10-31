"""C07 lib1_hatching 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/hatching_ref.py
（F14 共享核，单一事实源直接复用——亮度驱动双向排线/墨密度/纸底；
节点参数表与 hatching_ref.DEFAULTS 逐项一致；amount=0 恒等）。
程序几何（零采样）：tile_padding = 0。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_hatching"
HATCHING_LAYERS = ["1", "2"]

_EFFECT_PARAM_NAMES = ("line_spacing", "angle", "layers", "ink_color",
                       "paper", "amount")


class Lib1HatchingNode(Node):
    """C07：亮度驱动排线（F14 共享核的 Composite 入口）。"""

    node_type = EFFECT_KEY
    node_title = "Hatching (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["hatching", "排线", "crosshatch", "钢笔", "sketch lines"]
    DESCRIPTION = "亮度驱动排线/交叉影线（层 1/2，墨色+纸底）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("line_spacing", "float", 8.0, min_val=3.0, max_val=24.0, step=0.5),
        ParamDef("angle", "float", 45.0, min_val=-90.0, max_val=90.0, step=1.0),
        ParamDef("layers", "option", "2", labels=HATCHING_LAYERS),
        ParamDef("ink_color", "color", (0.05, 0.05, 0.08)),
        ParamDef("paper", "float", 0.9, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "line_spacing": 8.0,
            "angle": 45.0,
            "layers": "2",
            "ink_color": (0.05, 0.05, 0.08),
            "paper": 0.9,
            "amount": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """程序排线几何（零采样、像素比例驱动）：零 padding。"""
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
                "line_spacing": self.parameters.get("line_spacing", 8.0),
                "angle": self.parameters.get("angle", 45.0),
                "layers": self.parameters.get("layers", "2"),
                "ink_color": self.parameters.get("ink_color", (0.05, 0.05, 0.08)),
                "paper": self.parameters.get("paper", 0.9),
                "amount": self.parameters.get("amount", 1.0),
            },
        }
