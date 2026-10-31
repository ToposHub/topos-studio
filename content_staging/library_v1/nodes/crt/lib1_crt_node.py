"""C08 lib1_crt 节点（暂存草案，未接线）。

L16 批独立节点类草案：形态与 src/features/composite/nodes/lib1_nodes.py
逐字同构。生产接线由集成者做；接线前该 marker 属"未知 lib1_*"显式
拒绝——不发布。

算法事实源：content_staging/library_v1/references/crt_ref.py
（参照 plugins/CRT_Scanlines.tglsl：三角波扫描线 ×平方收窄 + 3px 周期
RGB 荫罩，两个乘性增益场；scan_strength=mask_strength=0 精确恒等）。
纯代数场：tile_padding = 0。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

EFFECT_KEY = "lib1_crt"
CRT_MODES = ["Scanlines", "RGB Grille", "Full"]

_EFFECT_PARAM_NAMES = ("mode", "scan_count", "scan_strength", "mask_strength")


class Lib1CrtNode(Node):
    """C08：CRT 扫描线 / RGB 荫罩（乘性增益场）。"""

    node_type = EFFECT_KEY
    node_title = "CRT (Lib1)"
    header_color = "#9e6e4a"
    SEARCH_ALIASES = ["crt", "扫描线", "scanlines", "荫罩", "aperture grille", "复古显像管"]
    DESCRIPTION = "CRT 扫描线 + RGB 荫罩条纹（亮度中性乘性增益；双强度 0 恒等）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "Full", labels=CRT_MODES),
        ParamDef("scan_count", "float", 480.0, min_val=120.0, max_val=1200.0, step=1.0),
        ParamDef("scan_strength", "float", 0.35, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("mask_strength", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "mode": "Full",
            "scan_count": 480.0,
            "scan_strength": 0.35,
            "mask_strength": 0.5,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """纯代数增益场（零采样）：零 padding。"""
        return 0

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
                "mode": self.parameters.get("mode", "Full"),
                "scan_count": self.parameters.get("scan_count", 480.0),
                "scan_strength": self.parameters.get("scan_strength", 0.35),
                "mask_strength": self.parameters.get("mask_strength", 0.5),
            },
        }
