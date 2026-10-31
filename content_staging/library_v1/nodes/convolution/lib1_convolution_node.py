"""C16 lib1_convolution 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/convolution_ref.py
（3×3 固定形状核 k00..k22 + Σ|K| 归一化（防御回退）+ bias；
恒等 = 中心核 k11=1 + bias 0（normalize 任意）——注意恒等核是中心
tap k11，k00=1 为 (−1,−1) 平移核）。
tile_padding = 1（3×3 邻域半径）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_convolution"

_EFFECT_PARAM_NAMES = (
    "k00", "k01", "k02", "k10", "k11", "k12", "k20", "k21", "k22",
    "normalize", "bias",
)

_IDENTITY_KERNEL: Dict[str, float] = {
    "k00": 0.0, "k01": 0.0, "k02": 0.0,
    "k10": 0.0, "k11": 1.0, "k12": 0.0,
    "k20": 0.0, "k21": 0.0, "k22": 0.0,
}


class Lib1ConvolutionNode(Node):
    """C16：3×3 自定义卷积核（归一化/偏置）。"""

    node_type = EFFECT_KEY
    node_title = "Convolution (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["convolution", "卷积", "kernel", "custom filter", "拉普拉斯"]
    DESCRIPTION = "3×3 自定义核 + Σ|K| 归一化 + 偏置（中心核 k11=1 恒等）"
    TAGS = ["filter", "convolve"]

    PARAM_DEFS = [
        ParamDef("k00", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k01", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k02", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k10", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k11", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k12", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k20", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k21", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("k22", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.05),
        ParamDef("normalize", "bool", False),
        ParamDef("bias", "float", 0.0, min_val=-4.0, max_val=4.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        params: Dict[str, Any] = dict(_IDENTITY_KERNEL)
        params.update({"normalize": False, "bias": 0.0})
        self.parameters = params

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """3×3 邻域：半径 1。"""
        return 1

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        fallback: Dict[str, Any] = dict(_IDENTITY_KERNEL)
        fallback.update({"normalize": False, "bias": 0.0})
        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": EFFECT_KEY,
            "_effect_input": image_input,
            "_effect_params": {
                name: self.parameters.get(name, fallback[name])
                for name in _EFFECT_PARAM_NAMES
            },
        }
