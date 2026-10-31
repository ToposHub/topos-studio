"""C18 lib1_distance_matte 节点（暂存草案，未接线；**全帧声明**）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。
**全帧声明子门未开：README 声明"全帧算法，生产接线待集成者开全帧
预算/取消子门；不宣称 tile-safe"。**

算法事实源：content_staging/library_v1/references/distance_matte_ref.py
（α → 精确有符号距离场：FG = alpha ≥ threshold；d = 到最近对类像素的
度量距离（Euclid/Chebyshev/Manhattan，像素单位）；s = ±d；dn =
clip(s/radius, −1, 1)；DistanceField/EdgeBand/Matte 三种 α 输出表示；
RGB 透传）。**不等同 erode/dilate**（度量距离开关，非结构元）。
"""

from __future__ import annotations

import math

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_distance_matte"
DM_METRICS = ["Euclid", "Chebyshev", "Manhattan"]
DM_OUTPUT_MODES = ["DistanceField", "EdgeBand", "Matte"]

_EFFECT_PARAM_NAMES = ("metric", "radius", "threshold", "output_mode")

# 全帧算法：距离上界 = 画幅对角线（1920×1080 基准 ceil+1）——有限上界
# 存在但算法按整帧计算，**不宣称 tile-safe**（全帧声明，README/manifest）。
_PAD_WIDTH_BASIS = 1920
_PAD_HEIGHT_BASIS = 1080


class Lib1DistanceMatteNode(Node):
    """C18：α→有符号距离场/边缘带/软重建（metric/radius/threshold）。"""

    node_type = EFFECT_KEY
    node_title = "Distance Matte (Lib1)"
    header_color = "#9e4a4a"
    SEARCH_ALIASES = ["distance matte", "距离场", "sdf", "edge band",
                      "距离遮罩"]
    DESCRIPTION = ("α→精确有符号距离场（metric/radius/threshold）；"
                   "DistanceField/EdgeBand/Matte 三种输出表示；RGB 透传")
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("metric", "option", "Euclid", labels=DM_METRICS),
        ParamDef("radius", "float", 8.0, min_val=1.0, max_val=64.0,
                 step=1.0),
        ParamDef("threshold", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01),
        ParamDef("output_mode", "option", "DistanceField",
                 labels=DM_OUTPUT_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "metric": "Euclid",
            "radius": 8.0,
            "threshold": 0.5,
            "output_mode": "DistanceField",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """全帧声明：距离上界=画幅对角线（1920×1080 基准 ceil+1=2204）。

        有限上界存在但**不宣称 tile-safe**——全帧算法预算/取消归 L04
        全帧预算子门（width-aware 精确换算属接线期）。
        """
        return int(math.ceil(math.hypot(_PAD_WIDTH_BASIS, _PAD_HEIGHT_BASIS))) + 1

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
                "metric": self.parameters.get("metric", "Euclid"),
                "radius": self.parameters.get("radius", 8.0),
                "threshold": self.parameters.get("threshold", 0.5),
                "output_mode": self.parameters.get("output_mode", "DistanceField"),
            },
        }
