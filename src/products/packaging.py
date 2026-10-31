"""Packaging configuration for product variants.

Separates "feature flags" (runtime toggling) from "packaging include/exclude"
(build-time decisions about which source directories, locales, and plugins to
include in a distribution).

Usage:
    from src.products.packaging import get_packaging_config
    config = get_packaging_config("topos-color")
    print(config["include_dirs"])   # directories under src/features/ to ship
    print(config["exclude_dirs"])   # directories under src/features/ to omit
"""

import logging
from typing import Dict, List

from src.products import (
    PRODUCT_PRESETS,
    ProductPreset,
    FEATURE_LIBRARY,
    FEATURE_COLOR,
    FEATURE_EDIT,
    FEATURE_COMPOSITE,
    FEATURE_DELIVER,
    FEATURE_TEACHING_ANIMATION,
)

logger = logging.getLogger(__name__)

# Map each feature to its src/features/ subdirectory
_FEATURE_TO_DIR: dict[str, str] = {
    FEATURE_LIBRARY: "color",      # library is bundled with color
    FEATURE_COLOR: "color",
    FEATURE_EDIT: "edit",
    FEATURE_COMPOSITE: "composite",
    FEATURE_DELIVER: "deliver",
    FEATURE_TEACHING_ANIMATION: "teaching_animation",
}

# Map each product to its locale file prefixes (derived from enabled features)
# Locales are stored in src/shared/i18n/locales/ as JSON files shared across
# all products, so this is informational only for packaging scripts.

# Composite node plugins to include per product
_PLUGIN_MAP: dict[str, List[str]] = {
    "topos-studio": ["*"],          # all plugins
    "topos-color": [],              # no composite plugins
    "topos-edit": [],               # no composite plugins
    "topos-composite": ["*"],       # all composite plugins
}


def get_packaging_config(product_name: str) -> Dict[str, List[str]]:
    """Return packaging configuration for the given product.

    Args:
        product_name: Product identifier (e.g., "topos-studio", "topos-color").

    Returns:
        Dict with keys:
            include_features: list of feature identifiers to include.
            exclude_features: list of feature identifiers to exclude.
            include_dirs: list of src/features/ directories to include.
            exclude_dirs: list of src/features/ directories to exclude.
            include_locales: list of feature identifiers whose translation
                keys are active in this product (AUD-21605: these are
                feature IDs, *not* "locale file prefixes" — every product
                ships all locale files since they are shared; the field
                mirrors ``include_features``).
            include_plugins: list of plugin names (or ["*"] for all).

    Raises:
        ValueError: If product_name is not a known product.
    """
    if product_name not in PRODUCT_PRESETS:
        raise ValueError(
            f"Unknown product: {product_name!r}. "
            f"Available: {list(PRODUCT_PRESETS.keys())}"
        )

    preset: ProductPreset = PRODUCT_PRESETS[product_name]
    enabled = preset.resolved_enabled
    disabled = preset.resolved_disabled

    include_features = sorted(enabled)
    exclude_features = sorted(disabled)

    include_dirs = sorted({
        _FEATURE_TO_DIR[f] for f in enabled if f in _FEATURE_TO_DIR
    })
    exclude_dirs = sorted({
        _FEATURE_TO_DIR[f] for f in disabled if f in _FEATURE_TO_DIR
    })

    # Locales: all products ship all locale files (they are shared).
    # The include_locales field documents which feature-related keys are active.
    include_locales = sorted(enabled)

    include_plugins = _PLUGIN_MAP.get(product_name, [])

    return {
        "include_features": include_features,
        "exclude_features": exclude_features,
        "include_dirs": include_dirs,
        "exclude_dirs": exclude_dirs,
        "include_locales": include_locales,
        "include_plugins": include_plugins,
    }
