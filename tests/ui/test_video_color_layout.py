"""Video Color layout and transparent mouse-target regressions."""
from types import SimpleNamespace

import pytest

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QTabBar, QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.dock_layout_controller import (
    DockLayoutMixin,
    restore_workspace_dock_state,
    save_workspace_dock_state,
)
from src.app.ui.workspace_panel_bar import WorkspacePanelBar
from src.shared.ui.style_sheet import DARK_THEME


class _Config(dict):
    def set(self, key: str, value: object, *, auto_save: bool = True) -> None:
        self[key] = value


class _Window(QMainWindow, DockLayoutMixin):
    _active_work_mode = "video"

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(DARK_THEME)
        self.resize(1100, 680)
        self.setDockNestingEnabled(True)
        self.setCentralWidget(QWidget())
        self.centralWidget().hide()
        self.config_manager = _Config()
        self.all_docks = []
        for name in ("presets", "lut", "viewport", "layers", "color_timeline_clips",
                     "raw", "grading", "base_grade", "chromogen", "denoise",
                     "curves", "warper", "qualifier", "masks", "tracker", "color_keyframes", "script",
                     "analysis", "grain", "effects", "mixer"):
            dock = QDockWidget(name, self)
            dock.setObjectName(name)
            dock.setWidget(QWidget())
            dock.widget().setMinimumHeight(68 if name == "color_timeline_clips" else 70)
            if name == "viewport":
                self.viewport_container = dock.widget()
            self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
            self.all_docks.append(dock)
            setattr(self, f"dock_{name}", dock)


def _dock_tabs(host: _Window, title: str) -> QTabBar:
    return next(bar for bar in host.findChildren(QTabBar)
                if bar.parent() is host and bar.isVisible() and bar.count() > 1
                and any(bar.tabText(index) == title for index in range(bar.count())))


@pytest.mark.parametrize("width", [1360, 1200])
def test_color_default_tab_order_and_selection(qtbot: QtBot, monkeypatch, width: int) -> None:
    host = _Window()
    qtbot.addWidget(host)
    monkeypatch.setattr(host, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1600, 900)))
    host.resize(width, 800)
    host._apply_default_video_color_layout()
    factory = save_workspace_dock_state(host, "Color")
    host.show()
    qtbot.wait(20)
    primary = ["raw", "grading", "base_grade", "chromogen", "denoise"]
    secondary = ["curves", "warper", "qualifier", "masks", "tracker"]
    groups = [("analysis", ["analysis", "effects", "grain"])]
    if width == 1360:
        groups.extend([("grading", primary), ("curves", secondary)])
    else:
        groups.append(("grading", primary + secondary))

    def check_defaults() -> None:
        for title, expected in groups:
            tabs = _dock_tabs(host, title)
            assert [tabs.tabText(index) for index in range(tabs.count())] == expected
            assert tabs.tabText(tabs.currentIndex()) == title

    check_defaults()
    host.dock_raw.raise_()
    host.dock_grain.raise_()
    assert restore_workspace_dock_state(host, factory, "Color")
    qtbot.wait(20)
    check_defaults()


