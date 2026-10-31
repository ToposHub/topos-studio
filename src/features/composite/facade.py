"""CompositeFacade -- thin re-export layer for composite types used by app controllers.

Instead of each controller importing individually from
src.features.composite.core.* (deep import), the controller imports
from this facade at the feature boundary.

This module is a *re-export shim*: it does not add behaviour, it merely
consolidates the surface area so the app layer has fewer deep-import
lines to maintain.  All imports are lazy (inside functions) so that
the module loads cleanly even when the composite feature is disabled.
"""
from __future__ import annotations

import logging
import threading
import types
from typing import Any, Optional

logger = logging.getLogger(__name__)

_NODE_PROVIDER_LOCK = threading.RLock()
_NODE_PROVIDERS: dict[str, Any] = {}


def get_teaching_animation_node_provider() -> Any:
    """Return the optional Teaching Animation node provider through this boundary."""
    try:
        from src.features.teaching_animation.host_adapters import create_motion_graphic_node_provider
        return create_motion_graphic_node_provider()
    except ImportError:
        logger.warning("Teaching Animation node provider unavailable")
        return None


def get_teaching_animation_effect_registry() -> Any:
    """Return optional ModernGL effect descriptors without importing ModernGL."""
    try:
        from src.features.teaching_animation.render.advanced import ModernGLEffectRegistry
        registry = ModernGLEffectRegistry()
        registry.register("glow", parameters=("radius", "intensity"))
        registry.register("particles", parameters=("count", "speed"))
        registry.register("mask", parameters=("feather", "opacity"))
        return registry
    except ImportError:
        logger.warning("Teaching Animation advanced effects unavailable")
        return None


def open_teaching_animation_node_editor(node: Any, scenes: dict[str, Any], *, registry: Any = None,
                                        parent: Any = None, saver: Any = None) -> Any:
    """Open a Motion Graphic Node's shared scene through the Composite boundary."""
    try:
        from src.features.teaching_animation.host_adapters import open_scene_editor
        scene = scenes.get(getattr(node, "scene_id", ""))
        if scene is None:
            raise KeyError(f"Teaching scene is not registered: {getattr(node, 'scene_id', '')}")
        return open_scene_editor(scene, registry=registry, parent=parent, saver=saver)
    except ImportError:
        logger.warning("Teaching Animation editor unavailable")
        return None


def register_node_provider(provider_id: str, provider: Any) -> bool:
    """Register an optional node provider through the Composite boundary.

    Providers are descriptors/services owned by another feature.  The facade
    stores them without importing their implementation, so Composite remains
    usable when the provider package is absent.  Duplicate registration is
    idempotent for the same object and rejected for a different provider.
    """
    if not provider_id:
        raise ValueError("provider_id cannot be empty")
    with _NODE_PROVIDER_LOCK:
        existing = _NODE_PROVIDERS.get(provider_id)
        if existing is not None:
            return existing is provider
        _NODE_PROVIDERS[provider_id] = provider
    logger.info("Registered Composite node provider: %s", provider_id)
    return True


def unregister_node_provider(provider_id: str) -> bool:
    """Unregister a provider and return whether it was present."""
    with _NODE_PROVIDER_LOCK:
        if provider_id not in _NODE_PROVIDERS:
            return False
        del _NODE_PROVIDERS[provider_id]
    logger.info("Unregistered Composite node provider: %s", provider_id)
    return True


def get_node_providers() -> dict[str, Any]:
    """Return a snapshot of registered optional node providers."""
    with _NODE_PROVIDER_LOCK:
        return dict(_NODE_PROVIDERS)


def get_format_classes() -> Optional[dict]:
    """Return ImageFormat and BBox classes.

    Returns None if the composite module is unavailable.
    """
    try:
        from src.features.composite.core.format import ImageFormat, BBox
        return {
            "ImageFormat": ImageFormat,
            "BBox": BBox,
        }
    except ImportError:
        logger.warning("Composite format module unavailable")
        return None


def build_overlay_objects(meta: dict) -> tuple:
    """Build image_format and bbox overlay objects from metadata dict.

    Returns (image_format, bbox) tuple.  Each element is either the real
    composite object or a SimpleNamespace fallback when composite is
    unavailable.  Returns (None, None) when no data is present.
    """
    fmt_classes = get_format_classes()

    fmt_data = meta.get("format")
    bbox_data = meta.get("bbox")

    fmt = None
    bbox = None

    if fmt_classes is not None:
        ImageFormat = fmt_classes["ImageFormat"]
        BBox = fmt_classes["BBox"]
        if fmt_data:
            fmt = ImageFormat(
                width=fmt_data["width"], height=fmt_data["height"],
                pixel_aspect=fmt_data.get("pixel_aspect", 1.0),
                name=fmt_data.get("name", ""),
                origin_x=fmt_data.get("origin_x", 0),
                origin_y=fmt_data.get("origin_y", 0),
            )
        if bbox_data:
            bbox = BBox(
                x=bbox_data["x"], y=bbox_data["y"],
                width=bbox_data["width"], height=bbox_data["height"],
            )
    else:
        # Fallback: construct simple namespace objects from the dict data
        if fmt_data:
            fmt = types.SimpleNamespace(**fmt_data)
        if bbox_data:
            bbox = types.SimpleNamespace(**bbox_data)

    return fmt, bbox


def get_node_graph_class() -> Optional[type]:
    """Return the NodeGraph class.

    Returns None if the composite module is unavailable.
    """
    try:
        from src.features.composite.core.node_engine import NodeGraph
        return NodeGraph
    except ImportError:
        logger.warning("Composite node_engine module unavailable")
        return None


def get_composite_runtime_class() -> Optional[type]:
    """Return the CompositeRuntime class.

    Returns None if the composite module is unavailable.
    """
    try:
        from src.features.composite.runtime import CompositeRuntime
        return CompositeRuntime
    except ImportError:
        logger.warning("Composite runtime module unavailable")
        return None


def get_main_window_adapter_class() -> Optional[type]:
    """Return the MainWindowAdapter class.

    Returns None if the composite module is unavailable.
    """
    try:
        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter
        return MainWindowAdapter
    except ImportError:
        logger.warning("Composite main_window_adapter module unavailable")
        return None


def get_composite_command_class() -> Optional[type]:
    """Return the CompositeCommand enum.

    Returns None if the composite module is unavailable.
    """
    try:
        from src.features.composite.render.composite_renderer import CompositeCommand
        return CompositeCommand
    except ImportError:
        logger.warning("Composite renderer module unavailable")
        return None


def make_composite_runtime_context(**kwargs) -> Any:
    """Create a CompositeRuntimeContext for a timeline clip.

    Keyword arguments are forwarded to ``CompositeRuntimeContext.for_timeline_clip()``.
    Returns None if the module is unavailable.
    """
    try:
        from src.features.composite.core.runtime_context import CompositeRuntimeContext
        return CompositeRuntimeContext.for_timeline_clip(**kwargs)
    except ImportError:
        logger.warning("CompositeRuntimeContext module unavailable")
        return None
