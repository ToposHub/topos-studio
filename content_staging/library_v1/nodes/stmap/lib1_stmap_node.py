"""C13 lib1_stmap 节点（暂存草案，未接线；**多输入**）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。
**多输入子门未开：README 声明"生产接线待集成者开子门（与 C12 同子门）"。**

算法事实源：content_staging/library_v1/references/stmap_ref.py
（绝对 UV 重映射：map 的 UV 是**绝对采样坐标**（非增量，与 C12 相对
位移区分）；origin 定 UV 原点角、invalid 定域外策略（Clamp 边缘钳制 /
Passthrough 原位透传）、filter 定 Nearest/Linear）。像素中心 = 整数索引。

端口角色契约（**不可串线**）：image = 被重映射内容（_effect_input）；
map = 绝对 UV 图（_effect_input_map）。GENERIC_MASK_MIX 默认（基类
CP-044 通用 mask 附加，与 despill/glitch 草案同构；算法本身无 mask
参数——工作单未列 mask）。
"""

from __future__ import annotations

import math

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_stmap"
STMAP_CHANNELS = ["RG", "BA"]
STMAP_ORIGINS = ["BottomLeft", "TopLeft"]
STMAP_INVALID = ["Clamp", "Passthrough"]
STMAP_FILTERS = ["Nearest", "Linear"]

_EFFECT_PARAM_NAMES = ("channels", "origin", "invalid", "filter")

# tile_padding 上界基准：绝对 UV 采样可指向画幅任意位置（与 C11 同理，
# 本质非 tile-safe）→ 对角线上界。
_PAD_WIDTH_BASIS = 1920
_PAD_HEIGHT_BASIS = 1080


class Lib1StmapNode(Node):
    """C13：绝对 UV map 重映射（channels/origin/invalid/filter）。"""

    node_type = EFFECT_KEY
    node_title = "STMap (Lib1)"
    header_color = "#9e8a4a"
    SEARCH_ALIASES = ["stmap", "uv map", "uv 重映射", "absolute uv",
                      "坐标贴图"]
    DESCRIPTION = ("绝对 UV map 重采样：channels=RG/BA、origin 定原点角、"
                   "invalid 定域外策略（Clamp/Passthrough）、Nearest/Linear")
    TAGS = ["filter", "distort"]

    PARAM_DEFS = [
        ParamDef("channels", "option", "RG", labels=STMAP_CHANNELS),
        ParamDef("origin", "option", "BottomLeft", labels=STMAP_ORIGINS),
        ParamDef("invalid", "option", "Clamp", labels=STMAP_INVALID),
        ParamDef("filter", "option", "Nearest", labels=STMAP_FILTERS),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("map", "input", "image"),
        ]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "channels": "RG",
            "origin": "BottomLeft",
            "invalid": "Clamp",
            "filter": "Nearest",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """绝对 UV 可采样画幅任意位置 → 对角线上界（ceil+1，严格大于）。

        1920×1080 基准：hypot ≈ 2202.9 → 2204（width-aware 属接线期；
        本质非 tile-safe，分块需整幅 halo）。
        """
        return int(math.ceil(math.hypot(_PAD_WIDTH_BASIS, _PAD_HEIGHT_BASIS))) + 1

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        map_input = inputs.get("map")
        if image_input is None or not image_input.is_valid:
            return {}
        if map_input is None or not map_input.is_valid:
            return {}   # map 缺失 = 重映射未定义 → no-op（不静默透传）
        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": EFFECT_KEY,
            "_effect_input": image_input,
            "_effect_input_map": map_input,
            "_effect_params": {
                "channels": self.parameters.get("channels", "RG"),
                "origin": self.parameters.get("origin", "BottomLeft"),
                "invalid": self.parameters.get("invalid", "Clamp"),
                "filter": self.parameters.get("filter", "Nearest"),
            },
        }
