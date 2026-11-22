"""Layer and Properties parameter controls share the live Composite graph."""

import pytest
from PyQt6.QtWidgets import QToolButton

from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def panels(qtbot, monkeypatch):
    page = CompositePage()
    layer_panel = KeyframesPanel()
    props = NodePropertiesPanel()
    for widget in (page, layer_panel, props):
        qtbot.addWidget(widget)
    monkeypatch.setattr(page, "_evaluate_graph", lambda: None)
    graph = page._node_graph
    graph.clear()
    graph.add_node(OutputNode(node_id="out"))
    layer_id = page._layer_stack_controller.create_generated_layer(
        "solid", "Card", {"width": 720, "height": 480})
    transform = page._layer_stack_controller.layer_transform_node(layer_id)
    page._composite_duration_frames = 100
    page.properties_panel = props
    page.set_keyframes_panel(layer_panel)
    layer_panel.set_composition_active(True)
    layer_panel.set_node_graph(graph, selected_layer_id=layer_id)
    layer_panel._model.set_row_expanded(f"layer:{layer_id}", True)
    layer_panel.resize(1120, 420)
    layer_panel.show()
    layer_panel._tree_view.refresh()
    page._refresh_properties_panel(transform.id)
    return page, layer_panel, props, transform


def test_static_parameter_edits_and_replay_update_both_existing_widgets(panels):
    page, layer_panel, props, transform = panels
    section = props._sections[transform.id]
    tree = layer_panel._tree_view
    props_rotation = section._param_widgets["rotation"]["widget"]
    layer_rotation = tree._value_controls[(transform.id, "rotation")]
    props_x = section._param_widgets["translate_x"]["widget"]
    layer_x = tree._value_controls[(transform.id, "translate_x")]

    props_rotation.setValue(16.125)
    assert transform.parameters["rotation"] == layer_rotation.value() == 16.125
    page.undo()
    assert props_rotation.value() == layer_rotation.value() == 0
    page.redo()
    assert props_rotation.value() == layer_rotation.value() == 16.125

    layer_x.setValue(125.625)
    assert transform.parameters["translate_x"] == props_x.value() == 125.625
    page.undo()
    assert props_x.value() == layer_x.value() == 0
    page.redo()
    assert props_x.value() == layer_x.value() == 125.625
    assert props._sections[transform.id] is section


def test_animated_values_keep_full_precision_and_do_not_overwrite_properties_base(panels):
    page, layer_panel, props, transform = panels
    track = KeyframeTrack("rotation", min_val=-360, max_val=360)
    track.add_keyframe(Keyframe(0, 0.123456))
    track.add_keyframe(Keyframe(10, 45.876543))
    transform.set_keyframe_track("rotation", track)
    page.set_composite_current_frame(5)

    section = props._sections[transform.id]
    layer_value = layer_panel._tree_view._value_controls[(transform.id, "rotation")].value()
    props_value = section._param_widgets["rotation"]["widget"].value()
    assert layer_value == props_value == pytest.approx(track.get_value(5))
    assert section.parameters["rotation"] == transform.parameters["rotation"] == 0


def test_centred_anchor_uses_input_size_in_both_panels(panels):
    page, layer_panel, props, transform = panels
    section = props._sections[transform.id]
    tree = layer_panel._tree_view
    props_x = section._param_widgets["pivot_x"]["widget"]
    layer_x = tree._value_controls[(transform.id, "pivot_x")]
    props_y = section._param_widgets["pivot_y"]["widget"]
    layer_y = tree._value_controls[(transform.id, "pivot_y")]
    assert props_x.value() == layer_x.value() == 360
    assert props_y.value() == layer_y.value() == 240
    assert not props_x.isEnabled() and not layer_x.isEnabled()

    mode = section._param_widgets["pivot_mode"]
    mode["widget"].setCurrentIndex(mode["options"].index("custom"))
    assert props_x.value() == layer_x.value() == transform.parameters["pivot_x"] == 0
    assert props_x.isEnabled() and layer_x.isEnabled()
    layer_x.setValue(33)
    assert props_x.value() == transform.parameters["pivot_x"] == 33
    mode["widget"].setCurrentIndex(mode["options"].index("center"))
    assert props_x.value() == layer_x.value() == 360
    assert transform.parameters["pivot_x"] == 33


def test_batch_edit_preserves_existing_properties_sections_and_replays_layer_values(panels):
    page, layer_panel, props, transform = panels
    controller = page._layer_stack_controller
    layer_id = controller.create_generated_layer("solid", "Second card", {})
    second = controller.layer_transform_node(layer_id)
    node_ids = [transform.id, second.id]
    page._on_nodes_selected(node_ids)
    sections = [props._sections[node_id] for node_id in node_ids]
    for row in layer_panel._model.rows:
        if row.row_type == "layer":
            layer_panel._model.set_row_expanded(row.row_id, True)
    layer_panel._tree_view.refresh()

    def shown_values():
        entries = layer_panel._tree_view._value_controls
        return [entries[(node_id, "rotation")].value() for node_id in node_ids]

    props.parameters_changed_batch.emit(node_ids, "rotation", 8.0, dict.fromkeys(node_ids, 0.0))
    assert shown_values() == [8.0, 8.0]
    page.undo()
    assert shown_values() == [0.0, 0.0]
    page.redo()
    assert shown_values() == [8.0, 8.0]
    assert [props._sections[node_id] for node_id in node_ids] == sections
    assert [section._param_widgets["rotation"]["widget"].value() for section in sections] == [8.0, 8.0]


