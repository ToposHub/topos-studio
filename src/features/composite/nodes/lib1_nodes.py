"""LIB1 内容库节点（计划 L / L04：C01 gradient_map + C17 edge_extend）。

事实源说明：本模块是**发布实现**；暂存区组件草案
（content_staging/library_v1/nodes/*/lib1_*.py）再导出本模块，组件测试
与生产行为同一实现（禁双份漂移）。marker 协议与 sharpen_node 等
单输入效果一致（base_node.py 头注释）；dispatch 分支在
base_evaluator._dispatch_effect 的 lib1_ 段——未知 lib1_* marker 被
显式拒绝（计划红线：无实现精确报错，不得警告后透传冒充成功）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.features.composite.nodes.base_node import Node, ParamDef, Port, TextureResult

INTERP_MODES = ["Linear", "Smooth"]
OUTPUT_MODES = ["color_only", "premultiplied"]


class Lib1GradientMapNode(Node):
    """C01：多停靠亮度映射（F16 共享核的 Composite 入口）。"""

    node_type = "lib1_gradient_map"
    node_title = "Gradient Map (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["gradient map", "渐变映射", "gradient", "duotone", "tone map"]
    DESCRIPTION = "亮度驱动的多停靠点颜色映射（4 停靠，线性/平滑插值）"
    TAGS = ["filter", "color"]

    PARAM_DEFS = [
        ParamDef("stop_0", "color", (0.02, 0.02, 0.05)),
        ParamDef("stop_1", "color", (0.10, 0.20, 0.45)),
        ParamDef("stop_2", "color", (0.90, 0.55, 0.25)),
        ParamDef("stop_3", "color", (1.00, 0.98, 0.90)),
        ParamDef("pos_1", "float", 0.33, min_val=0.0, max_val=1.0),
        ParamDef("pos_2", "float", 0.66, min_val=0.0, max_val=1.0),
        ParamDef("interp_mode", "option", "Linear", labels=INTERP_MODES),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "stop_0": (0.02, 0.02, 0.05),
            "stop_1": (0.10, 0.20, 0.45),
            "stop_2": (0.90, 0.55, 0.25),
            "stop_3": (1.00, 0.98, 0.90),
            "pos_1": 0.33,
            "pos_2": 0.66,
            "interp_mode": "Linear",
            "amount": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """纯逐像素映射：零 padding。"""
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
            "_effect_type": "lib1_gradient_map",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1EdgeExtendNode(Node):
    """C17：按 alpha 把前景 RGB 延伸进透明区（去黑边/白边）。"""

    node_type = "lib1_edge_extend"
    node_title = "Edge Extend (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["edge extend", "边缘延伸", "defringe", "去黑边", "edge bleed"]
    DESCRIPTION = "按 alpha 把前景 RGB 延伸至透明区（默认保留原 alpha）"
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("radius", "float", 4.0, min_val=0.0, max_val=8.0),
        ParamDef("threshold", "float", 0.5, min_val=0.0, max_val=1.0),
        ParamDef("output_mode", "option", "color_only", labels=OUTPUT_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "radius": 4.0,
            "threshold": 0.5,
            "output_mode": "color_only",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """扩张半径即 tile padding（每轮 1px，radius 上限 8）。"""
        return int(float(params.get("radius", 4.0)))

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
            "_effect_type": "lib1_edge_extend",
            "_effect_input": image_input,
            "_effect_params": {
                "radius": self.parameters.get("radius", 4.0),
                "threshold": self.parameters.get("threshold", 0.5),
                "output_mode": self.parameters.get("output_mode", "color_only"),
            },
        }


class Lib1RgbMatrixNode(Node):
    """C02：3x3 自定义通道矩阵 + RGB 偏置（L16）。"""

    node_type = "lib1_rgb_matrix"
    node_title = "RGB Matrix (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["rgb matrix", "通道矩阵", "channel mixer", "color matrix"]
    DESCRIPTION = "3x3 通道矩阵 + RGB 偏置 + strength 混合"
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
        self.parameters = {
            "row0_r": 1.0, "row0_g": 0.0, "row0_b": 0.0,
            "row1_r": 0.0, "row1_g": 1.0, "row1_b": 0.0,
            "row2_r": 0.0, "row2_g": 0.0, "row2_b": 1.0,
            "bias": (0.0, 0.0, 0.0),
            "strength": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_rgb_matrix",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1DespillNode(Node):
    """C03：绿幕去溢色（Subtract/Scale 双模式，L16）。"""

    node_type = "lib1_despill"
    node_title = "Despill (Lib1)"
    header_color = "#5a9a5a"
    SEARCH_ALIASES = ["despill", "去溢色", "green spill", "key spill"]
    DESCRIPTION = "绿幕去溢色：Subtract 削峰 / Scale 乘性双向"
    TAGS = ["filter", "keying"]

    PARAM_DEFS = [
        ParamDef("method", "option", "Subtract", labels=("Subtract", "Scale")),
        ParamDef("limit", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {"method": "Subtract", "limit": 1.0, "strength": 1.0}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_despill",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1SolarizeNode(Node):
    """C04：太阳化折返（F17 共享核，L16）。"""

    node_type = "lib1_solarize"
    node_title = "Solarize (Lib1)"
    header_color = "#9e7e4a"
    SEARCH_ALIASES = ["solarize", "太阳化", "fold", "invert fold"]
    DESCRIPTION = "阈值折返太阳化（Luma/RGB 双档，软过渡）"
    TAGS = ["filter", "color"]

    PARAM_DEFS = [
        ParamDef("threshold", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("softness", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("channel_mode", "option", "Luma", labels=("Luma", "RGB")),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {"threshold": 1.0, "softness": 0.0,
                           "channel_mode": "Luma", "amount": 1.0}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_solarize",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1MosaicNode(Node):
    """C05：块中心采样马赛克 + 阶梯量化 + Bayer 抖动（L16 批2）。"""

    node_type = "lib1_mosaic"
    node_title = "Mosaic (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["mosaic", "马赛克", "pixelate", "pixel blocks"]
    DESCRIPTION = "块中心采样马赛克 + 阶梯量化 + Bayer 抖动"
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
        self.parameters = dict({"block_px": 8.0, "levels": 8.0, "dither": False})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_mosaic",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1CrtNode(Node):
    """C08：CRT 扫描线 + RGB Grille 荫罩（Full=两场乘积）（L16 批2）。"""

    node_type = "lib1_crt"
    node_title = "CRT (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["crt", "扫描线", "scanlines", "aperture grille"]
    DESCRIPTION = "CRT 扫描线 + RGB Grille 荫罩（Full=两场乘积）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "Full", labels=("Scanlines", "RGB Grille", "Full")),
        ParamDef("scan_count", "float", 480.0, min_val=120.0, max_val=1200.0, step=1.0),
        ParamDef("scan_strength", "float", 0.35, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("mask_strength", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"mode": "Full", "scan_count": 480.0, "scan_strength": 0.35, "mask_strength": 0.5})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_crt",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1GlitchNode(Node):
    """C09：条带故障位移 + RGB 分离（hash 静态确定性）（L16 批2）。"""

    node_type = "lib1_glitch"
    node_title = "Glitch (Lib1)"
    header_color = "#9e4a6e"
    SEARCH_ALIASES = ["glitch", "故障", "datamosh", "band shift"]
    DESCRIPTION = "条带故障位移 + RGB 分离（hash 静态确定性）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("band_height", "float", 24.0, min_val=4.0, max_val=128.0, step=1.0),
        ParamDef("shift_amount", "float", 0.35, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("rgb_split", "float", 0.3, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("corruption", "float", 0.35, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("seed", "float", 0.0, min_val=0.0, max_val=100.0, step=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"band_height": 24.0, "shift_amount": 0.35, "rgb_split": 0.3, "corruption": 0.35, "seed": 0.0})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_glitch",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1KaleidoscopeNode(Node):
    """C10：N 扇区折叠镜像万花筒（扇区数/旋转/中心/混合）（L16 批2）。"""

    node_type = "lib1_kaleidoscope"
    node_title = "Kaleidoscope (Lib1)"
    header_color = "#7e4a9e"
    SEARCH_ALIASES = ["kaleidoscope", "万花筒", "mirror fold"]
    DESCRIPTION = "N 扇区折叠镜像万花筒（扇区数/旋转/中心/混合）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("sectors", "float", 6.0, min_val=3.0, max_val=12.0, step=1.0),
        ParamDef("rotation", "float", 0.0, min_val=-180.0, max_val=180.0, step=0.1),
        ParamDef("center", "vec2", (0.5, 0.5)),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"sectors": 6.0, "rotation": 0.0, "center": (0.5, 0.5), "amount": 1.0})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_kaleidoscope",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1MedianNode(Node):
    """C15：小核中值/百分位（Luma 保色相 / RGB 逐通道）（L16 批3）。"""

    node_type = "lib1_median"
    node_title = "Median (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["median", "中值", "rank filter"]
    DESCRIPTION = "小核中值/百分位（Luma 保色相 / RGB 逐通道）"
    TAGS = ["filter", "filter"]

    PARAM_DEFS = [
        ParamDef("kernel_size", "option", "3x3", labels=("3x3", "5x5")),
        ParamDef("percentile", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("channel_mode", "option", "RGB", labels=("RGB", "Luma")),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"kernel_size": "3x3", "percentile": 0.5, "channel_mode": "RGB", "amount": 1.0})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 2

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_median",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1ConvolutionNode(Node):
    """C16：3x3 自定义核 + Σ|K| 归一化 + 偏置（L16 批3）。"""

    node_type = "lib1_convolution"
    node_title = "Convolution (Lib1)"
    header_color = "#4a6e9e"
    SEARCH_ALIASES = ["convolution", "卷积", "kernel", "custom filter"]
    DESCRIPTION = "3x3 自定义核 + Σ|K| 归一化 + 偏置"
    TAGS = ["filter", "filter"]

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
        self.parameters = dict({"k00": 0.0, "k01": 0.0, "k02": 0.0, "k10": 0.0, "k11": 1.0, "k12": 0.0, "k20": 0.0, "k21": 0.0, "k22": 0.0, "normalize": False, "bias": 0.0})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 1

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_convolution",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1HalationNode(Node):
    """C20：高光红晕（12-tap 黄金角环，软膝门控）（L16 批3）。"""

    node_type = "lib1_halation"
    node_title = "Halation (Lib1)"
    header_color = "#9e4a4a"
    SEARCH_ALIASES = ["halation", "晕光", "bloom red"]
    DESCRIPTION = "高光红晕（12-tap 黄金角环，软膝门控）"
    TAGS = ["filter", "filter"]

    PARAM_DEFS = [
        ParamDef("strength", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("radius", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("red_scatter", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("threshold", "float", 0.55, min_val=0.0, max_val=2.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"strength": 0.5, "radius": 0.5, "red_scatter": 0.5, "threshold": 0.55})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_halation",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1HalftoneNode(Node):
    """C06：三版旋转网屏套印 halftone（CMY approx / Luma）（L16 批3b）。"""

    node_type = "lib1_halftone"
    node_title = "Halftone (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["halftone", "网点", "print screen", "套印"]
    DESCRIPTION = "三版旋转网屏套印 halftone（CMY approx / Luma）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("cmyk_separation", "option", "CMY approx", labels=("CMY approx", "Luma")),
        ParamDef("reg_offset_x", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.1),
        ParamDef("reg_offset_y", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.1),
        ParamDef("paper_base", "float", 0.9, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("ink_amount", "float", 0.8, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"cmyk_separation": "CMY approx", "reg_offset_x": 1.0, "reg_offset_y": 0.0, "paper_base": 0.9, "ink_amount": 0.8})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_halftone",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1HatchingNode(Node):
    """C07：亮度驱动周期排线（单/双层正交）（L16 批3b）。"""

    node_type = "lib1_hatching"
    node_title = "Hatching (Lib1)"
    header_color = "#9e7e4a"
    SEARCH_ALIASES = ["hatching", "排线", "cross hatch", "engraving"]
    DESCRIPTION = "亮度驱动周期排线（单/双层正交）"
    TAGS = ["filter", "stylize"]

    PARAM_DEFS = [
        ParamDef("line_spacing", "float", 8.0, min_val=2.0, max_val=64.0, step=1.0),
        ParamDef("angle", "float", 45.0, min_val=-90.0, max_val=90.0, step=1.0),
        ParamDef("layers", "option", "2", labels=("1", "2")),
        ParamDef("ink_color", "color", (0.05, 0.05, 0.08)),
        ParamDef("paper", "float", 0.9, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"line_spacing": 8.0, "angle": 45.0, "layers": "2", "ink_color": (0.05, 0.05, 0.08), "paper": 0.9, "amount": 1.0})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_hatching",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1PolarCoordinatesNode(Node):
    """C11：Cartesian↔polar UV 重映射（正/逆变换）（L17）。"""

    node_type = "lib1_polar_coordinates"
    node_title = "Polar Coordinates (Lib1)"
    header_color = "#7e4a9e"
    SEARCH_ALIASES = ["polar", "极坐标", "rectangular to polar"]
    DESCRIPTION = "Cartesian↔polar UV 重映射（正/逆变换）"
    TAGS = ["filter", "warp"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "CartesianToPolar",
                 labels=("CartesianToPolar", "PolarToCartesian")),
        ParamDef("center", "vec2", (0.5, 0.5)),
        ParamDef("angle_span", "float", 360.0, min_val=1.0, max_val=360.0, step=1.0),
        ParamDef("radius_scale", "float", 1.0, min_val=0.1, max_val=4.0, step=0.05),
        ParamDef("filter", "option", "Nearest", labels=("Nearest", "Linear")),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"mode": "CartesianToPolar", "center": (0.5, 0.5), "angle_span": 360.0, "radius_scale": 1.0, "filter": "Nearest"})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_polar_coordinates",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1TileArrayNode(Node):
    """C14：count 格胞阵列 + spacing 内边距 + offset 环绕相位（L17）。"""

    node_type = "lib1_tile_array"
    node_title = "Tile Array (Lib1)"
    header_color = "#4a9e7e"
    SEARCH_ALIASES = ["tile array", "瓦片阵列", "grid duplicate"]
    DESCRIPTION = "count 格胞阵列 + spacing 内边距 + offset 环绕相位"
    TAGS = ["filter", "warp"]

    PARAM_DEFS = [
        ParamDef("count_x", "float", 3.0, min_val=1.0, max_val=16.0, step=1.0),
        ParamDef("count_y", "float", 3.0, min_val=1.0, max_val=16.0, step=1.0),
        ParamDef("spacing", "float", 0.1, min_val=0.0, max_val=0.9, step=0.01),
        ParamDef("offset_x", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("offset_y", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = dict({"count_x": 3.0, "count_y": 3.0, "spacing": 0.1, "offset_x": 0.0, "offset_y": 0.0})

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_tile_array",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1DistanceMatteNode(Node):
    """C18：距离场 matte（全帧声明，L17 批2）。"""

    node_type = "lib1_distance_matte"
    node_title = "Distance Matte (Lib1)"
    header_color = "#5a5a9e"
    SEARCH_ALIASES = ["distance matte", "距离场", "sdf matte"]
    DESCRIPTION = "α→精确有符号距离场（Euclid/Chebyshev/Manhattan 三度量）"
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("metric", "option", "Euclid", labels=("Euclid", "Chebyshev", "Manhattan")),
        ParamDef("radius", "float", 8.0, min_val=1.0, max_val=64.0, step=1.0),
        ParamDef("threshold", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("output_mode", "option", "DistanceField",
                 labels=("DistanceField", "EdgeBand", "Matte")),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {"metric": "Euclid", "radius": 8.0,
                           "threshold": 0.5, "output_mode": "DistanceField"}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        # 全帧声明：不参与分块（capability fusible=False；padding 由
        # radius 理论需求但全帧执行下无 tile 语义）
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_distance_matte",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1FillHolesNode(Node):
    """C19：封闭孔填充（全帧声明，L17 批2）。"""

    node_type = "lib1_fill_holes"
    node_title = "Fill Holes (Lib1)"
    header_color = "#5a9e5a"
    SEARCH_ALIASES = ["fill holes", "填孔", "hole filler"]
    DESCRIPTION = "BG 连通域 BFS：触边背景永不填，封闭孔按面积填充"
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("connectivity", "option", "4", labels=("4", "8")),
        ParamDef("max_area", "float", 0.1, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("threshold", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("output_mode", "option", "Fill", labels=("Fill", "Holes")),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {"connectivity": "4", "max_area": 0.1,
                           "threshold": 0.5, "output_mode": "Fill"}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_fill_holes",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1VectorDisplaceNode(Node):
    """C12：向量图驱动位移（多输入子门，L17 批3）。"""

    node_type = "lib1_vector_displace"
    node_title = "Vector Displace (Lib1)"
    header_color = "#9e5a4a"
    SEARCH_ALIASES = ["vector displace", "向量位移", "displacement map"]
    DESCRIPTION = "向量图 RG 驱动位移 + mask 门控（相对位移）"
    TAGS = ["filter", "warp"]

    PARAM_DEFS = [
        ParamDef("scale", "float", 50.0, min_val=0.0, max_val=200.0, step=1.0),
        ParamDef("neutral", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("units", "option", "Pixels", labels=("Pixels", "UV")),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        # 多输入端口角色契约（不可串线）：image=内容 / vector=向量图 / mask=门控
        self.inputs = [
            Port("image", "input", "image"),
            Port("vector", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.inputs[2].max_connections = 1
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {"scale": 50.0, "neutral": 0.5, "units": "Pixels"}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        vector_input = inputs.get("vector")
        if image_input is None or not image_input.is_valid:
            return {}
        if vector_input is None or not vector_input.is_valid:
            return {}
        marker = {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_vector_displace",
            "_effect_input": image_input,
            "_effect_input_map": vector_input,
            "_effect_params": dict(self.parameters),
        }
        mask_input = inputs.get("mask")
        if mask_input is not None and mask_input.is_valid:
            marker["_effect_mask"] = mask_input
        return marker


class Lib1StmapNode(Node):
    """C13：绝对 UV 重采样（多输入子门，L17 批3）。"""

    node_type = "lib1_stmap"
    node_title = "STMAP (Lib1)"
    header_color = "#4a7e9e"
    SEARCH_ALIASES = ["stmap", "uv map", "重映射"]
    DESCRIPTION = "绝对 UV map 重采样（RG/BA、origin、invalid、Nearest/Linear）"
    TAGS = ["filter", "warp"]

    PARAM_DEFS = [
        ParamDef("channels", "option", "RG", labels=("RG", "BA")),
        ParamDef("origin", "option", "BottomLeft", labels=("BottomLeft", "TopLeft")),
        ParamDef("invalid", "option", "Clamp", labels=("Clamp", "Passthrough")),
        ParamDef("filter", "option", "Nearest", labels=("Nearest", "Linear")),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("uv_map", "input", "image"),
        ]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {"channels": "RG", "origin": "BottomLeft",
                           "invalid": "Clamp", "filter": "Nearest"}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        map_input = inputs.get("uv_map")
        if image_input is None or not image_input.is_valid:
            return {}
        if map_input is None or not map_input.is_valid:
            return {}
        return {
            "image": TextureResult(texture_id=-1, width=image_input.width,
                                   height=image_input.height),
            "_effect_type": "lib1_stmap",
            "_effect_input": image_input,
            "_effect_input_map": map_input,
            "_effect_params": dict(self.parameters),
        }


class Lib1FrequencySeparationNode(Node):
    """C21：频率分离（多输出子门，L17 批3）。"""

    node_type = "lib1_frequency_separation"
    node_title = "Frequency Separation (Lib1)"
    header_color = "#4a9e9e"
    SEARCH_ALIASES = ["frequency separation", "频率分离", "low high split"]
    DESCRIPTION = "可分离低通→low，high=RGB−low 带符号（声明域精确重建）"
    TAGS = ["filter", "channels"]

    PARAM_DEFS = [
        ParamDef("radius", "float", 4.0, min_val=0.0, max_val=64.0, step=1.0),
        ParamDef("filter", "option", "Box", labels=("Box", "Gaussian")),
        ParamDef("mode", "option", "LowHigh", labels=("LowHigh", "Reconstruct")),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        # 多输出子门：low/high 双占位（marker 契约）
        self.outputs = [Port("low", "output", "image"),
                        Port("high", "output", "image")]
        self.parameters = {"radius": 4.0, "filter": "Box", "mode": "LowHigh"}

    def compute_output_bbox(self, input_bboxes: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview"
                     ) -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}
        return {
            "low": TextureResult(texture_id=-1, width=image_input.width,
                                 height=image_input.height),
            "high": TextureResult(texture_id=-1, width=image_input.width,
                                  height=image_input.height),
            "_effect_type": "lib1_frequency_separation",
            "_effect_input": image_input,
            "_effect_params": dict(self.parameters),
        }
