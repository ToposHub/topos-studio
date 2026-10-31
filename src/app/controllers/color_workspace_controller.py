"""Color workspace preset controller (P1-COLOR-19).

MainWindow mixin that applies/saves/deletes Color page workspace presets.
纯数据与持久化逻辑在 :mod:`src.features.color.core.workspace_presets`；
这里只负责 dock 可见性、scope 输入源与 View 菜单的接线。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from src.shared.i18n.i18n import I18N
from src.shared.panels.registry import PanelDescriptor, get_panel_registry

logger = logging.getLogger(__name__)

# 颜色页可被工作区管理的面板 id 前缀（PanelRegistry 中 Workspace/Color/Advanced
# 类别下、无页面限制或限定 Color 页的面板都落在这三个前缀内）。
_COLOR_PAGE_PREFIXES = ("workspace.", "color.", "advanced.")


class ColorWorkspaceMixin:
    """Apply and manage Color page workspace presets on MainWindow."""

    # ------------------------------------------------------------------
    # Panel enumeration
    # ------------------------------------------------------------------
    def _color_workspace_panel_descriptors(self) -> List[PanelDescriptor]:
        """Registered panels the Color workspace is allowed to toggle."""
        try:
            descriptors = get_panel_registry().get_all()
        except Exception:
            return []
        work_mode = self._current_work_mode()
        enabled = self._get_enabled_features()
        result: List[PanelDescriptor] = []
        for desc in descriptors:
            if not desc.panel_id.startswith(_COLOR_PAGE_PREFIXES):
                continue
            if desc.feature_id not in enabled:
                continue
            if desc.page_ids and "Color" not in desc.page_ids:
                continue
            # work_mode 专属面板由 _enforce_page_dock_visibility 全权负责
            if desc.work_mode != "all" and desc.work_mode != work_mode:
                continue
            result.append(desc)
        return result

    def _color_workspace_docks(self) -> List[Tuple[str, object]]:
        """(panel_id, dock) pairs for every color-page capable panel."""
        pairs: List[Tuple[str, object]] = []
        for desc in self._color_workspace_panel_descriptors():
            if not desc.dock_attr:
                continue
            dock = getattr(self, desc.dock_attr, None)
            if dock is not None:
                pairs.append((desc.panel_id, dock))
        return pairs

    # ------------------------------------------------------------------
    # Preset operations
    # ------------------------------------------------------------------
    def get_color_workspace_presets(self) -> list:
        """Builtin + user workspace presets, in menu order."""
        from src.features.color.core.workspace_presets import (
            builtin_color_workspaces,
            load_user_workspaces,
        )
        return builtin_color_workspaces() + load_user_workspaces(
            getattr(self, "config_manager", None))

    def active_color_workspace_id(self) -> Optional[str]:
        """Return the preset id whose (sanitized) panel set matches current
        visibility exactly, else None."""
        from src.features.color.core.workspace_presets import sanitize_preset

        if getattr(self, "current_page", None) != "Color":
            return None
        docks = self._color_workspace_docks()
        if not docks:
            return None
        current = frozenset(pid for pid, dock in docks if dock.isVisible())
        registered = self._get_registered_panel_ids()
        work_mode = self._current_work_mode()
        for preset in self.get_color_workspace_presets():
            if sanitize_preset(preset, registered, work_mode) == current:
                return preset.preset_id
        return None

    def apply_color_workspace(self, preset_id: str) -> bool:
        """Apply a workspace preset to the Color page.

        Returns False when not on the Color page or the preset is unknown.
        """
        from src.features.color.core.workspace_presets import (
            find_workspace,
            sanitize_preset,
        )

        if getattr(self, "current_page", None) != "Color":
            return False
        preset = find_workspace(getattr(self, "config_manager", None), preset_id)
        if preset is None:
            return False

        visible = sanitize_preset(
            preset, self._get_registered_panel_ids(), self._current_work_mode())
        try:
            for panel_id, dock in self._color_workspace_docks():
                dock.setVisible(panel_id in visible)
            if preset.scope_source:
                scope = getattr(self, "scope_panel", None)
                if scope is not None and hasattr(scope, "set_scope_source"):
                    scope.set_scope_source(preset.scope_source)
            self._enforce_page_dock_visibility()
            # 把切换结果写回当前 Color 页布局（用户全局布局键不受影响）
            self.page_layouts["Color"] = self.saveState()
            self._save_current_layout_to_store("Color")
        except Exception as exc:
            logger.warning("Failed to apply color workspace %s: %s", preset_id, exc)
            return False
        self._refresh_color_workspace_menu()
        return True

    def save_current_color_workspace(self, name: str):
        """Snapshot the current Color page panels as a user workspace."""
        from src.features.color.core.workspace_presets import save_user_workspace

        if getattr(self, "current_page", None) != "Color":
            return None
        visible = [pid for pid, dock in self._color_workspace_docks() if dock.isVisible()]
        scope = getattr(self, "scope_panel", None)
        scope_source = getattr(scope, "scope_source", "") if scope is not None else ""
        preset = save_user_workspace(
            getattr(self, "config_manager", None), name, visible, scope_source)
        self._refresh_color_workspace_menu()
        return preset

    def delete_color_workspace(self, preset_id: str) -> bool:
        from src.features.color.core.workspace_presets import delete_user_workspace

        ok = delete_user_workspace(getattr(self, "config_manager", None), preset_id)
        if ok:
            self._refresh_color_workspace_menu()
        return ok

    # ------------------------------------------------------------------
    # Menu plumbing
    # ------------------------------------------------------------------
    def color_workspace_menu_items(self) -> list[tuple[str, str, bool]]:
        """(preset_id, display_name, is_user) rows for the View > Workspace menu."""
        rows = []
        for preset in self.get_color_workspace_presets():
            rows.append((preset.preset_id, preset.display_name(I18N.tr),
                         not preset.builtin))
        return rows

    def _refresh_color_workspace_menu(self) -> None:
        canvas = getattr(self, "canvas", None)
        if canvas is not None and hasattr(canvas, "refresh_color_workspace_menu"):
            try:
                canvas.refresh_color_workspace_menu()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Interactive handlers (wired from MainWindow)
    # ------------------------------------------------------------------
    def setup_color_workspace_menu(self) -> None:
        """Connect the viewport Workspace menu to this controller."""
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        menu = getattr(canvas, "viewport_menu", None)
        if menu is not None and hasattr(menu, "set_workspace_provider"):
            menu.set_workspace_provider(self._workspace_menu_provider)
        canvas.color_workspace_requested.connect(self.on_color_workspace_requested)
        canvas.color_workspace_save_requested.connect(
            self.on_save_color_workspace_requested)
        canvas.color_workspace_delete_requested.connect(
            self.on_delete_color_workspace_requested)

    def _workspace_menu_provider(self) -> tuple[list, Optional[str]]:
        return self.color_workspace_menu_items(), self.active_color_workspace_id()

    def _workspace_status(self, message: str) -> None:
        try:
            self.statusBar().showMessage(message, 4000)
        except Exception:
            pass

    def on_color_workspace_requested(self, preset_id: str) -> None:
        from src.features.color.core.workspace_presets import find_workspace

        if self.apply_color_workspace(preset_id):
            preset = find_workspace(getattr(self, "config_manager", None), preset_id)
            name = preset.display_name(I18N.tr) if preset is not None else preset_id
            self._workspace_status(
                I18N.tr("workspace.applied", "Workspace applied: {name}",
                        name=name))
        else:
            self._workspace_status(
                I18N.tr("workspace.not_on_color_page",
                        "Workspaces are only available on the Color page"))

    def on_save_color_workspace_requested(self) -> None:
        if getattr(self, "current_page", None) != "Color":
            self._workspace_status(
                I18N.tr("workspace.not_on_color_page",
                        "Workspaces are only available on the Color page"))
            return
        from PyQt6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self, I18N.tr("workspace.save_current", "Save Current as Workspace..."),
            I18N.tr("workspace.name_prompt", "Workspace name:"))
        if not ok:
            return
        preset = self.save_current_color_workspace(name)
        if preset is None:
            self._workspace_status(
                I18N.tr("workspace.save_failed", "Workspace name cannot be empty"))
            return
        self._workspace_status(
            I18N.tr("workspace.saved", "Workspace saved: {name}",
                    name=preset.name))

    def on_delete_color_workspace_requested(self) -> None:
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        user_presets = [p for p in self.get_color_workspace_presets()
                        if not p.builtin]
        if not user_presets:
            return
        names = [p.display_name(I18N.tr) for p in user_presets]
        name, ok = QInputDialog.getItem(
            self, I18N.tr("workspace.delete", "Delete Workspace..."),
            I18N.tr("workspace.delete_prompt", "Workspace to delete:"),
            names, 0, False)
        if not ok:
            return
        index = names.index(name) if name in names else -1
        if index < 0:
            return
        preset = user_presets[index]
        confirm = QMessageBox.question(
            self, I18N.tr("workspace.delete", "Delete Workspace..."),
            I18N.tr("workspace.delete_confirm", "Delete workspace \"{name}\"?",
                    name=preset.name))
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if self.delete_color_workspace(preset.preset_id):
            self._workspace_status(
                I18N.tr("workspace.deleted", "Workspace deleted: {name}",
                        name=preset.name))
