"""
LayoutStore - Manages layout profiles per product_mode + page_id.

LayoutStore is a new coordination layer on top of the existing
DockLayoutMixin / page_layouts system. It:

- Saves / loads LayoutProfiles to/from ConfigManager
- Filters panels by enabled features when restoring
- Skips panels not present in the panel registry
- Migrates legacy page_layouts format from config
- Provides default layouts per product mode
- Isolates layouts across product modes

The existing DockLayoutMixin continues to manage the live Qt dock
state. LayoutStore provides the data model and persistence layer.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.shared.layout.profile import LayoutProfile, LAYOUT_PROFILE_SCHEMA_VERSION

logger = logging.getLogger(__name__)

# Config key prefix for layout profiles
LAYOUT_PROFILES_KEY = "layout_profiles"

# Config key for legacy page_layouts (pre-Phase-3B)
LEGACY_PAGE_LAYOUTS_KEY = "page_layouts"

# Panel-id prefixes that belong to the application shell rather than a
# feature-flag-gated module. DEFAULT_PANELS uses "workspace.viewport" /
# "workspace.library" / "workspace.layers", and the registry also defines
# "workspace.files" / "advanced.script" (src/features/color/module.py).
# FeatureFlags.get_enabled_features() never contains "workspace" or
# "advanced", so these prefixes must bypass feature filtering — a plain
# prefix check would drop every workspace panel from default layouts
# (AUD-34102).
NON_FEATURE_PANEL_PREFIXES = frozenset({"workspace", "advanced"})


class LayoutStore:
    """
    Manages layout profiles keyed by (product_mode, page_id).

    Usage::

        store = LayoutStore(config_manager)
        profile = store.get_profile("studio", "Color")
        if profile is None:
            profile = store.get_default_profile("studio", "Color")
        filtered = store.filter_panels(profile, enabled_features={"color", "library"})
        store.save_profile(profile)
    """

    def __init__(self, config: Any = None) -> None:
        """
        Initialize LayoutStore.

        Args:
            config: A ConfigManager-like object with get/set methods.
                    Can be None for pure in-memory usage (useful in tests).
        """
        self._config = config
        self._cache: dict[str, LayoutProfile] = {}

    # ------------------------------------------------------------------
    # Profile CRUD
    # ------------------------------------------------------------------

    def save_profile(self, profile: LayoutProfile) -> None:
        """
        Save a LayoutProfile to config.

        Args:
            profile: The LayoutProfile to persist.
        """
        key = profile.config_key
        self._cache[key] = profile

        if self._config is not None:
            self._set_config_nested(
                key, profile.to_dict(), auto_save=True
            )

        logger.debug(
            "Saved layout profile: %s/%s (%d visible panels)",
            profile.product_mode,
            profile.page_id,
            len(profile.visible_panels),
        )

    def get_profile(
        self, product_mode: str, page_id: str
    ) -> Optional[LayoutProfile]:
        """
        Load a LayoutProfile from config.

        Returns None if no profile is stored for the given key.

        Args:
            product_mode: Product mode string.
            page_id: Page identifier.

        Returns:
            LayoutProfile or None.
        """
        key = LayoutProfile.make_key(product_mode, page_id)

        # Check in-memory cache first
        if key in self._cache:
            return self._cache[key]

        # Load from config
        if self._config is not None:
            data = self._get_config_nested(key)
            if data is not None and isinstance(data, dict):
                # AUD-34100：外部配置条目可能畸形——单条坏数据按
                # ValueError 跳过并告警，不得沿启动路径上抛使应用崩溃。
                try:
                    profile = LayoutProfile.from_dict(data)
                except ValueError as exc:
                    logger.warning(
                        "Skipping malformed layout profile %s: %s", key, exc)
                    return None
                self._cache[key] = profile
                return profile

        return None

    def delete_profile(
        self, product_mode: str, page_id: str
    ) -> bool:
        """
        Delete a LayoutProfile from config.

        Args:
            product_mode: Product mode string.
            page_id: Page identifier.

        Returns:
            True if a profile was deleted, False otherwise.
        """
        key = LayoutProfile.make_key(product_mode, page_id)
        existed = key in self._cache

        self._cache.pop(key, None)

        if self._config is not None:
            self._delete_config_nested(key)

        if existed:
            logger.debug(
                "Deleted layout profile: %s/%s", product_mode, page_id
            )
        return existed

    def list_profiles(self, product_mode: str | None = None) -> list[str]:
        """
        List stored profile keys.

        Args:
            product_mode: If provided, filter to this product mode.

        Returns:
            List of config key strings.
        """
        all_profiles = self._get_all_profiles_raw()
        keys: list[str] = []

        for pm_key in all_profiles.values():
            for page_key in pm_key.values():
                if isinstance(page_key, dict):
                    # Reconstruct key from the structure
                    pass

        # Alternative: scan config for layout_profiles/* keys
        if self._config is not None:
            root = self._config.get(LAYOUT_PROFILES_KEY)
            if isinstance(root, dict):
                for pm, pages in root.items():
                    if product_mode is not None and pm != product_mode:
                        continue
                    if isinstance(pages, dict):
                        for page_id in pages:
                            keys.append(
                                LayoutProfile.make_key(pm, page_id)
                            )

        # Also include cached profiles not yet persisted
        for key in self._cache:
            if key not in keys:
                profile = self._cache[key]
                if product_mode is None or profile.product_mode == product_mode:
                    keys.append(key)

        return keys

    # ------------------------------------------------------------------
    # Legacy migration
    # ------------------------------------------------------------------

    def migrate_legacy_page_layouts(self) -> int:
        """
        Migrate legacy page_layouts from config to LayoutProfiles.

        Two legacy shapes exist (both written by the application):
        - flat: ``{page_id: qt_state_hex}``
        - mode-scoped: ``{effective_mode: {page_id: qt_state_hex}}``
          (writer side: dock_layout_controller ``saved_layouts[mode_key]
          = bucket``; mode_key is the product mode, or
          ``studio_image``/``studio_video``, or ``all``).

        The mode-scoped shape is resolved exactly like the live reader
        (dock_layout_controller.initialize_page_layouts): pick the
        current effective layout mode's bucket with the "all" bucket as
        fallback. Every entry of the resolved bucket is migrated for the
        current product mode. The flat shape migrates for the current
        product mode directly (AUD-34103 — previously mode-scoped
        configs silently migrated 0 entries because every page value is
        a dict, not a str).

        Returns:
            Number of profiles migrated.
        """
        if self._config is None:
            return 0

        legacy = self._config.get(LEGACY_PAGE_LAYOUTS_KEY)
        if not legacy or not isinstance(legacy, dict):
            return 0

        # Determine current product mode
        product_mode = self._get_product_mode()

        if any(isinstance(v, dict) for v in legacy.values()):
            # Mode-scoped {mode: {page: hex}} — resolve the bucket for the
            # current session (effective layout mode, "all" fallback),
            # mirroring the dock reader's dual-shape handling.
            elm = self._get_effective_layout_mode()
            entries: Any = None
            if isinstance(legacy.get(elm), dict):
                entries = legacy[elm]
            elif isinstance(legacy.get("all"), dict):
                entries = legacy["all"]
            if entries is None:
                return 0
        else:
            # Flat {page: hex} (non-str leaf values are skipped below).
            entries = legacy

        migrated = 0
        for page_id, state_hex in entries.items():
            if not isinstance(state_hex, str):
                continue

            # Skip if a profile already exists for this key
            existing = self.get_profile(product_mode, page_id)
            if existing is not None:
                logger.debug(
                    "Skipping migration for %s/%s (already exists)",
                    product_mode,
                    page_id,
                )
                continue

            profile = LayoutProfile(
                product_mode=product_mode,
                page_id=page_id,
                qt_state_hex=state_hex,
            )
            self.save_profile(profile)
            migrated += 1

        if migrated > 0:
            logger.info(
                "Migrated %d legacy page_layouts for product_mode=%s",
                migrated,
                product_mode,
            )

        return migrated

    # ------------------------------------------------------------------
    # Panel filtering
    # ------------------------------------------------------------------

    def filter_panels(
        self,
        profile: LayoutProfile,
        enabled_features: set[str] | None = None,
        registered_panel_ids: set[str] | None = None,
    ) -> LayoutProfile:
        """
        Filter a profile's visible_panels by enabled features and registry.

        Panels whose feature is disabled are removed. Panels not present
        in the registry (registered_panel_ids) are also removed.

        Args:
            profile: The source LayoutProfile.
            enabled_features: Set of enabled feature IDs (e.g., {"color", "library"}).
                              If None, no feature filtering is applied.
            registered_panel_ids: Set of currently registered panel_id strings.
                                  If None, no registry filtering is applied.

        Returns:
            A new LayoutProfile with filtered visible_panels.
        """
        # The Edit monitors now share one panel. Preserve old profiles' intent
        # without changing their opaque Qt state or unrelated panel choices.
        legacy_viewers = {"edit.program_viewer", "edit.source_viewer"}
        filtered: list[str] = []
        for panel_id in profile.visible_panels:
            migrated_id = "edit.viewport" if panel_id in legacy_viewers else panel_id
            if migrated_id not in filtered:
                filtered.append(migrated_id)

        # Expand the retired Color Mapping container into independent tools.
        if "color.mapping" in filtered:
            filtered = list(dict.fromkeys(
                replacement
                for panel_id in filtered
                for replacement in (("color.warper", "color.mixer")
                                    if panel_id == "color.mapping" else (panel_id,))
            ))
        # Warper inherits the former container's floating geometry; a second
        # overlapping Mixer window would not represent the saved layout.
        floating = dict(profile.floating_panels)
        if "color.mapping" in floating:
            floating.setdefault("color.warper", floating.pop("color.mapping"))

        # Filter by enabled features (using panel_id prefix convention)
        # Panel IDs follow "{feature}.{panel_name}" format
        if enabled_features is not None:
            filtered = [
                pid
                for pid in filtered
                if self._panel_feature_enabled(pid, enabled_features)
            ]

        # Filter by registered panels (skip missing panels)
        if registered_panel_ids is not None:
            before = len(filtered)
            filtered = [pid for pid in filtered if pid in registered_panel_ids]
            skipped = before - len(filtered)
            if skipped > 0:
                logger.debug(
                    "Skipped %d missing panels during restore", skipped
                )

        if tuple(filtered) == profile.visible_panels and floating == profile.floating_panels:
            return profile

        return profile.with_visible_panels(filtered).with_floating_panels(floating)

    @staticmethod
    def _panel_feature_enabled(
        panel_id: str, enabled_features: set[str]
    ) -> bool:
        """
        Check if a panel's feature is enabled.

        Panel IDs use "{feature}.{name}" format. Shell prefixes
        (workspace.*/advanced.*, see NON_FEATURE_PANEL_PREFIXES) and
        dot-less IDs are always enabled; every other feature prefix is
        matched directly against enabled_features.
        """
        if "." in panel_id:
            feature_prefix = panel_id.split(".")[0]
            if feature_prefix in NON_FEATURE_PANEL_PREFIXES:
                # Shell panels are not gated by any feature flag — the
                # feature registry contains no "workspace"/"advanced"
                # flags, so they must bypass feature filtering (AUD-34102).
                return True
            return feature_prefix in enabled_features
        # Panels without feature prefix (e.g., legacy workspace panels) are
        # always enabled
        return True

    # ------------------------------------------------------------------
    # Default profiles
    # ------------------------------------------------------------------

    # Default visible panels per product mode per page.
    # These provide a sensible starting layout when no saved profile exists.
    DEFAULT_PANELS: dict[str, dict[str, list[str]]] = {
        "color": {
            "Color": [
                "workspace.viewport",
                "workspace.library",
                "workspace.layers",
                "color.grading",
                "color.curves",
                "color.warper",
                "color.mixer",
                "color.lut",
                "color.analysis",
                "color.masks",
                "color.qualifier",
            ],
        },
        "studio": {
            "Color": [
                "workspace.viewport",
                "workspace.library",
                "workspace.layers",
                "color.grading",
                "color.curves",
                "color.warper",
                "color.mixer",
                "color.analysis",
            ],
            "Edit": [
                "edit.media_pool",
                "edit.viewport",
                "edit.timeline",
                "edit.properties",
                "edit.effects",
            ],
            "Composite": [
                "composite.node_graph",
                "composite.performance",
            ],
            "Deliver": [
                "deliver.export",
            ],
        },
        "edit": {
            "Color": [
                "workspace.viewport",
                "color.grading",
                "color.curves",
                "color.analysis",
            ],
            "Edit": [
                "edit.media_pool",
                "edit.viewport",
                "edit.timeline",
                "edit.properties",
                "edit.effects",
            ],
        },
        "composite": {
            "Color": [
                "workspace.viewport",
                "color.grading",
                "color.curves",
            ],
            "Composite": [
                "composite.node_graph",
                "composite.performance",
            ],
        },
    }

    def get_default_profile(
        self, product_mode: str, page_id: str
    ) -> LayoutProfile:
        """
        Get the default LayoutProfile for a (product_mode, page_id) pair.

        Args:
            product_mode: Product mode string.
            page_id: Page identifier.

        Returns:
            A LayoutProfile with default visible_panels.
        """
        mode_defaults = self.DEFAULT_PANELS.get(product_mode, {})
        panels = mode_defaults.get(page_id, [])

        return LayoutProfile(
            product_mode=product_mode,
            page_id=page_id,
            visible_panels=tuple(panels),
            schema_version=LAYOUT_PROFILE_SCHEMA_VERSION,
        )

    def get_or_default(
        self,
        product_mode: str,
        page_id: str,
        enabled_features: set[str] | None = None,
        registered_panel_ids: set[str] | None = None,
    ) -> LayoutProfile:
        """
        Get a saved profile, or fall back to the default.

        The returned profile is filtered by enabled features and
        registered panel IDs.

        Args:
            product_mode: Product mode string.
            page_id: Page identifier.
            enabled_features: Set of enabled feature IDs.
            registered_panel_ids: Set of registered panel IDs.

        Returns:
            A filtered LayoutProfile.
        """
        profile = self.get_profile(product_mode, page_id)
        if profile is None:
            profile = self.get_default_profile(product_mode, page_id)

        return self.filter_panels(
            profile,
            enabled_features=enabled_features,
            registered_panel_ids=registered_panel_ids,
        )

    # ------------------------------------------------------------------
    # Cross-product isolation
    # ------------------------------------------------------------------

    def get_profiles_for_mode(
        self, product_mode: str
    ) -> dict[str, LayoutProfile]:
        """
        Get all stored profiles for a given product mode.

        Args:
            product_mode: Product mode string.

        Returns:
            Dict mapping page_id to LayoutProfile.
        """
        result: dict[str, LayoutProfile] = {}

        if self._config is not None:
            root = self._config.get(LAYOUT_PROFILES_KEY)
            if isinstance(root, dict):
                pages = root.get(product_mode, {})
                if isinstance(pages, dict):
                    for page_id, data in pages.items():
                        if isinstance(data, dict):
                            key = LayoutProfile.make_key(product_mode, page_id)
                            if key in self._cache:
                                result[page_id] = self._cache[key]
                            else:
                                # AUD-34100：同 get_profile——畸形条目跳过
                                # 并告警，防止单条坏数据阻断全部布局恢复。
                                try:
                                    profile = LayoutProfile.from_dict(data)
                                except ValueError as exc:
                                    logger.warning(
                                        "Skipping malformed layout profile "
                                        "%s: %s", key, exc)
                                    continue
                                self._cache[key] = profile
                                result[page_id] = profile

        # Include cached profiles not yet in config
        for key, profile in self._cache.items():
            if (
                profile.product_mode == product_mode
                and profile.page_id not in result
            ):
                result[profile.page_id] = profile

        return result

    # ------------------------------------------------------------------
    # Internal config helpers
    # ------------------------------------------------------------------

    def _get_config_nested(self, key: str) -> Any:
        """Get a nested config value using dot/slash notation."""
        if self._config is None:
            return None

        parts = key.split("/")
        current = self._config.get(parts[0])
        for part in parts[1:]:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def _set_config_nested(
        self, key: str, value: Any, auto_save: bool = True
    ) -> None:
        """Set a nested config value using slash notation."""
        if self._config is None:
            return

        parts = key.split("/")
        root_key = parts[0]

        # Get or create the root dict
        root = self._config.get(root_key)
        if not isinstance(root, dict):
            root = {}
            self._config.set(root_key, root, auto_save=False)

        # Navigate to the parent of the leaf
        current = root
        for part in parts[1:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]

        # Set the leaf value
        leaf = parts[-1]
        current[leaf] = value

        # Persist the updated root
        self._config.set(root_key, root, auto_save=auto_save)

    def _delete_config_nested(self, key: str) -> None:
        """Delete a nested config value."""
        if self._config is None:
            return

        parts = key.split("/")
        root_key = parts[0]

        root = self._config.get(root_key)
        if not isinstance(root, dict):
            return

        # Navigate to the parent of the leaf
        current = root
        for part in parts[1:-1]:
            if isinstance(current, dict):
                current = current.get(part, {})
            else:
                return

        leaf = parts[-1]
        current.pop(leaf, None)

        self._config.set(root_key, root, auto_save=True)

    def _get_all_profiles_raw(self) -> dict[str, Any]:
        """Get the raw layout_profiles dict from config."""
        if self._config is None:
            return {}
        root = self._config.get(LAYOUT_PROFILES_KEY)
        return root if isinstance(root, dict) else {}

    def _get_product_mode(self) -> str:
        """Get the current product mode from environment or config."""
        try:
            from src.shared.feature_flags import get_product_mode

            return get_product_mode()
        except ImportError:
            return "studio"

    def _get_effective_layout_mode(self) -> str:
        """The page_layouts bucket key for the current session.

        Mirrors the writer/reader side (feature_flags.
        get_effective_layout_mode): the product mode, with "studio"
        scoped by work mode (``studio_image``/``studio_video``). Falls
        back to "all" when the resolver is unavailable, matching the
        dock controller's fallback bucket (AUD-34103).
        """
        try:
            from src.shared.feature_flags import get_effective_layout_mode

            return get_effective_layout_mode(self._config)
        except Exception:
            return "all"

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear the in-memory profile cache."""
        self._cache.clear()

    def __repr__(self) -> str:
        return f"LayoutStore(cached={len(self._cache)})"
