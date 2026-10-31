"""
Denoise Node - GPU-based image denoising using bilateral filter.

Reduces noise while preserving edges using a bilateral filter approach.
This is a fast GPU shader fallback, distinct from AI Denoise which uses
ONNX models for higher quality but slower processing.

Technical implementation:
- Bilateral filter: spatial + color range weighting
- Separable approximation for performance
- Optional luminance/chroma-only processing modes
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef
from .blur_node import QUALITY_LEVELS

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class DenoiseNode(Node):
    """GPU-based image denoising using bilateral filter.

    Fast shader-based denoising that preserves edges.
    Uses bilateral filter with adjustable strength and radius.

    This node is distinct from AIDenoiseNode:
    - DenoiseNode: Fast GPU shader (bilateral filter), real-time preview
    - AIDenoiseNode: ONNX model-based, higher quality, async processing
    """

    node_type = "denoise"
    node_title = "Denoise"
    header_color = "#4a9b8e"  # Teal color

    # Metadata
    SEARCH_ALIASES = ["denoise", "降噪", "去噪", "noise", "clean", "清理", "bilateral"]
    DESCRIPTION = "GPU降噪滤镜（双边滤波），保留边缘细节"
    TAGS = ["filter", "image"]
    IS_PLACEHOLDER = False  # Real implementation, not placeholder

    PARAM_DEFS = [
        ParamDef("strength", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("radius", "int", 3, min_val=1, max_val=10),
        # T-P1-21 batch 2: 质量档位。fast 封顶 kernel 半径（预览级）、
        # high 加倍（封顶 schema 上限 10）；effective 半径换算在
        # effect_tiers.effective_denoise_radius，GPU/CPU 同契约。
        ParamDef("quality", "option", "normal", labels=QUALITY_LEVELS),
        ParamDef("luminance_only", "bool", False),
        ParamDef("chroma_only", "bool", False),
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
            "strength": 0.5,
            "radius": 3,
            "quality": "normal",
            "luminance_only": False,
            "chroma_only": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Denoise does not expand or shrink the bbox."""
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None
        # Bilateral filter does not change the data window
        return input_bbox

    def _effective_radius(self, radius: int, quality: str) -> int:
        """Quality-tier effective kernel radius (AUD-6609).

        Applies the shared tier contract in
        ``src.features.shared.effects.effect_tiers`` — the same mapping the
        Edit page (``effect_pass_runner._pack_denoise_params``) and the CPU
        export path use: ``fast`` caps the kernel at 2, ``normal`` keeps
        the user radius, ``high`` doubles it (capped at the schema max).
        Applying it here means the composite-page GPU dispatch honours the
        quality setting too (the evaluator consumes ``radius`` from the
        effect marker), restoring the documented GPU/CPU parity.
        """
        from src.features.shared.effects.effect_tiers import (
            effective_denoise_radius,
        )
        return effective_denoise_radius(radius, quality)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Denoise requires the full output ROI plus padding for kernel sampling."""
        radius = self._effective_radius(
            int(params.get("radius", 3)), str(params.get("quality", "normal")))
        expanded_roi = output_roi.expanded(radius)
        return {"image": expanded_roi}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Denoise requires the kernel radius on all sides to avoid artifacts
        at tile edges.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels (quality-tier effective kernel radius).
        """
        radius = self._effective_radius(
            int(params.get("radius", 3)), str(params.get("quality", "normal")))
        return int(radius)

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate the denoise node.

        Returns a marker dict that GraphEvaluatorMixin._resolve_output()
        will consume and replace with actual GPU-rendered TextureResult.

        Args:
            inputs: Input textures (image only)
            ctx: ModernGL context
            fb_manager: FramebufferManager
            resource_pool: Optional GPUResourcePool
            render_mode: "preview", "full", or "export"

        Returns:
            Dict with effect marker (will be resolved by _dispatch_effect)
        """
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Get parameters with validation
        strength = max(0.0, min(1.0, float(self.parameters.get("strength", 0.5))))
        radius = max(1, min(10, int(self.parameters.get("radius", 3))))
        quality = str(self.parameters.get("quality", "normal"))
        if quality not in QUALITY_LEVELS:
            quality = "normal"
        luminance_only = self.parameters.get("luminance_only", False)
        chroma_only = self.parameters.get("chroma_only", False)

        # Skip if strength is negligible
        if strength < 0.01:
            return {"result": image}

        # AUD-6609: apply the quality tier BEFORE the preview speed
        # reduction — the tier selects the effective kernel per the shared
        # contract, preview then scales that effective radius down.
        radius = self._effective_radius(radius, quality)

        # Preview mode: reduce radius for faster interactive response
        if render_mode == "preview":
            if radius > 4:
                radius = max(2, int(radius * 0.5))

        # Return effect marker for GraphEvaluatorMixin to dispatch
        return {
            "result": TextureResult(
                texture_id=-1,  # Will be replaced by actual GPU result
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "denoise",
            "_effect_input": image,
            "_effect_params": {
                "strength": strength,
                # Quality-adjusted effective radius (see _effective_radius);
                # kept explicit below so downstream dispatchers/cache keys
                # see the tier that produced it.
                "radius": radius,
                "quality": quality,
                "luminance_only": luminance_only,
                "chroma_only": chroma_only,
            },
        }