def test_color_default_and_visibility(qtbot: QtBot) -> None:
    host = _Window()
    qtbot.addWidget(host)
    legacy = host.saveState()
    host._apply_default_video_color_layout()
    bar = WorkspacePanelBar(host)
    bar.set_page("Color")
    host.show()

    assert not restore_workspace_dock_state(host, legacy, "Color")
    assert host.dock_presets.x() < host.dock_viewport.x() < host.dock_layers.x()
    assert host.dock_color_timeline_clips.width() == host.width()
    assert host.dock_viewport.geometry().bottom() < host.dock_color_timeline_clips.y()
    assert host.dock_color_timeline_clips.geometry().bottom() < host.dock_grading.y()
    assert host.dock_curves in host.tabifiedDockWidgets(host.dock_grading)
    assert not host.dock_viewport.toggleViewAction().isVisible()
    assert not host.dock_grading.toggleViewAction().isVisible()

    scope_tabs = host.tabifiedDockWidgets(host.dock_analysis)
    assert all(dock in scope_tabs and not dock.isHidden() for dock in (host.dock_grain, host.dock_effects))
    mixer_action = host.dock_mixer.toggleViewAction()
    assert mixer_action.isVisible()
    assert host.dock_mixer.isHidden()
    assert host.dock_mixer not in bar._color_tools
    mixer_action.trigger()
    assert not host.dock_mixer.isHidden()
    assert host.dock_mixer in host.tabifiedDockWidgets(host.dock_curves)
    mixer_action.trigger()

    scopes_action = next(action for action in bar.right_tools.actions()
                         if action.objectName() == "workspace_color_analysis")
    host.dock_grain.raise_()
    qtbot.waitUntil(lambda: not host.dock_grain.visibleRegion().isEmpty())
    original_tools = host.dock_grading.width()
    scopes_action.trigger()
    qtbot.waitUntil(lambda: host.dock_grading.width() > original_tools)
    assert all(dock.isHidden() for dock in (host.dock_analysis, host.dock_grain, host.dock_effects))
    scopes_action.trigger()
    qtbot.waitUntil(lambda: not host.dock_grain.visibleRegion().isEmpty())
    scopes_action.trigger()
    state = save_workspace_dock_state(host, "Color")
    host._apply_default_video_color_layout()
    assert not host.dock_analysis.isHidden()
    assert restore_workspace_dock_state(host, state, "Color")
    assert host.dock_analysis.isHidden()


def _drag(qtbot: QtBot, host: _Window, start: QPoint, offset: QPoint,
          cursor: Qt.CursorShape) -> None:
    handle = host.childAt(start)
    assert handle.cursor().shape() == cursor
    origin = host.mapToGlobal(start)
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(origin))
    for step in range(1, 5):
        end = origin + QPoint(offset.x() * step // 4, offset.y() * step // 4)
        qtbot.mouseMove(handle, pos=handle.mapFromGlobal(end))
        qtbot.wait(10)
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton,
                       pos=handle.mapFromGlobal(origin + offset))


