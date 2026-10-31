"""Runtime handoff tests for Model3D diagnostics and auxiliary docks."""

from __future__ import annotations

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.model3d_node import Model3DNode
from src.features.composite.ui.composite_page import CompositePage


class _ImportReportPanel:
    def __init__(self) -> None:
        self.summaries = []
        self.states = []
        self.resource_estimates = []
        self.probes = []
        self.diagnostics = []

    def update_summary(self, summary):
        self.summaries.append(summary)

    def update_import_state(self, status, progress, phase):
        self.states.append((status, progress, phase))

    def update_resource_estimate(self, estimate):
        self.resource_estimates.append(estimate)

    def update_format_probe(self, probe):
        self.probes.append(probe)

    def update_diagnostics(self, records):
        self.diagnostics.append(list(records))


def _page_with_model():
    page = CompositePage.__new__(CompositePage)
    page._node_graph = NodeGraph()
    node = Model3DNode("model-node")
    node.parameters.update({
        "model_path": "/tmp/robot.glb",
        "anim_clip": 1,
        "anim_play_mode": "pingpong",
        "anim_speed": 1.5,
        "anim_time_offset": 0.25,
        "anim_start_frame": 4.0,
        "anim_end_frame": 40.0,
    })
    page._node_graph.add_node(node)
    page._last_selected_node_id = node.id
    page._runtime_context = None
    page._composite_current_frame = 12
    page._comp_playing = False
    page._last_model_animation_clips = [
        {"name": "idle", "duration": 1.0},
        {"name": "walk", "duration": 2.0},
    ]
    page._last_model_source_path = "/tmp/robot.glb"
    page._last_model_import_report = {"source_path": "/tmp/robot.glb"}
    page._last_model_import_status = "loading"
    page._last_model_import_phase = "parse"
    page._last_model_import_progress = 0.15
    page._last_model_import_resource_estimate = {"total_bytes": 1024}
    page._last_model_import_probe = {"format_id": "gltf"}
    page._import_report_panel = _ImportReportPanel()
    return page, node


def test_selected_model_syncs_import_report():
    page, _node = _page_with_model()
    CompositePage._sync_import_report_for_selected_node(page)
    assert page._import_report_panel.summaries[-1]["source_path"] == "/tmp/robot.glb"
    assert page._import_report_panel.states[-1] == ("loading", 0.15, "parse")


def test_metadata_update_is_normalized_before_panel_handoff():
    page, _node = _page_with_model()
    CompositePage._update_model_panels_from_diagnostics(page, {
        "model_source_path": "/tmp/robot.glb",
        "model_animation_clips": [
            {"name": "run", "duration": "3.5"},
            {"name": "", "duration": 5.0},
            {"name": "bad", "duration": "not-a-number"},
        ],
        "model_import_report": {"source_path": "/tmp/robot.glb"},
        "model_import_status": "loading",
        "model_import_progress": 0.15,
        "model_import_phase": "parse",
        "model_import_resource_estimate": {"total_bytes": 2048},
        "model_import_probe": {"format_id": "gltf", "reason_code": "ready"},
        "diagnostic_records": [{
            "code": "skinning.weights_not_normalized",
            "severity": "warning",
            "message": "weights are not normalized",
        }],
    })
    assert page._last_model_animation_clips == [
        {"name": "run", "duration": 3.5},
        {"name": "bad", "duration": 0.0},
    ]
    assert page._import_report_panel.states[-1] == ("loading", 0.15, "parse")
    assert page._import_report_panel.resource_estimates[-1] == {
        "total_bytes": 2048}
    assert page._import_report_panel.probes[-1]["format_id"] == "gltf"
    assert page._import_report_panel.diagnostics[-1][0]["code"] == (
        "skinning.weights_not_normalized")


def test_non_model_selection_clears_import_report():
    page, _node = _page_with_model()
    page._last_selected_node_id = "missing"
    CompositePage._sync_import_report_for_selected_node(page)
    assert page._import_report_panel.summaries[-1] is None


def test_composite_save_persists_editor_selection_state():
    page = CompositePage.__new__(CompositePage)
    page._node_graph = NodeGraph()
    calls = []

    class _Editor:
        def save_ui_state(self):
            calls.append("saved")
            page._node_graph._viewer_state = {
                "selected_node_id": "model-node",
            }

    page._node_editor = _Editor()
    page._composite_fps = 24.0
    page._composite_duration_frames = 0

    data = CompositePage.get_composite_data(page)

    assert calls == ["saved"]
    assert data["viewer_state"]["selected_node_id"] == "model-node"


def test_outliner_lock_and_solo_write_back_to_model_parameters():
    page, node = _page_with_model()
    page._on_parameter_changed = lambda node_id, name, value: (
        node.parameters.__setitem__(name, value))

    CompositePage._on_scene_outliner_lock_changed(page, "body-id", True)
    CompositePage._on_scene_outliner_solo_changed(page, "head-id")

    assert node.parameters["locked_mesh_ids"] == "body-id"
    assert node.parameters["solo_mesh_id"] == "head-id"
