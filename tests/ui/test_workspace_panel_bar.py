"""Panel switches share real Qt dock state without taking viewport ownership."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest import MonkeyPatch
from PyQt6.QtCore import QPoint, QPointF, QSettings, QSize, Qt
from PyQt6.QtGui import QAction, QColor, QContextMenuEvent, QDragEnterEvent, QDropEvent, QImage
from PyQt6.QtWidgets import QApplication, QDockWidget, QHBoxLayout, QMainWindow, QMenu, QToolBar, QToolButton, QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.ui.files_panel import FilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.workspace_panel_bar import WorkspacePanelBar, WorkspaceToolbarContents
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


def test_mixer_toolbar_has_five_independent_labeled_panel_switches(qtbot: QtBot) -> None:
    window = _Window(_Config())
    qtbot.addWidget(window)
    top = Qt.DockWidgetArea.TopDockWidgetArea
    console = window.add_panel("dock_audio_console", top)
    pool = window.add_panel("dock_audio_media_pool", top)
    effects = window.add_panel("dock_audio_effects", top)
    viewer = window.add_panel("dock_audio_viewer", top)
    properties = window.add_panel("dock_audio_properties", top)
    timeline = window.add_panel("dock_audio_timeline", Qt.DockWidgetArea.BottomDockWidgetArea)
    editor = window.add_panel("dock_audio_clip_editor", Qt.DockWidgetArea.BottomDockWidgetArea)
    for dock in (pool, effects):
        window.tabifyDockWidget(console, dock)
    properties.hide()
    bar = window.create_panel_bar("Mixer")
    window.show()
    effects.raise_()
    qtbot.waitUntil(lambda: not effects.visibleRegion().isEmpty())

    pool_action, mixer_action, effects_action = [action for action in bar.actions() if action.isVisible()]
    preview, inspector = bar.right_tools.actions()
    assert (pool_action, mixer_action, effects_action, preview, inspector) == tuple(
        dock.toggleViewAction() for dock in (pool, console, effects, viewer, properties)
    )
    for toolbar, actions in ((bar, (pool_action, mixer_action, effects_action)),
                             (bar.right_tools, (preview, inspector))):
        for action in actions:
            button = toolbar.widgetForAction(action)
            assert isinstance(button, QToolButton)
            assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            assert button.text() and not button.icon().isNull()
    assert preview.isChecked() and not inspector.isChecked()
    assert mixer_action.isChecked()
    mixer_action.trigger()
    assert console.isHidden() and not pool.isHidden() and not effects.isHidden()
    mixer_action.trigger()
    qtbot.waitUntil(lambda: not console.visibleRegion().isEmpty())
    assert all(not dock.isHidden() for dock in (console, pool, effects))

    preview.trigger()
    inspector.trigger()
    assert viewer.isHidden() and not properties.isHidden()
    assert not timeline.isHidden() and not editor.isHidden()
    preview.trigger()
    inspector.trigger()
    assert not viewer.isHidden() and properties.isHidden()

    for action in (pool_action, mixer_action, effects_action, preview):
        action.trigger()
    assert all(dock.isHidden() for dock in (pool, console, effects, viewer, properties))
    assert not timeline.isHidden() and not editor.isHidden()
    effects_action.trigger()
    qtbot.waitUntil(lambda: not effects.visibleRegion().isEmpty())
    assert all(dock.isHidden() for dock in (pool, console, viewer, properties))


def test_grading_tools_keep_grade_tabs_available(qtbot: QtBot) -> None:
    window = _Window(_Config())
    qtbot.addWidget(window)
    bottom = Qt.DockWidgetArea.BottomDockWidgetArea
    wheels = window.add_panel("dock_grading", bottom)
    base_grade = window.add_panel("dock_base_grade", bottom)
    chromogen = window.add_panel("dock_chromogen", bottom)
    raw = window.add_panel("dock_raw", bottom)
    window.tabifyDockWidget(wheels, base_grade)
    window.tabifyDockWidget(base_grade, chromogen)
    window.tabifyDockWidget(wheels, raw)
    bar = window.create_panel_bar("Color")
    bar.compact_default_color_tools()
    window.show()

    for expected in (base_grade, raw, chromogen, wheels):
        selected = next(action for action, dock in bar._color_tool_actions if dock is expected)
        selected.trigger()
        qtbot.waitUntil(lambda: not expected.visibleRegion().isEmpty())
        assert not wheels.isHidden()
        assert not base_grade.isHidden()
        assert not chromogen.isHidden()
        assert {base_grade, chromogen} <= set(window.tabifiedDockWidgets(wheels))


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
        splitter = browser._workspace_splitter
        splitter.moveSplitter(480, 1)
        split_before = splitter.sizes()[1] / sum(splitter.sizes())
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
        assert splitter.sizes()[1] / sum(splitter.sizes()) == pytest.approx(split_before, abs=0.025)
        bar.reset_groups("Library")
        assert not toggle.isChecked() and not browser.hosts_media_pool(pool)
        toggle.trigger()
        assert browser.hosts_media_pool(pool)

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
    preferences = Mock()
    monkeypatch.setattr(window, "_show_welcome_dialog", welcome, raising=False)
    monkeypatch.setattr(window, "_show_project_manager", project_manager, raising=False)
    monkeypatch.setattr(window, "show_preferences_dialog", preferences, raising=False)
    for name in (
        "switch_to_page", "on_reset_all_click",
        "on_layout_combo_activated", "update_layout_combobox", "export_file_as",
    ):
        monkeypatch.setattr(window, name, lambda *_: None, raising=False)

    MenuToolbarMixin.setup_toolbar(window)
    window.resize(1600, 550)
    window.show()
    main_button = window.application_menu_button
    panel_button = window.workspace_panel_bar._panels_button
    qtbot.waitUntil(panel_button.isVisible)

    home_button = window.project_manager_btn
    qtbot.waitUntil(lambda: home_button.width() > 28)
    assert home_button.isVisible()
    assert home_button.height() == 28 and home_button.width() > 28
    assert not home_button.icon().isNull()
    home_label = (I18N.tr("welcome.recent_projects", "Recent Projects") if mode == "image"
                  else I18N.tr("action.open_project_manager", "Open Project Manager..."))
    assert home_button.toolTip() == home_button.accessibleName() == home_label
    assert home_button.text() == (home_label if mode == "image" else
                                  I18N.tr("action.project_manager_label", "Project Manager"))
    qtbot.mouseClick(home_button, Qt.MouseButton.LeftButton)
    if mode == "image":
        welcome.assert_called_once()
        project_manager.assert_not_called()
    else:
        project_manager.assert_called_once()
        welcome.assert_not_called()

    assert main_button.text() == I18N.tr("menu.application_menu", "Menu")
    assert panel_button.text() == I18N.tr("menu.panels", "Panels")
    assert main_button.toolButtonStyle() == panel_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert main_button.height() == panel_button.height() == 28
    assert min(main_button.width(), panel_button.width()) > 28
    assert main_button.iconSize() == panel_button.iconSize() == QSize(16, 16)
    assert window.pref_btn.isVisible() and not window.pref_btn.icon().isNull()
    assert not hasattr(window, "reset_all_btn")
    assert window.pref_btn.text() == I18N.tr("action.preferences_label", "Preferences")
    assert window.pref_btn.height() == 28 and window.pref_btn.width() > 28
    qtbot.mouseClick(window.pref_btn, Qt.MouseButton.LeftButton)
    preferences.assert_called_once()
    main_pos = main_button.mapTo(window, QPoint())
    panel_pos = panel_button.mapTo(window, QPoint())
    home_pos = home_button.mapTo(window, QPoint())
    assert main_pos.y() == panel_pos.y() == home_pos.y()
    assert panel_pos.x() - main_pos.x() == main_button.width() + 2
    assert home_pos.x() - panel_pos.x() == panel_button.width() + 2

    icon_centers = []
    for button in (main_button, panel_button):
        image = button.grab().toImage()
        icon_region_width = round(21 * image.devicePixelRatio())
        rows = [
            y for y in range(image.height())
            if any(image.pixelColor(x, y).lightness() > 120 for x in range(icon_region_width))
        ]
        icon_centers.append((rows[0] + rows[-1]) / (2 * image.devicePixelRatio()))
    assert abs(icon_centers[0] - icon_centers[1]) <= 0.5

    contents = window.main_toolbar.actions()[0].defaultWidget()
    compact_width = contents.minimumSizeHint().width()
    window.resize(compact_width + 35, 550)
    qtbot.waitUntil(lambda: main_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly)
    buttons = (main_button, panel_button, home_button, window.pref_btn)
    qtbot.waitUntil(lambda: all(button.isVisible() for button in buttons))
    for button in buttons:
        assert button.isVisible() and button.toolTip()
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly

    window.resize(1600, 550)
    qtbot.waitUntil(lambda: main_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    point = panel_button.rect().center()
    QApplication.sendEvent(panel_button, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, panel_button.mapToGlobal(point),
    ))
    qtbot.waitUntil(contents._menu.isVisible)
    labels = contents._menu.actions()[0]
    assert labels.isChecked()
    qtbot.mouseClick(contents._menu, Qt.MouseButton.LeftButton,
                     pos=contents._menu.actionGeometry(labels).center())
    assert window.config_manager.get("toolbar_show_icon_labels") is False
    assert all(button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly for button in buttons)
    restored = WorkspaceToolbarContents(window.config_manager)
    qtbot.addWidget(restored)
    assert not restored._menu.actions()[0].isChecked()
    labels.trigger()
    assert window.config_manager.get("toolbar_show_icon_labels") is True
    assert main_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon

    window.workspace_panel_bar.set_page("Library")
    qtbot.waitUntil(panel_button.isHidden)
    assert home_button.isVisible()


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
    label_x = round(28 * active.devicePixelRatio())
    assert active.copy(label_x, 0, active.width() - label_x, active.height()) == inactive.copy(
        label_x, 0, inactive.width() - label_x, inactive.height(),
    )

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


def test_edit_media_pool_shortcut_collapses_mixer_tab_and_restores_focus(qtbot: QtBot) -> None:
    window = _Window(_Config())
    qtbot.addWidget(window)
    window.setStyleSheet(DARK_THEME)
    area = Qt.DockWidgetArea.LeftDockWidgetArea
    pool = window.add_panel("dock_edit_media_pool", area)
    effects = window.add_panel("dock_edit_effects", area)
    mixer = window.add_panel("dock_audio_mixer", area)
    window.tabifyDockWidget(pool, effects)
    window.tabifyDockWidget(pool, mixer)
    bar = window.create_panel_bar("Edit")
    window.show()
    mixer.raise_()
    qtbot.waitUntil(lambda: not mixer.visibleRegion().isEmpty())

    pool_action = window.findChild(QAction, "workspace_edit_dock_edit_media_pool")
    assert window.findChild(QAction, "shortcut_dock_audio_mixer") is None
    pool_button = bar.widgetForAction(pool_action)
    assert pool_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert pool_button.text() == pool_action.iconText()
    assert pool_button.accessibleName() == pool_button.toolTip() == pool_action.text()
    assert not pool_button.icon().isNull()

    qtbot.mouseClick(pool_button, Qt.MouseButton.LeftButton)
    assert all(dock.isHidden() for dock in (pool, effects, mixer))
    assert not pool_action.isChecked()
    qtbot.mouseClick(pool_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not mixer.visibleRegion().isEmpty())
    assert all(not dock.isHidden() for dock in (pool, effects, mixer))

    pool.hide()
    effects.hide()
    assert pool_action.isChecked()
    qtbot.mouseClick(pool_button, Qt.MouseButton.LeftButton)
    assert mixer.isHidden() and not pool_action.isChecked()
    qtbot.mouseClick(pool_button, Qt.MouseButton.LeftButton)
    assert not mixer.isHidden() and pool_action.isChecked()


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


def test_photo_color_toolbar_filters_work_mode_and_selects_the_active_tool(qtbot) -> None:
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
    # Selecting a tool keeps the other tabs available as the window narrows.
    next(action for action in bar._tools_menu.actions() if action.text() == curves.windowTitle()).trigger()
    qtbot.waitUntil(lambda: not curves.visibleRegion().isEmpty())
    assert not wheel.isHidden()
    next(action for action in bar._tools_menu.actions() if action.text() == wheel.windowTitle()).trigger()
    qtbot.waitUntil(lambda: not wheel.visibleRegion().isEmpty())
    assert not curves.isHidden()

    bar.set_page("Library")
    assert not bar.isVisible()
    bar.set_page("Color")
    assert bar.isVisible()
    assert scopes.toggleViewAction() in bar.right_tools.actions()
