"""CompositeExportProvider -- adapter that mediates between app shell and composite render internals.

Provides a stable public API for export operations so the app layer
(dock_layout_controller, deliver_controller, etc.) never needs to
import from src.features.composite.render.* directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.features.composite.core.graph_snapshot import GraphSnapshot

logger = logging.getLogger(__name__)


@dataclass
class ExportResult:
    """Structured result from export rendering.

    Attributes:
        image: BGR float32 numpy array (h, w, 3) in [0, 1] range, or None on failure.
        error: Error message if export failed, None on success.
        used_tiling: Whether tile rendering was used.
        tile_count: Number of tiles rendered (0 for full-frame).
        render_time_ms: Total wall-clock render time in milliseconds.
    """
    image: Optional[np.ndarray] = None
    error: Optional[str] = None
    used_tiling: bool = False
    tile_count: int = 0
    render_time_ms: float = 0.0
    passes: dict[str, np.ndarray] = field(default_factory=dict)


class CompositeExportProvider:
    """Provides composite rendering for export without deep imports from the app layer.

    Usage from deliver_controller::

        provider = CompositeExportProvider()
        result = provider.render_full_res(
            snapshot=snapshot,
            interactive_scheduler=interactive_scheduler,
        )
        if result.image is not None:
            ...  # use result.image
    """

    def __init__(self, config_manager: object | None = None) -> None:
        """Create an export adapter with optional color-engine settings."""
        self._config_manager = config_manager

    def render_full_res(
        self,
        snapshot: GraphSnapshot,
        interactive_scheduler: object | None = None,
        render_pass: str = "beauty",
        color_image: np.ndarray | None = None,
        render_thread: object | None = None,
    ) -> ExportResult:
        """Render a single composite graph at full resolution and readback.

        Args:
            snapshot: Immutable graph snapshot.
            interactive_scheduler: Optional RenderScheduler from the interactive
                renderer to seed dirty tracking.
            render_pass: Semantic output pass to read back.

        Returns:
            ExportResult with image data on success, or error message on failure.
        """
        # When the live CompositeRenderer is available, submit the export to
        # its already-owned shared GL context.  This keeps one-shot/batch
        # composite readback off the UI thread while retaining a standalone
        # fallback for CLI/headless callers and tests.
        if color_image is not None and render_thread is not None:
            render_export = getattr(render_thread, "render_export", None)
            if callable(render_export):
                internal_result = render_export(
                    snapshot,
                    color_image,
                    config_manager=self._config_manager,
                    render_pass=render_pass,
                    interactive_scheduler=interactive_scheduler,
                )
                if internal_result is not None:
                    return ExportResult(
                        image=internal_result.image,
                        error=internal_result.error,
                        used_tiling=internal_result.used_tiling,
                        tile_count=internal_result.tile_count,
                        render_time_ms=internal_result.render_time_ms,
                    )
                return ExportResult(error="Composite render thread unavailable or timed out")

        try:
            from src.features.composite.render.composite_export_renderer import (
                CompositeExportRenderer as _Renderer,
            )
        except ImportError:
            logger.warning("CompositeExportRenderer not available")
            return ExportResult(error="Composite export renderer unavailable")

        renderer = _Renderer(interactive_scheduler=interactive_scheduler)
        if self._config_manager is not None:
            renderer.set_config_manager(self._config_manager)
        try:
            internal_result = renderer.render_and_readback(
                snapshot, render_pass=render_pass)
            return ExportResult(
                image=internal_result.image,
                error=internal_result.error,
                used_tiling=internal_result.used_tiling,
                tile_count=internal_result.tile_count,
                render_time_ms=internal_result.render_time_ms,
            )
        except Exception as e:
            logger.error(f"Export provider render error: {e}")
            return ExportResult(error=str(e))

    def render_full_res_passes(
        self,
        snapshot: GraphSnapshot,
        render_passes: Optional[tuple[str, ...] | list[str]] = None,
        interactive_scheduler: object | None = None,
        render_thread: object | None = None,
    ) -> ExportResult:
        """Evaluate once and read back multiple beauty/AOV passes.

        The adapter keeps the app layer independent from the renderer while
        exposing the multi-pass contract needed by layered delivery code.

        Args:
            snapshot: Immutable graph snapshot.
            render_passes: Semantic output passes to read back (None = all).
            interactive_scheduler: Optional RenderScheduler to seed dirty
                tracking.
            render_thread: Optional live ``CompositeRenderer``. When provided
                and it exposes ``render_export_passes``, the export is
                submitted to the renderer's already-owned shared GL context
                (same routing contract as :meth:`render_full_res`).

        Thread affinity (AUD-6200):
            The standalone fallback constructs a private
            ``CompositeExportRenderer`` and therefore creates a GL context on
            the *calling* thread. Callers without a render thread must invoke
            this method from a worker thread — never from the UI thread.
            Wiring a render thread keeps all GL work on its worker thread.
        """
        if render_thread is not None:
            render_export_passes = getattr(
                render_thread, "render_export_passes", None)
            if callable(render_export_passes):
                internal_result = render_export_passes(
                    snapshot,
                    render_passes=render_passes,
                    interactive_scheduler=interactive_scheduler,
                    config_manager=self._config_manager,
                )
                if internal_result is not None:
                    return ExportResult(
                        image=internal_result.image,
                        passes=dict(internal_result.passes),
                        error=internal_result.error,
                        used_tiling=internal_result.used_tiling,
                        tile_count=internal_result.tile_count,
                        render_time_ms=internal_result.render_time_ms,
                    )
                return ExportResult(
                    error="Composite render thread unavailable or timed out")

        logger.warning(
            "render_full_res_passes: no render-thread route available; "
            "falling back to a standalone GL context on the calling thread "
            "(must not be the UI thread)")
        try:
            from src.features.composite.render.composite_export_renderer import (
                CompositeExportRenderer as _Renderer,
            )
        except ImportError:
            logger.warning("CompositeExportRenderer not available")
            return ExportResult(error="Composite export renderer unavailable")

        renderer = _Renderer(interactive_scheduler=interactive_scheduler)
        if self._config_manager is not None:
            renderer.set_config_manager(self._config_manager)
        try:
            internal_result = renderer.render_and_readback_passes(
                snapshot, render_passes=render_passes)
            return ExportResult(
                image=internal_result.image,
                passes=dict(internal_result.passes),
                error=internal_result.error,
                used_tiling=internal_result.used_tiling,
                tile_count=internal_result.tile_count,
                render_time_ms=internal_result.render_time_ms,
            )
        except Exception as e:
            logger.error(f"Composite multi-pass export error: {e}")
            return ExportResult(error=str(e))
