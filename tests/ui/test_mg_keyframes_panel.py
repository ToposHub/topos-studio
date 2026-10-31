"""Keyframes timeline for the Motion Graphic editor: model + panel contract.

The row model and track-edit helpers are Qt-free and test directly; the panel
tests drive the self-painted halves through the same mouse-event surface the
user sees (press/move/release on diamonds, ruler and empty track space).
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest

from src.features.teaching_animation.core.model import (
    SceneAnimation, SceneElement, ScenePage, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.ui.editor_controller import SceneEditorController
from src.features.teaching_animation.ui.editor_controller import diff_scenes
from src.features.teaching_animation.ui.mg_keyframes_model import (
    FrameMapper, animatable_properties_for, build_keyframe_rows, effective_tracks,
    static_value_for, track_with_interpolation, track_with_keyframe_added,
    track_with_keyframe_copied, track_with_keyframe_value,
    track_with_keyframes_moved, track_with_keyframes_removed,
)
from src.features.teaching_animation.ui.mg_keyframes_panel import MgKeyframesPanel
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


def _element(eid: str = "t1", etype: str = "text", z: int = 0) -> SceneElement:
    return SceneElement(
        element_id=eid, type=etype, z_index=z, visible=True,
        transform=SceneTransform(x=100.0, y=200.0, width=300.0, height=80.0),
        style={}, content="Hello" if etype == "text" else {"kind": "rect"},
    )


def _scene(*elements: SceneElement, duration: int = 48) -> TeachingSceneDocument:
    from dataclasses import replace
    # create_default ships a demo title element; keyframe tests want an empty
    # stage so row ordering is fully controlled by the test.
    scene = replace(
        TeachingSceneDocument.create_default("kf", duration_frames=duration), elements=())
    for element in elements:
        scene = scene.with_element_added(element)
    return scene


def _opacity_track(frames=((0, 0.0), (24, 1.0))) -> KeyframeTrack:
    track = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    for frame, value in frames:
        track.add_keyframe(Keyframe(frame, value))
    return track


# ── model ────────────────────────────────────────────────────────────────────


def test_rows_follow_canvas_order_and_expand_into_property_rows() -> None:
    scene = _scene(_element("low", z=0), _element("high", z=1))
    rows = build_keyframe_rows(scene)
    assert [row.row_id for row in rows] == [
        "page:page_1", "element:high", "element:low",
        "background:page_1", "background:global",  # page parent first
    ]
    expanded = build_keyframe_rows(scene, expanded_ids={"high"})
    assert expanded[0].row_id == "page:page_1"
    assert expanded[1].row_id == "element:high"
    property_rows = [r for r in expanded if r.kind == "property" and r.element_id == "high"]
    assert len(property_rows) == len(animatable_properties_for(_element("high", z=1)))
    assert all(r.depth == 2 for r in property_rows)
    # The property block sits directly under its element row, then "low".
    low_index = next(i for i, r in enumerate(expanded) if r.row_id == "element:low")
    assert low_index == 2 + len(property_rows)


def test_page_edge_drag_emits_one_duration_change(qapp) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    scene = replace(
        _scene(duration=48),
        pages=(ScenePage("p1", duration_frames=24), ScenePage("p2", duration_frames=24)),
    )
    panel = MgKeyframesPanel()
    panel.set_scene(scene)
    changes: list[tuple[str, int]] = []
    panel.pageDurationChangeRequested.connect(
        lambda page_id, duration: changes.append((page_id, duration)),
    )
    row = next(row for row in panel._rows if row.kind == "page" and row.page_id == "p1")
    panel._tracks._begin_page_resize(row)
    panel._tracks._do_page_resize(panel._mapper.frame_to_x(30))
    assert panel._page_resize is not None
    assert panel._page_resize.duration_frames == 30
    panel._tracks.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(panel._mapper.frame_to_x(30), 42),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    assert changes == [("p1", 30)]
    panel.deleteLater()


def test_space_shortcut_is_exposed_by_keyframes_panel(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = MgKeyframesPanel()
    toggles: list[bool] = []
    panel.playbackToggleRequested.connect(lambda: toggles.append(True))
    panel._space_shortcut.activated.emit()
    assert toggles == [True]
    panel.deleteLater()


def test_horizontal_scrollbar_and_wheel_pan_timeline(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = MgKeyframesPanel()
    panel.set_scene(_scene(_element(), duration=240))
    panel.resize(900, 260)
    panel.show()
    qapp.processEvents()
    assert panel._horizontal_scrollbar.parentWidget() is panel._track_container
    assert panel._horizontal_scrollbar.height() == 8
    assert (
        panel._horizontal_scrollbar.mapTo(panel, QPoint(0, 0)).x()
        == panel._tracks.mapTo(panel, QPoint(0, 0)).x()
    )
    assert panel._horizontal_scrollbar.width() == panel._tracks.width()
    # The Edit-style initial view keeps enough content outside the viewport
    # for the scrollbar to be interactive immediately.
    assert panel._horizontal_scrollbar.isVisible()
    assert panel._horizontal_scrollbar.isEnabled() is True
    bar = panel._horizontal_scrollbar
    assert bar.maximum() > 0
    assert bar.isVisible() and bar.isEnabled()
    before = bar.value()
    panel._tracks.wheelEvent(QWheelEvent(
        QPointF(20, 50), QPointF(20, 50),
        QPoint(0, -120), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    ))
    assert bar.value() > before
    assert panel._mapper.scroll_offset == -float(bar.value())
    panel.deleteLater()


def test_horizontal_scrollbar_drag_pans_timeline_with_real_mouse_events(qapp) -> None:  # type: ignore[no-untyped-def]
    """Use Qt's delivered mouse events, not a direct ``setValue`` call."""
    panel = MgKeyframesPanel()
    panel.set_scene(_scene(_element(), duration=240))
    panel.resize(900, 260)
    panel.show()
    qapp.processEvents()
    bar = panel._horizontal_scrollbar
    assert bar.maximum() > 0 and bar.isEnabled()
    before = bar.value()

    QTest.mousePress(
        bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        QPoint(bar.width() // 4, bar.height() // 2),
    )
    QTest.mouseMove(bar, QPoint(bar.width() - 10, bar.height() // 2), delay=20)
    QTest.mouseRelease(
        bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        QPoint(bar.width() - 10, bar.height() // 2),
    )
    qapp.processEvents()

    assert bar.value() > before
    assert panel._mapper.scroll_offset == -float(bar.value())
    panel.deleteLater()


def test_timeline_children_toggle_playback_from_space(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = MgKeyframesPanel()
    toggles: list[bool] = []
    panel.playbackToggleRequested.connect(lambda: toggles.append(True))
    event = lambda: QKeyEvent(  # noqa: E731 - compact Qt event factory
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier,
    )

    panel._tree.keyPressEvent(event())
    panel._tracks.keyPressEvent(event())

    assert toggles == [True, True]
    panel.deleteLater()


def test_animatable_properties_per_element_type() -> None:
    assert "font_size" in animatable_properties_for(_element(etype="text"))
    assert "font_size" not in animatable_properties_for(_element(etype="shape"))
    assert {"fill_opacity", "stroke_width"} <= set(animatable_properties_for(_element(etype="shape")))
    for etype in ("text", "shape", "frame", "image"):
        assert {"x", "y", "scale_x", "scale_y", "rotation", "opacity"} <= set(
            animatable_properties_for(_element(etype=etype)))


def test_effective_tracks_scene_animation_overrides_local_preset_track() -> None:
    from dataclasses import replace
    local = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    local.add_keyframe(Keyframe(0, 0.5))
    scene = replace(_scene(_element()), elements=(replace(_element(), local_animation_tracks=(local,)),))
    tracks = effective_tracks(scene, "t1")
    assert tracks["opacity"][1] == "local"
    scene = scene.with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    tracks = effective_tracks(scene, "t1")
    assert tracks["opacity"][1] == "scene"
    assert [kf.frame for kf in tracks["opacity"][0].keyframes] == [0, 24]


def test_static_value_seeds_from_transform_and_style_defaults() -> None:
    element = _element()
    assert static_value_for(element, "x") == 100.0
    assert static_value_for(element, "opacity") == 1.0
    assert static_value_for(element, "font_size") == 32.0  # evaluator default


def test_track_helpers_preserve_values_and_empty_track_becomes_none() -> None:
    track = _opacity_track()
    added = track_with_keyframe_added(track, 12, 0.5, "opacity")
    assert [kf.frame for kf in added.keyframes] == [0, 12, 24]
    moved = track_with_keyframes_moved(track, [(0, 4), (24, 30)])
    assert [kf.frame for kf in moved.keyframes] == [4, 30]
    assert moved.get_value(4) == 0.0 and moved.get_value(30) == 1.0
    eased = track_with_interpolation(track, {0}, InterpolationType.EASE_IN)
    assert eased.keyframes[0].interpolation is InterpolationType.EASE_IN
    assert track.keyframes[0].interpolation is InterpolationType.LINEAR  # original untouched
    assert track_with_keyframes_removed(track, [0, 24]) is None
    assert track_with_keyframes_removed(None, [0]) is None


def test_keyframe_copy_move_and_value_edit_preserve_curve_metadata() -> None:
    source = Keyframe(
        5, 10.0, InterpolationType.BEZIER,
        bezier_in=(0.1, 0.2), bezier_out=(0.7, 0.8), tangent_mode="broken",
        handle_left=(-2.0, -3.0), handle_right=(4.0, 5.0),
    )
    track = KeyframeTrack("x", keyframes=[source], min_val=-1e6, max_val=1e6)

    moved = track_with_keyframes_moved(track, [(5, 15)])
    copied = track_with_keyframe_copied(track, source, 20, "x")
    edited = track_with_keyframe_value(track, 5, 12.5)

    for candidate, frame in ((moved.get_keyframe_at(15), 15),
                             (copied.get_keyframe_at(20), 20),
                             (edited.get_keyframe_at(5), 5)):
        assert candidate is not None
        assert candidate.frame == frame
        assert candidate.interpolation is InterpolationType.BEZIER
        assert candidate.bezier_in == (0.1, 0.2)
        assert candidate.bezier_out == (0.7, 0.8)
        assert candidate.tangent_mode == "broken"
        assert candidate.handle_left == (-2.0, -3.0)
        assert candidate.handle_right == (4.0, 5.0)
    assert edited.get_keyframe_at(5).value == 12.5


def test_animated_property_filter_hides_unanimated_rows() -> None:
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    rows = build_keyframe_rows(scene, expanded_ids={"t1"}, property_filter="animated")
    assert [row.property_name for row in rows if row.kind == "property"] == ["opacity"]


def test_scene_diff_reports_local_track_and_layout_changes() -> None:
    from dataclasses import replace
    local = _opacity_track()
    before = replace(_scene(_element()), elements=(replace(_element(), local_animation_tracks=(local,)),))
    changed_element = replace(
        before.element_by_id("t1"),
        local_animation_tracks=(),
        layout={"role": "hero"},
    )
    after = before.with_element_updated(changed_element)
    diff = diff_scenes(before, after)
    assert diff.changed_element_ids == {"t1"}
    assert diff.changed_fields["t1"] == {"layout", "local_animation_tracks"}
    assert diff.animation_changed is True


def test_frame_mapper_zoom_keeps_anchor_frame_under_cursor() -> None:
    mapper = FrameMapper(pixels_per_frame=10.0)
    assert mapper.frame_to_x(15) == 150.0
    mapper.zoom_at(150.0, 0.5)
    assert mapper.pixels_per_frame == pytest.approx(5.0)
    assert mapper.x_to_frame(150.0) == pytest.approx(15.0)


def test_ruler_and_header_use_edit_timecode_by_default(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = MgKeyframesPanel()
    try:
        assert panel.time_display == "00:00:00:00"
        assert panel._tracks._format_timecode_compact(0, 24.0, 1.0, 120) == "00:00"
        assert panel._mapper.origin_locked is True
    finally:
        panel.close()


def test_selected_row_is_reclamped_below_header_after_layout(qapp) -> None:  # type: ignore[no-untyped-def]
    """Selection applied before showing must not place a row in the ruler."""

    scene = _scene(_element())
    panel = MgKeyframesPanel()
    try:
        # Match the editor's order: set the scene while the parent is still
        # being assembled, then let the splitter establish its real viewport.
        panel.set_scene(scene, selected_ids={"t1"})
        panel.resize(1000, 360)
        panel.show()
        qapp.processEvents()
        assert panel._v_scroll == 0.0
        assert panel._row_y(0) >= 38.0
    finally:
        panel.close()


def test_edit_style_zoom_slider_keeps_frame_zero_fixed(qapp) -> None:  # type: ignore[no-untyped-def]
    """Slider/button/wheel zoom must never move the 0-second anchor."""

    panel = _shown_panel(_scene(_element()))
    try:
        panel._zoom(1.25)
        assert panel._mapper.scroll_offset == 0.0
        panel._zoom_slider.setValue(max(0, panel._zoom_slider.value() - 120))
        qapp.processEvents()
        assert panel._mapper.scroll_offset == 0.0
        assert panel._mapper.frame_to_x(0) == 0.0
    finally:
        panel.close()


# ── panel: state and edits without mouse events ──────────────────────────────


def _shown_panel(scene: TeachingSceneDocument) -> MgKeyframesPanel:
    panel = MgKeyframesPanel()
    panel.resize(1000, 360)
    panel.show()
    panel.set_scene(scene)
    return panel


def _hosted_panel(qapp, scene: TeachingSceneDocument) -> tuple[MgKeyframesPanel, SceneEditorController]:  # type: ignore[no-untyped-def]
    """Panel wired to a controller exactly like the editor window wires it:
    tracks_changed → apply_track_edits → scene refresh → panel.set_scene."""

    panel = _shown_panel(scene)
    controller = SceneEditorController(scene)
    controller.subscribe(
        lambda new_scene, _diff: panel.set_scene(new_scene, selected_ids=panel._selected_elements))
    panel.tracks_changed.connect(controller.apply_track_edits)
    return panel, controller


def test_summary_diamond_toggle_adds_then_removes_at_playhead(qapp) -> None:  # type: ignore[no-untyped-def]
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    panel, controller = _hosted_panel(qapp, scene)
    panel._toggle_expanded("t1")

    def _row() -> object:  # rows rebuild after every landed edit
        return next(r for r in panel._rows if r.property_name == "opacity")

    panel.set_current_frame(12)          # no keyframe at 12 → add (sampled 0.5)
    panel._toggle_keyframe_for_row(_row())
    animation = next(a for a in controller.scene.animations if a.element_id == "t1")
    assert [kf.frame for kf in animation.track.keyframes] == [0, 12, 24]
    assert animation.track.get_value(12) == pytest.approx(0.5)

    panel.set_current_frame(0)           # keyframe exists at 0 → remove
    panel._toggle_keyframe_for_row(_row())
    animation = next(a for a in controller.scene.animations if a.element_id == "t1")
    assert [kf.frame for kf in animation.track.keyframes] == [12, 24]
    # One undo per toggle (host landed each batch as a single command).
    controller.undo()
    animation = next(a for a in controller.scene.animations if a.element_id == "t1")
    assert [kf.frame for kf in animation.track.keyframes] == [0, 12, 24]


def test_removing_last_keyframe_emits_none_to_delete_the_animation(qapp) -> None:  # type: ignore[no-untyped-def]
    track = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(6, 0.4))
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", track))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    row = next(r for r in panel._rows if r.property_name == "opacity")
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)
    panel.set_current_frame(6)
    panel._toggle_keyframe_for_row(row)
    assert edits[-1][0][2] is None


def test_point_selection_survives_scene_refresh(qapp) -> None:  # type: ignore[no-untyped-def]
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    row = next(r for r in panel._rows if r.property_name == "opacity")
    panel._select_point(row, 24, Qt.KeyboardModifier.NoModifier)
    assert panel._point_selection == {("prop:t1:opacity", 24)}
    panel.set_scene(scene)               # same scene refresh (e.g. unrelated edit)
    assert panel._point_selection == {("prop:t1:opacity", 24)}
    refreshed = next(r for r in panel._rows if r.property_name == "opacity")
    assert any(point.selected and point.frame == 24 for point in refreshed.points)


def test_paste_reanchors_clipboard_at_the_playhead(qapp) -> None:  # type: ignore[no-untyped-def]
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    row = next(r for r in panel._rows if r.property_name == "opacity")
    panel._select_point(row, 0, Qt.KeyboardModifier.NoModifier)
    panel._select_point(row, 24, Qt.KeyboardModifier.ControlModifier)
    panel.copy_selected()
    assert len(panel._clipboard) == 2

    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)
    panel.set_current_frame(20)
    panel.paste_clipboard()
    track = edits[-1][0][2]
    assert [kf.frame for kf in track.keyframes] == [0, 20, 24, 44]
    assert panel._point_selection == {("prop:t1:opacity", 20), ("prop:t1:opacity", 44)}


def test_paste_preserves_interpolation_and_tangent_metadata(qapp) -> None:  # type: ignore[no-untyped-def]
    track = _opacity_track(((0, 0.0), (24, 1.0)))
    source = track.get_keyframe_at(0)
    source.interpolation = InterpolationType.BEZIER
    source.bezier_in = (0.1, 0.2)
    source.bezier_out = (0.7, 0.8)
    source.tangent_mode = "broken"
    source.handle_left = (-2.0, -3.0)
    source.handle_right = (4.0, 5.0)
    scene = _scene(_element()).with_animation_added(SceneAnimation("a1", "t1", "opacity", track))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    row = next(r for r in panel._rows if r.property_name == "opacity")
    panel._select_point(row, 0, Qt.KeyboardModifier.NoModifier)
    panel.copy_selected()
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)
    panel.set_current_frame(12)
    panel.paste_clipboard()
    pasted = edits[-1][0][2].get_keyframe_at(12)
    assert pasted is not None
    assert pasted.interpolation is InterpolationType.BEZIER
    assert pasted.tangent_mode == "broken"
    assert pasted.handle_left == (-2.0, -3.0)
    assert pasted.handle_right == (4.0, 5.0)


def test_edit_keyframe_value_emits_one_changed_track(monkeypatch, qapp) -> None:  # type: ignore[no-untyped-def]
    import src.features.teaching_animation.ui.mg_keyframes_panel as panel_module

    monkeypatch.setattr(
        panel_module.QInputDialog, "getDouble",
        staticmethod(lambda *args: (0.75, True)),
    )
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track(((0, 0.0),))))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    row = next(r for r in panel._rows if r.property_name == "opacity")
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)
    panel.edit_keyframe_value(row, 0)
    assert len(edits) == 1
    assert edits[0][0][2].get_keyframe_at(0).value == pytest.approx(0.75)


