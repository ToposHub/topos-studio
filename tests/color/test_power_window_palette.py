"""Video Window UI edits retain model identities and image-only tools."""
import copy

import pytest
from PyQt6.QtTest import QSignalSpy

from src.features.color.core.layer import MaskGroup, MaskShape
from src.features.color.ui.mask_panel import MaskPanel
from src.features.color.ui.power_window_panel import PowerWindowPanel

pytestmark = pytest.mark.usefixtures("qapp")


def test_five_empty_templates_and_signal_only_creation(qapp):
    palette = PowerWindowPanel()
    spy = QSignalSpy(palette.shape_added)
    assert palette.rows_layout.count() == 6
    for kind, button in palette.add_buttons.items():
        button.click()
        assert spy[-1] == [kind]
    assert not palette.delete_button.isEnabled()
    assert all(not value.isEnabled() for value in palette.controls.values())


def test_video_selection_and_nested_identity_are_quiet(qapp):
    palette = PowerWindowPanel()
    a, b = MaskShape("circle"), MaskShape("circle")
    group = MaskGroup()
    group.children = [b]
    palette.set_shapes([a, group])
    selection = QSignalSpy(palette.shape_selected)
    palette.select_shape(b.id)
    palette.set_shapes([a, group])
    assert palette._selected_id == b.id
    assert not selection
    assert palette._row_buttons[b.id].isChecked()
    palette._row_buttons[a.id].click()
    assert selection[-1] == [a.id]


def test_size_and_aspect_adjust_geometry_without_mutating_model(qapp):
    palette = PowerWindowPanel()
    circle = MaskShape("circle")
    circle.size = [.15, .15 * 16 / 9]
    palette.set_frame_aspect(16 / 9)
    palette.set_shapes([circle])
    palette.select_shape(circle.id)
    spy = QSignalSpy(palette.shape_updated)
    assert palette.controls["size"].value() == pytest.approx(50)
    assert palette.controls["aspect"].value() == pytest.approx(50)
    palette.controls["size"].setValue(100)
    assert spy[-1][1]["size"] == pytest.approx([.3, .3 * 16 / 9])
    assert circle.size == pytest.approx([.15, .15 * 16 / 9])
    palette.controls["aspect"].setValue(100)
    assert spy[-1][1]["size"] == pytest.approx([.3, .15 * 16 / 9])


