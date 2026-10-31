"""C05 lib1_mosaic 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/mosaic_ref.py
（参照 plugins/Pixel_Blocks.tglsl：块中心采样 + scene-linear 均匀
阶梯量化 + Bayer 4x4；block_px=1 + dither off + levels=32 为近似恒等
——量化阶宽 1/32，精确恒等需 levels→∞，README/known_limits 声明）。
tile_padding = ceil(block/2)（块中心采样距块内像素的最大距离上界）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_mosaic"

_EFFECT_PARAM_NAMES = ("block_px", "levels", "dither")


class Lib1MosaicNode(Node):
    """C05：像素块马赛克 + 线性阶梯量化 + Bayer 抖动。"""

    node_type = EFFECT_KEY
    node_title = "Mosaic (Lib1)"
    header_color = "#9e6e4a"
    SEARCH_ALIASES = ["mosaic", "马赛克", "pixelate", "pixel blocks", "量化"]
    DESCRIPTION = "块中心采样马赛克 + scene-linear 阶梯量化 + 4x4 Bayer 抖动"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("block_px", "float", 8.0, min_val=1.0, max_val=64.0, step=1.0),
        ParamDef("levels", "float", 8.0, min_val=2.0, max_val=32.0, step=1.0),
        ParamDef("dither", "bool", False),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "block_px": 8.0,
            "levels": 8.0,
            "dither": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """块中心采样：输出像素读同块中心，最大距离 ceil(block/2)。"""
        block = max(float(params.get("block_px", 8.0)), 1.0)
        return int(block // 2) + int(block % 2)

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
                "block_px": self.parameters.get("block_px", 8.0),
                "levels": self.parameters.get("levels", 8.0),
                "dither": self.parameters.get("dither", False),
            },
        }
