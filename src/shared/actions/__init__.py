"""Action contribution system for modular menu/toolbar/shortcut registration.

Each feature module contributes MenuActionDescriptors to describe its
menu items. The app shell builds menus dynamically from all registered
descriptors, filtering by enabled features.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MenuActionDescriptor:
    """Describes a menu action that can be contributed by a feature module.

    Attributes:
        action_id: Unique identifier, e.g. "edit.project.new"
        menu_path: Menu path, e.g. "File/Edit Project"
        label: Display text
        shortcut_id: Optional shortcut action ID to bind
        order: Sort order within the same menu_path (lower = first)
        feature_id: Feature that contributes this action. None = app-level.
        callback_name: Name of method on MainWindow or feature handler to call
        enabled_when: Optional condition for dynamic enable/disable
        checkable: When True the created QAction is checkable; the callback
            receives the new checked state as its first positional argument.
    """

    action_id: str
    menu_path: str
    label: str
    callback_name: str
    order: int = 100
    feature_id: Optional[str] = None
    shortcut_id: Optional[str] = None
    enabled_when: Optional[str] = None
    checkable: bool = False


class ActionRegistry:
    """Registry for menu action contributions from feature modules.

    Collects MenuActionDescriptors and provides filtered views
    based on currently enabled features.
    """

    def __init__(self):
        self._actions: dict[str, MenuActionDescriptor] = {}

    def register(self, descriptor: MenuActionDescriptor) -> None:
        """Register a menu action descriptor."""
        if descriptor.action_id in self._actions:
            return  # Duplicate, ignore
        self._actions[descriptor.action_id] = descriptor

    def get_actions(
        self, enabled_features: set[str] | None = None
    ) -> list[MenuActionDescriptor]:
        """Get all menu actions, optionally filtered by enabled features.

        Args:
            enabled_features: Set of enabled feature IDs. If None, return all.

        Returns:
            Sorted list of MenuActionDescriptors.
        """
        actions = []
        for action in self._actions.values():
            if enabled_features is not None:
                if action.feature_id is not None and action.feature_id not in enabled_features:
                    continue
            actions.append(action)
        return sorted(actions, key=lambda a: (a.menu_path, a.order, a.action_id))

    def get_action(self, action_id: str) -> Optional[MenuActionDescriptor]:
        """Get a specific action by ID."""
        return self._actions.get(action_id)

    def get_menu_paths(
        self, enabled_features: set[str] | None = None
    ) -> list[str]:
        """Get unique menu paths, sorted."""
        actions = self.get_actions(enabled_features)
        seen = set()
        result = []
        for a in actions:
            if a.menu_path not in seen:
                seen.add(a.menu_path)
                result.append(a.menu_path)
        return result