def test_color_drag_on_both_clip_edges_keeps_strip_height(qtbot: QtBot) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host._apply_default_video_color_layout()
    host.show()
    qtbot.wait(30)

    clips, viewer, grading = host.dock_color_timeline_clips, host.dock_viewport, host.dock_grading
    clip_height, view_height, tool_height = clips.height(), viewer.height(), grading.height()
    _drag(qtbot, host, QPoint(clips.width() // 2, clips.y() - 4), QPoint(0, -24),
          Qt.CursorShape.SplitVCursor)
    assert clips.height() == clip_height
    assert viewer.height() < view_height
    assert grading.height() > tool_height

    view_height, tool_height = viewer.height(), grading.height()
    _drag(qtbot, host, QPoint(clips.width() // 2, clips.geometry().bottom() + 4), QPoint(0, 19),
          Qt.CursorShape.SplitVCursor)
    assert clips.height() == clip_height
    assert viewer.height() > view_height
    assert grading.height() < tool_height

    view_width = viewer.width()
    _drag(qtbot, host, QPoint(viewer.x() - 4, viewer.geometry().center().y()), QPoint(27, 0),
          Qt.CursorShape.SplitHCursor)
    assert viewer.width() < view_width


def test_color_dual_tool_capture_fits_small_screen(qtbot: QtBot) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host._apply_default_video_color_layout()
    host.removeDockWidget(host.dock_mixer)
    host.removeDockWidget(host.dock_curves)
    host.splitDockWidget(host.dock_grading, host.dock_curves, Qt.Orientation.Horizontal)
    host.tabifyDockWidget(host.dock_curves, host.dock_mixer)
    host.dock_mixer.hide()
    host.dock_curves.show()
    host.dock_analysis.hide()
    assert host.dock_curves not in host.tabifiedDockWidgets(host.dock_grading)

    host.show()
    host._fit_video_color_tools_to_screen(1280)
    assert host.dock_curves in host.tabifiedDockWidgets(host.dock_grading)
    assert host.dock_analysis.isHidden()


def test_previous_video_color_state_upgrades_scope_tabs_without_resetting_visibility(qtbot: QtBot) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host._apply_default_video_color_layout()
    host.show()
    qtbot.wait(20)
    for dock in (host.dock_grain, host.dock_effects, host.dock_mixer):
        host.tabifyDockWidget(host.dock_curves, dock)
        dock.show()
    host.dock_analysis.hide()
    host.dock_presets.hide()
    previous = host.saveState(6)

    assert restore_workspace_dock_state(host, previous, "Color")
    assert host.dock_presets.isHidden()
    assert host.dock_analysis.isHidden()
    for dock in (host.dock_grain, host.dock_effects, host.dock_mixer):
        assert dock.isHidden()
        dock.show()
    host.dock_analysis.show()
    qtbot.wait(20)
    for dock in (host.dock_grain, host.dock_effects):
        assert dock in host.tabifiedDockWidgets(host.dock_analysis)
    assert host.dock_mixer in host.tabifiedDockWidgets(host.dock_curves)


def test_color_tools_reflow_when_window_crosses_threshold(qtbot: QtBot, monkeypatch) -> None:
    host = _Window()
    qtbot.addWidget(host)
    monkeypatch.setattr(host, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1600, 900)))
    host.resize(1360, 800)
    host._apply_default_video_color_layout()
    host.show()
    qtbot.waitUntil(lambda: host.dock_curves.x() > host.dock_grading.geometry().right())
    host.dock_mixer.show()
    host.resize(1280, 800)
    qtbot.waitUntil(lambda: host.dock_curves in host.tabifiedDockWidgets(host.dock_grading))
    assert not host.dock_mixer.isHidden()
    host.resize(1360, 800)
    qtbot.waitUntil(lambda: host.dock_curves not in host.tabifiedDockWidgets(host.dock_grading))
    assert host.dock_mixer in host.tabifiedDockWidgets(host.dock_curves)
    assert host.dock_grain in host.tabifiedDockWidgets(host.dock_analysis)


def test_color_tools_reflow_at_the_expanded_minimum_width(qtbot: QtBot, monkeypatch) -> None:
    host = _Window()
    qtbot.addWidget(host)
    monkeypatch.setattr(host, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1600, 900)))
    host.resize(1360, 800)
    host._apply_default_video_color_layout()
    compact = lambda: host.dock_curves in host.tabifiedDockWidgets(host.dock_grading)
    monkeypatch.setattr(host, "minimumSizeHint", lambda: QSize(1000 if compact() else 1346, 720))
    host._fit_video_color_tools_to_screen(1346)
    assert compact()
    host._fit_video_color_tools_to_screen(1346)
    assert compact()
    host._fit_video_color_tools_to_screen(1360)
    assert not compact()


@pytest.mark.parametrize(
    ("geometry", "available", "dpr", "expected"),
    [
        ((3840, 2160), (3840, 2100), 1, (1720, 950)),
        ((2560, 1440), (2560, 1400), 1, (1520, 880)),
        ((2048, 1080), (2048, 1040), 1, (1360, 800)),
        ((1920, 1080), (1920, 1040), 1, (1200, 740)),
        ((1440, 900), (1440, 855), 2, (1360, 800)),
        ((1280, 800), (1280, 760), 2, (1200, 730)),
    ],
)
def test_video_project_window_size_fits_the_display(
    qtbot: QtBot, monkeypatch, geometry: tuple[int, int], available: tuple[int, int],
    dpr: float, expected: tuple[int, int],
) -> None:
    host = _Window()
    qtbot.addWidget(host)
    screen = SimpleNamespace(
        geometry=lambda: QRect(0, 0, *geometry),
        availableGeometry=lambda: QRect(0, 0, *available),
        devicePixelRatio=lambda: dpr,
    )
    monkeypatch.setattr(host, "screen", lambda: screen)
    assert host._video_project_window_size() == expected
