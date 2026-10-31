"""Window-level contract for the Motion Graphic keyframes timeline.

Covers the integration promises of the timeline upgrade: the bottom area hosts
the keyframes panel below the retained transport, element selection stays
three-way synced (canvas / Layers / keyframes tree), ruler scrubbing drives the
same frame path as the transport slider, and panel edits land on the scene as
single-undo steps that the evaluator honours (scene track overrides presets).
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import (
    SceneAnimation, SceneElement, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow
from src.shared.keyframe import Keyframe, KeyframeTrack


def _native_scene(*elements: SceneElement, duration: int = 48) -> TeachingSceneDocument:
    scene = replace(
        TeachingSceneDocument.create_default("kfwin", duration_frames=duration), elements=())
    for element in elements:
        scene = scene.with_element_added(element)
    return scene


def _element(eid: str = "t1", etype: str = "text", local: tuple[KeyframeTrack, ...] = ()) -> SceneElement:
    return SceneElement(
        element_id=eid, type=etype, z_index=0, visible=True,
        transform=SceneTransform(x=100.0, y=200.0, width=300.0, height=80.0),
        style={}, content="Hello" if etype == "text" else {"kind": "rect"},
        local_animation_tracks=local,
    )


def _html_scene() -> TeachingSceneDocument:
    from src.features.teaching_animation.core.html_layer_commands import SetHtmlLayerCommand
    html = '<section data-topos-page="1">A</section><section data-topos-page="2">B</section>'
    layer = build_html_layer(
        layer_id="kf_html", sanitized_html=html, authored_source=html,
        viewport_width=1920, viewport_height=1080,
        fps_num=24, fps_den=1, duration_frames=48)
    # Production lands html layers through the command, which syncs pages.
    return SetHtmlLayerCommand(layer).apply(_native_scene(_element()))


def _opacity_track() -> KeyframeTrack:
    track = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(24, 1.0))
    return track


def test_bottom_area_hosts_keyframes_panel_with_toggle(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_native_scene(_element()))
    try:
        window.show()
        qapp.processEvents()
        panel = window.keyframes_panel
        assert panel.parentWidget() is window._timeline_panel
        assert panel.isHidden() is False
        assert window._keyframes_panel_action.isChecked() is True
        # The retained transport still lives in the same bottom frame.
        assert window.timeline.parentWidget() is window._timeline_panel
        assert window._timeline_panel.layout().indexOf(window.timeline) == 0

        window._keyframes_panel_action.setChecked(False)
        assert panel.isHidden() is True
        assert window.timeline.isHidden() is False
        assert window.timeline._transport_bar.parentWidget() is window.timeline
        window._keyframes_panel_action.setChecked(True)
        assert panel.isHidden() is False

        window._vertical_splitter.setSizes([560, 240])
        window._keyframes_panel_action.setChecked(False)
        window._keyframes_panel_action.setChecked(True)
        assert window._vertical_splitter.sizes()[1] >= 200
    finally:
        window.close()


def test_selection_is_three_way_synced(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(
        _native_scene(_element("t1"), _element("t2", etype="shape")))
    try:
        panel = window.keyframes_panel
        # canvas → panel (and Layers)
        window._set_selection({"t2"}, source="canvas")
        assert any(r.selected and r.element_id == "t2" for r in panel._rows)
        assert window.layer_tree.selected_row_ids == ("t2",)

        # keyframes tree → window + Layers + inspector
        element_row = next(r for r in panel._rows if r.element_id == "t1")
        panel._select_element_row(element_row)
        assert window._selected_elements == frozenset({"t1"})
        assert window.layer_tree.selected_row_ids == ("t1",)
        assert window.inspector_stack.currentIndex() == 0
    finally:
        window.close()


def test_keyframes_scrub_shares_the_transport_frame_path(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_native_scene(_element()))
    try:
        window.keyframes_panel.frame_changed.emit(12)
        assert window.editor_controller.current_frame == 12
        assert window.timeline.slider.value() == 12
        assert window.keyframes_panel.current_frame == 12
        # The transport slider drives the playhead back the other way.
        window.timeline.slider.setValue(30)
        assert window.keyframes_panel.current_frame == 30
        assert window.editor_controller.current_frame == 30
    finally:
        window.close()


def test_panel_edit_lands_as_one_undo_and_evaluates(qapp) -> None:  # type: ignore[no-untyped-def]
    # A preset-built local x track (static 50) exists; the panel edit must
    # create the scene-level animation that overrides it at evaluation time.
    local = KeyframeTrack("x", min_val=-1e6, max_val=1e6)
    local.add_keyframe(Keyframe(0, 50.0))
    local.add_keyframe(Keyframe(47, 50.0))
    window = MotionGraphicEditorWindow(_native_scene(_element("t1", local=(local,))))
    try:
        panel = window.keyframes_panel
        panel._toggle_expanded("t1")
        row = next(r for r in panel._rows if r.property_name == "x")
        assert row.track_source == "local"          # preset badge source

        panel.set_current_frame(10)
        panel._toggle_keyframe_for_row(row)          # add scene keyframe at 10
        animations = [a for a in window.editor_controller.scene.animations
                      if a.element_id == "t1" and a.property == "x"]
        assert len(animations) == 1
        assert animations[0].track.get_value(10) == pytest.approx(50.0)

        from src.features.teaching_animation.render.evaluator import evaluate
        evaluated = evaluate(window.editor_controller.scene, 10)
        target = next(e for e in evaluated.elements if e.source.element_id == "t1")
        assert target.transform.x == pytest.approx(50.0)

        window.editor_controller.mark_clean()
        window.editor_controller.undo()              # one step restores no scene track
        animations = [a for a in window.editor_controller.scene.animations
                      if a.element_id == "t1" and a.property == "x"]
        assert animations == []
        evaluated = evaluate(window.editor_controller.scene, 10)
        target = next(e for e in evaluated.elements if e.source.element_id == "t1")
        assert target.transform.x == pytest.approx(50.0)   # local preset still drives
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_html_scene_groups_elements_under_page_rows(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_html_scene())
    try:
        window.editor_controller.mark_clean()
        panel = window.keyframes_panel
        # The former read-only HTML master row is replaced by one page row per
        # detected section, each carrying its frame extent.
        assert not [row for row in panel._rows if row.kind == "html"]
        page_rows = [row for row in panel._rows if row.kind == "page"]
        assert len(page_rows) == 2
        assert all(row.editable is False for row in page_rows)
        starts = window.editor_controller.scene.page_start_frames()
        assert page_rows[0].page_spans == ((0, starts[1] - 1),)
        assert page_rows[1].page_spans == ((starts[1], 47),)
        # The native overlay element sits under the first page's header.
        element_row = next(row for row in panel._rows
                           if row.kind == "element" and row.element_id == "t1")
        assert panel._rows.index(page_rows[0]) < panel._rows.index(element_row)
        assert panel._rows.index(element_row) < panel._rows.index(page_rows[1])
        # Playback auto-switches the active page and the page-scoped canvas:
        # t1 lives on page 1, so the view is empty while page 2 is active.
        assert window._active_page_index == 0
        assert [e.element_id for e in window._page_view.elements] == ["t1"]
        window._frame_changed(30)
        assert window._active_page_index == 1
        assert [e.element_id for e in window._page_view.elements] == []
        window._frame_changed(5)
        assert window._active_page_index == 0
        assert [e.element_id for e in window._page_view.elements] == ["t1"]
    finally:
        window.close()


def test_drag_release_lands_one_composite_undo(qapp) -> None:  # type: ignore[no-untyped-def]
    scene = _native_scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    window = MotionGraphicEditorWindow(scene)
    try:
        panel = window.keyframes_panel
        panel._toggle_expanded("t1")
        panel._mapper.pixels_per_frame = 10.0
        panel._mapper.scroll_offset = 0.0
        tracks = panel._tracks
        row = next(r for r in panel._rows if r.property_name == "opacity")
        index = panel._rows.index(row)
        y = 38 + index * 24 + 12

        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        def _mouse(kind: int, x: float, buttons, mods=Qt.KeyboardModifier.NoModifier) -> None:
            event = QMouseEvent(kind, QPointF(x, y), Qt.MouseButton.LeftButton, buttons, mods)
            if kind == QEvent.Type.MouseButtonPress:
                tracks.mousePressEvent(event)
            elif kind == QEvent.Type.MouseMove:
                tracks.mouseMoveEvent(event)
            else:
                tracks.mouseReleaseEvent(event)

        _mouse(QEvent.Type.MouseButtonPress, 0.0, Qt.MouseButton.LeftButton)   # kf at frame 0
        _mouse(QEvent.Type.MouseMove, 200.0, Qt.MouseButton.LeftButton)        # drag to frame 20
        _mouse(QEvent.Type.MouseButtonRelease, 200.0, Qt.MouseButton.NoButton)

        animation = next(a for a in window.editor_controller.scene.animations
                         if a.element_id == "t1" and a.property == "opacity")
        assert [kf.frame for kf in animation.track.keyframes] == [20, 24]

        window.editor_controller.mark_clean()
        window.editor_controller.undo()
        animation = next(a for a in window.editor_controller.scene.animations
                         if a.element_id == "t1" and a.property == "opacity")
        assert [kf.frame for kf in animation.track.keyframes] == [0, 24]
    finally:
        window.editor_controller.mark_clean()
        window.close()
