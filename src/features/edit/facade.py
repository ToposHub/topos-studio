"""EditFacade -- thin re-export layer for edit types used by app controllers.

Instead of each controller importing individually from
src.features.edit.core.* (deep import), the controller imports
from this facade at the feature boundary.

This module is a *re-export shim*: it does not add behaviour, it merely
consolidates the surface area so the app layer has fewer deep-import
lines to maintain.  All imports are lazy (inside functions) so that
the module loads cleanly even when the edit feature is disabled.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def open_teaching_animation_clip_editor(clip: Any, scenes: dict[str, Any], *, registry: Any = None,
                                        parent: Any = None, saver: Any = None) -> Any:
    """Open a Teaching Animation Clip through the Edit feature boundary."""
    try:
        from src.features.teaching_animation.host_adapters import open_clip_editor
        return open_clip_editor(clip, scenes, registry=registry, parent=parent, saver=saver)
    except ImportError:
        logger.warning("Teaching Animation editor unavailable")
        return None


# ---------------------------------------------------------------------------
# CompositeGraphData helpers
# ---------------------------------------------------------------------------

def parse_composite_graph_data(data: dict | Any) -> Any:
    """Parse serialized dict into a CompositeGraphData object.

    If *data* is already a CompositeGraphData instance, return as-is.
    If it is a dict, call ``CompositeGraphData.from_dict(data)``.
    Returns None when the module is unavailable **or** when *data* is
    neither a dict nor a CompositeGraphData (AUD-19301: external/project
    data is no longer passed through unvalidated — the previous
    pass-through contradicted this docstring and let garbage of any type
    flow into callers that expect a parsed graph or None).
    """
    try:
        from src.features.edit.core.composite_graph_data import CompositeGraphData
    except ImportError:
        logger.warning("CompositeGraphData module unavailable")
        return None

    if isinstance(data, CompositeGraphData):
        return data
    if isinstance(data, dict):
        return CompositeGraphData.from_dict(data)
    logger.warning(
        "parse_composite_graph_data: expected dict or CompositeGraphData, "
        "got %s; returning None", type(data).__name__)
    return None


# ---------------------------------------------------------------------------
# RenderCache helpers
# ---------------------------------------------------------------------------

def get_render_cache_status() -> Any:
    """Return the RenderCacheStatus enum.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.render_cache import RenderCacheStatus
        return RenderCacheStatus
    except ImportError:
        logger.warning("RenderCacheStatus module unavailable")
        return None


def get_render_cache_manifest_class() -> Any:
    """Return the RenderCacheManifest class.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.render_cache import RenderCacheManifest
        return RenderCacheManifest
    except ImportError:
        logger.warning("RenderCacheManifest module unavailable")
        return None


def create_render_cache_manifest(**kwargs) -> Any:
    """Create a RenderCacheManifest with the given fields.

    Keyword arguments are forwarded to the RenderCacheManifest constructor.
    Returns None if the module is unavailable.
    """
    Manifest = get_render_cache_manifest_class()
    if Manifest is None:
        return None
    return Manifest(**kwargs)


def create_render_cache_manifest_new(**kwargs) -> Any:
    """Call RenderCacheManifest.create_new() with the given fields.

    Returns None if the module is unavailable.
    """
    Manifest = get_render_cache_manifest_class()
    if Manifest is None:
        return None
    return Manifest.create_new(**kwargs)


def create_render_cache_worker(manifest: Any, edit_project: Any,
                               timeline_sequences: dict) -> Any:
    """Create and return a RenderCacheWorker.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.render_cache_worker import RenderCacheWorker
        return RenderCacheWorker(
            manifest=manifest,
            edit_project=edit_project,
            timeline_sequences=timeline_sequences,
        )
    except ImportError:
        logger.warning("RenderCacheWorker module unavailable")
        return None


# ---------------------------------------------------------------------------
# GPU preview renderer helpers
# ---------------------------------------------------------------------------

def create_edit_gpu_decode_manager() -> Any:
    """Create and return an EditGpuDecodeManager.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
        return EditGpuDecodeManager()
    except ImportError:
        logger.warning("EditGpuDecodeManager module unavailable")
        return None


def create_edit_gpu_preview_renderer(main_context: Any, parent: Any = None) -> Any:
    """Create and return an EditGpuPreviewRenderer.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
        return EditGpuPreviewRenderer(main_context=main_context, parent=parent)
    except ImportError:
        logger.warning("EditGpuPreviewRenderer module unavailable")
        return None


def create_edit_render_task(frame: int, generation_id: int,
                            layers: list, viewport_size: tuple,
                            output_fbo_size: tuple,
                            coordinate_size: tuple | None = None) -> Any:
    """Create and return an EditRenderTask.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask
        return EditRenderTask(
            frame=frame,
            generation_id=generation_id,
            layers=layers,
            viewport_size=viewport_size,
            output_fbo_size=output_fbo_size,
            coordinate_size=coordinate_size,
        )
    except ImportError:
        logger.warning("EditRenderTask module unavailable")
        return None


# ---------------------------------------------------------------------------
# TimelineClipFrameProvider helper
# ---------------------------------------------------------------------------

def create_timeline_clip_frame_provider() -> Any:
    """Create and return a TimelineClipFrameProvider.

    Returns None if the module is unavailable.
    """
    try:
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        return TimelineClipFrameProvider()
    except ImportError:
        logger.warning("TimelineClipFrameProvider module unavailable")
        return None
