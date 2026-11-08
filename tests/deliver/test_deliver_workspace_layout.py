"""Regression tests for the integrated Deliver settings + queue workspace."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QMainWindow,
    QSlider,
    QTabBar,
    QToolButton,
    QWidget,
)

from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
from src.shared.ui.widgets import ChevronComboBox


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _TimelinePanelStub(QWidget):
    """Small shared-panel double for Deliver's reparenting test."""

    def __init__(self):
        super().__init__()
        self.btn_select = QToolButton(self)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._toolbar_display_mode = "edit"
        self._deliver_timeline_controls = None
        self._deliver_range_type = None

    def set_deliver_timeline_controls(self, controls):
        self._deliver_timeline_controls = controls
        if controls is not None:
            controls.setParent(self)
            controls.setVisible(self._toolbar_display_mode == "deliver")

    def set_display_mode(self, mode):
        self._toolbar_display_mode = mode
        self.btn_select.setVisible(mode == "edit")
        self.zoom_slider.setVisible(True)
        if self._deliver_timeline_controls is not None:
            self._deliver_timeline_controls.setVisible(mode == "deliver")

    def set_deliver_range_type(self, range_type):
        self._deliver_range_type = range_type


def test_deliver_workspace_contains_settings_left_of_queue(qapp):
    workspace = DeliverWorkspace()

    assert workspace.splitter.orientation() == Qt.Orientation.Horizontal
    assert workspace.splitter.indexOf(workspace.settings_panel) == 0
    assert workspace.splitter.indexOf(workspace.timeline_splitter) == 1
    assert workspace.timeline_splitter.orientation() == Qt.Orientation.Vertical
    assert workspace.timeline_splitter.indexOf(workspace.edit_preview_splitter) == 0
    assert workspace.edit_preview_splitter.orientation() == Qt.Orientation.Horizontal
    assert workspace.edit_preview_splitter.indexOf(workspace.batch_panel) == 1
    # Deliver's three internal separators are draggable but must render as a
    # single application-owned pixel, matching the cross-platform panel
    # divider policy rather than Qt's wider native handle.
    for splitter in (
        workspace.splitter,
        workspace.timeline_splitter,
        workspace.edit_preview_splitter,
    ):
        assert splitter.handleWidth() == 1
        assert "margin:" not in splitter.styleSheet()
    assert workspace.settings_panel.objectName() == "VideoDeliverSettingsPanel"
    assert workspace.batch_panel.objectName() == "VideoBatchExportPanel"
    assert workspace.batch_panel._model.rowCount() == 0


def test_queue_can_detach_and_return_without_rebuilding_widget(qapp):
    window = QMainWindow()
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    workspace.attach_batch_panel_host(window)
    original_panel_id = id(workspace.batch_panel)

    workspace.float_batch_panel()
    assert workspace._batch_panel_host.mode == "floating"
    assert workspace._batch_panel_host.dock is not None

    workspace.return_batch_panel_to_deliver()
    assert workspace._batch_panel_host.mode == "embedded"
    assert workspace.edit_preview_splitter.indexOf(workspace.batch_panel) == 1
    assert id(workspace.batch_panel) == original_panel_id

    window.close()


def test_edit_viewer_and_timeline_can_be_reused_by_deliver(qapp):
    window = QMainWindow()
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)

    viewer = QWidget()
    timeline = _TimelinePanelStub()
    viewer_dock = QDockWidget("Program", window)
    timeline_dock = QDockWidget("Timeline", window)
    viewer_dock.setWidget(viewer)
    timeline_dock.setWidget(timeline)
    window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, viewer_dock)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline_dock)

    workspace.attach_edit_panels(viewer, timeline, viewer_dock, timeline_dock)
    assert workspace.edit_preview_splitter.indexOf(viewer) == 0
    assert workspace.timeline_splitter.indexOf(timeline) == 1
    assert viewer_dock.widget() is None
    assert timeline_dock.widget() is None
    assert timeline._toolbar_display_mode == "deliver"
    assert timeline.btn_select.isHidden()
    assert not timeline.zoom_slider.isHidden()
    assert timeline._deliver_range_type == "entire"
    controls = workspace.settings_panel.get_deliver_timeline_controls()
    assert timeline._deliver_timeline_controls is controls
    assert controls.parent() is timeline
    assert not controls.isHidden()

    workspace.settings_panel._range_combo.setCurrentIndex(1)
    assert timeline._deliver_range_type == "in_out"

    workspace.detach_edit_panels()
    assert viewer_dock.widget() is viewer
    assert timeline_dock.widget() is timeline
    assert timeline._toolbar_display_mode == "edit"
    assert not timeline.btn_select.isHidden()
    assert controls.isHidden()
    assert timeline._deliver_range_type is None
    window.close()


