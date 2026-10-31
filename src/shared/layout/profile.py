"""
LayoutProfile - Immutable data class for a single layout snapshot.

A LayoutProfile captures the dock arrangement for a specific
(product_mode, page_id) pair. It stores:
- Which panels should be visible
- The Qt saveState hex blob for dock positions/sizes
- Floating panel geometry overrides

LayoutProfiles are stored by LayoutStore in config, keyed by
"layout_profiles/{product_mode}/{page_id}".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


LAYOUT_PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LayoutProfile:
    """
    Immutable snapshot of a dock layout for one (product_mode, page_id) pair.

    Attributes:
        product_mode: Product mode string (e.g., "color", "studio", "edit").
        page_id: Page identifier (e.g., "Color", "Edit", "Composite").
        visible_panels: Ordered list of panel_id strings that should be visible.
        qt_state_hex: Optional Qt saveState() hex-encoded string.
        floating_panels: Dict of panel_id -> {x, y, w, h} for floating panels.
        schema_version: Schema version for forward-compatible migration.
    """

    product_mode: str
    page_id: str
    visible_panels: tuple[str, ...] = ()
    qt_state_hex: str = ""
    floating_panels: dict[str, dict[str, int]] = field(default_factory=dict)
    schema_version: int = LAYOUT_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.product_mode:
            raise ValueError("product_mode cannot be empty")
        if not self.page_id:
            raise ValueError("page_id cannot be empty")

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "product_mode": self.product_mode,
            "page_id": self.page_id,
            "visible_panels": list(self.visible_panels),
            "qt_state_hex": self.qt_state_hex,
            "floating_panels": dict(self.floating_panels),
            "schema_version": self.schema_version,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> LayoutProfile:
        """Deserialize from a dict produced by to_dict().

        Config JSON is external data (hand-edited or half-corrupted
        entries are possible, AUD-34100), so every field is type-checked:
        malformed scalar fields fall back to their declared defaults and
        malformed list/dict shapes are dropped. A missing, empty or
        non-string product_mode/page_id raises ValueError — callers
        (LayoutStore.get_profile / get_profiles_for_mode) treat that as
        "skip this entry" instead of failing startup.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"layout profile entry must be a dict, "
                f"got {type(data).__name__}")

        product_mode = data.get("product_mode", "")
        if not isinstance(product_mode, str):
            raise ValueError(
                f"layout profile product_mode must be str, "
                f"got {type(product_mode).__name__}")

        page_id = data.get("page_id", "")
        if not isinstance(page_id, str):
            raise ValueError(
                f"layout profile page_id must be str, "
                f"got {type(page_id).__name__}")

        visible = data.get("visible_panels", [])
        if isinstance(visible, list):
            visible_panels = tuple(
                p for p in visible if isinstance(p, str))
        else:
            visible_panels = tuple()

        qt_state_hex = data.get("qt_state_hex", "")
        if not isinstance(qt_state_hex, str):
            qt_state_hex = ""

        floating = data.get("floating_panels", {})
        if isinstance(floating, dict):
            floating_panels = {
                str(panel): geom
                for panel, geom in floating.items()
                if isinstance(geom, dict)
            }
        else:
            floating_panels = {}

        schema_version = data.get("schema_version", LAYOUT_PROFILE_SCHEMA_VERSION)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            schema_version = LAYOUT_PROFILE_SCHEMA_VERSION

        return LayoutProfile(
            product_mode=product_mode,
            page_id=page_id,
            visible_panels=visible_panels,
            qt_state_hex=qt_state_hex,
            floating_panels=floating_panels,
            schema_version=schema_version,
        )

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(product_mode: str, page_id: str) -> str:
        """Return the config key used to store this profile."""
        return f"layout_profiles/{product_mode}/{page_id}"

    @property
    def config_key(self) -> str:
        """The config key under which this profile is stored."""
        return self.make_key(self.product_mode, self.page_id)

    def with_visible_panels(self, panel_ids: list[str]) -> LayoutProfile:
        """Return a new profile with updated visible_panels."""
        return LayoutProfile(
            product_mode=self.product_mode,
            page_id=self.page_id,
            visible_panels=tuple(panel_ids),
            qt_state_hex=self.qt_state_hex,
            floating_panels=self.floating_panels,
            schema_version=self.schema_version,
        )

    def with_qt_state(self, qt_state_hex: str) -> LayoutProfile:
        """Return a new profile with updated Qt state."""
        return LayoutProfile(
            product_mode=self.product_mode,
            page_id=self.page_id,
            visible_panels=self.visible_panels,
            qt_state_hex=qt_state_hex,
            floating_panels=self.floating_panels,
            schema_version=self.schema_version,
        )

    def with_floating_panels(
        self, floating: dict[str, dict[str, int]]
    ) -> LayoutProfile:
        """Return a new profile with updated floating panel geometry."""
        return LayoutProfile(
            product_mode=self.product_mode,
            page_id=self.page_id,
            visible_panels=self.visible_panels,
            qt_state_hex=self.qt_state_hex,
            floating_panels=floating,
            schema_version=self.schema_version,
        )

    def __repr__(self) -> str:
        return (
            f"LayoutProfile(product_mode={self.product_mode!r}, "
            f"page_id={self.page_id!r}, "
            f"visible={len(self.visible_panels)}, "
            f"schema={self.schema_version})"
        )
