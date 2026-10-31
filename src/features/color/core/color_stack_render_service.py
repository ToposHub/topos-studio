"""
ColorStackRenderService - Pure service for applying ColorStack grading.

This service extracts the color grading application logic from the Color page
so it can be used by the ToposColorNode in Composite without depending on
QWidget, the Color page canvas, or the current color_output texture.

Usage:
    service = ColorStackRenderService()
    grading_params = service.resolve_grading_params(
        stack_source="clip_stack",
        color_stack_id="stack_123",
        project=project,
        runtime_context=ctx,
    )
    # Then pass grading_params to _apply_topos_color via effect_params
"""

import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.features.edit.core.project import EditProject


class ColorStackRenderService:
    """Pure service for resolving and preparing ColorStack grading parameters.

    This service does NOT do GPU rendering directly.
    It resolves the correct ColorStack data based on stack_source
    and returns the grading_params dict that the Topos Color shader consumes.

    The actual GPU rendering is done by _apply_topos_color in the evaluator.
    """

    def resolve_grading_params(
        self,
        stack_source: str,
        color_stack_id: str = "",
        preset_id: str = "",
        embedded_stack: Optional[Dict[str, Any]] = None,
        project: Optional["EditProject"] = None,
        runtime_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Resolve the grading parameters for a Topos Color node.

        Args:
            stack_source: "clip_stack", "preset", or "embedded"
            color_stack_id: ID of the ColorStack (for clip_stack mode)
            preset_id: ID of the preset (for preset mode)
            embedded_stack: Embedded stack snapshot (for embedded mode)
            project: The current EditProject
            runtime_context: Composite RuntimeContext (for clip_stack resolution)

        Returns:
            Dict with grading_params and other stack data ready for the shader.
        """
        if stack_source == "clip_stack":
            return self._resolve_clip_stack(
                color_stack_id, project, runtime_context
            )
        elif stack_source == "preset":
            return self._resolve_preset(preset_id, project)
        elif stack_source == "embedded":
            return self._resolve_embedded(embedded_stack)
        else:
            logger.warning(f"Unknown stack_source: {stack_source}")
            return {"grading_params": self._default_grading_params()}

    def _resolve_clip_stack(
        self,
        color_stack_id: str,
        project: Optional["EditProject"],
        runtime_context: Optional[Any],
    ) -> Dict[str, Any]:
        """Resolve ColorStack from project by color_stack_id."""
        if not color_stack_id or project is None:
            return {"grading_params": self._default_grading_params()}

        color_stacks = getattr(project, 'color_stacks', {})
        stack = color_stacks.get(color_stack_id)
        if stack is None:
            logger.debug(f"ColorStack not found: {color_stack_id}")
            return {"grading_params": self._default_grading_params()}

        return {
            "grading_params": dict(stack.grading_params) if hasattr(stack, 'grading_params') else {},
            "curves_data": dict(stack.curves_data) if hasattr(stack, 'curves_data') else {},
            "hsl_curve_data": dict(stack.hsl_curve_data) if hasattr(stack, 'hsl_curve_data') else {},
            "mixer_params": dict(stack.mixer_params) if hasattr(stack, 'mixer_params') else {},
            "soft_clip_params": dict(stack.soft_clip_params) if hasattr(stack, 'soft_clip_params') else {},
            "lut_path": stack.lut_path if hasattr(stack, 'lut_path') else None,
            "lut_intensity": stack.lut_intensity if hasattr(stack, 'lut_intensity') else 1.0,
        }

    def _resolve_preset(
        self,
        preset_id: str,
        project: Optional["EditProject"],
    ) -> Dict[str, Any]:
        """Resolve ColorStack from a color preset."""
        if not preset_id or project is None:
            return {"grading_params": self._default_grading_params()}

        presets = getattr(project, 'color_presets', {})
        preset = presets.get(preset_id)
        if preset is None:
            logger.debug(f"Color preset not found: {preset_id}")
            return {"grading_params": self._default_grading_params()}

        return {
            "grading_params": dict(preset.get("stack", {}).get("grading_params", {})),
        }

    def _resolve_embedded(
        self,
        embedded_stack: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Resolve ColorStack from embedded snapshot in the node."""
        if not embedded_stack:
            return {"grading_params": self._default_grading_params()}

        # AUD-0405：键集合与 _resolve_clip_stack 对齐。嵌入快照来自
        # ColorStackData.to_dict()（含 lut_path/lut_intensity），此前这两键
        # 被静默丢弃——带 3D LUT 的调色栈嵌入 Composite 节点后渲染不含
        # LUT。快照未携带时按中性默认回退，消费方以 .get() 读取。
        return {
            "grading_params": dict(embedded_stack.get("grading_params", {})),
            "curves_data": dict(embedded_stack.get("curves_data", {})),
            "hsl_curve_data": dict(embedded_stack.get("hsl_curve_data", {})),
            "mixer_params": dict(embedded_stack.get("mixer_params", {})),
            "soft_clip_params": dict(embedded_stack.get("soft_clip_params", {})),
            "lut_path": embedded_stack.get("lut_path"),
            "lut_intensity": embedded_stack.get("lut_intensity", 1.0),
        }

    @staticmethod
    def _default_grading_params() -> Dict[str, Any]:
        """Return default (no-op) grading parameters."""
        return {
            "lift": (0, 0, 0),
            "gamma": (0, 0, 0),
            "gain": (0, 0, 0),
            "exposure": 0,
            "temp": 0,
            "tint": 0,
            "contrast": 0,
            "shadow": 0,
            "highlights": 0,
            "saturation": 0,
            "vibrance": 0,
            "mid_detail": 0,
            "hue_shift": 0,
        }
