"""C21 lib1_frequency_separation 节点（暂存草案，未接线；**多输出**）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。
**多输出子门未开：README 声明"生产接线待集成者开多输出子门（首个
双彩色输出——mask_merge result+matte 是彩色+遮罩先例，非双彩色）"。**

算法事实源：content_staging/library_v1/references/frequency_separation_ref.py
（RGB 可分离 Box/Gaussian 低通 → low；high = RGB − low **带符号允许
负值、不 clamp**；low+high 在声明域精确重建输入——**不是 Sharpen
预设**；α 透传 low、high α=0；radius≤0 恒等锚 low=RGB/high=0）。

marker 多输出占位：两个输出端口各返回一个 TextureResult 占位
（texture_id=-1），如实反映"多输出子门未开"。
"""

from __future__ import annotations

import math

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_frequency_separation"
FS_FILTERS = ["Box", "Gaussian"]
FS_MODES = ["LowHigh", "Reconstruct"]

_EFFECT_PARAM_NAMES = ("radius", "filter", "mode")


class Lib1FrequencySeparationNode(Node):
    """C21：频率分离（low/high 双彩色输出；radius/filter/mode）。"""

    node_type = EFFECT_KEY
    node_title = "Frequency Separation (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["frequency separation", "频率分离", "high pass",
                      "低高频", "retouch"]
    DESCRIPTION = ("low/high 双彩色输出：可分离 Box/Gaussian 低通；"
                   "high=RGB−low 带符号（允许负值）；low+high 精确重建"
                   "（非 Sharpen 预设）")
    TAGS = ["filter", "retouch"]

    PARAM_DEFS = [
        ParamDef("radius", "float", 4.0, min_val=0.0, max_val=64.0,
                 step=1.0),
        ParamDef("filter", "option", "Box", labels=FS_FILTERS),
        ParamDef("mode", "option", "LowHigh", labels=FS_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("low", "output", "image"),
                        Port("high", "output", "image")]
        self.parameters = {
            "radius": 4.0,
            "filter": "Box",
            "mode": "LowHigh",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """核半宽上界：Box k=round(radius) ≤ radius；Gaussian 半宽
        ceil(3σ)=ceil(radius) ≤ radius → padding = ceil(radius)+1。"""
        radius = max(0.0, float(params.get("radius", 4.0)))
        return int(math.ceil(radius)) + 1

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        # 多输出 marker 占位：两个输出端口各一个 TextureResult 占位
        #（texture_id=-1；生产双纹理接线由集成者开多输出子门后做）
        return {
            "low": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "high": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": EFFECT_KEY,
            "_effect_input": image_input,
            "_effect_params": {
                "radius": self.parameters.get("radius", 4.0),
                "filter": self.parameters.get("filter", "Box"),
                "mode": self.parameters.get("mode", "LowHigh"),
            },
        }
