"""
Defocus Node - Screen-position camera defocus (CP-010 scope note).

Simulates defocus using separable Gaussian blur with radius varying by
distance from a SCREEN-SPACE focal point. Supports both separable
convolution and mipmap-based approximation for performance.

Honest boundary (CP-010): this node has NO depth input — the blur radius
is driven by pixel distance to (focal_x, focal_y), not by a Z/depth
channel, and the kernel is Gaussian rather than a true bokeh disc.
For depth-driven defocus (ZBlur in Nuke terms), use the ``zblur`` node,
which consumes a depth/AOV input with a Vogel-disc bokeh gather.

Key features:
- Focal point control (where the image is sharpest)
- Focal size (depth of field range)
- Separable and mipmap-based rendering modes
- Alpha-aware processing
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Defocus rendering modes
DEFOCUS_MODES = ["separable", "mipmap"]


class DefocusNode(Node):
    """Camera defocus effect with focal point and size controls."""

    node_type = "defocus"
    node_title = "Defocus"
    header_color = "#7a6b8a"  # Muted purple

    # Metadata
    SEARCH_ALIASES = ["defocus", "散焦", "景深", "dof", "depth of field", "blur", "焦外"]
    DESCRIPTION = "屏幕位置驱动的散焦近似（无深度输入；深度景深请用 ZBlur）"
    TAGS = ["filter", "image", "depth"]

    HELP_TEXT = """\
**Defocus（散焦·屏幕位置近似）**

按像素到焦点（focal_x/focal_y）的屏幕距离改变模糊半径，模拟散焦。

**边界（CP-010）：** 本节点**无深度输入**——半径由屏幕位置而非 Z 通道
驱动，核为高斯而非真实 bokeh 圆盘。需要深度驱动的景深（Nuke ZDefocus
对应物）请改用 **ZBlur** 节点（吃 depth/AOV 输入，Vogel 圆盘采样）。

**用途：** 快速焦外柔化、焦点视觉引导

**输入：**
- image：输入图像

**输出：**
- result：散焦后的图像

**关键参数：**
- mode：渲染模式（separable/mipmap）
- focal_x/focal_y：焦点位置（归一化 0-1）
- focal_size：景深范围（归一化）
- max_radius：最大模糊半径
- edge_policy：边缘处理策略（clamp/wrap/mirror）
- process_alpha：是否处理 alpha 通道
"""

    PARAM_DEFS = [
        ParamDef("mode", "option", "separable", labels=DEFOCUS_MODES),
        ParamDef("focal_x", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("focal_y", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("focal_size", "float", 0.3, min_val=0.01, max_val=1.0, step=0.01),
        ParamDef("max_radius", "float", 20.0, min_val=0.0, max_val=100.0, step=0.5),
        ParamDef("edge_policy", "option", "clamp", labels=["clamp", "wrap", "mirror"]),
        ParamDef("process_alpha", "bool", True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "mode": "separable",
            "focal_x": 0.5,
            "focal_y": 0.5,
            "focal_size": 0.3,
            "max_radius": 20.0,
            "edge_policy": "clamp",
            "process_alpha": True,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Defocus expands the bbox by the maximum radius."""
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None
        max_radius = int(params.get("max_radius", 20.0))
        return input_bbox.expanded(max_radius)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """To defocus output_roi, we need input expanded by max radius."""
        max_radius = int(params.get("max_radius", 20.0))
        expanded_roi = output_roi.expanded(max_radius)
        return {"image": expanded_roi}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Defocus is essentially a spatial blur with variable radius.
        Requires max_radius pixels on all sides.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels (max_radius).
        """
        return int(params.get("max_radius", 20.0))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        mode = self.parameters.get("mode", "separable")
        focal_x = float(self.parameters.get("focal_x", 0.5))
        focal_y = float(self.parameters.get("focal_y", 0.5))
        focal_size = float(self.parameters.get("focal_size", 0.3))
        max_radius = float(self.parameters.get("max_radius", 20.0))
        edge_policy = self.parameters.get("edge_policy", "clamp")
        process_alpha = self.parameters.get("process_alpha", True)

        # Skip if max_radius is negligible
        if max_radius < 0.1:
            return {"result": image}

        # Preview mode: reduce max radius for faster interactive response
        if render_mode == "preview" and max_radius > 15.0:
            max_radius = max(5.0, max_radius * 0.4)

        # Mipmap mode has lower quality but is faster
        if mode == "mipmap" and render_mode == "preview":
            max_radius *= 0.7

        return {
            "result": TextureResult(
                texture_id=-1,  # Will be replaced by actual GPU result
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "defocus",
            "_effect_input": image,
            "_effect_params": {
                "mode": mode,
                "focal_x": focal_x,
                "focal_y": focal_y,
                "focal_size": focal_size,
                "max_radius": max_radius,
                "edge_policy": edge_policy,
                "process_alpha": process_alpha,
            },
        }
