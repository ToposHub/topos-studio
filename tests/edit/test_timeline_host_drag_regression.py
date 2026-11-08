"""Timeline drag through the real host selection/inspector callback.

Unlike isolated view checks, this fixture rebuilds real hidden Properties and
Info docks on selection. It does not start renderers or alter user projects.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.project import EditProject
from src.features.edit.core.selection import TimelineSelection
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.edit_info_panel import EditInfoPanel
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_ui


@pytest.fixture
def host(qtbot: Any, qapp: Any) -> SimpleNamespace:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(1480, 760)
    panel = TimelinePanel()
    window.setCentralWidget(panel)
    properties = EditPropertiesPanel()
    prop_dock = QDockWidget("Properties", window)
    prop_dock.setWidget(properties)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, prop_dock)
    prop_dock.hide()
    info = EditInfoPanel()
    info_dock = QDockWidget("Info", window)
    info_dock.setWidget(info)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, info_dock)
    info_dock.hide()
    videos, audios = [], []
    for index, (start, duration) in enumerate(((0, 550), (550, 350), (900, 200))):
        clip = ClipInstance(
            f"v{index}", "asset", "v1", start, duration, start, duration,
            linked_clip_id=f"a{index}",
        )
        videos.append(clip)
        audios.append(replace(clip, clip_id=f"a{index}", track_id="a1", linked_clip_id=f"v{index}"))
    sequence = TimelineSequence(
        "host-drag", "Host drag", fps=Rational(25, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=tuple(videos)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=tuple(audios)),),
    )
    panel.load_timeline(sequence)
    view = panel.timeline_view
    view.set_zoom(1.0)
    view.set_snap_enabled(False)
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(_current_timeline=sequence, _fps=25.0, _program_viewer=None)
    controller.edit_properties_panel = properties
    controller.edit_info_panel = info
    controller._current_edit_project = EditProject.create()
    targets: list[str | None] = []
    controller.edit_effects_panel = SimpleNamespace(set_selected_clip=targets.append)
    view.selection_changed.connect(controller._on_timeline_selection_changed)
    # Loaded projects can already own a selection. Build those real controls
    # before initial activation, so platform startup deactivation is not
    # confused with a user leaving the application in the middle of a drag.
    view.set_selection(TimelineSelection().with_clip_selected("v0", "v1", sequence.timeline_id))
    window.show()
    window.activateWindow()
    QTest.qWait(50)
    view.setFocus(Qt.FocusReason.MouseFocusReason)
    qapp.processEvents()
    return SimpleNamespace(window=window, panel=panel, view=view, properties=properties,
                           controller=controller, targets=targets, original=sequence)


def _body(host: SimpleNamespace, clip_id: str = "v1") -> QPoint:
    item = host.view._items_map[clip_id]
    return host.view.mapFromScene(item.mapToScene(QPointF(item._width / 2.0, 35.0)))


def test_hidden_real_inspectors_do_not_destroy_drag_grab_or_linked_move(
    host: SimpleNamespace, qapp: Any,
) -> None:
    view = host.view
    origin = _body(host)
    destination = origin + QPoint(60, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    qapp.processEvents()
    item = view._items_map["v1"]
    assert view.scene().mouseGrabberItem() is item
    assert item._drag_pending, f"Unexpected cancellation; app state={qapp.applicationState().name}"
    assert host.properties._current_clips[0].clip_id == "v1"
    assert host.targets[-1] == "v1"
    QTest.mouseMove(view.viewport(), destination, 10)
    qapp.processEvents()
    assert view._items_map["v1"] is item
    assert item._start_frame == 610
    assert view._items_map["a1"]._start_frame == 610
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=destination)
    assert view.get_timeline().get_clip_by_id("v1").timeline_start == 610
    assert view.get_timeline().get_clip_by_id("a1").timeline_start == 610
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    restored = view.get_timeline().to_dict()
    original = host.original.to_dict()
    # Undo restores content while the public commit revision stays monotonic.
    restored.pop("revision")
    original.pop("revision")
    assert restored == original


def test_actual_window_focus_change_still_cancels_host_drag(
    host: SimpleNamespace, qtbot: Any, qapp: Any,
) -> None:
    view = host.view
    origin = _body(host)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    qapp.processEvents()
    QTest.mouseMove(view.viewport(), origin + QPoint(60, 0), 10)
    assert view._items_map["v1"]._is_dragging
    other = QWidget()
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    other.setFocus()
    qapp.processEvents()
    assert view._interaction.is_idle
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=origin + QPoint(60, 0))
    assert view.get_timeline().to_dict() == host.original.to_dict()
    assert not view._command_stack._undo_stack


def test_numeric_factory_does_not_create_a_top_level_slider(host: SimpleNamespace) -> None:
    slider = host.properties._make_numeric_slider(False, 0.0, 1.0, 0.01, 0.5)
    assert slider.parentWidget() is host.properties
    assert not slider.isWindow()


@pytest.mark.parametrize("factory", ["selection", "effect"])
def test_selection_rebuild_never_shows_a_top_level_numeric_slider(
    host: SimpleNamespace, qapp: Any, factory: str,
) -> None:
    from PyQt6.QtCore import QEvent, QObject
    from src.shared.ui.widgets.topos_controls import ToposClickableSlider

    top_level_shows: list[str] = []

    class ShowObserver(QObject):
        def eventFilter(self, obj: QObject, event: QEvent) -> bool:
            if (event.type() == QEvent.Type.Show
                    and isinstance(obj, ToposClickableSlider) and obj.isWindow()):
                top_level_shows.append(obj.objectName() or type(obj).__name__)
            return False

    observer = ShowObserver()
    qapp.installEventFilter(observer)
    try:
        # This is the exact synchronous host callback reached during press;
        # every slider must have a parent before any request to show it.
        if factory == "selection":
            host.view.set_selection(TimelineSelection().with_clip_selected("v1", "v1", "host-drag"))
        else:
            from src.features.edit.core.effect_instance import ParamSchema
            from src.features.edit.ui.edit_properties_panel import _create_effect_param_widget
            row = _create_effect_param_widget(
                host.properties, "amount", ParamSchema(type="float", min_value=0.0, max_value=1.0),
                0.5, "fx",
            )
            row.setParent(host.properties)
            sliders = row.findChildren(ToposClickableSlider)
            assert sliders and all(slider.parentWidget() is row and not slider.isWindow() for slider in sliders)
        qapp.processEvents()
        assert top_level_shows == []
    finally:
        qapp.removeEventFilter(observer)
