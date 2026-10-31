"""
Layer adapter for project serialization.

Provides a thin indirection layer so that shared/project/ does not
directly depend on the features color module. The actual
Layer class is resolved lazily at first use.
"""

from __future__ import annotations

import logging
from importlib import import_module
from typing import Any, Optional

logger = logging.getLogger(__name__)

_layer_cls = None


def _get_layer_class():
    """Lazily resolve the Layer class from features.color."""
    global _layer_cls
    if _layer_cls is None:
        module = import_module("src.features.color.core.layer")
        _layer_cls = module.Layer
    return _layer_cls


def layer_from_dict(data: dict, assets_dir: Optional[str] = None) -> Any:
    """
    Deserialize a Layer from a dict.

    Delegates to Layer.from_dict() after lazily importing the class.

    Args:
        data: Serialized layer data
        assets_dir: Optional path to assets directory for bitmap loading

    Returns:
        Layer instance
    """
    Layer = _get_layer_class()
    return Layer.from_dict(data, assets_dir=assets_dir)
