"""Topos Studio product entry point.

Topos Studio is the full product including all features:
- Color (color grading, core)
- Edit (timeline editing)
- Composite (node-based compositing)
- Deliver (export and delivery)
"""

import logging
from typing import TYPE_CHECKING

from src.products import ProductPreset, PRODUCT_PRESETS

if TYPE_CHECKING:
    from src.app.feature_registry import FeatureRegistry

logger = logging.getLogger(__name__)


def get_preset() -> ProductPreset:
    """Return the Topos Studio product preset."""
    return PRODUCT_PRESETS["topos-studio"]


def register(registry: "FeatureRegistry") -> None:
    """Register all feature modules for Topos Studio.

    [R2-3] 注册实现收敛到 module_loader 的单一数据驱动表，本入口只负责
    提供 preset（Color 恒注册 guarded，其余按 preset 启用集过滤）。

    Args:
        registry: FeatureRegistry instance (must be initialized).
    """
    from src.app.module_loader import register_preset_modules
    register_preset_modules(registry, get_preset())
