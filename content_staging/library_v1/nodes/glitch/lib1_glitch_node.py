"""C09 lib1_glitch 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/glitch_ref.py
（参照 plugins/Glitch_Blocks.tglsl + seed 进 hash 的静态确定性扩展；
corruption=0 且 rgb_split=0 精确恒等；动态逐帧模式过 frame/seed 子门
后才开——L00 台账 C09 行）。
tile_padding：水平位移上界 = split 12px + 12.5% 画幅宽·shift_amount
（width-aware 精确换算属接线期，先按 1920 基准上界声明）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_glitch"

_EFFECT_PARAM_NAMES = ("band_height", "shift_amount", "rgb_split",
                       "corruption", "seed")

_MAX_SPLIT_PX = 12          # rgb_split·12·(0.25+0.75) ≤ 12px
_SHIFT_WIDTH_BASIS = 1920   # shift 位移按画幅宽 12.5%·shift_amount 的上界基准


class Lib1GlitchNode(Node):
    """C09：条带位移 + RGB 分离（seed 静态确定性）。"""

    node_type = EFFECT_KEY
    node_title = "Glitch (Lib1)"
    header_color = "#9e4a6e"
    SEARCH_ALIASES = ["glitch", "故障", "datamosh", "signal error", "条带错位"]
    DESCRIPTION = "hash 激活条带水平位移 + RGB 分离（seed 确定性；corruption=0+split=0 恒等）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("band_height", "float", 24.0, min_val=4.0, max_val=128.0, step=1.0),
        ParamDef("shift_amount", "float", 0.35, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("rgb_split", "float", 0.3, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("corruption", "float", 0.35, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("seed", "float", 0.0, min_val=0.0, max_val=100.0, step=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "band_height": 24.0,
            "shift_amount": 0.35,
            "rgb_split": 0.3,
            "corruption": 0.35,
            "seed": 0.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """水平采样位移上界：split ≤ 12px + 0.125·画幅宽·shift_amount。"""
        shift = max(0.0, float(params.get("shift_amount", 0.35)))
        return _MAX_SPLIT_PX + int(0.125 * _SHIFT_WIDTH_BASIS * shift) + 1

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
                "band_height": self.parameters.get("band_height", 24.0),
                "shift_amount": self.parameters.get("shift_amount", 0.35),
                "rgb_split": self.parameters.get("rgb_split", 0.3),
                "corruption": self.parameters.get("corruption", 0.35),
                "seed": self.parameters.get("seed", 0.0),
            },
        }
