"""Persistent entry points for the current workspace's optional panels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QContextMenuEvent, QIcon, QResizeEvent
from PyQt6.QtWidgets import (
    QDockWidget, QGridLayout, QLayout, QMenu, QSizePolicy, QToolBar, QToolButton, QWidget,
)

from src.app.ui.video_color_layout import VIDEO_COLOR_PRIMARY, VIDEO_COLOR_SCOPES, VIDEO_COLOR_SECONDARY
from src.shared.assets.icon_manager import get_icon
from src.shared.i18n.i18n import I18N
from src.shared.panels.registry import get_panel_registry
from src.shared.ui.style_sheet import C

if TYPE_CHECKING:
    from src.app.main_window import MainWindow
    from src.shared.config.config_manager import ConfigManager


_SHORTCUT_TEXT = "#a4a4ac"
_SHORTCUT_ACTIVE = "#f0f0f2"


def _shortcut_icon(name: str) -> QIcon:
    return get_icon(f"fa5s.{name}", _SHORTCUT_TEXT, active_color=_SHORTCUT_ACTIVE)


def create_toolbar_menu_button(
    parent: QWidget, text: str, icon_name: str, menu: QMenu,
) -> QToolButton:
    """Create a popup button with a label beside its icon."""
    button = QToolButton(parent)
    button.setAccessibleName(text)
    button.setToolTip(text)
    button.setIcon(_shortcut_icon(icon_name))
    button.setIconSize(QSize(16, 16))
    button.setFixedHeight(28)
    button.setText(text)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    button.setMenu(menu)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    button.setStyleSheet(f"""
        QToolButton {{
            background: transparent; border: 1px solid transparent;
            color: {_SHORTCUT_TEXT}; font-size: 12px;
            border-radius: 4px; margin: 0; padding: 0 4px; outline: none;
        }}
        QToolButton:hover, QToolButton:pressed, QToolButton:open {{
            background: {C.BG_ACTIVE}; color: {_SHORTCUT_ACTIVE};
        }}
        QToolButton:focus {{ border-color: {C.BORDER_HOVER}; }}
        QToolButton::menu-indicator {{ image: none; }}
    """)
    return button


class DockGroupAction(QAction):
    """Collapse a panel group without losing its open or selected tabs."""

    def __init__(self, title: str, window: MainWindow, page: str,
                 docks: tuple[QDockWidget, ...],
                 tab_peers: tuple[QDockWidget, ...] = (), *, reopen_all: bool = False,
                 opposite_column: tuple[QDockWidget, ...] = (),
                 center: QDockWidget | None = None) -> None:
        super().__init__(title, window)
        self._window = window
        self._docks = docks
        self._tab_peers = tab_peers
        self._opposite_column = opposite_column
        self._center = center
        self._default_open = docks if reopen_all else docks[:1]
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

    def _group_docks(self) -> tuple[QDockWidget, ...]:
        if sip.isdeleted(self._window):
            return ()
        anchors = tuple(dock for dock in self._docks if not sip.isdeleted(dock))
        if not self._tab_peers:
            return anchors
        tabs = {peer for dock in anchors for peer in self._window.tabifiedDockWidgets(dock)}
        peers = tuple(dock for dock in self._tab_peers if not sip.isdeleted(dock))
        if anchors and all(dock.isHidden() for dock in anchors):
            # Qt stops reporting tabs for hidden anchors, even if a peer is still open.
            # The independent Audio workspace puts its Mixer in the opposite area.
            area = self._window.dockWidgetArea(anchors[0])
            if area != Qt.DockWidgetArea.NoDockWidgetArea:
                tabs.update(dock for dock in peers if not dock.isFloating()
                            and self._window.dockWidgetArea(dock) == area)
        return (*anchors, *(dock for dock in peers if dock in tabs))

    def sync(self) -> None:
        # A background tab is still open. QDockWidget.visibilityChanged(False)
        # also fires for that case, so read explicit hide state instead.
        self.setChecked(any(not dock.isHidden() for dock in self._group_docks()))

    def reset(self) -> None:
        self._config.set(self._key, None, auto_save=False)
        self.sync()

    def _toggle(self, checked: bool) -> None:
        opposite = next((dock for dock in self._opposite_column
                         if not dock.isFloating() and not dock.visibleRegion().isEmpty()), None)
        columns = [(opposite, opposite.width())] if opposite is not None else []
        if checked:
            saved = self._config.get(self._key)
            opened = saved.get("open") if isinstance(saved, dict) else None
            front = saved.get("front") if isinstance(saved, dict) else None
            width = saved.get("width") if isinstance(saved, dict) else None
            remembered = [
                dock for dock in (*self._docks, *self._tab_peers)
                if isinstance(opened, list) and dock.objectName() in opened
            ]
            for dock in remembered or self._default_open:
                dock.show()
            for dock in remembered:
                if isinstance(front, list) and dock.objectName() in front:
                    dock.raise_()
                    if type(width) is int and width > 0 and not dock.isFloating():
                        columns.append((dock, width))
            if not remembered and len(self._default_open) > 1:
                self._default_open[0].raise_()
        else:
            opened_docks = [dock for dock in self._group_docks() if not dock.isHidden()]
            foreground = [dock for dock in opened_docks if not dock.visibleRegion().isEmpty()]
            restore: dict[str, list[str] | int] = {
                "open": [dock.objectName() for dock in opened_docks],
                "front": [dock.objectName() for dock in foreground],
            }
            if self._center is not None and foreground and not foreground[0].isFloating():
                restore["width"] = foreground[0].width()
            self._config.set(self._key, restore, auto_save=False)
            for dock in opened_docks:
                dock.hide()

        if columns:
            self._resize_around_viewer(columns)
        self.sync()

    def _resize_around_viewer(self, columns: list[tuple[QDockWidget, int]]) -> None:
        center = self._center
        columns = [(dock, width) for dock, width in columns if not dock.isHidden()]
        if len(columns) == 2 and columns[1][0] in self._window.tabifiedDockWidgets(columns[0][0]):
            return  # Both shortcuts address the same column in compact Edit.
        if (columns and center is not None and not center.isFloating()
                and not center.visibleRegion().isEmpty()):
            center_width = self._window.contentsRect().width() - sum(width for _, width in columns)
            self._window.resizeDocks(
                [center, *(dock for dock, _ in columns)],
                [max(1, center_width), *(width for _, width in columns)],
                Qt.Orientation.Horizontal,
            )


class WorkspacePanelBar(QToolBar):
    """Panel menu and page-specific shortcuts on both sides of navigation."""

    page_changed = pyqtSignal()

    def __init__(self, window: MainWindow, *, home_button: QWidget | None = None) -> None:
        super().__init__(I18N.tr("menu.panels", "Panels"), window)
        self._window = window
        self._active_page = ""
        self.right_tools = QToolBar(self.windowTitle(), window)
        self._style_toolbar(self, "workspacePanelBar")
        self._style_toolbar(self.right_tools, "workspacePanelBarRight")
        self._right_actions: set[QAction] = set()
        self._panels_menu = QMenu(self)
        self._panels_button = create_toolbar_menu_button(
            self, self.windowTitle(), "columns", self._panels_menu,
        )
        self._tools_menu = QMenu(self)
        self._tools_button = create_toolbar_menu_button(
            self, I18N.tr("workspace.grading_tools", "Grading Tools"),
            "sliders-h", self._tools_menu,
        )
        self._panel_menu_action = self.addWidget(self._panels_button)
        self._has_home_button = home_button is not None
        if home_button is not None:
            self.addWidget(home_button)
        self._tool_menu_action = self.addWidget(self._tools_button)

        self._mode = window._current_work_mode()
        self._primary_actions = self._build_primary_actions()
        self._visible_panel_actions = self._build_panel_menu_actions()
        self._color_tool_actions: list[tuple[QAction, QDockWidget]] = []
        self._color_tools = self._build_color_tools_menu()

    @staticmethod
    def _style_toolbar(toolbar: QToolBar, name: str) -> None:
        toolbar.setObjectName(name)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setStyleSheet(f"""
            QToolBar#{name} {{
                background: transparent; border: none; spacing: 2px; padding: 0;
            }}
            QToolBar#{name} QToolButton {{
                background: transparent; color: {_SHORTCUT_TEXT};
                border: none; border-bottom: 1px solid transparent; outline: none;
                font-size: 12px; padding: 4px 4px 3px; margin: 0;
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
                  reopen_all: bool = False,
                  opposite_column: tuple[str, ...] = ()) -> None:
            docks = self._docks(*attrs)
            if not docks:
                return
            if len(docks) > 1:
                action = DockGroupAction(label or docks[0].windowTitle(), window, page,
                                         docks, self._docks(*peers), reopen_all=reopen_all,
                                         opposite_column=self._docks(*opposite_column),
                                         center=getattr(window, "dock_program_viewer", None)
                                         if opposite_column else None)
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

        media = ("dock_edit_media_pool", "dock_edit_effects")
        inspector = ("dock_properties", "dock_edit_info", "dock_caption_workspace",
                     "dock_multicam", "dock_ai_edit_assistant")
        group("Edit", "folder-open", I18N.tr("panel.media_pool", "Media Pool"), *media,
              peers=("dock_audio_mixer",), opposite_column=inspector)
        group("Edit", "list-alt", I18N.tr("panel.properties", "Properties"), *inspector,
              peers=("dock_adr_take_sheet",), right=True,
              opposite_column=(*media, "dock_audio_mixer"))
        gallery_label = (I18N.tr("panel.gallery", "Gallery") if self._mode == "video"
                         else I18N.tr("workspace.color_resources", "Color Resources"))
        group("Color", "images", gallery_label,
              "dock_presets", "dock_lut", "dock_plugin", "dock_grade_sets",
              peers=() if self._mode == "video" else ("dock_library", "dock_files"))
        group("Color", "project-diagram", "", "dock_layers", right=True)
        if self._mode not in ("video", "image"):
            group("Color", "film", "", "dock_color_timeline_clips")
        scopes = VIDEO_COLOR_SCOPES if self._mode == "video" else ("dock_analysis",)
        group("Color", "chart-line", "", *scopes, right=True, reopen_all=self._mode == "video")
        group("Composite", "folder-open", "", "dock_edit_media_pool")
        group("Composite", "list-alt", I18N.tr("panel.node_properties", "Node Properties"),
              "dock_node_properties", "dock_composite_scene_outliner", "dock_composite_material",
              right=True)
        group("Composite", "stream", "", "dock_composite_keyframes")
        group("Composite", "project-diagram", "", "dock_composite")
        group("Composite", "chart-line", "", "dock_composite_scope", right=True)
        group("Mixer", "folder-open", "", "dock_audio_media_pool")
        group("Mixer", "sliders-h", "", "dock_audio_console")
        group("Mixer", "magic", "", "dock_audio_effects")
        group("Mixer", "desktop", "", "dock_audio_viewer", right=True)
        group("Mixer", "list-alt", "", "dock_audio_properties", right=True)
        deliver_page = getattr(window, "deliver_page", None)
        queue = getattr(deliver_page, "render_queue_visibility_action", None)
        if isinstance(queue, QAction):
            queue.setIcon(_shortcut_icon("tasks"))
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
                if self._mode == "video" and descriptor.dock_attr == "dock_mixer":
                    continue
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
        if self._mode == "video":
            groups = [
                (self._tools_menu.addMenu(title), docks)
                for title, names in (
                    (I18N.tr("workspace.primary_grading", "Primary Grading"), VIDEO_COLOR_PRIMARY),
                    (I18N.tr("workspace.secondary_grading", "Secondary Grading"), VIDEO_COLOR_SECONDARY),
                )
                if (docks := self._docks(*names))
            ]
        else:
            docks = self._docks(
                "dock_grading", "dock_base_grade", "dock_chromogen",
                "dock_curves", "dock_masks", "dock_qualifier",
                "dock_warper", "dock_mixer", "dock_raw", "dock_denoise",
                "dock_effects", "dock_grain", "dock_deblur", "dock_crop",
            )
            if self._mode != "image":
                docks += self._docks("dock_tracker")
            groups = [(self._tools_menu, docks)]

        tools = tuple(dock for _, docks in groups for dock in docks)
        for menu, docks in groups:
            for dock in docks:
                action = menu.addAction(dock.windowTitle())
                action.setCheckable(True)
                action.triggered.connect(
                    lambda _checked, panel=dock: self._select_color_tool(panel, tools),
                )
                dock.windowTitleChanged.connect(action.setText)
                self._color_tool_actions.append((action, dock))
        self._tools_menu.aboutToShow.connect(self._sync_color_tool_checks)
        return tools

    def _sync_color_tool_checks(self) -> None:
        for action, dock in self._color_tool_actions:
            action.setChecked(not dock.isHidden() and not dock.visibleRegion().isEmpty())

    def _select_color_tool(self, selected: QDockWidget, tools: tuple[QDockWidget, ...]) -> None:
        selected.show()
        if self._mode in ("video", "image"):
            selected.raise_()
            return
        peers = self._window.tabifiedDockWidgets(selected)
        grade_tabs = set(self._docks("dock_grading", "dock_base_grade", "dock_chromogen"))
        keep_grade_tabs = len(grade_tabs.intersection((selected, *peers))) > 1
        for peer in peers:
            if peer in tools and not (keep_grade_tabs and peer in grade_tabs):
                peer.hide()
        selected.raise_()

    def compact_default_color_tools(self) -> None:
        """Keep primary grade tabs available while compacting other tools."""
        if self._mode == "video":
            return
        scopes = getattr(self._window, "dock_analysis", None)
        grade_tabs = self._docks("dock_grading", "dock_base_grade", "dock_chromogen")
        for dock in self._color_tools:
            if dock.isHidden() or dock.isFloating():
                continue
            peers = self._window.tabifiedDockWidgets(dock)
            if scopes in peers and not scopes.isHidden():
                dock.hide()
                continue
            for peer in peers:
                if peer in self._color_tools and not (dock in grade_tabs and peer in grade_tabs):
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
        for action in dict.fromkeys([*primary, *self._visible_panel_actions.get(page, ())]):
            self._panels_menu.addAction(action)
        show_tools = page == "Color" and bool(self._tools_menu.actions())
        self._panel_menu_action.setVisible(page not in ("Library", "Mixer") and bool(self._panels_menu.actions()))
        self._tool_menu_action.setVisible(show_tools)
        for action in primary:
            if isinstance(action, DockGroupAction):
                action.sync()
            toolbar = self.right_tools if action in self._right_actions else self
            toolbar.addAction(action)
            button = toolbar.widgetForAction(action)
            if isinstance(button, QToolButton):
                button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
                button.setAccessibleName(action.iconText())
        self.setVisible(self._has_home_button or bool(primary or self._panels_menu.actions()) or show_tools)
        self.right_tools.setVisible(bool(self.right_tools.actions()))
        self.page_changed.emit()

    def set_labels_visible(self, visible: bool) -> None:
        style = (Qt.ToolButtonStyle.ToolButtonTextBesideIcon if visible
                 else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setToolButtonStyle(style)
        self.right_tools.setToolButtonStyle(style)
        self._panels_button.setToolButtonStyle(style)
        self._tools_button.setToolButtonStyle(style)

    def reset_groups(self, page: str | None = None) -> None:
        if page == "Library":
            for action in self._primary_actions.get(page, ()):
                action.setChecked(False)
        groups = self._primary_actions.values() if page is None else (self._primary_actions.get(page, []),)
        for actions in groups:
            for action in actions:
                if isinstance(action, DockGroupAction):
                    action.reset()


class WorkspaceToolbarContents(QWidget):
    """Fit the top toolbar on one row, keeping accessible names in both modes."""

    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self._config = config
        self._menu = QMenu(self)
        self._labels_action = self._menu.addAction(I18N.tr("menu.show_toolbar_labels", "Show Icon Labels"))
        self._labels_action.setCheckable(True)
        self._labels_action.setChecked(config.get("toolbar_show_icon_labels") is not False)
        self._labels_action.toggled.connect(self._save_label_preference)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.grid = QGridLayout(self)
        self.grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._labelled_width = 0
        self._compact_width = 0
        self._labels_visible = True
        self._measure = QTimer(self)
        self._measure.setSingleShot(True)
        self._measure.timeout.connect(self._refresh_label_widths)
        self._reflow = QTimer(self)
        self._reflow.setSingleShot(True)
        self._reflow.timeout.connect(self._fit_labels)

    def bind_buttons(
        self, panels: WorkspacePanelBar, buttons: tuple[QToolButton, ...],
    ) -> None:
        self._panels = panels
        self._buttons = buttons
        panels.page_changed.connect(lambda: self._measure.start(0))
        self._measure.start(0)

    def _refresh_label_widths(self) -> None:
        self._set_labels_visible(False)
        self._compact_width = self.grid.minimumSize().width()
        if self._labels_action.isChecked():
            self._set_labels_visible(True)
            self._labelled_width = self.grid.minimumSize().width()
        self.updateGeometry()
        self._reflow.start(0)

    def minimumSizeHint(self) -> QSize:
        return QSize(self._compact_width, self.grid.minimumSize().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow.start(0)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self.show_context_menu(event.globalPos())
        event.accept()

    def show_context_menu(self, position: QPoint) -> None:
        self._menu.popup(position)

    def _save_label_preference(self, enabled: bool) -> None:
        self._config.set("toolbar_show_icon_labels", enabled)
        if enabled:
            self._refresh_label_widths()
        self._fit_labels()

    def _fit_labels(self) -> None:
        if self._compact_width <= 0:
            return
        self._set_labels_visible(
            self._labels_action.isChecked() and self.width() >= self._labelled_width,
        )
        self.grid.activate()
        for section in (self._panels, self._panels.right_tools):
            section.layout().setGeometry(section.rect())

    def _set_labels_visible(self, visible: bool) -> None:
        if self._labels_visible == visible:
            return
        self._labels_visible = visible
        style = (Qt.ToolButtonStyle.ToolButtonTextBesideIcon if visible
                 else Qt.ToolButtonStyle.ToolButtonIconOnly)
        for button in self._buttons:
            button.setToolButtonStyle(style)
        self._panels.set_labels_visible(visible)
        for section in (self._panels, self._panels.right_tools):
            section.layout().invalidate()
            section.updateGeometry()
            section.parentWidget().layout().invalidate()
            section.parentWidget().updateGeometry()
        self.grid.invalidate()
