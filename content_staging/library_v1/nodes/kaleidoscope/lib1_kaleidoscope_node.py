"""C10 lib1_kaleidoscope 节点（暂存草案，未接线）。

**参数名修正（F16/F19 先例）**：混合参数 mix 是 GLSL 保留字（shader_compiler AUD-0832 黑名单），L17 集成时必炸——提前改名 amount。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/kaleidoscope_ref.py
（扇区折叠镜像采样：θ mod (2π/N) 镜像回基准扇区，半径保持、方位折叠；
amount=0 精确恒等；Kaleido_Mirror 无扇区数旋转语义——差量新实现）。
tile_padding：折叠位移 ≤ 2·r_max ≤ 画幅对角线（宽度感知换算属接线期，
按 2048×2048 上限 ≈2897 取整声明）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_kaleidoscope"

_EFFECT_PARAM_NAMES = ("sectors", "rotation", "center", "amount")

_DIAGONAL_2K = 2897  # hypot(2048, 2048) ≈ 2896.3 → 折叠位移上界取整


class Lib1KaleidoscopeNode(Node):
    """C10：扇区数旋转万花筒（折叠镜像采样）。"""

    node_type = EFFECT_KEY
    node_title = "Kaleidoscope (Lib1)"
    header_color = "#6e4a9e"
    SEARCH_ALIASES = ["kaleidoscope", "万花筒", "kaleido", "mirror sectors", "扇区镜像"]
    DESCRIPTION = "N 扇区折叠镜像万花筒（扇区数/旋转/中心/混合；amount=0 恒等）"
    TAGS = ["filter", "warp"]

    PARAM_DEFS = [
        ParamDef("sectors", "int", 6, min_val=3.0, max_val=12.0, step=1.0),
        ParamDef("rotation", "float", 0.0, min_val=-180.0, max_val=180.0, step=1.0),
        ParamDef("center", "vec2", (0.5, 0.5)),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "sectors": 6,
            "rotation": 0.0,
            "center": (0.5, 0.5),
            "amount": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """折叠位移 ≤ 2·r_max ≤ 画幅对角线（2048² 上界，宽度感知属接线期）。"""
        return _DIAGONAL_2K

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
                "sectors": self.parameters.get("sectors", 6),
                "rotation": self.parameters.get("rotation", 0.0),
                "center": self.parameters.get("center", (0.5, 0.5)),
                "mix": self.parameters.get("amount", 1.0),
            },
        }
