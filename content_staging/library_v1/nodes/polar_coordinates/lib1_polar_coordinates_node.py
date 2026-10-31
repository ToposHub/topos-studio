"""C11 lib1_polar_coordinates 节点（暂存草案，未接线）。

L17 批独立节点类草案：形态与 content_staging/library_v1/nodes/despill/
lib1_despill_node.py 逐字同构（marker 协议）。生产接线（dispatch/GLSL/
registry）由集成者做；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。

算法事实源：content_staging/library_v1/references/polar_coordinates_ref.py
（Cartesian↔polar UV 重映射：输出像素的极坐标 (θ, r) 映射到输入
(u, v)——x 轴=角度（angle_span 度角域）、y 轴=半径（r_max =
radius_scale·min(W,H)/2）；两方向为同一映射 F 的正/逆，位置级
F∘G = id（图像级两趟往返另含中间重采样插值误差，见 README））。
像素中心 = 整数索引（与 C10 kaleidoscope 同构）。

采样域声明：输出像素可采样输入任意位置（整幅角向缠绕）——非 tile-safe，
tile_padding 按对角线上界（1920×1080 基准；width-aware 属接线期）。
接缝（θ=±π）/中心奇点（r→0）见 README。
"""

from __future__ import annotations

import math

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_polar_coordinates"
POLAR_MODES = ["CartesianToPolar", "PolarToCartesian"]
POLAR_FILTERS = ["Nearest", "Linear"]

_EFFECT_PARAM_NAMES = ("mode", "center", "angle_span", "radius_scale", "filter")

# tile_padding 上界基准：ToPolar 方向输出像素可采样输入任意位置
# （v = r/r_max 覆盖整幅高度、u 覆盖整幅宽度）→ 位移上界 = 画幅对角线。
_PAD_WIDTH_BASIS = 1920
_PAD_HEIGHT_BASIS = 1080


class Lib1PolarCoordinatesNode(Node):
    """C11：Cartesian↔polar UV 重映射（中心/角度/半径/滤波）。"""

    node_type = EFFECT_KEY
    node_title = "Polar Coordinates (Lib1)"
    header_color = "#4a7e9e"
    SEARCH_ALIASES = ["polar", "极坐标", "rect to polar", "polar to rect",
                      "坐标变换"]
    DESCRIPTION = ("Cartesian↔polar UV 重映射：角度→x 轴、半径→y 轴；"
                   "两方向互为逆变换（angle_span/radius_scale/center）")
    TAGS = ["filter", "transform"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "CartesianToPolar", labels=POLAR_MODES),
        ParamDef("center", "vec2", (0.5, 0.5), min_val=0.0, max_val=1.0,
                 step=0.01),
        ParamDef("angle_span", "float", 360.0, min_val=1.0, max_val=360.0,
                 step=1.0),
        ParamDef("radius_scale", "float", 1.0, min_val=0.1, max_val=4.0,
                 step=0.01),
        ParamDef("filter", "option", "Nearest", labels=POLAR_FILTERS),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "mode": "CartesianToPolar",
            "center": (0.5, 0.5),
            "angle_span": 360.0,
            "radius_scale": 1.0,
            "filter": "Nearest",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """输出像素可采样输入任意位置（整幅角向缠绕）→ 对角线上界。

        1920×1080 基准：hypot(1920, 1080) ≈ 2202.9 → ceil+1 = 2204
        （严格大于对角线的声明上界；width-aware 精确换算属接线期；
        极坐标映射本质非 tile-safe，分块需整幅 halo）。
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
                "mode": self.parameters.get("mode", "CartesianToPolar"),
                "center": self.parameters.get("center", (0.5, 0.5)),
                "angle_span": self.parameters.get("angle_span", 360.0),
                "radius_scale": self.parameters.get("radius_scale", 1.0),
                "filter": self.parameters.get("filter", "Nearest"),
            },
        }
