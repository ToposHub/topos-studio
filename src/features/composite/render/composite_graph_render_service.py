# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Composite Graph Render Service — unified render interface for Edit/Cache/Export.

Provides both GPU (texture) and CPU (numpy) output paths for rendering
composite graphs. The CPU path is always available; the GPU path requires
an active OpenGL context and shared GL context configuration.

Usage:
    service = CompositeGraphRenderService()

    # CPU path (always works, for Render Cache / Export)
    frame_bgr = service.render_cpu(
        graph_data=graph_dict,
        timeline_frame=42,
        edit_project=project,
        timeline_sequences=seqs,
    )

    # GPU path (requires GL context, for real-time Edit preview)
    handle = service.render_gpu(
        graph_data=graph_dict,
        timeline_frame=42,
        edit_project=project,
        timeline_sequences=seqs,
    )

    service.cleanup()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from src.shared.gpu.composite_gpu_frame_handle import CompositeGpuFrameHandle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GpuFrameHandle:
    """Handle to a GPU-rendered frame (texture in GL context)."""
    texture_id: int
    width: int
    height: int
    color_space: str = "srgb"
    orientation: str = "gl_bottom_left"  # or "image_top_left"
    owner_context_id: str = ""
    revision: int = 0

    @property
    def is_valid(self) -> bool:
        return self.texture_id > 0 and self.width > 0 and self.height > 0


