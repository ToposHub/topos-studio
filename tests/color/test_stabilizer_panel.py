"""Stabilizer controls emit edits only and keep a usable narrow layout."""
from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from PyQt6.QtWidgets import QApplication

from src.features.color.ui.stabilizer_panel import STABILIZER_DEFAULTS, StabilizerPanel

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def panel(qapp: QApplication) -> Iterator[StabilizerPanel]:
    widget = StabilizerPanel()
    widget.resize(786, 388)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_defaults_and_video_only_analysis(panel: StabilizerPanel) -> None:
    assert panel.get_params() == STABILIZER_DEFAULTS
    assert not panel.btn_stabilize.isEnabled()
    assert panel.graph.isEnabled()
    assert panel.mode_combo.count() == 3
    seen: list[str] = []
    panel.analyze_requested.connect(lambda: seen.append("analyze"))
    panel.btn_stabilize.click()
    assert not seen
    panel.set_video_available(True)
    panel.btn_stabilize.click()
    assert seen == ["analyze"]


def test_programmatic_restore_does_not_emit_or_request_analysis(panel: StabilizerPanel) -> None:
    params: list[dict] = []
    analyses: list[bool] = []
    panel.params_changed.connect(params.append)
    panel.analyze_requested.connect(lambda: analyses.append(True))
    values = {"mode": "translation", "camera_lock": True, "zoom": False,
              "bypass": True, "cropping_ratio": .8, "smooth": .9, "strength": .4}
    panel.set_params(values)
    assert panel.get_params() == values
    assert params == [] and analyses == []
    assert not panel.controls["smooth"].isEnabled()


def test_real_edit_emits_complete_params_and_camera_lock_disables_numeric_controls(panel: StabilizerPanel) -> None:
    panel.set_video_available(True)
    seen: list[dict] = []
    panel.params_changed.connect(seen.append)
    panel.controls["strength"].setValue(.6)
    panel.camera_lock_box.click()
    assert len(seen) == 2
    assert seen[-1]["strength"] == .6
    assert seen[-1]["camera_lock"]
    assert not panel.controls["smooth"].isEnabled()
    assert all(not control.isEnabled() for control in panel.controls.values())
    assert panel.zoom_box.isEnabled() and panel.mode_combo.isEnabled()
    panel.camera_lock_box.click()
    assert all(control.isEnabled() for control in panel.controls.values())
    assert panel.controls["strength"].value() == .6


def test_running_locks_parameters_and_button_requests_cancel(panel: StabilizerPanel) -> None:
    panel.set_video_available(True)
    seen: list[str] = []
    panel.analyze_requested.connect(lambda: seen.append("analyze"))
    panel.cancel_requested.connect(lambda: seen.append("cancel"))
    panel.set_running(True)
    panel.set_progress(5, 20)
    assert panel.status_label.isVisible()
    assert "25%" in panel.status_label.text()
    assert not panel.mode_combo.isEnabled()
    assert not panel.camera_lock_box.isEnabled()
    assert all(not value.isEnabled() for value in panel.controls.values())
    panel.btn_stabilize.click()
    assert seen == ["cancel"]
    # A source change cannot remove the running task's cancel affordance.
    panel.set_video_available(False)
    assert panel.btn_stabilize.isEnabled()
    panel.set_running(False)
    assert not panel.btn_stabilize.isEnabled()
    assert not panel.status_label.isVisible()
    panel.set_video_available(True)
    assert panel.mode_combo.isEnabled()


def test_invalid_optional_values_do_not_poison_ui(panel: StabilizerPanel) -> None:
    panel.set_params({"mode": "unknown", "zoom": "false", "strength": math.nan,
                      "smooth": math.inf, "cropping_ratio": -1})
    params = panel.get_params()
    assert params["mode"] == "perspective"
    assert params["zoom"] is True
    assert params["strength"] == 1.0 and params["smooth"] == .25
    assert params["cropping_ratio"] == 0.0


@pytest.mark.parametrize("width", [320, 480, 786])
def test_footer_groups_stay_inside_without_overlap(
        panel: StabilizerPanel, qapp: QApplication, width: int) -> None:
    panel.resize(width, 418)
    qapp.processEvents()
    groups = panel._parameter_groups
    rectangles = [group.geometry() for group in groups]
    for rectangle in rectangles:
        assert rectangle.left() >= 0
        assert rectangle.right() < panel.footer.width()
        assert rectangle.bottom() < panel.footer.height()
    for index, rectangle in enumerate(rectangles):
        assert not any(rectangle.intersects(other) for other in rectangles[index + 1:])
    if width == 786:
        assert panel._footer_rows == 1
    else:
        assert panel._footer_rows > 1


def test_shared_graph_keeps_ruler_and_motion_series(panel: StabilizerPanel) -> None:
    panel.set_frame_range(50, 149)
    panel.set_timebase(25)
    panel.set_current_frame(80)
    panel.set_motion_series({"tx": [(50, 0.0), (80, 4.0), (149, 8.0)]})
    assert panel.graph.view._current_frame == 80
    assert panel.graph.view._series["tx"][-1] == (149, 8.0)
    assert panel.graph.view._clip_extent == (50, 149)
    assert panel.graph.view.frame_to_x(80) > panel.graph.view.frame_to_x(50)