def test_interpolation_edit_targets_selected_points_only(qapp) -> None:  # type: ignore[no-untyped-def]
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    row = next(r for r in panel._rows if r.property_name == "opacity")
    panel._select_point(row, 24, Qt.KeyboardModifier.NoModifier)
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)
    panel.set_selected_interpolation(InterpolationType.HOLD)
    track = edits[-1][0][2]
    assert track.keyframes[0].interpolation is InterpolationType.LINEAR
    assert track.keyframes[1].interpolation is InterpolationType.HOLD


def test_page_rows_replace_html_master_row(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.html_layer import build_html_layer
    from src.features.teaching_animation.core.html_layer_commands import SetHtmlLayerCommand
    html = '<section data-topos-page="1">A</section><section data-topos-page="2">B</section>'
    layer = build_html_layer(
        layer_id="l1", sanitized_html=html, authored_source=html,
        viewport_width=1920, viewport_height=1080,
        fps_num=24, fps_den=1, duration_frames=48)
    scene = SetHtmlLayerCommand(layer).apply(_scene(_element()))
    panel = _shown_panel(scene)
    assert not [row for row in panel._rows if row.kind == "html"]
    page_rows = [row for row in panel._rows if row.kind == "page"]
    assert len(page_rows) == 2
    assert all(row.editable is False for row in page_rows)
    starts = scene.page_start_frames()
    assert page_rows[0].page_spans == ((0, starts[1] - 1),)
    assert page_rows[1].page_spans == ((starts[1], scene.duration_frames - 1),)
    # The element row is grouped between the two page headers.
    element_row = next(row for row in panel._rows
                       if row.kind == "element" and row.element_id == "t1")
    assert panel._rows.index(page_rows[0]) < panel._rows.index(element_row)
    assert panel._rows.index(element_row) < panel._rows.index(page_rows[1])
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)
    panel._toggle_keyframe_for_row(page_rows[0])   # read-only header: no-op
    assert edits == []
    frames: list[int] = []
    panel.frame_changed.connect(frames.append)
    panel._seek_to_page_row(page_rows[1])          # header click seeks the page
    assert frames == [starts[1]]
    assert panel._page_row_active(page_rows[1]) is True
    assert panel._page_row_active(page_rows[0]) is False


