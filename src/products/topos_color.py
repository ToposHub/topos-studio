"""Topos Color product entry point.

Topos Color is the color grading focused product:
- Color (color grading, core)
- Deliver (lightweight still/image export only)

Edit and Composite are disabled.
"""

import logging
from typing import TYPE_CHECKING

from src.products import ProductPreset, PRODUCT_PRESETS

if TYPE_CHECKING:
    from src.app.feature_registry import FeatureRegistry

logger = logging.getLogger(__name__)


def get_preset() -> ProductPreset:
    """Return the Topos Color product preset."""
    return PRODUCT_PRESETS["topos-color"]


def register(registry: "FeatureRegistry") -> None:
    """Register feature modules for Topos Color.

    Registers Color (core) and lightweight Deliver.  Edit and Composite are
    intentionally excluded.

    [R2-3] 注册实现收敛到 module_loader 的单一数据驱动表，本入口只负责
    提供 preset（Color 恒注册 guarded，其余按 preset 启用集过滤）。

    Args:
        registry: FeatureRegistry instance (must be initialized).
    """
    from src.app.module_loader import register_preset_modules
    register_preset_modules(registry, get_preset())
