"""C15 lib1_median 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/median_ref.py
（F19 median_rank 共享核再导出，单一事实源——小核中值/百分位，
edge 边界钳制；节点参数表与 F19 oracle DEFAULTS 逐项一致；
amount=0 恒等）。
tile_padding = 核半径（3x3→1、5x5→2）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_median"
MEDIAN_KERNEL_SIZES = ["3x3", "5x5"]
MEDIAN_CHANNEL_MODES = ["RGB", "Luma"]

_EFFECT_PARAM_NAMES = ("kernel_size", "percentile", "channel_mode", "amount")


class Lib1MedianNode(Node):
    """C15：小核中值/百分位滤波（F19 共享核的 Composite 入口）。"""

    node_type = EFFECT_KEY
    node_title = "Median (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["median", "中值", "median rank", "percentile", "去噪"]
    DESCRIPTION = "3x3/5x5 中值/百分位（RGB/Luma 档；amount=0 恒等）"
    TAGS = ["filter", "denoise"]

    PARAM_DEFS = [
        ParamDef("kernel_size", "option", "3x3", labels=MEDIAN_KERNEL_SIZES),
        ParamDef("percentile", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("channel_mode", "option", "RGB", labels=MEDIAN_CHANNEL_MODES),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "kernel_size": "3x3",
            "percentile": 0.5,
            "channel_mode": "RGB",
            "amount": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """邻域半径：3x3 → 1、5x5 → 2。"""
        return 2 if params.get("kernel_size", "3x3") == "5x5" else 1

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
                "kernel_size": self.parameters.get("kernel_size", "3x3"),
                "percentile": self.parameters.get("percentile", 0.5),
                "channel_mode": self.parameters.get("channel_mode", "RGB"),
                "amount": self.parameters.get("amount", 1.0),
            },
        }
