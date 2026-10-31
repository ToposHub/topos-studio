"""C20 lib1_halation 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/halation_ref.py
（F06 multiscale_halation 共享核再导出，单一事实源；GPU 单 pass 等价
口径 = evaluate_degraded_single_scale（单尺度 12-tap 黄金角环 + 逐 tap
软膝门控）；节点参数表与 F06 oracle DEFAULTS 逐项一致；strength=0
恒等；**真多尺度多 pass 版待原生 renderer 接线**——README 声明）。
tile_padding = 扩散半径 R_px = W·(0.004+0.026·radius) 的 4K 上界近似。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_halation"

_EFFECT_PARAM_NAMES = ("strength", "radius", "red_scatter", "threshold")

# R_px = W·(0.004+0.026·radius) 在 W=4096、radius=1 时的上界 ≈123，取整余量
_RADIUS_4K_BASIS = 128


class Lib1HalationNode(Node):
    """C20：高光散射 halation（F06 共享核单 pass 口径）。"""

    node_type = EFFECT_KEY
    node_title = "Halation (Lib1)"
    header_color = "#9e6e4a"
    SEARCH_ALIASES = ["halation", "光晕", "highlight bloom", "高光散射", "halo"]
    DESCRIPTION = "软膝高光提取 + 12-tap 黄金角环扩散 + 红重着色（strength=0 恒等）"
    TAGS = ["filter", "glow"]

    PARAM_DEFS = [
        ParamDef("strength", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("radius", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("red_scatter", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("threshold", "float", 0.55, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "strength": 0.5,
            "radius": 0.5,
            "red_scatter": 0.5,
            "threshold": 0.55,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """扩散半径 R_px = W·(0.004+0.026·radius)：按 4K 宽上界近似，
        width-aware 精确换算属接线期。"""
        radius = max(0.0, min(float(params.get("radius", 0.5)), 1.0))
        return int(_RADIUS_4K_BASIS * (0.004 + 0.026 * radius)) + 2

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
                "strength": self.parameters.get("strength", 0.5),
                "radius": self.parameters.get("radius", 0.5),
                "red_scatter": self.parameters.get("red_scatter", 0.5),
                "threshold": self.parameters.get("threshold", 0.55),
            },
        }