class CompositeGraphRenderService:
    """Unified composite graph rendering service.

    Provides:
    - render_cpu(): BGR float32 numpy output (for Render Cache, Export)
    - render_gpu(): GPU texture handle (for Edit preview, zero-copy display)

    The GPU path requires GL context sharing to be configured.
    If GPU is unavailable, render_gpu() returns None (caller should fall back
    to render_cpu()).
    """

    def __init__(self, gl_context=None) -> None:
        self._gl_context = gl_context
        self._composite_renderer = None
        self._revision_counter = 0
        self._telemetry: list[dict] = []

    def render_cpu(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        edit_project: Any = None,
        timeline_sequences: dict | None = None,
        timeline_id: str | None = None,
        clip_id: str | None = None,
        asset_id: str | None = None,
        quality: str = "preview",
        caption_input: object | None = None,
    ) -> Optional[np.ndarray]:
        """Render composite graph to CPU numpy array.

        Args:
            graph_data: CompositeGraphData dict (nodes, connections, etc.)
            timeline_frame: Frame number for animation
            edit_project: EditProject instance
            timeline_sequences: {timeline_id: TimelineSequence}
            quality: "preview" or "export"

        Returns:
            BGR float32 numpy array [0-1] on success, None on failure.
        """
        import time as _time
        t0 = _time.monotonic()

        try:
            from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer

            if self._composite_renderer is None:
                self._composite_renderer = CompositeFrameRenderer()

            result = self._composite_renderer.render_comp_clip_frame(
                graph_data=graph_data,
                timeline_id=timeline_id,
                clip_id=clip_id,
                asset_id=asset_id,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
                timeline_frame=timeline_frame,
                quality=quality,
                caption_input=caption_input,
            )

            elapsed_ms = (_time.monotonic() - t0) * 1000
            self._record_telemetry(
                frame=timeline_frame, path="cpu", elapsed_ms=elapsed_ms,
                success=result is not None,
            )
            return result

        except Exception as e:
            elapsed_ms = (_time.monotonic() - t0) * 1000
            self._record_telemetry(
                frame=timeline_frame, path="cpu", elapsed_ms=elapsed_ms,
                success=False, error=str(e),
            )
            logger.error("CompositeGraphRenderService CPU render failed: %s", e)
            return None

    def render_gpu(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        edit_project: Any = None,
        timeline_sequences: dict | None = None,
        timeline_id: str | None = None,
        clip_id: str | None = None,
        asset_id: str | None = None,
        quality: str = "preview",
        caption_input: object | None = None,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> Optional[CompositeGpuFrameHandle]:
        """Render composite graph to GPU texture.

        The caller must supply an active/shared GL context.  The returned
        value is the lease-bearing ``CompositeGpuFrameHandle`` produced by
        ``CompositeFrameRenderer``; callers must release it after the GPU
        consumer has finished.

        ``quality`` is kept for API compatibility.  The GPU producer uses
        the preview graph path; final-quality output remains on ``render_cpu``
        or the dedicated export renderer.

        Returns:
            A presentable or explicitly unavailable composite GPU handle;
            None when no context/valid graph is available.
        """
        if self._gl_context is None:
            self._record_telemetry(
                frame=timeline_frame,
                path="gpu",
                elapsed_ms=0.0,
                success=False,
                error="no active GL context",
            )
            logger.debug("CompositeGraphRenderService GPU path unavailable: no GL context")
            return None

        # Keep the service as a thin adapter over the same producer used by
        # Edit's timeline worker.  The caller owns/makes-current the supplied
        # GL context; CompositeFrameRenderer owns the graph snapshot,
        # persistent node cache and CompositeGpuFrameHandle lease.
        import time as _time
        t0 = _time.monotonic()
        if not isinstance(graph_data, dict) or "nodes" not in graph_data:
            self._record_telemetry(
                frame=timeline_frame,
                path="gpu",
                elapsed_ms=0.0,
                success=False,
                error="empty or invalid graph_data",
            )
            return None

        try:
            from src.features.composite.render.composite_frame_renderer import (
                CompositeFrameRenderer,
            )

            if self._composite_renderer is None:
                self._composite_renderer = CompositeFrameRenderer()

            handle = self._composite_renderer.render_comp_clip_gpu(
                graph_data=graph_data,
                timeline_id=timeline_id,
                clip_id=clip_id,
                asset_id=asset_id,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
                timeline_frame=timeline_frame,
                generation=generation,
                caption_input=caption_input,
                trace_id=trace_id,
            )
            elapsed_ms = (_time.monotonic() - t0) * 1000.0
            available = bool(getattr(handle, "available", False))
            error = "" if available else str(
                getattr(handle, "fallback_reason", "gpu handle unavailable")
            )
            self._record_telemetry(
                frame=timeline_frame,
                path="gpu",
                elapsed_ms=elapsed_ms,
                success=available,
                error=error,
            )
            return handle
        except Exception as e:
            elapsed_ms = (_time.monotonic() - t0) * 1000.0
            self._record_telemetry(
                frame=timeline_frame,
                path="gpu",
                elapsed_ms=elapsed_ms,
                success=False,
                error=str(e),
            )
            logger.error("CompositeGraphRenderService GPU render failed: %s", e)
            return None

    def get_telemetry(self) -> list[dict]:
        """Get recent render telemetry entries."""
        return list(self._telemetry[-100:])  # Last 100 entries

    # AUD-80020: keep the telemetry ring bounded — get_telemetry() exposes
    # only the last 100 entries, so anything older is dead weight that
    # accumulated unboundedly over long playback sessions.
    _TELEMETRY_MAX_ENTRIES = 100

    def _record_telemetry(self, frame: int, path: str, elapsed_ms: float,
                          success: bool, error: str = "") -> None:
        self._telemetry.append({
            "frame": frame,
            "path": path,
            "elapsed_ms": round(elapsed_ms, 1),
            "success": success,
            "error": error,
        })
        if len(self._telemetry) > self._TELEMETRY_MAX_ENTRIES:
            del self._telemetry[:-self._TELEMETRY_MAX_ENTRIES]
        self._revision_counter += 1

    def cleanup(self) -> None:
        """Release rendering resources."""
        if self._composite_renderer is not None:
            try:
                self._composite_renderer.cleanup()
            except Exception:
                pass
            self._composite_renderer = None
