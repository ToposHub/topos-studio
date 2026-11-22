"""Persistent entry points for the current workspace's optional panels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QDockWidget, QMenu, QSizePolicy, QToolBar, QToolButton, QWidget

from src.shared.assets.icon_manager import get_icon
from src.shared.i18n.i18n import I18N
from src.shared.panels.registry import get_panel_registry
from src.shared.ui.style_sheet import C

if TYPE_CHECKING:
    from src.app.main_window import MainWindow


_SHORTCUT_TEXT = "#a4a4ac"
_SHORTCUT_ACTIVE = "#f0f0f2"


def _shortcut_icon(name: str) -> QIcon:
    return get_icon(f"ph.{name}", _SHORTCUT_TEXT, active_color=_SHORTCUT_ACTIVE)


def create_toolbar_menu_button(parent: QWidget, text: str, icon_name: str, menu: QMenu) -> QToolButton:
    """Create a centered icon button for the top toolbar's popup menus."""
    button = QToolButton(parent)
    button.setAccessibleName(text)
    button.setToolTip(text)
    button.setIcon(_shortcut_icon(icon_name))
    button.setIconSize(QSize(16, 16))
    button.setFixedSize(28, 28)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    button.setMenu(menu)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    button.setStyleSheet(f"""
        QToolButton {{
            background: transparent; border: 1px solid transparent;
            border-radius: 4px; margin: 0; padding: 0; outline: none;
        }}
        QToolButton:hover, QToolButton:pressed, QToolButton:open {{
            background: {C.BG_ACTIVE};
        }}
        QToolButton:focus {{ border-color: {C.BORDER_HOVER}; }}
        QToolButton::menu-indicator {{ image: none; }}
    """)
    return button


class DockGroupAction(QAction):
    """Collapse a panel group without losing its open or selected tabs."""

    def __init__(self, title: str, window: MainWindow, page: str,
                 docks: tuple[QDockWidget, ...],
                 tab_peers: tuple[QDockWidget, ...] = ()) -> None:
        super().__init__(title, window)
        self._window = window
        self._docks = docks
        self._tab_peers = tab_peers
        self._config = window.config_manager
        mode = window._layout_profile_mode()
        self._key = f"workspace_panel_restore_{mode}_{page}_{docks[0].objectName()}"
        self.setObjectName(f"workspace_{page.lower()}_{docks[0].objectName()}")
        self.setCheckable(True)
        self.triggered.connect(self._toggle)
        for dock in (*docks, *tab_peers):
            dock.toggleViewAction().changed.connect(self.sync)
            dock.dockLocationChanged.connect(self.sync)
        self.sync()

    def sync(self) -> None:
        # A background tab is still open. QDockWidget.visibilityChanged(False)
        # also fires for that case, so read explicit hide state instead.
        self.setChecked(any(not dock.isHidden() for dock in self._docks))

    def reset(self) -> None:
        self._config.set(self._key, None, auto_save=False)
        self.sync()

    def _toggle(self, checked: bool) -> None:
        if checked:
            saved = self._config.get(self._key)
            opened = saved.get("open") if isinstance(saved, dict) else None
            front = saved.get("front") if isinstance(saved, dict) else None
            remembered = [
                dock for dock in (*self._docks, *self._tab_peers)
                if isinstance(opened, list) and dock.objectName() in opened
            ]
            for dock in remembered or [self._docks[0]]:
                dock.show()
            for dock in remembered:
                if isinstance(front, list) and dock.objectName() in front:
                    dock.raise_()
        else:
            opened_docks = [dock for dock in self._docks if not dock.isHidden()]
            peers = {peer for dock in opened_docks
                     for peer in self._window.tabifiedDockWidgets(dock)}
            opened_docks.extend(
                dock for dock in self._tab_peers if dock in peers and not dock.isHidden()
            )
            self._config.set(self._key, {
                "open": [dock.objectName() for dock in opened_docks],
                "front": [
                    dock.objectName() for dock in opened_docks
                    if not dock.visibleRegion().isEmpty()
                ],
            }, auto_save=False)
            for dock in opened_docks:
                dock.hide()
        self.sync()


class DockTabAction(QAction):
    """Bring a background tab forward before treating its shortcut as a toggle."""

    def __init__(self, dock: QDockWidget, window: MainWindow) -> None:
        super().__init__(dock.windowTitle(), window)
        self.dock = dock
        self.setCheckable(True)
        self.setObjectName("shortcut_" + dock.objectName())
        dock.windowTitleChanged.connect(self.setText)
        dock.visibilityChanged.connect(self.setChecked)
        dock.toggleViewAction().changed.connect(self._sync_enabled)
        self.triggered.connect(self._toggle)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        self.setEnabled(self.dock.toggleViewAction().isEnabled())

    def sync(self) -> None:
        self.setChecked(not self.dock.visibleRegion().isEmpty())

    def _toggle(self, checked: bool) -> None:
        self.dock.setVisible(checked)
        if checked:
            self.dock.raise_()


