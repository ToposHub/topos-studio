"""Monitor workspace behavior using real bars and lightweight canvas doubles."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from src.features.edit.ui.edit_viewport import EditViewport
from src.features.edit.ui.program_viewer import ProgramViewerBar, TransportControlBar
from src.features.edit.ui.source_viewer import SourceSlider, SourceViewerBar
from src.shared.i18n.i18n import I18N


class _Canvas(QWidget):
    zoom_changed = pyqtSignal(float)
    is_fit_to_window = True

    def fit_to_window(self) -> None:
        self.is_fit_to_window = True
        self.zoom_changed.emit(1.0)

    def actual_size(self) -> None:
        self.set_zoom(2.0)

    def set_zoom(self, zoom: float) -> None:
        self.is_fit_to_window = False
        self.zoom_changed.emit(zoom)


class _Program(QWidget):
    transport_play = pyqtSignal()
    timeline_switch_requested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._toolbar = ProgramViewerBar(None, self)
        self._viewer = _Canvas(self)
        self._transport = TransportControlBar(self)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._viewer, 1)
        layout.addWidget(self._transport)
        self._transport.play_clicked.connect(self.transport_play.emit)
        self._toolbar.timeline_switch_requested.connect(self.timeline_switch_requested.emit)

    def get_viewer(self) -> _Canvas:
        return self._viewer

    def get_toolbar(self) -> ProgramViewerBar:
        return self._toolbar

    def get_transport_bar(self) -> TransportControlBar:
        return self._transport


class _Source(QWidget):
    range_changed = pyqtSignal(object, object)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.viewer = _Canvas(self)
        self.slider = SourceSlider(self)
        self.toolbar = SourceViewerBar(self)
        layout.addWidget(self.viewer, 1)
        layout.addWidget(self.slider)
        layout.addWidget(self.toolbar)
        self.current_asset = None
        self.loop_enabled = False
        self.stop_calls = 0

    def set_loop(self, enabled: bool) -> None:
        self.loop_enabled = enabled

    def stop_in_place(self) -> None:
        self.stop_calls += 1


@pytest.fixture
def viewport(qapp, widget_shelter):
    widget = EditViewport(_Program(), _Source(), widget_shelter)
    widget.setWindowFlag(Qt.WindowType.Window, True)
    widget.resize(1000, 500)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.hide()


def test_single_dual_reuses_canvases_and_presents_source_left(viewport, qapp) -> None:
    source = viewport.source_viewer.viewer
    program = viewport.program_viewer.get_viewer()
    source_parent = source.parentWidget()
    program_parent = program.parentWidget()
    assert not viewport.dual_view
    assert viewport.source_pane.isHidden()
    assert not viewport.program_pane.isHidden()
    viewport.program_header.dual_button.click()
    qapp.processEvents()
    assert viewport.dual_view
    assert not viewport.source_pane.isHidden()
    assert viewport.splitter.indexOf(viewport.source_pane) == 0
    assert viewport.splitter.indexOf(viewport.program_pane) == 1
    viewport.show_source()
    viewport.program_header.single_button.click()
    assert viewport.program_pane.isHidden()
    assert not viewport.source_pane.isHidden()
    assert source is viewport.source_viewer.viewer
    assert program is viewport.program_viewer.get_viewer()
    assert source.parentWidget() is source_parent
    assert program.parentWidget() is program_parent


def test_switch_preserves_marks_scrub_position_and_zoom(viewport) -> None:
    source = viewport.source_viewer
    source.slider.setValue(37)
    source.toolbar.update_in_out_state(10, 60)
    viewport.set_dual_view(True)
    viewport.show_source()
    viewport.set_dual_view(False)
    viewport.show_program()
    viewport.show_source()
    assert source.slider.value() == 37
    assert (source.toolbar._in_point, source.toolbar._out_point) == (10, 60)
    source.viewer.set_zoom(1.5)
    assert viewport.source_header.zoom_button.text() == "150%"


def test_focus_and_canvas_click_select_monitor(viewport, qapp) -> None:
    viewport.set_dual_view(True)
    viewport.show_source()
    qapp.processEvents()
    assert QApplication.focusWidget() is viewport.source_viewer
    assert viewport.active_monitor == "source"
    QApplication.sendEvent(viewport.program_viewer.get_viewer(), QEvent(QEvent.Type.FocusIn))
    assert viewport.active_monitor == "program"
    assert viewport.program_header.timeline_button.isChecked()
    assert "#e26458" in viewport.program_header.title.styleSheet()
    viewport.show_program()
    qapp.processEvents()
    assert QApplication.focusWidget() is viewport.program_viewer.get_viewer()


def test_header_uses_same_live_labels_and_timeline_selector(viewport, qapp) -> None:
    source_bar = viewport.source_viewer.toolbar
    source_bar.set_source_name("Camera A.mov")
    source_bar.set_timecode("01:02:03:04", 25.0)
    source_bar.set_duration("00:00:10:00")
    assert viewport.source_header.title is source_bar.source_label
    assert viewport.source_header.title.text() == "Camera A.mov"
    assert viewport.source_header.timecode.text() == "01:02:03:04"
    assert viewport.source_header.duration.text() == "00:00:10:00"
    assert viewport.source_header.fps_indicator is source_bar.fps_indicator
    assert viewport.source_header.isAncestorOf(source_bar.fps_indicator)
    transport = viewport.program_viewer.get_transport_bar()
    transport.update_timecode("10:20:30:05")
    assert viewport.program_header.timecode is transport.timecode_label
    assert viewport.program_header.timecode.text() == "10:20:30:05"
    toolbar = viewport.program_viewer.get_toolbar()
    assert viewport.program_header.fps_indicator is toolbar.fps_indicator
    assert viewport.program_header.isAncestorOf(toolbar.fps_indicator)
    assert toolbar.fps_indicator.isVisible()
    toolbar.set_timeline_choices({"one": SimpleNamespace(name="Timeline 1"), "two": SimpleNamespace(name="Timeline 2")}, "two")
    qapp.processEvents()
    assert viewport.program_header.title is toolbar._timeline_selector
    assert viewport.program_header.title.currentData() == "two"
    selected: list[str] = []
    viewport.program_viewer.timeline_switch_requested.connect(selected.append)
    viewport.program_header.title.setCurrentIndex(0)
    assert selected == ["one"]


def test_opening_another_source_resets_header_zoom_and_metadata(qapp, widget_shelter, monkeypatch) -> None:
    """The new chrome follows real source setters without decoding media."""
    from src.features.edit.ui.source_viewer import SourceViewerWidget

    source = SourceViewerWidget(widget_shelter)
    monkeypatch.setattr(source, "_load_frame", lambda _frame: None)
    monkeypatch.setattr(source, "_sync_scrub_audio_for_asset", lambda: None)
    monkeypatch.setattr(source, "close_source", lambda: None)
    widget = EditViewport(_Program(), source, widget_shelter)
    asset = SimpleNamespace(asset_id="one", display_name="Camera A.mov",
                            total_frames=250, fps=25.0, width=1920, height=1080)
    source.open_asset(asset)
    source.viewer._view_transform.zoom = 1.5
    source.viewer._view_transform.mode = "manual"
    source.viewer.zoom_changed.emit(1.5)
    assert widget.source_header.zoom_button.text() == "150%"
    source.open_asset(SimpleNamespace(asset_id="two", display_name="Camera B.mov",
                                     total_frames=500, fps=25.0, width=1920, height=1080))
    assert widget.source_header.zoom_button.text() == I18N.tr("viewport.zoom_auto", "Auto")
    assert widget.source_header.title.text() == "Camera B.mov"
    assert widget.source_header.timecode.text() == "00:00:00:00"
    assert source.slider.maximum() == 499
    widget.hide()


def test_existing_transport_and_guide_buttons_keep_signal_routes(viewport) -> None:
    played: list[bool] = []
    marked: list[bool] = []
    cleared: list[bool] = []
    transformed: list[bool] = []
    viewport.program_viewer.transport_play.connect(lambda: played.append(True))
    viewport.source_viewer.toolbar.mark_in_requested.connect(lambda: marked.append(True))
    viewport.source_viewer.toolbar.clear_in_out_requested.connect(lambda: cleared.append(True))
    viewport.program_viewer.get_toolbar().transform_tool_requested.connect(transformed.append)
    viewport.program_viewer.get_transport_bar().btn_play.click()
    viewport.source_viewer.toolbar.btn_mark_in.click()
    viewport.source_viewer.toolbar.update_in_out_state(10, 35)
    viewport.source_viewer.toolbar.btn_clear.click()
    viewport.program_viewer.get_toolbar().transform_btn.click()
    assert played == [True]
    assert marked == [True]
    assert cleared == [True]
    assert viewport.source_footer.right_group.isAncestorOf(viewport.source_viewer.toolbar.btn_clear)
    assert transformed == [True]
    assert viewport.program_viewer.isAncestorOf(viewport.program_viewer.get_transport_bar().btn_play)
    assert viewport.source_viewer.isAncestorOf(viewport.source_viewer.toolbar.btn_mark_in)


def test_stop_uses_explicit_in_place_route_without_toggling_play(viewport) -> None:
    played: list[bool] = []
    stopped: list[bool] = []
    viewport.program_viewer.transport_play.connect(lambda: played.append(True))
    viewport.source_viewer.toolbar.play_pause_requested.connect(lambda: played.append(True))
    viewport.program_stop_requested.connect(lambda: stopped.append(True))
    for _ in range(2):
        viewport.program_stop_button.click()
        viewport.source_stop_button.click()
    assert stopped == [True, True]
    assert viewport.source_viewer.stop_calls == 2
    assert played == []


def test_mixer_transport_exposes_separate_record_and_stop_actions(viewport: EditViewport, qapp: QApplication) -> None:
    requests: list[str] = []
    viewport.program_record_requested.connect(lambda: requests.append("record"))
    viewport.program_stop_requested.connect(lambda: requests.append("stop"))
    stop, record = viewport.program_stop_button, viewport.program_record_button
    assert stop.isHidden() and record.isHidden()

    viewport.program_footer.setFixedWidth(300)
    viewport.set_mixer_recording_controls(True)
    qapp.processEvents()
    assert stop.isVisible() and record.isVisible()
    assert not record.isEnabled()
    record.click()
    assert requests == []
    center = viewport.program_footer.center_group
    assert center.geometry().contains(record.geometry().translated(center.pos()))

    viewport.set_mixer_recording_controls(True, can_record=True)
    record.click()
    assert requests == ["record"]
    viewport.set_mixer_recording_controls(True, recording=True)
    record.click()
    stop.click()
    assert requests == ["record", "stop"]

    viewport.set_mixer_recording_controls(False)
    assert stop.isHidden() and record.isHidden()
    assert viewport.program_footer.groups[1].count() == 6


def test_source_loop_menu_tracks_policy_and_requires_open_asset(viewport) -> None:
    menu = viewport.source_header.options_button.menu()
    menu.aboutToShow.emit()
    assert not viewport.source_loop_action.isEnabled()
    viewport.source_viewer.current_asset = object()
    menu.aboutToShow.emit()
    assert viewport.source_loop_action.isEnabled()
    viewport.source_loop_action.trigger()
    assert viewport.source_viewer.loop_enabled
    viewport.source_viewer.set_loop(False)
    menu.aboutToShow.emit()
    assert not viewport.source_loop_action.isChecked()


def test_transport_keeps_original_color_page_glyphs_and_button_sizes(viewport) -> None:
    from PyQt6.QtCore import QSize
    from src.shared.assets.icon_manager import IconColors, icon_play, icon_pause

    program = viewport.program_viewer.get_transport_bar()
    source = viewport.source_viewer.toolbar
    expected_program = [program.btn_jump_start, program.btn_prev_frame, program.btn_play,
                        program.btn_next_frame, program.btn_jump_end, program.btn_loop]
    expected_source = [source.btn_jump_start, source.btn_prev, source.btn_play,
                       source.btn_next, source.btn_jump_end]
    assert [viewport.program_footer.groups[1].itemAt(i).widget() for i in range(6)] == expected_program
    assert [viewport.source_footer.groups[1].itemAt(i).widget() for i in range(5)] == expected_source
    assert viewport.program_footer.groups[1].spacing() == 3
    assert viewport.source_footer.groups[1].spacing() == 3
    for bar, play in ((program, program.btn_play), (source, source.btn_play)):
        assert play.size() == QSize(32, 28)
        assert play.iconSize() == QSize(18, 18)
        assert play.icon().pixmap(QSize(18, 18)).toImage() == icon_play(IconColors.ORANGE).pixmap(QSize(18, 18)).toImage()
        update = bar.update_play_button if bar is program else bar.update_play_state
        update(True)
        assert play.icon().pixmap(QSize(18, 18)).toImage() == icon_pause(IconColors.ORANGE).pixmap(QSize(18, 18)).toImage()
        update(False)
    for button in (program.btn_jump_start, program.btn_jump_end, source.btn_jump_start, source.btn_jump_end):
        assert button.size() == QSize(28, 28)
        assert button.iconSize() == QSize(15, 15)


def test_program_scrub_distinguishes_updates_from_user_seeks(viewport) -> None:
    seeks: list[int] = []
    viewport.program_seek_requested.connect(seeks.append)
    viewport.set_program_range(250, 25.0)
    viewport.set_program_position(50)
    assert seeks == []
    assert viewport.program_slider.maximum() == 249
    assert viewport.program_header.duration.text() == "00:00:10:00"
    viewport.program_slider.setValue(75)
    assert seeks == [75]
    viewport.set_program_range(0)
    assert not viewport.program_slider.isEnabled()


def test_color_monitor_keeps_program_controls_and_restores_edit_panes(viewport) -> None:
    viewport.show_source(focus=False)
    viewport.set_dual_view(True)
    program = viewport.program_viewer.get_viewer()
    changes = []
    viewport.mode_changed.connect(changes.append)
    viewport.active_monitor_changed.connect(changes.append)
    viewport.set_program_only(True, color_monitor=True)
    assert viewport.source_pane.isHidden()
    assert not viewport.program_pane.isHidden()
    assert not viewport.program_slider.isHidden()
    assert not viewport.program_footer.isHidden()
    assert not viewport.timeline_monitor_status.isHidden()
    assert viewport.program_viewer.get_viewer() is program
    viewport.set_program_only(False)
    assert viewport.timeline_monitor_status.isHidden()
    assert viewport.dual_view and viewport.active_monitor == "source"
    assert not viewport.source_pane.isHidden()
    assert not changes


def test_program_scrubber_starts_at_pointer_and_distinguishes_lost_grab(viewport, qtbot) -> None:
    viewport.set_program_only(True, color_monitor=True)
    slider = viewport.program_slider
    viewport.set_program_range(200)
    slider.setValue(5)
    starts, releases, cancels = [], [], []
    slider.scrub_started_at.connect(starts.append)
    slider.scrub_finished_at.connect(releases.append)
    slider.scrub_cancelled.connect(lambda: cancels.append(True))
    point = QPoint(slider.width() * 2 // 3, slider.height() // 2)
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=point)
    assert starts == [slider.value()] and starts[0] > 100
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point)
    assert releases == starts and not cancels
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=point)
    QApplication.sendEvent(slider, QEvent(QEvent.Type.UngrabMouse))
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point)
    assert cancels == [True] and len(releases) == 1


def test_program_duration_uses_existing_drop_frame_timecode(viewport) -> None:
    from src.shared.media.metadata import Rational

    viewport.set_program_range(107892, Rational(30000, 1001))
    assert viewport.program_header.duration.text() == "01:00:00;00"


def test_dual_split_ratio_survives_switching_to_single(viewport, qapp) -> None:
    viewport.set_dual_view(True)
    qapp.processEvents()
    viewport.splitter.setSizes([400, 600])
    before = viewport.splitter.sizes()
    viewport.set_dual_view(False)
    viewport.set_dual_view(True)
    qapp.processEvents()
    assert viewport.splitter.sizes() == before


def test_saved_split_ratio_restores_when_starting_in_single_view(viewport, qapp) -> None:
    saved = [400, 600]
    viewport.restore_splitter_sizes(saved)
    saved[0] = 10
    assert viewport.source_pane.isHidden()
    viewport.set_dual_view(True)
    qapp.processEvents()
    first, second = viewport.splitter.sizes()
    assert first / (first + second) == pytest.approx(0.4, abs=0.002)


@pytest.mark.parametrize("sizes", [[], [0, 1], [1, -1], [True, 2], [1.2, 2], [1, 2, 3]])
def test_split_restore_rejects_invalid_sizes(viewport, sizes) -> None:
    with pytest.raises(ValueError, match="two positive integers"):
        viewport.restore_splitter_sizes(sizes)


@pytest.mark.parametrize("dual", [False, True])
def test_timeline_activation_hides_source_range_without_losing_marks(viewport, widget_shelter, dual) -> None:
    from PyQt6.QtWidgets import QGraphicsView

    timeline = QGraphicsView(widget_shelter)
    viewport.bind_timeline_widget(timeline)
    viewport.set_dual_view(dual)
    source = viewport.source_viewer
    source_slider = source.slider
    viewport.show_source(focus=False)
    assert source_slider.isHidden()
    source.current_asset = SimpleNamespace(asset_id="clip")
    source_slider.set_in_out_points(12, 77)
    source.range_changed.emit(12, 77)
    viewport.set_program_range(100)
    viewport.set_program_position(50)
    assert viewport.active_monitor == "source"
    assert not source_slider.isHidden()
    assert viewport.program_slider.isHidden() is not dual
    QApplication.sendEvent(timeline.viewport(), QEvent(QEvent.Type.FocusIn))
    assert viewport.active_monitor == "program"
    assert source_slider.isHidden() is not dual
    assert viewport.program_slider.isHidden() is not dual
    assert viewport.source_pane.isHidden() is not dual
    viewport.show_source(focus=False)
    assert not source_slider.isHidden()
    assert (source_slider._in_point, source_slider._out_point) == (12, 77)
    source.current_asset = None
    source.range_changed.emit(None, None)
    assert source_slider.isHidden()


def test_dual_program_range_mirrors_timeline_marks_clear_and_undo(viewport, qtbot) -> None:
    from src.app.controllers.dock_layout_controller import DockLayoutMixin
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    host = SimpleNamespace(edit_viewport=viewport, timeline_panel=SimpleNamespace(get_timeline_view=lambda: view))
    sequence = TimelineSequence(video_tracks=(EditTrack(
        "v", "V1", "video", 0, clips=(ClipInstance("clip", "asset", "v", 0, 120, 0, 120),)
    ),), range_start=48)
    view.load_timeline(sequence)
    DockLayoutMixin._sync_edit_viewport_timeline(host, sequence)
    view.timeline_changed.connect(lambda timeline: DockLayoutMixin._sync_edit_viewport_timeline(host, timeline))
    view.range_display_changed.connect(viewport.set_program_range)
    seeks: list[int] = []
    viewport.program_seek_requested.connect(seeks.append)
    viewport.set_program_position(60)
    viewport.set_dual_view(True)
    slider = viewport.program_slider
    assert slider.isVisible()
    assert slider.maximum() == 239
    assert (slider._in_point, slider._out_point) == (48, 239)

    view._mark_out_at_frame(144)
    assert (slider._in_point, slider._out_point) == (48, 144)
    view._clear_timeline_range()
    assert (slider._in_point, slider._out_point) == (None, None)
    view.undo()
    assert (slider._in_point, slider._out_point) == (48, 144)
    assert slider.value() == 60 and not seeks

    view._on_range_handle_dragged("out", 259)
    assert (slider.maximum(), slider._out_point) == (259, 259)
    view._on_range_drag_cancelled()
    assert (slider.maximum(), slider._out_point) == (239, 144)
    view._extend_runtime_duration(250)
    assert slider.maximum() == 479
    view.clear()
    assert (slider._in_point, slider._out_point) == (None, None)
    viewport.set_dual_view(False)
    assert slider.isHidden()


def test_fit_visible_monitors_resets_only_the_shown_canvases(viewport: EditViewport) -> None:
    program = viewport.program_viewer.get_viewer()
    source = viewport.source_viewer.viewer
    program.set_zoom(2.0)
    source.set_zoom(1.5)

    viewport.fit_visible_monitors()
    assert program.is_fit_to_window
    assert not source.is_fit_to_window
    assert viewport.program_header.zoom_button.text() == I18N.tr("viewport.zoom_auto", "Auto")

    viewport.set_dual_view(True)
    program.set_zoom(0.5)
    viewport.fit_visible_monitors()
    assert program.is_fit_to_window and source.is_fit_to_window
    assert viewport.source_header.zoom_button.text() == I18N.tr("viewport.zoom_auto", "Auto")


def test_program_zoom_presets_keep_viewer_api(viewport) -> None:
    toolbar = viewport.program_viewer.get_toolbar()
    half = next(action for action in toolbar.view_menu.actions() if action.text() == "50%")
    half.trigger()
    assert viewport.program_header.zoom_button.text() == "50%"


def test_deliver_borrow_returns_complete_program_monitor(viewport, qapp, widget_shelter) -> None:
    parent = QWidget(widget_shelter)
    borrowed = QVBoxLayout(parent)
    viewport.set_dual_view(True)
    viewport.prepare_for_deliver()
    borrowed.addWidget(viewport.program_viewer)
    assert not viewport.program_slider.isHidden()
    assert viewport.program_viewer.isAncestorOf(viewport.program_header)
    assert viewport.program_viewer.isAncestorOf(viewport.program_viewer.get_transport_bar().btn_play)
    assert viewport.program_header.dual_button.isHidden()
    viewport.restore_program_viewer()
    qapp.processEvents()
    assert viewport.program_viewer.parentWidget() is viewport.program_pane
    assert viewport.dual_view
    assert not viewport.program_header.single_button.isHidden()
    assert viewport.program_header.dual_button.isHidden()


@pytest.mark.parametrize("width,dual", [(320, False), (800, False), (800, True), (1280, True)])
def test_mode_buttons_stay_inside_header_at_narrow_width(viewport, qapp, width, dual) -> None:
    viewport.resize(width, 480)
    viewport.set_dual_view(dual)
    qapp.processEvents()
    header = viewport.program_header
    mode_button = header.single_button if dual else header.dual_button
    assert viewport.width() == width
    assert not mode_button.isHidden()
    timecode_x = header.timecode.mapTo(header, QPoint(0, 0)).x()
    fps_x = header.fps_indicator.mapTo(header, QPoint(0, 0)).x()
    mode_x = mode_button.mapTo(header, QPoint(0, 0)).x()
    options_x = header.options_button.mapTo(header, QPoint(0, 0)).x()
    assert mode_x >= 0
    assert options_x + header.options_button.width() <= header.width()
    assert timecode_x + header.timecode.width() <= fps_x
    assert fps_x + header.fps_indicator.width() <= mode_x
    if not header.title.isHidden():
        assert header.title.geometry().right() < timecode_x


def test_reference_title_and_transport_are_centered_on_whole_monitor(viewport, qapp) -> None:
    viewport.resize(1364, 840)
    qapp.processEvents()
    header = viewport.program_header
    footer = viewport.program_footer
    assert header.height() == 30
    assert footer.height() == 34
    assert abs(header.title.geometry().center().x() - header.rect().center().x()) <= 1
    assert abs(footer.center_group.geometry().center().x() - footer.rect().center().x()) <= 1
    assert footer.groups[1].count() == 6
    assert header.single_button.isHidden()
    assert not header.dual_button.isHidden()


def test_optional_header_controls_leave_room_for_the_timeline_title(viewport, qapp) -> None:
    toolbar = viewport.program_viewer.get_toolbar()
    header = viewport.program_header
    name = "Timeline 1"
    toolbar.set_timeline_choices({"one": SimpleNamespace(name=name)}, "one")

    for width in (559, 560, 759, 760, 823, 824, 1000):
        viewport.resize(width, 480)
        qapp.processEvents()
        title = header.title
        assert title.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, title.width() - 22) == name
        assert abs(title.geometry().center().x() - header.rect().center().x()) <= 1
        assert title.geometry().left() > header.left_group.geometry().right()
        assert title.geometry().right() < header.right_group.x()

    assert all(not control.isHidden() for control in header.extra_controls)


def test_transform_drag_preserves_timeline_header(qapp, qtbot, widget_shelter, monkeypatch) -> None:
    from src.features.edit.core.clip_params import TransformParams
    from src.features.edit.ui.program_viewer import ProgramViewerWidget

    program = ProgramViewerWidget()
    viewport = EditViewport(program, _Source(), widget_shelter)
    qtbot.addWidget(viewport)
    viewport.setWindowFlag(Qt.WindowType.Window, True)
    viewport.resize(760, 480)
    viewport.show()
    program.set_timeline_choices({"one": SimpleNamespace(name="Timeline 1")}, "one")
    qapp.processEvents()

    canvas = program.get_viewer()
    program.get_toolbar().transform_btn.click()
    canvas.set_selected_clip_transform("clip", TransformParams(), 1920, 1080)
    qapp.processEvents()
    header = viewport.program_header
    title_geometry = header.title.geometry()
    layout_passes: list[bool] = []
    position_title = header.position_title

    def record_layout_pass() -> None:
        layout_passes.append(True)
        position_title()

    monkeypatch.setattr(header, "position_title", record_layout_pass)
    transforms: list[TransformParams] = []
    program.clip_transform_interaction_updated.connect(lambda _clip_id, transform: transforms.append(transform))
    start = canvas.rect().center() + QPoint(45, 30)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    for offset in (15, 35, 55, 5):
        QTest.mouseMove(canvas, start + QPoint(offset, 5))
        QTest.qWait(40)
        assert header.title.geometry() == title_geometry
        assert header.title.currentText() == "Timeline 1"
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=start + QPoint(5, 5))
    assert any(transform.translate_x > 0 for transform in transforms)
    assert transforms[-1] == TransformParams()
    assert not layout_passes


@pytest.mark.parametrize("width", [280, 300, 320, 460, 700])
@pytest.mark.parametrize("pane", ["program", "source"])
def test_transport_groups_do_not_overlap_at_small_pane_widths(viewport, qapp, width, pane) -> None:
    footer = getattr(viewport, pane + "_footer")
    footer.setFixedWidth(width)
    footer.position_groups()
    qapp.processEvents()
    assert footer.left_group.geometry().right() < footer.center_group.geometry().left()
    assert footer.center_group.geometry().right() < footer.right_group.geometry().left()
    if pane == "source":
        assert not footer.right_group.isHidden()


# ── I03：capability 动作回填与分发 ─────────────────────────────────


def test_i03_capability_actions_refresh_and_dispatch(viewport, qapp) -> None:
    from src.features.edit.core.viewer_capabilities import ViewerCapability

    source_mf = viewport._capability_actions["source.match_frame"][0]
    timeline_mf = viewport._capability_actions["timeline.match_frame"][0]
    reverse = viewport._capability_actions["source.reverse"][0]
    assert not source_mf.isEnabled()      # 未回填前默认禁用（不虚亮）
    assert not timeline_mf.isEnabled()
    assert not reverse.isEnabled()

    viewport.refresh_capabilities({
        "match_frame.source_to_timeline": ViewerCapability(
            action="match_frame.source_to_timeline", supported=True),
        "match_frame.timeline_to_source": ViewerCapability(
            action="match_frame.timeline_to_source", supported=False,
            reason="No timeline is open."),
        "source.reverse_play": ViewerCapability(
            action="source.reverse_play", supported=False,
            reason="No source media is open in the viewer."),
    })

    assert source_mf.isEnabled()
    assert not timeline_mf.isEnabled()
    assert "No timeline is open." in timeline_mf.toolTip()
    assert not reverse.isEnabled()
    assert "No source media" in reverse.toolTip()

    # reverse 本地消化（真实 widget 入口在 source_viewer 上）
    reverse_calls: list[int] = []
    viewport.source_viewer.play_reverse = lambda: reverse_calls.append(1)  # type: ignore[method-assign]
    viewport.refresh_capabilities({
        "source.reverse_play": ViewerCapability(
            action="source.reverse_play", supported=True)})
    assert reverse.isEnabled()
    reverse.trigger()
    assert reverse_calls == [1]

    # 其余键经宿主信号执行
    requested: list[str] = []
    viewport.capability_action_requested.connect(requested.append)
    source_mf.trigger()
    assert requested == ["source.match_frame"]
