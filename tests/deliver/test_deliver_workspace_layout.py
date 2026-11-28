"""Regression tests for the integrated Deliver settings + queue workspace."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
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
from src.shared.config.config_manager import ConfigManager
from src.shared.ui.style_sheet import orange_tab_style
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.panel_splitter import PANEL_RULE_WIDTH, PanelSplitter


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
        assert isinstance(splitter, PanelSplitter)
        assert splitter.handleWidth() == PANEL_RULE_WIDTH
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


def test_compact_export_keeps_preview_queue_and_timeline_usable(qapp):
    window = QMainWindow()
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    viewer = QWidget()
    viewer.setMinimumSize(200, 150)
    timeline = _TimelinePanelStub()
    timeline.setMinimumHeight(140)
    workspace.attach_edit_panels(viewer, timeline, None, None)
    window.resize(1160, 590)
    window.show()
    qapp.processEvents()
    workspace.ensure_deliver_layout()
    qapp.processEvents()

    assert window.width() == 1160
    assert window.height() == 590
    assert viewer.width() >= 400
    assert workspace.batch_panel.width() >= workspace.batch_panel.minimumWidth()
    assert timeline.height() >= 240
    assert workspace.settings_panel._add_btn.isVisible()

    queue = workspace.batch_panel
    workspace.render_queue_visibility_action.trigger()
    qapp.processEvents()
    assert queue.isHidden()
    assert viewer.width() >= 700
    workspace.reset_render_queue_visibility()
    qapp.processEvents()
    assert queue.isVisible()
    assert viewer.width() >= 400

    preview_splitter = workspace.edit_preview_splitter
    preview_splitter.moveSplitter(preview_splitter.width() - 400 - preview_splitter.handleWidth(), 1)
    workspace.render_queue_visibility_action.trigger()
    workspace.render_queue_visibility_action.trigger()
    qapp.processEvents()
    assert queue.width() == 400
    window.close()


def test_render_queue_toggle_reclaims_preview_width_and_survives_restart(qapp, tmp_path):
    config_path = tmp_path / "config.json"
    window = QMainWindow()
    window.config_manager = ConfigManager(config_path)
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    workspace.attach_batch_panel_host(window)
    window.resize(1480, 760)
    window.show()
    qapp.processEvents()

    action = workspace.render_queue_visibility_action
    assert action.isCheckable() and action.isChecked()
    toolbar_button = QToolButton()
    toolbar_button.setDefaultAction(action)

    splitter = workspace.edit_preview_splitter
    viewer = splitter.widget(0)
    splitter.moveSplitter(splitter.width() - 430 - splitter.handleWidth(), 1)
    queue_width = workspace.batch_panel.width()
    original_preview_width = viewer.width()
    queue = workspace.controller.queue

    toolbar_button.click()
    qapp.processEvents()
    assert not action.isChecked()
    assert workspace.batch_panel.isHidden()
    assert viewer.width() > original_preview_width + 300
    assert workspace.controller.queue is queue
    window.close()

    restored_window = QMainWindow()
    restored_window.config_manager = ConfigManager(config_path)
    restored_workspace = DeliverWorkspace()
    restored_window.setCentralWidget(restored_workspace)
    restored_workspace.attach_batch_panel_host(restored_window)
    restored_window.resize(1480, 760)
    restored_window.show()
    qapp.processEvents()

    restored_action = restored_workspace.render_queue_visibility_action
    assert not restored_action.isChecked()
    assert restored_workspace.batch_panel.isHidden()
    restored_action.trigger()
    qapp.processEvents()
    assert restored_workspace.batch_panel.isVisible()
    assert restored_workspace.batch_panel.width() == pytest.approx(queue_width, abs=1)

    restored_action.trigger()
    restored_workspace.reset_render_queue_visibility()
    qapp.processEvents()
    assert restored_action.isChecked()
    assert restored_workspace.batch_panel.isVisible()
    assert restored_workspace.batch_panel.width() == pytest.approx(370, abs=1)
    restored_window.close()


def test_render_queue_toggle_preserves_detached_panel_and_placement(qapp):
    window = QMainWindow()
    window.all_docks = []
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    workspace.attach_batch_panel_host(window)
    window.resize(1480, 760)
    window.show()
    qapp.processEvents()

    panel = workspace.batch_panel
    queue_width = panel.width()
    action = workspace.render_queue_visibility_action
    workspace.float_batch_panel()
    floating_dock = window.all_docks[0]
    assert floating_dock.property("toposPanelCloseAction") is action
    assert floating_dock.widget() is panel
    assert floating_dock.isVisible()
    action.trigger()
    assert floating_dock.isHidden()
    assert not action.isChecked()

    action.trigger()
    assert floating_dock.isVisible()
    assert action.isChecked()
    workspace.return_batch_panel_to_deliver()
    qapp.processEvents()
    assert workspace.edit_preview_splitter.widget(1) is panel
    assert panel.isVisible()
    assert panel.width() == pytest.approx(queue_width, abs=1)
    assert window.all_docks == []
    window.close()


def test_detached_queue_stays_with_deliver_when_switching_to_an_old_qt_state(qapp, tmp_path):
    window = QMainWindow()
    window.config_manager = ConfigManager(tmp_path / "config.json")
    window.all_docks = []
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    workspace.attach_batch_panel_host(window)
    window.resize(1480, 760)
    window.show()
    qapp.processEvents()
    other_page_state = window.saveState()

    workspace.float_batch_panel()
    dock = window.all_docks[0]
    dock.move(32, 64)
    position = dock.pos()
    deliver_state = window.saveState()
    action = workspace.render_queue_visibility_action
    saved_preferences = window.config_manager.config.copy()

    assert window.restoreState(other_page_state)
    workspace.set_workspace_active(False)
    assert dock.isHidden()
    assert action.isChecked()
    assert window.config_manager.config == saved_preferences

    assert window.restoreState(deliver_state)
    workspace.set_workspace_active(True)
    qapp.processEvents()
    assert dock.isVisible()
    assert dock.pos() == position
    assert window.all_docks == [dock]
    assert dock.widget() is workspace.batch_panel
    assert window.config_manager.config == saved_preferences

    action.trigger()
    assert not action.isChecked()
    assert dock.isHidden()
    workspace.set_workspace_active(False)
    assert window.restoreState(deliver_state)
    workspace.set_workspace_active(True)
    assert not action.isChecked()
    assert dock.isHidden()
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


def test_export_defaults_to_full_timeline_and_drag_updates_render_range(qapp):
    from src.features.edit.core.timeline_export import (
        ExportRangeType, TimelineExportConfig, TimelineExporter, estimate_export_duration,
    )
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.ui.timeline_items import TRACK_HEADER_WIDTH
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.shared.media.metadata import Rational

    window = QMainWindow()
    workspace = DeliverWorkspace()
    window.setCentralWidget(workspace)
    timeline = TimelinePanel()
    dock = QDockWidget("Timeline", window)
    dock.setWidget(timeline)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    clip = ClipInstance(
        clip_id="clip", asset_id="asset", track_id="v1", timeline_start=0,
        timeline_duration=1200, source_start=0, source_duration=1200,
    )
    sequence = TimelineSequence(
        timeline_id="sequence", name="Export Range", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        range_start=30, range_end=1499,
    )
    timeline.load_timeline(sequence)
    timeline.set_zoom_level(4.0)
    window.resize(1440, 900)
    window.show()
    qapp.processEvents()
    workspace.attach_edit_panels(QWidget(), timeline, None, dock)
    workspace.set_sequence(sequence)
    qapp.processEvents()

    view = timeline.get_timeline_view()
    ruler_range = view._range_item
    assert timeline.btn_zoom_fit.isChecked()
    assert view.horizontalScrollBar().value() == 0
    assert timeline.export_presentation_state()["custom_pixels_per_frame"] == 4.0
    assert (ruler_range._range_start, ruler_range._range_end) == (0, 1199)
    assert view.get_timeline() is sequence
    assert workspace.settings_panel.current_range_type() == "entire"

    exporter = SimpleNamespace(_sequence=sequence)
    config = TimelineExportConfig(range_type=ExportRangeType.ENTIRE)
    assert TimelineExporter._calculate_frame_range(exporter, config) == (0, 1200)
    assert estimate_export_duration(config, sequence) == 50.0
    assert workspace.settings_panel._output_duration_seconds() == 50.0

    def ruler_point(frame: int) -> QPoint:
        return view.mapFromScene(QPointF(
            TRACK_HEADER_WIDTH + frame * timeline.get_zoom_level(), ruler_range._bar_y,
        ))

    target = ruler_point(600)
    assert view.viewport().rect().contains(ruler_point(1199))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=ruler_point(1199))
    QTest.mouseMove(view.viewport(), target)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    qapp.processEvents()

    assert workspace.settings_panel.current_range_type() == "in_out"
    adjusted = view.get_timeline()
    assert adjusted.range_start == 0
    assert adjusted.range_end == pytest.approx(600, abs=2)
    exporter._sequence = adjusted
    config = TimelineExportConfig(range_type=ExportRangeType.IN_OUT)
    assert TimelineExporter._calculate_frame_range(exporter, config) == (0, adjusted.range_end + 1)
    assert workspace.settings_panel._output_duration_seconds() == (adjusted.range_end + 1) / 24

    workspace.detach_edit_panels()
    assert timeline.get_zoom_level() == 4.0
    assert not timeline.btn_zoom_fit.isChecked()
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
    # The compact preset selector uses the same shared control as parameters.
    assert len(settings.findChildren(ChevronComboBox)) == 21

    tabs = settings.findChild(QTabBar, "DeliverModeTabs")
    assert tabs is not None
    assert tabs.styleSheet() == orange_tab_style("QTabBar#DeliverModeTabs")
    assert tabs.count() == 3
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Video", "Audio", "File"]
    assert settings._mode_stack.currentIndex() == 0
    tabs.setCurrentIndex(1)
    assert settings._mode_stack.currentIndex() == 1


def test_compact_preset_selector_replaces_multi_selection_with_one_draft(qapp):
    workspace = DeliverWorkspace()
    settings = workspace.settings_panel
    settings._preset_cards["web_h264"].set_checked(True)
    settings._preset_cards["mobile_h264"].set_checked(True)
    assert settings._preset_combo.currentData() == "__multiple__"
    settings._on_preset_activated(settings._preset_combo.currentIndex())
    assert settings._selected_preset_ids == ["web_h264", "mobile_h264"]

    settings._on_preset_activated(settings._preset_combo.findData("master_prores422"))
    assert settings._selected_preset_ids == ["master_prores422"]
    assert settings._codec_combo.currentData() == "prores_422"
    assert settings._preset_combo.currentData() == "master_prores422"
    assert not settings._preset_cards["web_h264"]._checked

    settings._on_preset_activated(0)
    assert settings._selected_preset_ids == []
    assert settings._preset_combo.currentData() is None
    # Custom Export keeps the draft the user was just editing.
    assert settings._codec_combo.currentData() == "prores_422"


def test_deliver_disclosures_keep_parameters_and_footer_reachable(qapp):
    from PyQt6.QtWidgets import QScrollArea

    workspace = DeliverWorkspace()
    workspace.resize(1240, 620)
    workspace.show()
    qapp.processEvents()
    settings = workspace.settings_panel
    assert settings.findChild(QWidget, "DeliverAdvancedBody").isHidden()
    assert settings.findChild(QWidget, "DeliverSubtitleBody").isHidden()
    buttons = settings.findChildren(QToolButton, "DeliverDisclosure")
    assert len(buttons) == 2
    buttons[1].click()
    settings._subtitle_combo.setCurrentIndex(1)
    settings._mode_tabs.setCurrentIndex(2)
    settings._mode_tabs.setCurrentIndex(0)
    assert not settings._subtitle_combo.parent().isHidden()
    assert settings._subtitle_combo.currentData() == "burn"
    assert isinstance(settings._mode_stack.currentWidget(), QScrollArea)
    footer = settings.findChild(QWidget, "DeliverSettingsFooter")
    assert footer.isAncestorOf(settings._add_btn)
    assert settings._add_btn.isVisible()
    assert settings._add_and_start_btn.isHidden()
    assert settings._unique_position_label.isHidden()
    assert settings._stems_rows_label.isHidden()
    settings._individual_clips_radio.setChecked(True)
    assert not settings._unique_position_label.isHidden()
    workspace.close()


def test_reload_custom_preset_keeps_edited_draft_and_selection(qapp, monkeypatch):
    from dataclasses import replace
    from src.features.deliver.core import custom_export_presets
    from src.features.deliver.core.video_export_presets import DEFAULT_VIDEO_PRESETS

    preset = replace(DEFAULT_VIDEO_PRESETS[0], preset_id="user_review")
    monkeypatch.setattr(custom_export_presets, "list_user_presets", lambda: [preset])
    workspace = DeliverWorkspace()
    settings = workspace.settings_panel
    settings._preset_cards[preset.preset_id].set_checked(True)
    settings._quality_value_spin.setValue(8765)
    settings._reload_custom_presets()
    assert settings._quality_value_spin.value() == 8765
    assert settings._preset_cards[preset.preset_id]._checked
    assert settings._preset_combo.currentData() == preset.preset_id


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
