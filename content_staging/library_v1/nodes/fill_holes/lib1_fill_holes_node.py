"""C19 lib1_fill_holes 节点（暂存草案，未接线；**全帧声明**）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。
**全帧声明子门未开：README 声明"全帧算法（连通域两遍扫描），生产接线
待集成者开全帧预算/取消子门；不宣称 tile-safe"。**

算法事实源：content_staging/library_v1/references/fill_holes_ref.py
（BG 连通域 BFS 标注；触边 BG = 真背景永不填；未触边且面积 ≤
max_area·画幅 的封闭 BG 连通域 = 孔 → 填充；Fill=α 置 1 / Holes=孔
mask；RGB 透传）。max_area=0 → 恒等锚（α 原样透传）。
**不等同 matte_cleanup**（erode/dilate/blur/clamp/invert/levels——
无连通域语义）。
"""

from __future__ import annotations

import math

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_fill_holes"
FH_CONNECTIVITIES = ["4", "8"]
FH_OUTPUT_MODES = ["Fill", "Holes"]

_EFFECT_PARAM_NAMES = ("connectivity", "max_area", "threshold", "output_mode")


class Lib1FillHolesNode(Node):
    """C19：matte 孔洞填充（connectivity/max_area/threshold/output_mode）。"""

    node_type = EFFECT_KEY
    node_title = "Fill Holes (Lib1)"
    header_color = "#6e9e4a"
    SEARCH_ALIASES = ["fill holes", "填孔", "孔洞填充", "hole filling",
                      "close holes"]
    DESCRIPTION = ("matte 孔洞填充：BG 连通域标注（4/8 连通）；触边背景"
                   "不误填；面积 ≤ max_area 的封闭孔填充（Fill/Holes）")
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("connectivity", "option", "4", labels=FH_CONNECTIVITIES),
        ParamDef("max_area", "float", 0.1, min_val=0.0, max_val=1.0,
                 step=0.01),
        ParamDef("threshold", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01),
        ParamDef("output_mode", "option", "Fill", labels=FH_OUTPUT_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "connectivity": "4",
            "max_area": 0.1,
            "threshold": 0.5,
            "output_mode": "Fill",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """全帧声明：连通域标注需整帧两遍扫描——无有限分块 halo。

        返回画幅对角线上界（1920×1080 基准 ceil+1=2204）仅为数值口径
        统一；**不宣称 tile-safe**（全帧算法预算/取消归 L04 全帧预算
        子门）。
        """
        return int(math.ceil(math.hypot(1920, 1080))) + 1

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
                "connectivity": self.parameters.get("connectivity", "4"),
                "max_area": self.parameters.get("max_area", 0.1),
                "threshold": self.parameters.get("threshold", 0.5),
                "output_mode": self.parameters.get("output_mode", "Fill"),
            },
        }