# ── panel: mouse-driven timeline interactions ────────────────────────────────


def _press(widget, x: float, y: float, button=Qt.MouseButton.LeftButton,
           mods=Qt.KeyboardModifier.NoModifier) -> None:
    widget.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), button, button, mods))


def _move(widget, x: float, y: float, button=Qt.MouseButton.LeftButton,
          mods=Qt.KeyboardModifier.NoModifier) -> None:
    widget.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), button, button, mods))


def _release(widget, x: float, y: float, button=Qt.MouseButton.LeftButton,
             mods=Qt.KeyboardModifier.NoModifier) -> None:
    widget.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y), button,
        Qt.MouseButton.NoButton, mods))


def _prepare_drag_panel(qapp) -> tuple[MgKeyframesPanel, object]:  # type: ignore[valid-type]
    """Panel with one x-track keyframe at frame 5, at 10 px/frame."""

    track = KeyframeTrack("x", min_val=-1e6, max_val=1e6)
    track.add_keyframe(Keyframe(5, 10.0))
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "x", track))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    panel._mapper.pixels_per_frame = 10.0
    panel._mapper.scroll_offset = 0.0
    row = next(r for r in panel._rows if r.property_name == "x")
    return panel, row


def _row_center_y(panel: MgKeyframesPanel, row) -> float:  # type: ignore[no-untyped-def]
    index = panel._rows.index(row)
    return 38 + index * 24 + 12  # ruler + row grid, matches _row_y()