def test_curve_numeric_pan_preserves_bezier_and_feather(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("curve")
    shape.points = [{"pos": [.3, .4], "c1": [.2, .4], "c2": [.4, .4], "type": "smooth", "fi": .03, "fo": .04}]
    original = copy.deepcopy(shape.points)
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["pan"].setValue(60)
    points = spy[-1][1]["points"]
    assert points[0]["pos"] == pytest.approx([.4, .4])
    assert points[0]["c1"] == pytest.approx([.3, .4])
    assert points[0]["fi"] == .03
    assert shape.points == original


def test_gradient_controls_edit_endpoints_and_softness(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("gradient")
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["pan"].setValue(60)
    assert spy[-1][1]["grad_points"][0] == pytest.approx([.5, .5])
    palette.controls["soft1"].setValue(35)
    assert spy[-1][1] == {"softness": .35}
    assert not palette.controls["soft2"].isEnabled()


def test_mask_panel_preserves_image_tools_across_modes(qapp):
    panel = MaskPanel()
    panel.show()
    panel.set_image_only_tools_visible(False)
    assert panel.video_panel.isVisible()
    assert not panel.image_panel.isVisible()
    circle = MaskShape("circle")
    panel.update_shapes_list([circle])
    panel.select_shape(circle.id)
    qapp.processEvents()
    assert panel.video_panel._selected_id == circle.id
    panel.set_image_only_tools_visible(True)
    assert panel.image_panel.isVisible()
    assert not panel.video_panel.isVisible()
    for button in (panel.btn_birefnet, panel.btn_sam, panel.btn_brush, panel.btn_eraser):
        assert button.isVisible()
    spy = QSignalSpy(panel.brush_toggled)
    panel.btn_brush.click()
    assert spy[-1] == [True]
    panel.set_image_only_tools_visible(False)
    assert spy[-1] == [False]


def test_deferred_params_do_not_leak_to_different_shape(qapp):
    panel = MaskPanel()
    changes = QSignalSpy(panel.shape_updated)
    panel._schedule_param_update("first", {"feather": 10})
    panel._schedule_param_update("second", {"opacity": .3})
    panel._finish_param_interaction()
    assert list(changes) == [["first", {"feather": 10}], ["second", {"opacity": .3}]]
    assert not panel._param_timer.isActive()


def test_end_interaction_flushes_value_before_end_signal(qapp):
    panel = MaskPanel()
    events = []
    panel.shape_updated.connect(lambda sid, params: events.append((sid, params)))
    panel.interaction_ended.connect(lambda: events.append("end"))
    panel._schedule_param_update("one", {"feather": 25})
    panel.feather_drag.interaction_ended.emit()
    assert events == [("one", {"feather": 25}), "end"]


def test_curve_inner_outer_softness_uses_model_units(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("curve")
    shape.points = [{"pos": [.3, .4], "c1": [.2, .4], "c2": [.4, .4], "type": "smooth", "fi": 3.0, "fo": 4.0}]
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    assert palette.controls["inside"].value() == 3.0
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["inside"].setValue(12)
    assert spy[-1][1]["points"][0]["fi"] == 12.0
    assert spy[-1][1]["points"][0]["fo"] == 4.0


def test_evaluated_geometry_is_numeric_transform_origin(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("gradient")
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    palette.set_geometry({"center": [.6, .5], "size": [.3, .3], "rotation": 0,
                          "grad_points": [[.5, .5], [.7, .5]]})
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["pan"].setValue(70)
    assert spy[-1][1]["grad_points"][0] == pytest.approx([.6, .5])


def test_positive_tilt_moves_window_up(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("circle")
    shape.center = [.5, .6]
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    assert palette.controls["tilt"].value() == pytest.approx(40)
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["tilt"].setValue(60)
    assert spy[-1][1]["center"] == pytest.approx([.5, .4])


def test_selected_window_button_toggles_activation_without_deleting(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("circle")
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    edits = QSignalSpy(palette.shape_updated)
    removals = QSignalSpy(palette.shape_removed)
    palette._row_buttons[shape.id].click()
    assert edits[-1] == [shape.id, {"visible": False}]
    assert not palette.controls["size"].isEnabled()
    palette._row_buttons[shape.id].click()
    assert edits[-1] == [shape.id, {"visible": True}]
    assert palette.controls["size"].isEnabled()
    assert not removals
    assert shape.visible, "UI edits must be committed by the controller, not mutate the model directly"


def test_rectangle_four_edges_and_transform_match_default_window(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("rect")
    shape.size = [.4, .4]
    shape.edge_softness = [1.25] * 4
    palette.set_frame_aspect(16 / 9)
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    assert palette.controls["size"].value() == pytest.approx(50)
    assert palette.controls["aspect"].value() == pytest.approx(50)
    for key in ("soft1", "soft2", "soft3", "soft4"):
        assert palette.controls[key].isEnabled()
        assert palette.controls[key].value() == 1.25
    assert not palette.controls["inside"].isEnabled()
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["soft3"].setValue(4)
    assert spy[-1] == [shape.id, {"edge_softness": [1.25, 1.25, 4.0, 1.25]}]
    assert shape.edge_softness == [1.25] * 4
    palette.controls["size"].setValue(100)
    assert spy[-1][1]["size"] == pytest.approx([.8, .8])


def test_curve_rotation_is_not_baked_twice(qapp):
    palette = PowerWindowPanel()
    shape = MaskShape("curve")
    shape.points = [{"pos": [.3, .4], "c1": [.2, .4], "c2": [.4, .4], "type": "smooth"}]
    palette.set_shapes([shape])
    palette.select_shape(shape.id)
    spy = QSignalSpy(palette.shape_updated)
    palette.controls["rotate"].setValue(45)
    assert spy[-1][1]["rotation"] == 45
    assert spy[-1][1]["points"] == shape.points
