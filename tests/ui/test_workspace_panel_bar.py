"""Panel switches share real Qt dock state without taking viewport ownership."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest import MonkeyPatch
from PyQt6.QtCore import QPoint, QPointF, QSettings, QSize, Qt
from PyQt6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent, QImage
from PyQt6.QtWidgets import QApplication, QDockWidget, QHBoxLayout, QMainWindow, QMenu, QToolBar, QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.ui.files_panel import FilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.workspace_panel_bar import WorkspacePanelBar
from src.features.edit.core.project import EditProject
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.shared.i18n.i18n import I18N
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


def test_grading_tools_keep_color_wheel_and_base_grade_tabs_available(qtbot: QtBot) -> None:
    window = _Window(_Config())
    qtbot.addWidget(window)
    bottom = Qt.DockWidgetArea.BottomDockWidgetArea
    wheels = window.add_panel("dock_grading", bottom)
    base_grade = window.add_panel("dock_base_grade", bottom)
    raw = window.add_panel("dock_raw", bottom)
    window.tabifyDockWidget(wheels, base_grade)
    window.tabifyDockWidget(wheels, raw)
    bar = window.create_panel_bar("Color")
    bar.compact_default_color_tools()
    window.show()

    for expected in (base_grade, raw, wheels):
        selected = next(action for action in bar._tools_menu.actions()
                        if action.text() == expected.windowTitle())
        selected.trigger()
        qtbot.waitUntil(lambda: not expected.visibleRegion().isEmpty())
        assert not wheels.isHidden()
        assert not base_grade.isHidden()
        assert base_grade in window.tabifiedDockWidgets(wheels)


def test_library_pool_toggle_switches_layout_and_imports_into_the_live_project(
    qtbot: QtBot, monkeypatch: MonkeyPatch, tmp_path: Path,
) -> None:
    """Menu actions and viewport drops share the same Edit project and worker."""
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    preferences = NativeFilesPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    monkeypatch.setattr("src.app.ui.native_files_prefs.NativeFilesPreferences", lambda: preferences)

    class Window(_Window, DockLayoutMixin):
        current_page = "Library"

    window = Window(_Config())
    qtbot.addWidget(window)
    window.centralWidget().hide()
    area = Qt.DockWidgetArea.LeftDockWidgetArea
    files = window.add_panel("dock_files", area)
    dock = window.add_panel("dock_edit_media_pool", area)
    files.setWidget(browser := FilesPanel(window, include_plugins=False))
    window.files_panel = browser
    dock.setWidget(pool := MediaPoolPanel())
    window.media_pool_panel = pool
    dock.hide()
    pool.auto_project_needed.connect(lambda: pool.set_project(EditProject.create("Library import")))
    bar = window.create_panel_bar("Library")
    window.resize(1220, 760)
    window.show()

    try:
        panel = browser._native_panel
        media_dir = tmp_path / "Test Media"
        media_dir.mkdir()
        paths = [media_dir / filename for filename in ("first.png", "second.png", "third.png")]
        image = QImage(24, 24, QImage.Format.Format_RGB32)
        image.fill(QColor("darkorange"))
        for path in paths:
            assert image.save(str(path))
        transport = browser._native_transport
        with qtbot.waitSignal(transport.library_changed):
            transport.pin_library(str(media_dir))
        qtbot.waitUntil(lambda: panel.is_browsable_path(str(media_dir)))
        panel.navigate(str(media_dir))
        qtbot.waitUntil(lambda: panel.current_path == str(media_dir) and panel.proxy.rowCount() == 3)
        panel.view_combo.setCurrentIndex(panel.view_combo.findData("grid"))
        view = panel.icon_view
        flag = panel.selection.SelectionFlag

        def trigger_import_at(index: int) -> None:
            row = panel.proxy.index(index, 0)
            qtbot.waitUntil(lambda: view.indexAt(view.visualRect(row).center()) == row)
            menu = panel._build_context_menu(view, view.visualRect(row).center())
            label = I18N.tr("files.native.add_to_media_pool", "添加到媒体池")
            next(action for action in menu.actions() if action.text() == label).trigger()

        # Right-click a single file with the pool closed, then import a batch
        # including that same file. It must only appear once in the project.
        with qtbot.waitSignal(pool.media_import_status):
            trigger_import_at(0)
        assert len(pool.get_project().assets) == 1
        for row in (0, 1):
            panel.selection.select(panel.proxy.index(row, 0), flag.Select | flag.Rows)
        with qtbot.waitSignal(pool.media_import_status):
            trigger_import_at(1)
        assert len(pool.get_project().assets) == 2

        toggle = window.findChild(QAction, "workspace_library_media_pool")
        button = bar.widgetForAction(toggle)
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: pool.isVisible() and pool.y() > browser.content_stack.y())
        assert browser.hosts_media_pool(pool)
        window.resize(1220, 500)
        qtbot.waitUntil(lambda: pool.x() > browser.content_stack.x())
        assert pool.y() == browser.content_stack.y()

        panel.selection.select(panel.proxy.index(2, 0), flag.ClearAndSelect | flag.Rows)
        mime = panel.mime_data_for_selection()
        viewport = pool.grid_view.viewport()
        pos = viewport.rect().center()
        action = Qt.DropAction.CopyAction
        press = Qt.MouseButton.LeftButton
        mods = Qt.KeyboardModifier.NoModifier
        enter = QDragEnterEvent(pos, action, mime, press, mods)
        drop = QDropEvent(QPointF(pos), action, mime, press, mods)
        with qtbot.waitSignal(pool.media_import_status):
            QApplication.sendEvent(viewport, enter)
            QApplication.sendEvent(viewport, drop)
        assert drop.isAccepted()
        assert set(a.path for a in pool.get_project().assets.values()) == {str(p) for p in paths}
        assert all(path.is_file() for path in paths)
        window.resize(1220, 760)
        qtbot.waitUntil(lambda: pool.y() > browser.content_stack.y())

        # The exact same widget goes back to Edit and keeps its project.
        window.set_library_media_pool_visible(False)
        window.current_page = "Edit"
        bar.set_page("Edit")
        dock.show()
        assert dock.widget() is pool and len(pool.get_project().assets) == 3
        window.current_page = "Library"
        dock.hide()
        bar.set_page("Library")
        assert toggle.isChecked() and browser.hosts_media_pool(pool)
        toggle.trigger()
        assert not pool.isVisible() and dock.widget() is pool
    finally:
        browser.shutdown()
        assert browser.wait_for_shutdown(3)


@pytest.mark.parametrize("mode", ["image", "video"])
def test_main_and_panel_menu_buttons_align_in_top_toolbar(qtbot, monkeypatch, mode: str) -> None:
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    window = _Window(_Config(work_mode=mode, show_welcome_on_startup=False), mode=mode)
    qtbot.addWidget(window)
    window.setStyleSheet(DARK_THEME)
    window.add_panel("dock_edit_media_pool", Qt.DockWidgetArea.LeftDockWidgetArea)
    window.current_page = "Edit"
    window._edit_available = True
    window._composite_available = False
    window._deliver_available = False
    window.menuBar().addMenu("File").addAction("Open")
    monkeypatch.setattr(window, "_is_video_work_mode",
                        lambda: MenuToolbarMixin._is_video_work_mode(window), raising=False)
    welcome = Mock()
    project_manager = Mock()
    monkeypatch.setattr(window, "_show_welcome_dialog", welcome, raising=False)
    monkeypatch.setattr(window, "_show_project_manager", project_manager, raising=False)
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

    home_button = window.project_manager_btn
    assert home_button.isVisible()
    assert home_button.size() == QSize(28, 28)
    assert not home_button.icon().isNull()
    home_label = (I18N.tr("welcome.recent_projects", "Recent Projects") if mode == "image"
                  else I18N.tr("action.open_project_manager", "Open Project Manager..."))
    assert home_button.toolTip() == home_button.accessibleName() == home_label
    qtbot.mouseClick(home_button, Qt.MouseButton.LeftButton)
    if mode == "image":
        welcome.assert_called_once()
        project_manager.assert_not_called()
    else:
        project_manager.assert_called_once()
        welcome.assert_not_called()

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


def test_composite_toolbar_opens_node_graph_without_closing_timeline(qtbot: QtBot) -> None:
    window = _Window(_Config())
    qtbot.addWidget(window)
    graph = window.add_panel("dock_composite", Qt.DockWidgetArea.TopDockWidgetArea)
    graph.setWindowTitle("Node Graph")
    timeline = window.add_panel("dock_composite_keyframes", Qt.DockWidgetArea.BottomDockWidgetArea)
    timeline.setWindowTitle("Layer Timeline")
    graph.hide()
    bar = window.create_panel_bar("Composite")
    window.show()

    assert graph.toggleViewAction() in bar.actions()
    assert timeline.toggleViewAction() in bar.actions()
    graph.toggleViewAction().trigger()
    assert not graph.isHidden()
    assert not timeline.isHidden()
    graph.toggleViewAction().trigger()
    assert graph.isHidden()
    assert not timeline.isHidden()


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