def test_dragging_a_keyframe_emits_one_moved_track(qapp) -> None:  # type: ignore[no-untyped-def]
    panel, row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    x0 = panel._mapper.frame_to_x(5)
    y = _row_center_y(panel, row)
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)

    _press(tracks, x0, y)
    _move(tracks, panel._mapper.frame_to_x(15), y)
    _release(tracks, panel._mapper.frame_to_x(15), y)

    assert len(edits) == 1
    element_id, prop, track = edits[0][0]
    assert (element_id, prop) == ("t1", "x")
    assert [kf.frame for kf in track.keyframes] == [15]
    assert track.get_value(15) == 10.0  # value carried by the move


def test_drag_onto_another_keyframe_is_refused_entirely(qapp) -> None:  # type: ignore[no-untyped-def]
    track = KeyframeTrack("x", min_val=-1e6, max_val=1e6)
    track.add_keyframe(Keyframe(5, 10.0))
    track.add_keyframe(Keyframe(9, 12.0))
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "x", track))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    panel._mapper.pixels_per_frame = 10.0
    panel._mapper.scroll_offset = 0.0
    row = next(r for r in panel._rows if r.property_name == "x")
    tracks = panel._tracks
    y = _row_center_y(panel, row)
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)

    _press(tracks, panel._mapper.frame_to_x(5), y)
    _move(tracks, panel._mapper.frame_to_x(9), y)   # frame collision
    _release(tracks, panel._mapper.frame_to_x(9), y)

    assert edits == []                              # whole move refused