def test_deliver_returns_nested_program_without_replacing_viewport(qapp):
    """Deliver borrows the canvas widget while the Edit dock retains its shell."""
    from PyQt6.QtWidgets import QVBoxLayout
    from src.app.controllers.dock_layout_controller import DockLayoutMixin

    window = QMainWindow()
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    viewer = QWidget()
    viewport = QWidget()
    viewport_layout = QVBoxLayout(viewport)
    viewport_layout.addWidget(viewer)
    modes: list[bool] = []
    viewport.set_deliver_mode = modes.append
    viewport.restore_program_viewer = lambda: viewport_layout.addWidget(viewer)
    timeline = _TimelinePanelStub()
    viewer_dock = QDockWidget("Viewport", window)
    viewer_dock.setWidget(viewport)
    timeline_dock = QDockWidget("Timeline", window)
    timeline_dock.setWidget(timeline)
    window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, viewer_dock)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline_dock)
    host = SimpleNamespace(
        deliver_page=workspace, edit_viewport=viewport,
        program_viewer_widget=viewer, timeline_panel=timeline,
        dock_program_viewer=viewer_dock, dock_edit_timeline=timeline_dock,
    )

    for _ in range(2):
        DockLayoutMixin._attach_edit_panels_to_deliver(host)
        assert viewer_dock.widget() is viewport
        assert workspace.edit_preview_splitter.indexOf(viewer) == 0
        DockLayoutMixin._restore_edit_panels_from_deliver(host)
        assert viewer_dock.widget() is viewport
        assert viewport_layout.indexOf(viewer) == 0
        assert timeline_dock.widget() is timeline
    assert modes == [True, False, True, False]
    window.close()


def test_video_settings_use_global_combos_and_mode_tabs(qapp):
    workspace = DeliverWorkspace()
    settings = workspace.settings_panel
    assert settings.findChild(QWidget, "DeliverOutputHeader") is not None
    assert settings._filename_edit.parent() is settings.findChild(QWidget, "DeliverOutputHeader")
    assert settings._output_dir_edit.parent() is not None
    assert settings.findChild(QWidget, "VideoSourceSection") is None
    assert settings.get_deliver_timeline_controls().parent() is settings
    controls = settings.get_deliver_timeline_controls()
    assert controls.findChild(QWidget, "DeliverTimelineCombo") is None
    assert controls.findChild(QWidget, "DeliverRangeCombo") is not None
    assert "border: none" in controls.styleSheet()

    # Every selectable export parameter must use the shared macOS-safe
    # ChevronComboBox instead of a local QComboBox stylesheet.
    # R7：+2 = Topos pix_fmt 选择器 + alpha_budget_policy 策略 combo
    # v1.1 音频轨（plan M-A6）：+1 声道布局选择（2.0/5.1/7.1）
    # R17：+1（图像序列位深组合框）
    assert len(settings.findChildren(ChevronComboBox)) == 20

    tabs = settings.findChild(QTabBar, "DeliverModeTabs")
    assert tabs is not None
    assert tabs.count() == 3
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Video", "Audio", "File"]
    assert settings._mode_stack.currentIndex() == 0
    tabs.setCurrentIndex(1)
    assert settings._mode_stack.currentIndex() == 1


def test_workspace_reads_timeline_mapping_values_and_promotes_active_sequence(
    qapp, monkeypatch,
):
    workspace = DeliverWorkspace()
    ref1 = SimpleNamespace(timeline_id="t1", name="Timeline 1")
    ref2 = SimpleNamespace(timeline_id="t2", name="Timeline 2")
    project = SimpleNamespace(
        project_id="p1",
        revision=1,
        name="Project",
        timelines={"t1": ref1, "t2": ref2},
        active_timeline=ref1,
    )
    monkeypatch.setattr(workspace.controller, "restore_from_disk", lambda: None)

    workspace.set_project(project)

    assert [
        workspace.settings_panel._timeline_combo.itemData(i)
        for i in range(workspace.settings_panel._timeline_combo.count())
    ] == ["t1", "t2"]
    # U03（第六轮审计）：轻量 ref 只是 combo 显示数据，不再进入队列
    # 上下文（否则导出器拿到 Ref 会在 audio_tracks 上崩溃）。
    assert workspace.controller._sequences_by_id == {}

    from src.features.edit.core.timeline_v2 import TimelineSequence
    from src.shared.media.metadata import Rational

    full_sequence = TimelineSequence(
        timeline_id="t2",
        name="Timeline 2",
        fps=Rational(25, 1),
        width=1920,
        height=1080,
    )

    workspace.set_sequence(full_sequence)

    assert workspace.controller._sequences_by_id["t2"] is full_sequence
    assert workspace.settings_panel._timeline_combo.currentData() == "t2"
