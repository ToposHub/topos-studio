"""C12 lib1_vector_displace 节点（暂存草案，未接线；**多输入**）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。
**多输入子门未开：README 声明"生产接线待集成者开多输入子门"。**

算法事实源：content_staging/library_v1/references/vector_displace_ref.py
（向量图驱动位移：dx = (vx − neutral)·scale、dy = (vy − neutral)·scale；
units=UV 时按画幅比例换算；可选 mask 按像素乘性门控位移量；
clamp-to-edge 双线性采样，像素中心 = 整数索引）。

端口角色契约（**不可串线**）：image = 被位移的内容（_effect_input）；
vector = 位移向量图（_effect_input_map，R=x / G=y）；mask = 位移门控
（_effect_mask）。marker 多输入键为接线方 dispatcher 契约（base_node
marker 协议允许的私有键）。GENERIC_MASK_MIX = False：mask 是本算法
语义端口（乘性门控），不吃通用 mask/mix 后处理。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_vector_displace"
DISPLACE_UNITS = ["Pixels", "UV"]

_EFFECT_PARAM_NAMES = ("scale", "neutral", "units")


class Lib1VectorDisplaceNode(Node):
    """C12：向量图驱动位移（scale/neutral/units + 可选 mask 门控）。"""

    node_type = EFFECT_KEY
    node_title = "Vector Displace (Lib1)"
    header_color = "#7e6a9e"
    SEARCH_ALIASES = ["vector displace", "向量位移", "displacement map",
                      "idistort", "warp map"]
    DESCRIPTION = ("向量图（R=x/G=y）驱动位移：scale/neutral/units；"
                   "可选 mask 乘性门控位移量（vector=neutral 精确恒等）")
    TAGS = ["filter", "distort"]
    GENERIC_MASK_MIX = False   # mask 为本算法语义端口（乘性门控）

    PARAM_DEFS = [
        ParamDef("scale", "float", 50.0, min_val=0.0, max_val=1000.0,
                 step=1.0),
        ParamDef("neutral", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01),
        ParamDef("units", "option", "Pixels", labels=DISPLACE_UNITS),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("vector", "input", "image"),
            Port("mask", "input", "image"),
        ]
        # mask 端口 required=False（可选门控）；Port 无 required 字段，
        # 语义声明在 manifest ports + README（连线缺失 = 不门控）。
        self.inputs[2].max_connections = 1
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "scale": 50.0,
            "neutral": 0.5,
            "units": "Pixels",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """位移上界：|v − neutral| ≤ 1 → 每轴 ≤ scale（Pixels）/ scale·画幅
        （UV，1920 宽基准）。mask∈[0,1] 声明域内的乘性门控不放大上界。"""
        scale = max(0.0, float(params.get("scale", 50.0)))
        if str(params.get("units", "Pixels")) == "UV":
            scale *= 1920.0
        return int(scale) + 1

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        vector_input = inputs.get("vector")
        if image_input is None or not image_input.is_valid:
            return {}
        if vector_input is None or not vector_input.is_valid:
            return {}   # vector 缺失 = 位移未定义 → no-op（不静默透传）
        marker: Dict[str, Any] = {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": EFFECT_KEY,
            "_effect_input": image_input,
            "_effect_input_map": vector_input,
            "_effect_params": {
                "scale": self.parameters.get("scale", 50.0),
                "neutral": self.parameters.get("neutral", 0.5),
                "units": self.parameters.get("units", "Pixels"),
            },
        }
        mask_input = inputs.get("mask")
        if mask_input is not None and mask_input.is_valid:
            marker["_effect_mask"] = mask_input
        return marker