def test_alt_drag_duplicates_instead_of_moving(qapp) -> None:  # type: ignore[no-untyped-def]
    panel, row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    y = _row_center_y(panel, row)
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)

    _press(tracks, panel._mapper.frame_to_x(5), y, mods=Qt.KeyboardModifier.AltModifier)
    _move(tracks, panel._mapper.frame_to_x(15), y, mods=Qt.KeyboardModifier.AltModifier)
    _release(tracks, panel._mapper.frame_to_x(15), y, mods=Qt.KeyboardModifier.AltModifier)

    track = edits[0][0][2]
    assert [kf.frame for kf in track.keyframes] == [5, 15]
    assert track.get_value(15) == 10.0


def test_double_click_on_empty_track_adds_a_keyframe(qapp) -> None:  # type: ignore[no-untyped-def]
    panel, row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    y = _row_center_y(panel, row)
    edits: list[list] = []
    panel.tracks_changed.connect(edits.append)

    event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(panel._mapper.frame_to_x(20), y),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    tracks.mouseDoubleClickEvent(event)
    track = edits[0][0][2]
    assert [kf.frame for kf in track.keyframes] == [5, 20]


def test_ruler_click_seeks_and_plain_click_on_track_seeks(qapp) -> None:  # type: ignore[no-untyped-def]
    panel, row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    frames: list[int] = []
    panel.frame_changed.connect(frames.append)

    _press(tracks, panel._mapper.frame_to_x(12), 10)   # ruler band
    _release(tracks, panel._mapper.frame_to_x(12), 10)
    assert frames[-1] == 12

    y = _row_center_y(panel, row)
    _press(tracks, panel._mapper.frame_to_x(30), y)    # empty track space
    _release(tracks, panel._mapper.frame_to_x(30), y)
    assert frames[-1] == 30


