"""C14 lib1_tile_array 节点（暂存草案，未接线）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。

算法事实源：content_staging/library_v1/references/tile_array_ref.py
（count_x×count_y 格胞阵列；spacing 格胞内边距比例；offset 瓦片内环绕
相位；**空隙 alpha=0**、输出 bbox=输入画幅——显式布局契约，非 Crop
outside=repeat 换名）。源采样为 texel-block 最近邻 floor(u·W)（纹素 i
覆盖 [i/W,(i+1)/W)）——count 1×1+spacing 0+offset 0 → 精确恒等
（像素中心 round(u·(W−1)) 口径被手算对拍否决：u_c=x/W → 0.75x≠x）。
采样环绕（frac 正模）在输入画幅内 → tile_padding = 0。
layout bounds：布局上界=输出画幅（bbox 透传输入），空隙透明——
bbox/alpha 语义由 compute_output_bbox + manifest image + README 声明。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_tile_array"

_EFFECT_PARAM_NAMES = ("count_x", "count_y", "spacing", "offset_x", "offset_y")


class Lib1TileArrayNode(Node):
    """C14：count_x×count_y 瓦片阵列（spacing/offset；空隙 alpha=0）。"""

    node_type = EFFECT_KEY
    node_title = "Tile Array (Lib1)"
    header_color = "#4a9e5e"
    SEARCH_ALIASES = ["tile array", "平铺", "阵列", "repeat array",
                      "九宫格", "poster grid"]
    DESCRIPTION = ("count_x×count_y 瓦片阵列：spacing 格胞边距、offset "
                   "环绕相位；空隙 alpha=0（非 Crop repeat 换名）")
    TAGS = ["filter", "layout"]

    PARAM_DEFS = [
        ParamDef("count_x", "int", 3, min_val=1, max_val=16, step=1),
        ParamDef("count_y", "int", 3, min_val=1, max_val=16, step=1),
        ParamDef("spacing", "float", 0.1, min_val=0.0, max_val=0.9,
                 step=0.01),
        ParamDef("offset_x", "float", 0.0, min_val=-1.0, max_val=1.0,
                 step=0.01),
        ParamDef("offset_y", "float", 0.0, min_val=-1.0, max_val=1.0,
                 step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "count_x": 3,
            "count_y": 3,
            "spacing": 0.1,
            "offset_x": 0.0,
            "offset_y": 0.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        # layout bounds：阵列布局在输入画幅内完成（空隙透明）——输出
        # bbox = 输入画幅（接线期 bbox 语义与 BBox 契约由集成者细化）。
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """源采样环绕（frac 正模）永在输入画幅内 → 零 padding。"""
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
                "count_x": self.parameters.get("count_x", 3),
                "count_y": self.parameters.get("count_y", 3),
                "spacing": self.parameters.get("spacing", 0.1),
                "offset_x": self.parameters.get("offset_x", 0.0),
                "offset_y": self.parameters.get("offset_y", 0.0),
            },
        }
