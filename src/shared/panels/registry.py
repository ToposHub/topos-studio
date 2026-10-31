"""Panel Registry — manages panel descriptors for Window > Panels menu.

Panels are organized into categories (Workspace, Color, Edit, Composite,
Deliver, Advanced) and filtered by product mode via feature_id.

Usage:
    from src.shared.panels import PanelDescriptor, PanelRegistry

    registry = PanelRegistry()
    registry.register(PanelDescriptor(
        panel_id="color.color_wheel",
        title_key="panel.color_wheel",
        fallback_title="Color Wheel",
        feature_id="color",
        category="Color",
        dock_attr="dock_grading",
        order=300,
    ))

    # Get panels grouped by category for menu construction
    grouped = registry.get_panels_by_category(enabled_features={"color", "library"})
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Category ordering for consistent menu presentation
CATEGORY_ORDER: list[str] = [
    "Workspace",
    "Color",
    "Edit",
    "Composite",
    "Deliver",
    "Advanced",
]

# Default category for panels without an explicit category
DEFAULT_CATEGORY = "Workspace"


@dataclass(frozen=True)
class PanelDescriptor:
    """Describes a dock panel for the Window > Panels menu.

    Attributes:
        panel_id: Stable identifier (e.g., "color.color_wheel"). Never changes.
        title_key: i18n key for the panel title.
        fallback_title: English fallback if i18n key is missing.
        feature_id: Feature this panel belongs to (for product mode filtering).
        category: Menu group: Workspace / Color / Edit / Composite / Deliver / Advanced.
        page_ids: Pages where this panel is primarily used (e.g., ["Color"]).
        dock_attr: QDockWidget attribute name on MainWindow (transition helper).
        order: Sort order within category (lower = higher in menu).
        default_visible: Whether the panel is visible in default layout.
        work_mode: Studio work mode this panel belongs to ("all"/"image"/"video").
            "image" panels (Library, image batch export) are hidden from the
            menu in video mode; "video" panels (Timeline Clips) are hidden in
            image mode. "all" (default) shows in every mode.
    """
    panel_id: str
    title_key: str
    fallback_title: str
    feature_id: str = "color"
    category: str = DEFAULT_CATEGORY
    page_ids: tuple[str, ...] = ()
    dock_attr: str = ""
    order: int = 100
    default_visible: bool = True
    work_mode: str = "all"


class PanelRegistry:
    """Central registry for panel descriptors.

    Modules register their panels during initialization. The registry
    is then used to build the Window > Panels menu with proper grouping
    and product-mode filtering.
    """

    def __init__(self) -> None:
        self._panels: Dict[str, PanelDescriptor] = {}

    def register(self, descriptor: PanelDescriptor) -> None:
        """Register a panel descriptor.

        Logs a warning if a panel_id is already registered and skips
        the duplicate registration.
        """
        if descriptor.panel_id in self._panels:
            logger.warning(
                "Duplicate panel_id %r (existing=%r, new=%r). Skipping.",
                descriptor.panel_id,
                self._panels[descriptor.panel_id].dock_attr,
                descriptor.dock_attr,
            )
            return
        self._panels[descriptor.panel_id] = descriptor

    def get(self, panel_id: str) -> Optional[PanelDescriptor]:
        """Return a panel descriptor by ID, or None."""
        return self._panels.get(panel_id)

    def get_all(self) -> List[PanelDescriptor]:
        """Return all registered descriptors, sorted by category and order."""
        return sorted(
            self._panels.values(),
            key=lambda d: (CATEGORY_ORDER.index(d.category)
                           if d.category in CATEGORY_ORDER
                           else len(CATEGORY_ORDER), d.order),
        )

    def get_panels_by_category(
        self,
        enabled_features: Optional[Set[str]] = None,
        work_mode: Optional[str] = None,
    ) -> Dict[str, List[PanelDescriptor]]:
        """Return panels grouped by category, filtered by enabled features.

        Args:
            enabled_features: Set of enabled feature IDs. If None, all panels
                are returned.
            work_mode: Studio work mode ("all"/"image"/"video"). When set,
                panels scoped to the opposite mode are hidden (e.g. image-only
                Library/export are hidden in video mode). None = no filtering.

        Returns:
            Dict mapping category name to list of PanelDescriptor, sorted
            by order within each category.
        """
        result: Dict[str, List[PanelDescriptor]] = {}
        for desc in self.get_all():
            # Filter by enabled features
            if enabled_features is not None and desc.feature_id not in enabled_features:
                continue
            # Filter by work mode: a panel scoped to one mode is hidden when
            # the active mode is the opposite. "all" mode (and None) shows
            # every panel regardless of its work_mode scope.
            if work_mode in ("image", "video") and desc.work_mode != "all" and desc.work_mode != work_mode:
                continue
            result.setdefault(desc.category, []).append(desc)
        return result

    def get_categories(
        self,
        enabled_features: Optional[Set[str]] = None,
        work_mode: Optional[str] = None,
    ) -> List[str]:
        """Return ordered list of categories that have visible panels."""
        grouped = self.get_panels_by_category(enabled_features, work_mode)
        return [cat for cat in CATEGORY_ORDER if cat in grouped]

    def get_panel_ids_for_feature(self, feature_id: str) -> List[str]:
        """Return all panel_ids belonging to a feature."""
        return [
            d.panel_id for d in self._panels.values()
            if d.feature_id == feature_id
        ]

    def unregister_feature(self, feature_id: str) -> int:
        """Remove all panels for a feature. Returns count removed."""
        to_remove = [
            pid for pid, desc in self._panels.items()
            if desc.feature_id == feature_id
        ]
        for pid in to_remove:
            del self._panels[pid]
        return len(to_remove)


def validate_dock_integrity(
    registry: PanelRegistry,
    window,
    valid_features: set[str] | None = None,
) -> list[str]:
    """Validate that every registered panel maps to a real dock widget.

    This is a guardrail: if a panel descriptor references a dock attribute
    that does not exist on the window, the Window > Panels menu item will
    be silently ignored, confusing users.

    Args:
        registry: The PanelRegistry containing all descriptors.
        window: The MainWindow (or any object with dock attributes).
        valid_features: Set of valid feature IDs.  If provided, descriptors
            whose ``feature_id`` is not in this set are reported as issues.

    Returns:
        List of human-readable issue strings.  Empty list means all good.
    """
    issues: list[str] = []

    seen_ids: dict[str, str] = {}  # panel_id -> dock_attr (for dup detection)
    seen_dock_attrs: dict[str, str] = {}  # dock_attr -> panel_id (for dup detection)

    for desc in registry.get_all():
        # -- panel_id uniqueness --
        if desc.panel_id in seen_ids:
            issues.append(
                f"Duplicate panel_id {desc.panel_id!r} "
                f"(first dock_attr={seen_ids[desc.panel_id]!r}, "
                f"second dock_attr={desc.dock_attr!r})"
            )
        seen_ids[desc.panel_id] = desc.dock_attr

        # -- dock_attr non-empty --
        if not desc.dock_attr:
            issues.append(
                f"Panel {desc.panel_id!r} has empty dock_attr"
            )
            continue

        # -- feature_id validity --
        if valid_features is not None and desc.feature_id not in valid_features:
            issues.append(
                f"Panel {desc.panel_id!r} references unknown feature_id "
                f"{desc.feature_id!r} (valid: {sorted(valid_features)})"
            )

        # -- dock widget exists on window --
        if not hasattr(window, desc.dock_attr):
            issues.append(
                f"Panel {desc.panel_id!r} dock_attr={desc.dock_attr!r} "
                f"not found on window"
            )
            continue

        dock = getattr(window, desc.dock_attr)
        if dock is None:
            # Optional docks (e.g. edit docks when edit is disabled) are None.
            # This is not an error if the feature is not enabled.
            pass
        else:
            # -- dock has toggleViewAction --
            if not hasattr(dock, "toggleViewAction"):
                issues.append(
                    f"Panel {desc.panel_id!r} dock_attr={desc.dock_attr!r} "
                    f"widget has no toggleViewAction method"
                )

        # -- dock_attr uniqueness (two panels sharing the same dock) --
        if desc.dock_attr in seen_dock_attrs:
            first = seen_dock_attrs[desc.dock_attr]
            # This is a soft warning -- multiple panels CAN share a dock
            # (e.g. workspace.export and deliver.export both use dock_export).
            # We still report it for visibility but as informational.
            issues.append(
                f"Panel {desc.panel_id!r} shares dock_attr={desc.dock_attr!r} "
                f"with panel {first!r} (shared dock)"
            )
        seen_dock_attrs[desc.dock_attr] = desc.panel_id

    return issues


# Module-level singleton
_instance: Optional[PanelRegistry] = None


def get_panel_registry() -> PanelRegistry:
    """Return the global PanelRegistry singleton."""
    global _instance
    if _instance is None:
        _instance = PanelRegistry()
    return _instance