# ── controller batch: one undo step for a whole drag/paste ──────────────────


def test_apply_track_edits_creates_replaces_and_removes_in_one_undo() -> None:
    controller = SceneEditorController(_scene(_element()))
    track = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(0, 0.0))

    controller.apply_track_edits([("t1", "opacity", track)])
    assert [a.property for a in controller.scene.animations] == ["opacity"]

    moved = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    moved.add_keyframe(Keyframe(10, 0.5))
    x_track = KeyframeTrack("x", min_val=-1e6, max_val=1e6)
    x_track.add_keyframe(Keyframe(4, 12.0))
    controller.apply_track_edits([("t1", "opacity", moved), ("t1", "x", x_track)])

    animations = {a.property: a for a in controller.scene.animations}
    assert [kf.frame for kf in animations["opacity"].track.keyframes] == [10]
    assert "x" in animations

    controller.apply_track_edits([("t1", "opacity", None)])  # delete the animation
    assert "opacity" not in {a.property for a in controller.scene.animations}

    assert controller.undo() is not None   # undo delete
    assert "opacity" in {a.property for a in controller.scene.animations}
    assert controller.undo() is not None   # undo the two-track batch as ONE step
    animations = {a.property: a for a in controller.scene.animations}
    assert [kf.frame for kf in animations["opacity"].track.keyframes] == [0]
    assert "x" not in animations


