"""C06 lib1_halftone 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/halftone_ref.py
（F13 print_registration 共享核再导出，单一事实源——三版网点/套印；
节点参数表与 F13 oracle DEFAULTS 逐项一致；ink_amount=0 恒等）。
网点几何位置驱动（pitch 8px/角度 0°/15°/30°）：tile_padding = 0。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_halftone"
SEPARATION_MODES = ["Luma", "CMY approx"]

_EFFECT_PARAM_NAMES = ("cmyk_separation", "reg_offset_x", "reg_offset_y",
                       "paper_base", "ink_amount")


class Lib1HalftoneNode(Node):
    """C06：CMY 网点套印（F13 共享核的 Composite 入口）。"""

    node_type = EFFECT_KEY
    node_title = "Halftone (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["halftone", "网点", "半调", "print registration", "套印"]
    DESCRIPTION = "三版 CMY 网点 + 套印错位（纸反射域艺术效果，显式声明）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("cmyk_separation", "option", "CMY approx", labels=SEPARATION_MODES),
        ParamDef("reg_offset_x", "float", 1.0, min_val=-4.0, max_val=4.0, step=0.01),
        ParamDef("reg_offset_y", "float", 0.0, min_val=-4.0, max_val=4.0, step=0.01),
        ParamDef("paper_base", "float", 0.9, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("ink_amount", "float", 0.8, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "cmyk_separation": "CMY approx",
            "reg_offset_x": 1.0,
            "reg_offset_y": 0.0,
            "paper_base": 0.9,
            "ink_amount": 0.8,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """三版采样位偏移 ±reg_offset（F13 口径，≤4px）：padding = 最大偏移。"""
        ox = abs(float(params.get("reg_offset_x", 1.0)))
        oy = abs(float(params.get("reg_offset_y", 0.0)))
        return int(max(ox, oy)) + 1

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
                "cmyk_separation": self.parameters.get("cmyk_separation", "CMY approx"),
                "reg_offset_x": self.parameters.get("reg_offset_x", 1.0),
                "reg_offset_y": self.parameters.get("reg_offset_y", 0.0),
                "paper_base": self.parameters.get("paper_base", 0.9),
                "ink_amount": self.parameters.get("ink_amount", 0.8),
            },
        }
