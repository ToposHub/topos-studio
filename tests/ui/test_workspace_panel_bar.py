"""Panel switches share real Qt dock state without taking viewport ownership."""

from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QDockWidget, QHBoxLayout, QMainWindow, QMenu, QToolBar, QWidget

from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.ui.workspace_panel_bar import WorkspacePanelBar
from src.shared.ui.style_sheet import DARK_THEME


class _Config(dict):
    def set(self, key: str, value: object, *, auto_save: bool = True) -> None:
        self[key] = value


class _Window(QMainWindow):
    def __init__(self, config: _Config, mode: str = "video") -> None:
        super().__init__()
        self.config_manager = config
        self._mode = mode
        self.setCentralWidget(QWidget())
        self.resize(1100, 550)

    def add_panel(self, attr: str, area: Qt.DockWidgetArea) -> QDockWidget:
        panel = QDockWidget(attr, self)
        panel.setObjectName(attr)
        panel.setWidget(QWidget())
        setattr(self, attr, panel)
        self.addDockWidget(area, panel)
        return panel

    def create_panel_bar(self, page: str) -> WorkspacePanelBar:
        main = QToolBar(self)
        main.setObjectName("mainToolbar")
        self.addToolBar(main)
        toolbar_content = QWidget(main)
        layout = QHBoxLayout(toolbar_content)
        main.addWidget(toolbar_content)
        bar = WorkspacePanelBar(self)
        layout.addWidget(bar)
        layout.addWidget(bar.right_tools)
        bar.set_page(page)
        return bar

    def _current_work_mode(self) -> str:
        return self._mode

    def _get_enabled_features(self) -> set[str]:
        return {"edit", "color", "composite", "mixer"}

    def _layout_profile_mode(self) -> str:
        return f"studio_{self._mode}"


def _edit_window(qtbot, config: _Config) -> tuple[_Window, WorkspacePanelBar]:
    window = _Window(config)
    qtbot.addWidget(window)
    right = Qt.DockWidgetArea.RightDockWidgetArea
    props = window.add_panel("dock_properties", right)
    info = window.add_panel("dock_edit_info", right)
    window.tabifyDockWidget(props, info)
    return window, window.create_panel_bar("Edit")


def test_main_and_panel_menu_buttons_align_in_top_toolbar(qtbot, monkeypatch) -> None:
    window = _Window(_Config())
    qtbot.addWidget(window)
    window.setStyleSheet(DARK_THEME)
    window.add_panel("dock_edit_media_pool", Qt.DockWidgetArea.LeftDockWidgetArea)
    window.current_page = "Edit"
    window._edit_available = True
    window._composite_available = False
    window._deliver_available = False
    window.menuBar().addMenu("File").addAction("Open")
    monkeypatch.setattr(window, "_is_video_work_mode", lambda: False, raising=False)
    for name in (
        "switch_to_page", "on_reset_all_click", "show_preferences_dialog",
        "on_layout_combo_activated", "update_layout_combobox", "export_file_as",
    ):
        monkeypatch.setattr(window, name, lambda *_: None, raising=False)

    MenuToolbarMixin.setup_toolbar(window)
    window.show()
    main_button = window.application_menu_button
    panel_button = window.workspace_panel_bar._panels_button
    qtbot.waitUntil(panel_button.isVisible)

    assert main_button.size() == panel_button.size() == QSize(28, 28)
    assert main_button.iconSize() == panel_button.iconSize() == QSize(16, 16)
    main_pos = main_button.mapTo(window, QPoint())
    panel_pos = panel_button.mapTo(window, QPoint())
    assert main_pos.y() == panel_pos.y()
    assert panel_pos.x() - main_pos.x() == main_button.width() + 3

    icon_centers = []
    for button in (main_button, panel_button):
        image = button.grab().toImage()
        rows = [
            y for y in range(image.height())
            if any(image.pixelColor(x, y).lightness() > 120 for x in range(image.width()))
        ]
        icon_centers.append((rows[0] + rows[-1]) / (2 * image.devicePixelRatio()))
    assert abs(icon_centers[0] - icon_centers[1]) <= 0.5