def test_apply_track_edits_rejects_unknown_element() -> None:
    controller = SceneEditorController(_scene(_element()))
    with pytest.raises(ValueError):
        controller.apply_track_edits([("missing", "opacity", _opacity_track())])


def test_full_delete_on_a_preset_track_removes_the_embedded_local_track() -> None:
    """Deleting every keyframe of a preset (local) track must be a real
    deletion — a scene-level None cannot shadow an embedded track."""
    from dataclasses import replace
    local = _opacity_track()
    scene = replace(_scene(_element()), elements=(replace(_element(), local_animation_tracks=(local,)),))
    controller = SceneEditorController(scene)
    element = controller.scene.element_by_id("t1")
    assert [t.param_name for t in element.local_animation_tracks] == ["opacity"]

    controller.apply_track_edits([("t1", "opacity", None)])
    element = controller.scene.element_by_id("t1")
    assert element.local_animation_tracks == ()           # preset track gone
    assert not any(a.property == "opacity" for a in controller.scene.animations)

    controller.undo()                                     # reversible
    assert [t.param_name for t in controller.scene.element_by_id("t1").local_animation_tracks] == ["opacity"]


def test_partial_delete_on_a_preset_track_creates_an_overriding_scene_track() -> None:
    from dataclasses import replace
    local = _opacity_track()
    scene = replace(_scene(_element()), elements=(replace(_element(), local_animation_tracks=(local,)),))
    controller = SceneEditorController(scene)
    remaining = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    remaining.add_keyframe(Keyframe(24, 1.0))
    controller.apply_track_edits([("t1", "opacity", remaining)])

    animation = next(a for a in controller.scene.animations
                     if a.element_id == "t1" and a.property == "opacity")
    assert [kf.frame for kf in animation.track.keyframes] == [24]
    # The embedded preset is untouched; the scene track wins at evaluation.
    assert [t.param_name for t in controller.scene.element_by_id("t1").local_animation_tracks] == ["opacity"]