def test_layer_trim_and_replay_keep_open_video_properties_in_sync(panels):
    page, layer_panel, props, _ = panels
    graph = page._node_graph
    controller = page._layer_stack_controller
    video = VideoFrameNode(node_id="video")
    video.set_parameter("total_frames", 500)
    graph.add_node(video)
    layer_id = controller.add_media_layer("Video", video.id)
    root = controller.layer_transform_node(layer_id)
    page._refresh_properties_panel(video.id)
    trim = props._sections[video.id]._param_widgets["trim_range"]["widget"]
    assert trim.values() == (0, 499)

    layer_panel.range_edit_changed.emit(
        root.id, {"layer_id": layer_id, "comp_start": 16, "comp_end": 120,
                  "enabled": True, "edit": "trim_in"})
    assert trim.values()[0] == graph.get_node(video.id).parameters["source_in"] == 16
    page.undo()
    assert trim.values() == (0, 499)
    page.redo()
    assert trim.values()[0] == 16
    assert props._current_node_id == video.id


def test_scale_lock_and_values_are_shared_by_both_panels(panels) -> None:
    page, layer_panel, props, transform = panels
    tree = layer_panel._tree_view
    section = props._sections[transform.id]
    props_x = section._param_widgets["scale_x"]["widget"]
    props_y = section._param_widgets["scale_y"]["widget"]
    props_lock = next(btn for btn in props_x.parentWidget().findChildren(QToolButton)
                      if btn.property("link_button"))
    layer_lock = tree._scale_buttons[transform.id]
    layer_x = tree._value_controls[(transform.id, "scale_x")]
    layer_y = tree._value_controls[(transform.id, "scale_y")]
    assert props_lock.isChecked() and layer_lock.isChecked()

    layer_x.setValue(135.0)
    assert props_x.value() == props_y.value() == pytest.approx(1.35)
    assert layer_x.value() == layer_y.value() == pytest.approx(135)
    page.undo()
    assert props_x.value() == props_y.value() == 1
    page.redo()
    assert props_x.value() == props_y.value() == pytest.approx(1.35)

    props_lock.click()
    assert not props_lock.isChecked() and not layer_lock.isChecked()
    props_y.setValue(2.75)
    assert (layer_x.value(), layer_y.value()) == pytest.approx((135, 275))
    layer_lock.click()
    assert props_lock.isChecked() and layer_lock.isChecked()
    props_y.setValue(3.5)
    assert (layer_x.value(), layer_y.value()) == pytest.approx((350, 350))
    assert transform.parameters["scale_x"] == transform.parameters["scale_y"] == 3.5


def test_narrow_layer_panel_fits_both_full_scale_values_and_lock(panels, qtbot) -> None:
    page, layer_panel, _, transform = panels
    tree = layer_panel._tree_view
    tree._scale_buttons[transform.id].click()
    layer_panel._splitter.moveSplitter(240, 1)
    page._on_parameter_changed(transform.id, "scale_x", 100.0)
    page._on_parameter_changed(transform.id, "scale_y", 87.25)
    qtbot.waitUntil(lambda: tree.width() < 300)

    lock = tree._scale_buttons[transform.id]
    x_control = tree._value_controls[(transform.id, "scale_x")]
    y_control = tree._value_controls[(transform.id, "scale_y")]
    assert x_control.y() == y_control.y()
    assert lock.geometry().right() < x_control.geometry().left()
    assert x_control.geometry().right() < y_control.geometry().left()
    assert y_control.geometry().right() < tree.width()
    for control, value in ((x_control, "10000.0%"), (y_control, "8725.0%")):
        label = control._value_label
        assert label.text() == value
        assert label.fontMetrics().horizontalAdvance(value) <= label.contentsRect().width()