class WorkspacePanelBar(QToolBar):
    """Panel menu and page-specific shortcuts on both sides of navigation."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(I18N.tr("menu.panels", "Panels"), window)
        self._window = window
        self._active_page = ""
        self.right_tools = QToolBar(self.windowTitle(), window)
        self._style_toolbar(self, "workspacePanelBar")
        self._style_toolbar(self.right_tools, "workspacePanelBarRight")
        self._right_actions: set[QAction] = set()
        self._panels_menu = QMenu(self)
        self._panels_button = create_toolbar_menu_button(
            self, self.windowTitle(), "layout", self._panels_menu,
        )
        self._tools_menu = QMenu(self)
        self._tools_button = create_toolbar_menu_button(
            self, I18N.tr("workspace.grading_tools", "Grading Tools"),
            "sliders-horizontal", self._tools_menu,
        )
        self._panel_menu_action = self.addWidget(self._panels_button)
        self._tool_menu_action = self.addWidget(self._tools_button)

        self._mode = window._current_work_mode()
        self._primary_actions = self._build_primary_actions()
        self._visible_panel_actions = self._build_panel_menu_actions()
        self._color_tools = self._build_color_tools_menu()

    @staticmethod
    def _style_toolbar(toolbar: QToolBar, name: str) -> None:
        toolbar.setObjectName(name)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setStyleSheet(f"""
            QToolBar#{name} {{
                background: transparent; border: none; spacing: 3px; padding: 0;
            }}
            QToolBar#{name} QToolButton {{
                background: transparent; color: {_SHORTCUT_TEXT};
                border: none; border-bottom: 1px solid transparent; outline: none;
                font-size: 12px; padding: 4px 6px 3px; margin: 0;
            }}
            QToolBar#{name} QToolButton:checked, QToolBar#{name} QToolButton:hover,
            QToolBar#{name} QToolButton:focus, QToolBar#{name} QToolButton:pressed,
            QToolBar#{name} QToolButton:open {{
                background: transparent; color: {_SHORTCUT_ACTIVE};
            }}
            QToolBar#{name} QToolButton:focus {{
                border-bottom-color: {_SHORTCUT_ACTIVE};
            }}
            QToolBar#{name} QToolButton:disabled {{ color: {C.TEXT_HINT}; }}
        """)

    def _docks(self, *names: str) -> tuple[QDockWidget, ...]:
        return tuple(
            dock for name in names
            if isinstance(dock := getattr(self._window, name, None), QDockWidget)
        )

    def _build_primary_actions(self) -> dict[str, list[QAction]]:
        window = self._window
        actions: dict[str, list[QAction]] = {}

        def group(page: str, icon: str, label: str, *attrs: str,
                  peers: tuple[str, ...] = (), right: bool = False,
                  foreground_tab: bool = False) -> None:
            docks = self._docks(*attrs)
            if not docks:
                return
            if foreground_tab:
                action = DockTabAction(docks[0], window)
            elif len(docks) > 1:
                action = DockGroupAction(label or docks[0].windowTitle(), window, page,
                                         docks, self._docks(*peers))
            else:
                dock = docks[0]
                action = dock.toggleViewAction()
                action.triggered.connect(lambda checked, panel=dock: panel.raise_() if checked else None)
            action.setIcon(_shortcut_icon(icon))
            if right:
                self._right_actions.add(action)
            actions.setdefault(page, []).append(action)

        show_library_pool = getattr(window, "set_library_media_pool_visible", None)
        if self._docks("dock_edit_media_pool") and callable(show_library_pool):
            pool = QAction(_shortcut_icon("folder-open"), I18N.tr("panel.media_pool", "Media Pool"), self)
            pool.setObjectName("workspace_library_media_pool")
            pool.setCheckable(True)
            pool.toggled.connect(show_library_pool)
            actions["Library"] = [pool]

        group("Edit", "folder-open", I18N.tr("panel.media_pool", "Media Pool"),
              "dock_edit_media_pool", "dock_edit_effects",
              peers=("dock_audio_mixer",))
        group("Edit", "sliders-horizontal", I18N.tr("panel.properties", "Properties"),
              "dock_properties", "dock_edit_info", "dock_caption_workspace",
              "dock_multicam", "dock_ai_edit_assistant",
              peers=("dock_adr_take_sheet",), right=True)
        group("Color", "circles-three", I18N.tr("workspace.color_resources", "Color Resources"),
              "dock_presets", "dock_lut", "dock_plugin", "dock_grade_sets",
              peers=("dock_library", "dock_files"))
        group("Color", "stack", "", "dock_layers", right=True)
        if self._mode != "image":
            group("Color", "film-strip", "", "dock_color_timeline_clips")
        group("Color", "chart-line", "", "dock_analysis", right=True)
        group("Composite", "folder-open", "", "dock_edit_media_pool")
        group("Composite", "sliders-horizontal", I18N.tr("panel.node_properties", "Node Properties"),
              "dock_node_properties", "dock_composite_scene_outliner", "dock_composite_material",
              right=True)
        group("Composite", "stack", "", "dock_composite_keyframes")
        group("Composite", "tree-structure", "", "dock_composite")
        group("Composite", "chart-line", "", "dock_composite_scope", right=True)
        group("Mixer", "faders", "", "dock_audio_console")
        group("Mixer", "monitor-play", "", "dock_audio_viewer", right=True)
        group("Mixer", "activity", "", "dock_audio_clip_editor", right=True, foreground_tab=True)
        group("Mixer", "folder-open", "", "dock_audio_media_pool", foreground_tab=True)
        group("Mixer", "waves", "", "dock_audio_effects", foreground_tab=True)
        group("Mixer", "sliders-horizontal", "", "dock_audio_properties", right=True)
        deliver_page = getattr(window, "deliver_page", None)
        queue = getattr(deliver_page, "render_queue_visibility_action", None)
        if isinstance(queue, QAction):
            queue.setIcon(_shortcut_icon("list-bullets"))
            self._right_actions.add(queue)
            actions["Deliver"] = [queue]
        return actions

    def _build_panel_menu_actions(self) -> dict[str, list[QAction]]:
        window = self._window
        descriptors = get_panel_registry().get_panels_by_category(
            window._get_enabled_features(), self._mode,
        )
        by_page: dict[str, list[QAction]] = {}
        for group in descriptors.values():
            for descriptor in group:
                docks = self._docks(descriptor.dock_attr)
                if not docks:
                    continue
                for page in descriptor.page_ids:
                    by_page.setdefault(page, []).append(docks[0].toggleViewAction())
        # These workspace panels predate page-scoped descriptors.
        for page, attrs in (
            ("Edit", ("dock_edit_info",)),
            ("Color", ("dock_presets", "dock_info", "dock_files")),
        ):
            for dock in self._docks(*attrs):
                if dock is getattr(window, "dock_info", None) and self._mode == "video":
                    continue
                by_page.setdefault(page, []).append(dock.toggleViewAction())
        return by_page

    def _build_color_tools_menu(self) -> tuple[QDockWidget, ...]:
        docks = self._docks(
            "dock_grading", "dock_curves", "dock_masks", "dock_qualifier",
            "dock_warper", "dock_mixer", "dock_raw", "dock_denoise",
            "dock_effects", "dock_grain",
        )
        if self._mode != "image":
            docks += self._docks("dock_tracker")
        if self._mode != "video":
            docks += self._docks("dock_deblur", "dock_crop")
        for dock in docks:
            action = self._tools_menu.addAction(dock.windowTitle())
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked, panel=dock: self._select_color_tool(panel, docks),
            )
            dock.windowTitleChanged.connect(action.setText)
        self._tools_menu.aboutToShow.connect(self._sync_color_tool_checks)
        return docks

    def _sync_color_tool_checks(self) -> None:
        for action, dock in zip(self._tools_menu.actions(), self._color_tools):
            action.setChecked(not dock.isHidden() and not dock.visibleRegion().isEmpty())

    def _select_color_tool(self, selected: QDockWidget, tools: tuple[QDockWidget, ...]) -> None:
        selected.show()
        for peer in self._window.tabifiedDockWidgets(selected):
            if peer in tools:
                peer.hide()
        selected.raise_()

    def compact_default_color_tools(self) -> None:
        """Show one tool per existing tab group in the default Color layout."""
        scopes = getattr(self._window, "dock_analysis", None)
        for dock in self._color_tools:
            if dock.isHidden() or dock.isFloating():
                continue
            peers = self._window.tabifiedDockWidgets(dock)
            if scopes in peers and not scopes.isHidden():
                dock.hide()
                continue
            for peer in peers:
                if peer in self._color_tools:
                    peer.hide()

    def set_page(self, page: str) -> None:
        if page == self._active_page:
            return
        for action in self._primary_actions.get(self._active_page, ()):
            toolbar = self.right_tools if action in self._right_actions else self
            toolbar.removeAction(action)
        self._active_page = page
        primary = self._primary_actions.get(page, [])
        if page == "Library" and primary:
            self._window.set_library_media_pool_visible(primary[0].isChecked())
        self._panels_menu.clear()
        tab_toggles = {action.dock.toggleViewAction() for action in primary
                       if isinstance(action, DockTabAction)}
        for action in dict.fromkeys([*primary, *self._visible_panel_actions.get(page, ())]):
            if action not in tab_toggles:
                self._panels_menu.addAction(action)
        show_tools = page == "Color" and bool(self._tools_menu.actions())
        self._panel_menu_action.setVisible(page != "Library" and bool(self._panels_menu.actions()))
        self._tool_menu_action.setVisible(show_tools)
        for action in primary:
            if isinstance(action, (DockGroupAction, DockTabAction)):
                action.sync()
            toolbar = self.right_tools if action in self._right_actions else self
            toolbar.addAction(action)
            button = toolbar.widgetForAction(action)
            if button is not None:
                button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setVisible(bool(primary or self._panels_menu.actions()) or show_tools)
        self.right_tools.setVisible(bool(self.right_tools.actions()))

    def reset_groups(self, page: str | None = None) -> None:
        groups = self._primary_actions.values() if page is None else (self._primary_actions.get(page, []),)
        for actions in groups:
            for action in actions:
                if isinstance(action, DockGroupAction):
                    action.reset()