def test_toolbar_panel_shortcuts_and_popups_paint_against_dark_theme(qtbot) -> None:
    window, bar = _edit_window(qtbot, _Config())
    window.setStyleSheet(DARK_THEME)
    window.show()
    action = window.findChild(QAction, "workspace_edit_dock_properties")
    button = bar.right_tools.widgetForAction(action)
    assert action.isChecked()

    active = button.grab().toImage()
    action.trigger()
    assert not action.isChecked()
    inactive = button.grab().toImage()
    assert active.size() == inactive.size()
    assert active.pixelColor(2, active.height() // 2) == inactive.pixelColor(2, inactive.height() // 2)
    assert active != inactive

    # An application-menu popup is nested under a plain toolbar container.
    app_menu = QMenu(bar.parentWidget())
    app_menu.addAction("File")
    bar._tools_menu.addAction("Tool")
    for menu in (app_menu, bar._panels_menu, bar._tools_menu):
        menu.ensurePolished()
        menu.adjustSize()
        image = menu.grab().toImage()
        color = image.pixelColor(round(2 * image.devicePixelRatio()), image.height() // 2)
        assert color.name() == "#27272b"
        assert color.alpha() == 255


def test_edit_switch_recovers_preview_space_and_restores_selected_tab(qtbot) -> None:
    config = _Config()
    window, bar = _edit_window(qtbot, config)
    window.show()
    window.resizeDocks([window.dock_properties], [275], Qt.Orientation.Horizontal)
    window.dock_edit_info.raise_()
    qtbot.waitUntil(lambda: not window.dock_edit_info.visibleRegion().isEmpty())
    props_toggle = window.findChild(QAction, "workspace_edit_dock_properties")
    assert props_toggle.isChecked()
    original_preview_width = window.centralWidget().width()
    button = bar.right_tools.widgetForAction(props_toggle)
    assert not button.icon().isNull()
    assert button.focusPolicy() == Qt.FocusPolicy.TabFocus
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.centralWidget().width() > original_preview_width)
    assert window.dock_properties.isHidden()
    assert window.dock_edit_info.isHidden()
    assert button.isVisible()
    assert not props_toggle.isChecked()
    saved = window.saveState()

    restored, restored_bar = _edit_window(qtbot, config)
    assert restored.restoreState(saved)
    restored.show()
    qtbot.waitUntil(restored.isVisible)
    action = restored.findChild(QAction, "workspace_edit_dock_properties")
    assert restored_bar.right_tools.widgetForAction(action).isVisible()
    assert not action.isChecked()
    action.trigger()
    qtbot.waitUntil(lambda: not restored.dock_edit_info.visibleRegion().isEmpty())
    assert restored.dock_properties.visibleRegion().isEmpty()
    assert not restored.dock_properties.isHidden()
    assert action.isChecked()
    assert restored.dock_edit_info.width() == 275


def test_color_toolbar_filters_work_mode_and_replaces_the_active_tool(qtbot) -> None:
    window = _Window(_Config(), mode="image")
    qtbot.addWidget(window)
    bottom = Qt.DockWidgetArea.BottomDockWidgetArea
    wheel = window.add_panel("dock_grading", bottom)
    curves = window.add_panel("dock_curves", bottom)
    clips = window.add_panel("dock_color_timeline_clips", bottom)
    scopes = window.add_panel("dock_analysis", bottom)
    window.tabifyDockWidget(wheel, curves)
    bar = window.create_panel_bar("Color")
    window.show()

    assert clips.toggleViewAction() not in bar.actions()
    assert scopes.toggleViewAction() in bar.right_tools.actions()
    curves.hide()
    # The menu can always select a hidden tool without showing another tab.
    next(action for action in bar._tools_menu.actions() if action.text() == curves.windowTitle()).trigger()
    assert wheel.isHidden()
    assert not curves.isHidden()
    next(action for action in bar._tools_menu.actions() if action.text() == wheel.windowTitle()).trigger()
    assert curves.isHidden()
    assert not wheel.isHidden()

    bar.set_page("Library")
    assert not bar.isVisible()
    bar.set_page("Color")
    assert bar.isVisible()
    assert scopes.toggleViewAction() in bar.right_tools.actions()
