"""Viewer-contained trim presentation, ownership, focus and async lifecycle."""
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QApplication

from src.features.edit.ui.program_viewer import ProgramViewerWidget
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.trim_numeric_dialog import TrimMonitorWidget
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_ui


def _fixture(qtbot: Any, track_view: bool = True) -> tuple[ProgramViewerWidget, TimelineView, QWidget]:
    # Exercise the real presentation host without creating any OpenGL context.
    host = ProgramViewerWidget.__new__(ProgramViewerWidget)
    QWidget.__init__(host)
    host._viewer = QWidget(host)
    QVBoxLayout(host).addWidget(host._viewer)
    host._trim_monitor_widget = None
    host._viewer.installEventFilter(host)
    qtbot.addWidget(host)
    host.resize(800, 400)
    host.show()
    view = TimelineView()
    if track_view:
        qtbot.addWidget(view)
    view.load_timeline(TimelineSequence('embedded', 'Embedded', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(
            ClipInstance('left', 'asset', 'v', 0, 100, 0, 100),
            ClipInstance('right', 'asset', 'v', 100, 100, 100, 100),
        )),)))
    view.set_trim_monitor_host(host)
    QTest.qWait(5)
    return host, view, host._viewer


def test_roll_uses_viewer_child_and_never_creates_another_window(qtbot: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    windows = set(QApplication.topLevelWidgets())
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    assert monitor.parentWidget() is canvas
    assert not monitor.isWindow()
    assert monitor.isVisible()
    assert monitor.geometry() == canvas.rect()
    assert canvas.isVisible()
    assert monitor.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert monitor.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert set(QApplication.topLevelWidgets()) == windows
    view._on_roll_trim_dragged(10)
    view._on_roll_trim_finished('left', 'right', 10)
    assert monitor.isHidden()
    assert canvas.isVisible()
    assert view.get_timeline().get_clip_by_id('left').timeline_duration == 110


def test_overlay_follows_canvas_resize_and_fullscreen_reparent(qtbot: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    fullscreen = QWidget()
    qtbot.addWidget(fullscreen)
    fullscreen.resize(1100, 620)
    QVBoxLayout(fullscreen).addWidget(canvas)
    fullscreen.show()
    QTest.qWait(5)
    assert monitor.parentWidget() is canvas
    assert monitor.geometry() == canvas.rect()
    assert not monitor.isWindow()
    canvas.setParent(host)
    host.layout().addWidget(canvas)
    canvas.show()
    QTest.qWait(5)
    assert monitor.geometry() == canvas.rect()


def test_clear_and_host_replacement_restore_view_and_ignore_old_frames(qtbot: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    generation = view._trim_frame_generation
    view.set_trim_monitor_host(None)
    assert monitor.isHidden()
    assert monitor.parentWidget() is view
    assert host._trim_monitor_widget is None
    image = QImage(50, 40, QImage.Format.Format_RGB888)
    view._on_trim_frame_ready('left', 99, image, generation)
    assert not monitor._images
    view.set_trim_monitor_host(host)
    view._on_roll_trim_started('left', 'right', 100)
    view.clear()
    assert monitor.isHidden()
    assert canvas.isVisible()


def test_host_destruction_does_not_leave_dangling_overlay(qtbot: Any, qapp: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    view._on_roll_trim_started('left', 'right', 100)
    host.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert view._trim_monitor_host is None
    assert view._trim_monitor_dialog is None
    assert view._interaction.is_idle
    replacement = view._trim_monitor()
    assert replacement.parentWidget() is view
    assert not replacement.isWindow()
    assert replacement.isHidden()


def test_resize_rescales_source_images_and_single_trim_clears_right(qtbot: Any) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    monitor = TrimMonitorWidget(parent)
    monitor.resize(600, 300)
    parent.show()
    monitor.show()
    image = QImage(160, 90, QImage.Format.Format_RGB888)
    image.fill(Qt.GlobalColor.green)
    monitor.update_frame('left', image)
    monitor.update_frame('right', image)
    width = monitor._frame_a.pixmap().width()
    monitor.resize(900, 420)
    QTest.qWait(5)
    assert monitor._frame_a.pixmap().width() > width
    monitor.update_trim('00:00:01:00', 2)
    monitor.resize(600, 300)
    assert 'right' not in monitor._images
    assert monitor._frame_b.pixmap().isNull()


def test_page_host_binding_supports_both_registration_orders(qtbot: Any) -> None:
    from src.features.edit.ui.edit_page import EditPage
    host, view, canvas = _fixture(qtbot)
    page = EditPage.__new__(EditPage)
    QWidget.__init__(page)
    qtbot.addWidget(page)
    page._program_viewer = None
    page._timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    page._connect_trim_monitor_to_viewer()
    assert view._trim_monitor_host is None
    page._program_viewer = host
    page._connect_trim_monitor_to_viewer()
    assert view._trim_monitor_host is host
    page._timeline_panel = None
    page._connect_trim_monitor_to_viewer()
    assert view._trim_monitor_host is None
    page._connect_trim_monitor_to_viewer()  # viewer first, timeline later
    page._timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    page._connect_trim_monitor_to_viewer()
    assert view._trim_monitor_host is host


def test_timeline_destroyed_first_releases_overlay_and_host_resize_is_safe(qtbot: Any, qapp: Any) -> None:
    from PyQt6 import sip
    host, view, canvas = _fixture(qtbot, track_view=False)
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    view.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(monitor)
    assert host._trim_monitor_widget is None
    host.resize(700, 350)
    qapp.processEvents()


def test_finish_is_emitted_once_and_switching_timeline_hides_overlay(qtbot: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    finished = []
    view.trim_frame_preview_finished.connect(finished.append)
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    view._hide_trim_monitor()
    view._hide_trim_monitor()
    assert finished == [0]
    view._on_roll_trim_started('left', 'right', 100)
    view.load_timeline(TimelineSequence('other', 'Other', fps=24))
    assert monitor.isHidden()
    assert view._interaction.is_idle


def test_finishing_while_host_hidden_does_not_resurrect_overlay(qtbot: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    host.hide()
    view._hide_trim_monitor()
    host.show()
    QTest.qWait(5)
    assert monitor.isHidden()


def test_destroying_host_removes_overlay_from_reparented_canvas(qtbot: Any, qapp: Any) -> None:
    from PyQt6 import sip
    host, view, canvas = _fixture(qtbot)
    fullscreen = QWidget()
    qtbot.addWidget(fullscreen)
    QVBoxLayout(fullscreen).addWidget(canvas)
    fullscreen.show()
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    host.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(monitor)
    assert view._trim_monitor_host is None
    assert view._trim_monitor_dialog is None


def test_page_can_rebind_after_previous_timeline_is_deleted(qtbot: Any, qapp: Any) -> None:
    from src.features.edit.ui.edit_page import EditPage
    host, view, canvas = _fixture(qtbot, track_view=False)
    page = EditPage.__new__(EditPage)
    QWidget.__init__(page)
    qtbot.addWidget(page)
    page._program_viewer = host
    page._timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    page._connect_trim_monitor_to_viewer()
    view._trim_monitor()
    view.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    page._connect_trim_monitor_to_viewer()  # stale panel returns the deleted view
    assert page._trim_monitor_timeline_view is None
    page._timeline_panel = None
    page._connect_trim_monitor_to_viewer()


def test_retired_overlay_deletion_cannot_clear_replacement_cache(qtbot: Any, qapp: Any) -> None:
    from PyQt6 import sip
    host, view, canvas = _fixture(qtbot)
    fullscreen = QWidget()
    qtbot.addWidget(fullscreen)
    QVBoxLayout(fullscreen).addWidget(canvas)
    fullscreen.show()
    old_monitor = view._trim_monitor()
    host.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    replacement_host, _other_view, _canvas = _fixture(qtbot)
    view.set_trim_monitor_host(replacement_host)
    replacement = view._trim_monitor()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(old_monitor)
    assert view._trim_monitor_dialog is replacement
    assert not sip.isdeleted(replacement)


def test_dynamic_trim_keys_reach_view_before_panel_transport_shortcuts(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel
    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.resize(1000, 500)
    panel.show()
    panel.activateWindow()
    sequence = TimelineSequence('keys', 'Keys', fps=24, video_tracks=(
        EditTrack('v', 'V1', 'video', 0, clips=(
            ClipInstance('left', 'asset', 'v', 0, 100, 0, 100),
            ClipInstance('right', 'asset', 'v', 100, 100, 100, 100),
        )),))
    panel.load_timeline(sequence)
    view = panel.get_timeline_view()
    view.setFocus()
    QTest.qWait(5)
    transport_shortcuts = []
    panel._shortcut_l.activated.connect(lambda: transport_shortcuts.append('L'))
    panel._shortcut_j.activated.connect(lambda: transport_shortcuts.append('J'))
    panel._shortcut_k.activated.connect(lambda: transport_shortcuts.append('K'))
    for item in view._items_map.values():
        item._max_source_frames = 1000
    view._on_roll_trim_started('left', 'right', 100)
    QTest.keyClick(view, Qt.Key.Key_L)
    assert view._roll_preview_state['delta'] == 1
    QTest.keyClick(view, Qt.Key.Key_J)
    assert view._roll_preview_state['delta'] == 0
    QTest.keyClick(view, Qt.Key.Key_K)
    assert view._roll_preview_state['delta'] == 0
    assert transport_shortcuts == []
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view._command_stack._undo_stack


def test_decode_failure_is_visible_and_stale_failure_cannot_replace_frame(qtbot: Any) -> None:
    host, view, canvas = _fixture(qtbot)
    view._on_roll_trim_started('left', 'right', 100)
    monitor = view._trim_monitor_dialog
    generation = view._trim_frame_generation
    image = QImage(80, 60, QImage.Format.Format_RGB888)
    image.fill(Qt.GlobalColor.green)
    monitor.update_frame('left', image)
    view._on_trim_frame_failed('left', 'bad codec', generation - 1)
    assert 'left' in monitor._images
    view._on_trim_frame_failed('left', 'bad codec', generation)
    assert 'left' not in monitor._images
    assert monitor._frame_a.text() != '…'
    assert monitor._frame_a.pixmap().isNull()


def test_window_transport_shortcut_respects_active_roll_even_if_toolbar_has_focus(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel
    panel = TimelinePanel()
    qtbot.addWidget(panel)
    timeline = TimelineSequence('shortcut-roll', 'Shortcut roll', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(
            ClipInstance('left', 'asset', 'v', 0, 100, 0, 100),
            ClipInstance('right', 'asset', 'v', 100, 100, 100, 100),
        )),))
    panel.load_timeline(timeline)
    view = panel.get_timeline_view()
    for item in view._items_map.values():
        item._max_source_frames = 1000
    monkeypatch.setattr(panel, '_transport_context_active', lambda: True)
    view._on_roll_trim_started('left', 'right', 100)
    panel._on_l_pressed()
    assert view._roll_preview_state['delta'] == 1
    panel._on_j_pressed()
    panel._on_k_pressed()
    assert view._roll_preview_state['delta'] == 0
    assert panel._playback_controller.state.value != 'playing'
    view._cancel_current_interaction()
