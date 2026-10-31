"""
Timeline Clip Input Node - P0-3/P2 Comp Clip integration.

Timeline Clip 的合成主输入节点。不直接解码视频文件，
而是从 CompositeRuntimeContext 获取当前帧的 GPU texture。

设计原则：
- 不直接打开视频文件
- 不直接调用 VideoFrameNode._decode_frame()
- 不返回 np.ndarray
- 只从 runtime context 获取 TextureResult
- 无 runtime context 时返回空结果（不崩溃）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base_node import Node, Port, TextureResult, ParamDef

logger = logging.getLogger(__name__)


class TimelineClipInputNode(Node):
    """
    Timeline Clip 输入节点。

    从 Edit 时间线获取当前 clip 的已调色帧作为合成输入。
    通过 CompositeRuntimeContext 获取帧数据。

    Parameters:
        timeline_id: 来源时间线 ID
        clip_id: 来源 clip ID
        asset_id: 来源素材 ID
        input_stage: 输入阶段 (source_raw, after_color, after_clip_effects)
    """
    node_type = "timeline_clip_input"
    node_title = "Timeline Clip Input"
    header_color = "#2e8b57"
    SEARCH_ALIASES = ["timeline", "clip input", "video input", "footage"]
    DESCRIPTION = "Input from timeline clip source"
    TAGS = ["input", "timeline", "clip", "source"]

    PARAM_DEFS = [
        ParamDef("timeline_id", "str", ""),
        ParamDef("clip_id", "str", ""),
        ParamDef("asset_id", "str", ""),
        ParamDef("input_stage", "option", "source_raw",
                 labels=["source_raw", "after_color", "after_clip_effects"]),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("output", "output", "image")
        ]
        self.parameters = {
            "timeline_id": "",
            "clip_id": "",
            "asset_id": "",
            "input_stage": "source_raw",
        }
        # Cached texture from runtime context
        self._cached_texture: Optional[TextureResult] = None
        self._revision: int = 0
        # Last detected source color-space tag (set during evaluation from the
        # provider's metadata-driven detection; read by the preview HDR detector).
        self._last_source_color_tag: Optional[str] = None

    def set_cached_texture(self, texture: Optional[TextureResult]) -> None:
        """Set cached texture from runtime context.

        Called by the composite evaluator before graph evaluation.
        """
        self._cached_texture = texture
        if texture is not None:
            self._revision += 1
        self._dirty = True

    def _do_evaluate(
        self,
        inputs: Dict[str, TextureResult],
        ctx: Any,
        fb_manager: Any,
        resource_pool: Any = None,
        render_mode: str = "preview",
    ) -> Dict[str, TextureResult]:
        """Evaluate node - return cached texture from runtime context.

        The actual frame data is provided externally via set_cached_texture()
        or through the ctx object's runtime_context attribute.

        When ctx is a CompositeEvalContext, extracts runtime_context and
        timeline_clip_gpu_provider directly. Falls back to getattr for
        backward compatibility with plain moderngl.Context.

        Returns empty dict if no texture is available (not an error).
        """
        # Check if ctx is a CompositeEvalContext (preferred path)
        from src.features.composite.core.eval_context import CompositeEvalContext
        if isinstance(ctx, CompositeEvalContext):
            runtime = ctx.runtime_context
            provider = ctx.timeline_clip_gpu_provider
        else:
            # Backward compat: try getattr on plain moderngl.Context
            runtime = getattr(ctx, 'runtime_context', None)
            provider = getattr(ctx, 'timeline_clip_gpu_provider', None)

        has_runtime_provider = runtime is not None and provider is not None
        if has_runtime_provider:
            try:
                node_stage = self.parameters.get("input_stage", "source_raw")
                if node_stage and getattr(runtime, "input_stage", None) != node_stage:
                    try:
                        from dataclasses import replace
                        runtime = replace(runtime, input_stage=node_stage)
                    except Exception:
                        # AUD-7407 fix: no silent pass — a failed stage
                        # override means the provider serves the wrong
                        # input stage; keep going with the runtime's own
                        # stage but leave a diagnosable trace.
                        logger.debug(
                            "TimelineClipInputNode: input_stage override to "
                            f"'{node_stage}' failed, using runtime stage "
                            f"'{getattr(runtime, 'input_stage', None)}'",
                            exc_info=True,
                        )
                if isinstance(ctx, CompositeEvalContext) and hasattr(provider, 'set_gl_context'):
                    provider.set_gl_context(ctx.gl)
                tex = provider.get_timeline_clip_texture(runtime)
                if tex is not None:
                    # Convert internal _TexResult to TextureResult if needed
                    from src.features.edit.core.timeline_clip_frame_provider import _to_texture_result
                    tex = _to_texture_result(tex)
                    # Cache the detected source color-space tag so the preview
                    # HDR detector (page_controller) can tell whether the
                    # timeline source is PQ/HLG without re-probing. Set from
                    # the provider's metadata-driven detection (P0-4).
                    self._last_source_color_tag = getattr(tex, "color_space", None)
                    return {"output": tex}
                logger.warning(
                    "TimelineClipInputNode: provider returned no texture "
                    f"timeline={getattr(runtime, 'timeline_id', '')} "
                    f"clip={getattr(runtime, 'clip_id', '')} "
                    f"asset={getattr(runtime, 'asset_id', '')} "
                    f"source_frame={getattr(runtime, 'source_frame', '')}"
                )
            except Exception as e:
                # AUD-7407 fix: "no texture is not an error" is a PREVIEW
                # contract. In full/export mode a provider failure means the
                # rendered output would silently drop a frame while the task
                # still reports success — re-raise so the evaluator records
                # the node error (and the export log carries the traceback).
                if str(render_mode).lower() in {"full", "export"}:
                    logger.error(
                        "TimelineClipInputNode: provider failed during "
                        f"{render_mode} evaluation, failing the node",
                        exc_info=True,
                    )
                    raise
                logger.warning(
                    "TimelineClipInputNode: provider failed "
                    f"(render_mode={render_mode}): {e}",
                    exc_info=True,
                )
                self.error_message = str(e)

        # Fall back to cached texture only for legacy callers that explicitly
        # pre-seed the node. In timeline runtime mode, returning a cached
        # texture after provider failure can display the previous clip/frame.
        if not has_runtime_provider and self._cached_texture is not None:
            return {"output": self._cached_texture}

        # No texture available - return empty (not an error for preview mode)
        return {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize node to dict."""
        return super().to_dict()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TimelineClipInputNode:
        """Deserialize node from dict."""
        return super().from_dict(data)