def test_set_scene_fast_path_skips_rebuild_for_unchanged_scene(qapp) -> None:  # type: ignore[no-untyped-def]
    """Playback refreshes push the same immutable scene every frame; the row
    list must not be rebuilt (selection and expansion stay untouched)."""
    scene = _scene(_element()).with_animation_added(
        SceneAnimation("a1", "t1", "opacity", _opacity_track()))
    panel = _shown_panel(scene)
    panel._toggle_expanded("t1")
    rows_before = panel._rows
    panel.set_current_frame(10)                      # frame-only push first
    panel.set_scene(scene, selected_ids=panel._selected_elements)
    assert panel._rows is rows_before                # identity: no rebuild
    point = next(p for r in panel._rows if r.property_name == "opacity" for p in r.points if p.frame == 0)
    panel._select_point(next(r for r in panel._rows if r.property_name == "opacity"),
                        0, Qt.KeyboardModifier.NoModifier)
    rows_before = panel._rows
    panel.set_selection({"t1"})                      # selection path rebuilds…
    assert panel._rows is not rows_before
    rows_before = panel._rows
    panel.set_scene(scene, selected_ids={"t1"})      # …then the fast path holds
    assert panel._rows is rows_before


def test_drag_release_keeps_moved_keyframes_selected(qapp) -> None:  # type: ignore[no-untyped-def]
    panel, row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    y = _row_center_y(panel, row)
    panel.tracks_changed.connect(lambda edits: None)   # no host: panel keeps state
    _press(tracks, panel._mapper.frame_to_x(5), y)
    _move(tracks, panel._mapper.frame_to_x(15), y)
    _release(tracks, panel._mapper.frame_to_x(15), y)
    assert panel._point_selection == {("prop:t1:x", 15)}


def test_arrow_keys_step_the_playhead(qapp) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtGui import QKeyEvent
    panel, _row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    frames: list[int] = []
    panel.frame_changed.connect(frames.append)

    def _key(key: int, mods=Qt.KeyboardModifier.NoModifier) -> None:
        tracks.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    _key(Qt.Key.Key_Right)
    assert frames[-1] == 1 and panel.current_frame == 1
    _key(Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)   # +10
    assert frames[-1] == 11
    _key(Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)    # -10
    assert frames[-1] == 1
    _key(Qt.Key.Key_End)
    assert frames[-1] == panel._duration - 1
    _key(Qt.Key.Key_Home)
    assert frames[-1] == 0
    _key(Qt.Key.Key_Left)                                        # clamped at 0
    assert frames[-1] == 0


def test_double_click_add_keeps_the_new_keyframe_selected(qapp) -> None:  # type: ignore[no-untyped-def]
    """Qt's press/release/press/dblclick/release sequence must not end with
    the marquee release clearing the fresh keyframe's selection."""
    panel, row = _prepare_drag_panel(qapp)
    tracks = panel._tracks
    y = _row_center_y(panel, row)
    x = panel._mapper.frame_to_x(20)
    panel.tracks_changed.connect(lambda edits: None)

    _press(tracks, x, y)      # empty track → marquee
    _release(tracks, x, y)
    _press(tracks, x, y)      # second press of the double-click
    tracks.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    _release(tracks, x, y)    # trailing release must be a no-op now

    assert panel._point_selection == {("prop:t1:x", 20)}