def test_linked_scale_writes_the_current_keyframe_and_undoes_both_axes(panels) -> None:
    page, layer_panel, props, transform = panels
    track = KeyframeTrack("scale_x", min_val=0.01, max_val=100.0)
    track.add_keyframe(Keyframe(0, 1))
    track.add_keyframe(Keyframe(10, 3))
    transform.set_keyframe_track("scale_x", track)
    before = transform.keyframe_tracks["scale_x"]
    page.set_composite_current_frame(5)

    def layer_values():
        controls = layer_panel._tree_view._value_controls
        return [controls[(transform.id, key)].value() for key in ("scale_x", "scale_y")]

    def properties_values():
        widgets = props._sections[transform.id]._param_widgets
        return [widgets[key]["widget"].value() for key in ("scale_x", "scale_y")]

    assert layer_values() == pytest.approx([200, 100])
    props._sections[transform.id]._param_widgets["scale_y"]["widget"].setValue(2.75)
    assert layer_values() == pytest.approx([275, 275])
    assert properties_values() == pytest.approx([2.75, 2.75])
    assert transform.parameters["scale_x"] == 1
    assert transform.parameters["scale_y"] == pytest.approx(2.75)
    assert [key["frame"] for key in transform.keyframe_tracks["scale_x"]["keyframes"]] == [0, 5, 10]
    page.undo()
    assert transform.keyframe_tracks["scale_x"] == before
    assert layer_values() == pytest.approx([200, 100])
    assert properties_values() == pytest.approx([2, 1])
    page.redo()
    assert layer_values() == pytest.approx([275, 275])
    assert properties_values() == pytest.approx([2.75, 2.75])


def test_projection_switch_keeps_axis_controls_and_selection_shared(panels, qtbot):
    page, panel, props, transform = panels
    tree = panel._tree_view
    viewport = panel._timeline_view
    panel.set_frame_range(0, 99)
    panel.set_comp_extent(0, 99)
    panel.fit_time_range()
    viewport.zoom_time_at(viewport.width() / 2, 2)
    before = panel._frame_mapper.pixels_per_frame, panel._frame_mapper.scroll_offset
    track = KeyframeTrack("scale_x")
    track.add_keyframe(Keyframe(6, 1.1))
    transform.set_keyframe_track("scale_x", track)
    panel._rebuild_filtered_model()
    pair = next(row for row in panel._model.rows
                if row.node_id == transform.id and row.component_params == ("scale_x", "scale_y"))
    panel._toggle_dimensions(pair.row_id)
    row_id = f"param:{transform.id}:scale_x"
    viewport._select_keyframe(row_id, 6)
    assert viewport.selected_keyframes() == [(row_id, 6)]

    panel._projection_button.click()
    assert panel.layer_view_mode == "nodes"
    assert (panel._frame_mapper.pixels_per_frame, panel._frame_mapper.scroll_offset) == before
    assert panel._tree_view is tree and panel._timeline_view is viewport
    assert viewport.selected_keyframes() == [(row_id, 6)]
    assert row_id in {row.row_id for row in panel._model.visible_rows()}
    panel._set_filter_mode("selected")
    assert viewport.selected_keyframes() == [(row_id, 6)]
    assert row_id in {row.row_id for row in panel._model.visible_rows()}
    panel._set_filter_mode("all")

    panel._search_edit.setText(transform.display_title)
    panel.set_current_frame(6)
    qtbot.waitUntil(lambda: (transform.id, "scale_x") in tree._value_controls)
    section = props._sections[transform.id]
    lock = tree._scale_buttons[transform.id]
    assert lock.isChecked()
    lock.click()
    control = section._param_widgets["scale_x"]["widget"]
    props_lock = next(btn for btn in control.parentWidget().findChildren(QToolButton)
                      if btn.property("link_button"))
    assert not props_lock.isChecked()
    tree._value_controls[(transform.id, "scale_x")].setValue(145)
    assert transform.parameter_value_at_frame("scale_x", 6) == pytest.approx(1.45)
    page.set_composite_current_frame(6)
    assert section._param_widgets["scale_x"]["widget"].value() == pytest.approx(1.45)


def test_node_timeline_eye_and_lock_share_properties_and_canvas_state(panels, qtbot):
    from PyQt6.QtCore import QPoint, Qt

    page, panel, props, transform = panels
    editor = page._node_editor
    editor.build_from_graph(page._node_graph)
    editor.select_node(transform.id)
    panel._projection_button.click()
    panel._search_edit.setText(transform.display_title)
    tree = panel._tree_view

    def row():
        return next(row for row in panel._model.visible_rows()
                    if row.node_id == transform.id and row.row_type == "node")

    def click(column):
        left, right = tree._column_geometry()[column]
        index = panel._model.visible_rows().index(row())
        point = QPoint((left + right) // 2, tree._header_height + 24 * index + 12)
        qtbot.mouseClick(tree, Qt.MouseButton.LeftButton, pos=point)

    section = props._sections[transform.id]
    qtbot.mouseClick(section._enable_btn, Qt.MouseButton.LeftButton)
    assert transform.disabled and not row().layer_enabled
    click("eye")
    assert not transform.disabled and row().layer_enabled
    assert section._enable_btn.isChecked()
    page.undo()
    assert transform.disabled and not row().layer_enabled
    assert not props._sections[transform.id]._enable_btn.isChecked()

    click("lock")
    assert editor.is_node_locked(transform.id) and row().locked
    assert not tree._value_controls[(transform.id, "rotation")].isEnabled()
    click("lock")
    assert not editor.is_node_locked(transform.id) and not row().locked
