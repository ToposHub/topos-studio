"""C02 lib1_rgb_matrix 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构（node_type lib1_<slug> / PARAM_DEFS / marker dict
_effect_type/_effect_input/_effect_params / _tile_padding /
compute_output_bbox）。生产接线（base_evaluator dispatch 分支、GLSL、
NODE_REGISTRY）由集成者做（patches/proposal_l04_node_seams.md）；接线前
该 marker 在 base_evaluator 属"未知 lib1_*"显式拒绝——不发布。

算法事实源：content_staging/library_v1/references/rgb_matrix_ref.py
（out = M·rgb + bias，strength 混合；默认 identity 矩阵 + bias 0 恒等）。
纯逐像素线性变换：tile_padding = 0。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_rgb_matrix"

_IDENTITY_ROWS = (
    ("row0_r", 1.0, "row0_g", 0.0, "row0_b", 0.0),
    ("row1_r", 0.0, "row1_g", 1.0, "row1_b", 0.0),
    ("row2_r", 0.0, "row2_g", 0.0, "row2_b", 1.0),
)

_EFFECT_PARAM_NAMES = (
    "row0_r", "row0_g", "row0_b",
    "row1_r", "row1_g", "row1_b",
    "row2_r", "row2_g", "row2_b",
    "bias", "strength",
)


class Lib1RgbMatrixNode(Node):
    """C02：显式 3×3 通道矩阵 + 偏置（Composite 入口草案）。"""

    node_type = EFFECT_KEY
    node_title = "RGB Matrix (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["rgb matrix", "矩阵", "channel mixer", "color matrix", "通道串扰"]
    DESCRIPTION = "3×3 通道矩阵 + RGB 偏置 + 强度混合（默认恒等矩阵）"
    TAGS = ["filter", "color"]

    PARAM_DEFS = [
        ParamDef("row0_r", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row0_g", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row0_b", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row1_r", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row1_g", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row1_b", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row2_r", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row2_g", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("row2_b", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("bias", "vec3", (0.0, 0.0, 0.0)),
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        params: Dict[str, Any] = {}
        for row in _IDENTITY_ROWS:
            params[row[0]], params[row[2]], params[row[4]] = row[1], row[3], row[5]
        params.update({
            "bias": (0.0, 0.0, 0.0),
            "strength": 1.0,
        })
        self.parameters = params

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """纯逐像素线性变换：零 padding。"""
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        defaults = {
            "row0_r": 1.0, "row0_g": 0.0, "row0_b": 0.0,
            "row1_r": 0.0, "row1_g": 1.0, "row1_b": 0.0,
            "row2_r": 0.0, "row2_g": 0.0, "row2_b": 1.0,
            "bias": (0.0, 0.0, 0.0),
            "strength": 1.0,
        }
        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": EFFECT_KEY,
            "_effect_input": image_input,
            "_effect_params": {
                name: self.parameters.get(name, defaults[name])
                for name in _EFFECT_PARAM_NAMES
            },
        }
