"""Product presets and entry points for Topos product variants.

Products:
    topos-studio    = color + edit + composite + deliver (full product)
    topos-color     = color + lightweight deliver (color grading focused)
    topos-edit      = edit + deliver + minimal color viewer/transform
    topos-composite = composite + deliver + minimal color viewer/transform

Usage:
    from src.products import get_current_product, get_enabled_features
    preset = get_current_product()
    features = get_enabled_features()
"""

import os
import logging
from dataclasses import dataclass, field

from src.shared.feature_flags import load_feature_flags

logger = logging.getLogger(__name__)

# Core feature identifiers (must match src/shared/feature_flags.py keys)
FEATURE_LIBRARY = "library"
FEATURE_COLOR = "color"
FEATURE_EDIT = "edit"
FEATURE_COMPOSITE = "composite"
FEATURE_DELIVER = "deliver"
FEATURE_TEACHING_ANIMATION = "teaching_animation"

ALL_FEATURES = frozenset({
    FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_EDIT, FEATURE_COMPOSITE,
    FEATURE_DELIVER, FEATURE_TEACHING_ANIMATION,
})

# Color is always enabled in every product
ALWAYS_ENABLED = frozenset({FEATURE_COLOR, FEATURE_LIBRARY})


@dataclass(frozen=True)
class ProductPreset:
    """Defines a product configuration with enabled and disabled features.

    Attributes:
        name: Internal product identifier (e.g., "topos-studio").
        display_name: Human-readable product name.
        enabled_features: Features explicitly enabled for this product.
        disabled_features: Features explicitly disabled for this product.
        description: Short description of the product variant.
    """
    name: str
    display_name: str
    enabled_features: frozenset = field(default_factory=frozenset)
    disabled_features: frozenset = field(default_factory=frozenset)
    description: str = ""

    @property
    def resolved_enabled(self) -> frozenset:
        """Compute the final enabled set: enabled minus disabled, plus always-on features."""
        return (self.enabled_features - self.disabled_features) | ALWAYS_ENABLED

    @property
    def resolved_disabled(self) -> frozenset:
        """Features that are disabled after resolving overrides."""
        return self.disabled_features - ALWAYS_ENABLED


# ---------------------------------------------------------------------------
# Product preset definitions
# ---------------------------------------------------------------------------

PRODUCT_PRESETS: dict[str, ProductPreset] = {
    "topos-studio": ProductPreset(
        name="topos-studio",
        display_name="Topos Studio",
        enabled_features=frozenset(ALL_FEATURES),
        disabled_features=frozenset(),
        description="Full product: color grading, editing, compositing, and delivery.",
    ),
    "topos-color": ProductPreset(
        name="topos-color",
        display_name="Topos Color",
        enabled_features=frozenset({FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_DELIVER}),
        disabled_features=frozenset({FEATURE_EDIT, FEATURE_COMPOSITE, FEATURE_TEACHING_ANIMATION}),
        description="Color grading focused: color tools plus lightweight still/image export.",
    ),
    "topos-edit": ProductPreset(
        name="topos-edit",
        display_name="Topos Edit",
        enabled_features=frozenset({FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_EDIT, FEATURE_DELIVER}),
        disabled_features=frozenset({FEATURE_COMPOSITE, FEATURE_TEACHING_ANIMATION}),
        description="Editing focused: timeline editing, delivery, and minimal color viewer/transform.",
    ),
    "topos-composite": ProductPreset(
        name="topos-composite",
        display_name="Topos Composite",
        enabled_features=frozenset({
            FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_COMPOSITE, FEATURE_DELIVER,
            FEATURE_TEACHING_ANIMATION,
        }),
        disabled_features=frozenset({FEATURE_EDIT}),
        description="Compositing focused: node-based compositing, delivery, and minimal color viewer/transform.",
    ),
}

# Map legacy short names used by TOPOS_PRODUCT_MODE to product preset keys
_SHORT_NAME_MAP: dict[str, str] = {
    "studio": "topos-studio",
    "color": "topos-color",
    "edit": "topos-edit",
    "composite": "topos-composite",
}

DEFAULT_PRODUCT = "topos-studio"


def get_current_product() -> ProductPreset:
    """Return the ProductPreset matching the TOPOS_PRODUCT_MODE env var.

    Falls back to ``topos-studio`` when the variable is unset or references
    an unknown product.  Both full names (``topos-studio``) and legacy short
    names (``studio``) are accepted.

    Returns:
        The resolved ProductPreset.
    """
    raw = os.environ.get("TOPOS_PRODUCT_MODE", "").strip().lower()

    if not raw:
        return PRODUCT_PRESETS[DEFAULT_PRODUCT]

    # Try full name first
    if raw in PRODUCT_PRESETS:
        return PRODUCT_PRESETS[raw]

    # Try legacy short name
    if raw in _SHORT_NAME_MAP:
        return PRODUCT_PRESETS[_SHORT_NAME_MAP[raw]]

    logger.warning(
        "Unknown TOPOS_PRODUCT_MODE=%r, falling back to %s",
        raw, DEFAULT_PRODUCT,
    )
    return PRODUCT_PRESETS[DEFAULT_PRODUCT]


def get_enabled_features() -> frozenset[str]:
    """Return the enabled feature set for the current product.

    [AUD-21601] Delegates to ``src.shared.feature_flags.load_feature_flags()``
    as the single parsing entry for ``TOPOS_PRODUCT_MODE`` /
    ``TOPOS_DISABLE_FEATURES`` / ``TOPOS_FEATURE_*``.  This module previously
    re-implemented the same environment parsing with drifted semantics
    (TOPOS_DISABLE_FEATURES was lower-cased here, ``library`` was pinned on,
    and unknown-mode fallback differed), so product registration / packaging
    checks and the runtime gate could reach different conclusions for the
    same environment.  Both tracks now agree by construction: this function
    projects the runtime flags onto the product-domain features
    (``ALL_FEATURES``).

    Returns:
        frozenset of enabled feature identifier strings.
    """
    flags = load_feature_flags()
    return frozenset(name for name in ALL_FEATURES if flags.is_enabled(name))
