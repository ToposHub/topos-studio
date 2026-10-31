"""P0-07/P0-10 regression tests for render-pass and 3D metadata contracts."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pytest

from src.features.composite.core.graph_preflight import MODE_EXPORT, run_preflight_data
from src.features.composite.core.graph_snapshot import NodeSnapshot
from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.file_output_node import FileOutputNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.render.cache_manager import CacheManager
from src.features.composite.render.composite_renderer import CompositeRenderer
from src.features.composite.render.composite_export_renderer import CompositeExportRenderer


pytestmark = pytest.mark.composite_core


def _aov_result() -> TextureResult:
    return TextureResult(
        texture_id=10,
        width=640,
        height=360,
        revision=7,
        depth_texture_id=20,
        has_deep_data=True,
        aov_texture_ids={"depth": 20, "object_id": 30},
        aov_dimensions={"depth": (640, 360), "object_id": (640, 360)},
        render_diagnostics={"geometry_source": "model"},
    )


def test_texture_result_signature_and_copy_preserve_all_auxiliary_metadata():
    source = _aov_result()
    copied = source.with_updates(texture_id=11)

    assert copied.texture_id == 11
    assert copied.depth_texture_id == 20
    assert copied.aov_texture_ids == {"depth": 20, "object_id": 30}
    assert copied.render_diagnostics == {"geometry_source": "model"}
    assert ("depth", 20, (640, 360), 7) in copied.aov_signature()


def test_renderer_aov_metadata_is_explicit_and_typed_for_viewer_copy():
    metadata = CompositeRenderer._build_aov_metadata(_aov_result())

    assert metadata == {
        "depth": {
            "texture_id": 20, "width": 640, "height": 360,
            "storage": "r32f",
        },
        "object_id": {
            "texture_id": 30, "width": 640, "height": 360,
            "storage": "rgba8",
        },
    }


def test_renderer_uses_rgba8_storage_for_material_id_aov():
    result = _aov_result().with_updates(
        aov_texture_ids={"material_id": 31},
        aov_dimensions={"material_id": (640, 360)},
    )

    assert CompositeRenderer._build_aov_metadata(result) == {
        "material_id": {
            "texture_id": 31, "width": 640, "height": 360,
            "storage": "rgba8",
        },
    }


def test_viewer_render_pass_selection_is_display_only():
    from src.features.composite.ui.composite_viewport import CompositeViewer

    viewer = CompositeViewer.__new__(CompositeViewer)
    viewer._selected_render_pass = "beauty"
    viewer._owned_display_tex_id = 11
    viewer._owned_display_tex_size = (640, 360)
    viewer._beauty_dimensions = (640, 360)
    viewer._owned_aov_tex_ids = {"depth": 20}
    viewer._owned_aov_tex_sizes = {"depth": (640, 360)}
    viewer._toolbar = SimpleNamespace(update_render_pass_label=lambda _: None)
    viewer._inspector_bar = SimpleNamespace(update_format_info=lambda *_: None)
    viewer._current_tex_id = 11
    viewer._tex_width = 640
    viewer._tex_height = 360
    viewer.update = lambda: None

    assert CompositeViewer.set_render_pass(viewer, "depth") is True
    assert viewer._current_tex_id == 20
    assert viewer._selected_render_pass == "depth"
    assert CompositeViewer.set_render_pass(viewer, "normal") is False
    assert viewer._current_tex_id == 11
    assert viewer._selected_render_pass == "beauty"


def test_texture_result_memory_budget_counts_distinct_aov_buffers():
    source = _aov_result()
    expected = 640 * 360 * (4 * 2 + 1 * 2 + 4)  # beauty + depth + object_id
    assert source.memory_bytes() == expected


def test_output_resize_invalidates_unresampled_auxiliary_passes():
    source = _aov_result()
    output = OutputNode._result_like(source, texture_id=99, width=320, height=180)

    assert output.texture_id == 99
    assert output.depth_texture_id is None
    assert output.has_deep_data is False
    assert output.aov_texture_ids == {}
    assert output.render_diagnostics["auxiliary_passes_invalidated"] == "output_resize"


def test_file_output_can_select_scene3d_aov_pass():
    selected = FileOutputNode._select_render_pass(_aov_result(), "depth")

    assert selected is not None
    assert selected.texture_id == 20
    assert selected.width == 640
    assert selected.height == 360
    assert selected.aov_texture_ids == {}
    assert FileOutputNode._select_render_pass(_aov_result(), "normal") is None


def test_file_output_can_select_motion_vector_aov_pass():
    source = _aov_result().with_updates(
        aov_texture_ids={"motion_vector": 40},
        aov_dimensions={"motion_vector": (640, 360)},
    )
    selected = FileOutputNode._select_render_pass(source, "motion_vector")

    assert selected is not None
    assert selected.texture_id == 40
    assert selected.width == 640
    assert selected.height == 360
    assert selected.aov_texture_ids == {}


def test_scene3d_exposes_exportable_aovs_as_image_ports():
    node = Scene3DNode(node_id="scene")
    port_names = {port.name for port in node.outputs}

    assert "display" in port_names
    assert {
        pass_name for pass_name in ("depth", "normal", "position", "uv",
                                    "object_id", "material_id",
                                    "motion_vector")
    }.issubset(port_names)
    assert all(port.data_type == "image" for port in node.outputs)


def test_scene3d_aov_port_result_keeps_one_pass_semantics():
    source = _aov_result()
    selected = Scene3DNode._make_aov_output_result(
        source, "object_id", 30, 640, 360)

    assert selected.texture_id == 30
    assert selected.aov_semantic == "object_id"
    assert selected.color_space == "data"
    assert selected.has_deep_data is False
    assert selected.aov_texture_ids == {"object_id": 30}


def test_export_renderer_selects_aov_without_mutating_source_result():
    source = _aov_result()
    selected = CompositeExportRenderer._select_render_pass(source, "object_id")

    assert selected is not None
    assert selected.texture_id == 30
    assert selected.width == 640
    assert selected.height == 360
    assert selected.aov_texture_ids == {}
    assert source.texture_id == 10
    assert source.aov_texture_ids["object_id"] == 30


def test_frame_output_forwards_selected_pass_to_frame_renderer():
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput,
    )

    frame_renderer = Mock()
    frame_renderer.render_comp_clip_frame.return_value = None
    output = CompositeRenderOutput()
    output._frame_renderer = frame_renderer

    output._render_one_frame(
        graph_data={"composite_id": "comp"},
        timeline_frame=12,
        edit_project=None,
        timeline_sequences=None,
        comp_clip=None,
        render_pass="depth",
    )

    assert frame_renderer.render_comp_clip_frame.call_args.kwargs["render_pass"] == "depth"


def test_auxiliary_sequence_conversion_does_not_apply_display_transfer():
    import numpy as np

    from src.features.composite.render.composite_render_output import (
        _convert_for_image_ext,
    )
    from src.shared.color.color_space_descriptor import REC709_VIDEO

    source = np.array([[[0.01, 0.25, 0.5]]], dtype=np.float32)
    encoded = _convert_for_image_ext(
        source, "png", output_descriptor=REC709_VIDEO, render_pass="depth")
    expected = (source * 255.0).astype(np.uint8)

    assert np.array_equal(encoded, expected)


def test_render_dialog_persists_available_render_pass(qapp):
    from src.features.composite.render.composite_render_output import CompositeMode
    from src.features.composite.ui.composite_render_dialog import CompositeRenderDialog

    dialog = CompositeRenderDialog(
        mode=CompositeMode.IMAGE,
        default_dir="/tmp",
        available_render_passes=("beauty", "depth", "object_id"),
    )
    try:
        pass_names = [
            dialog.render_pass_combo.itemData(index)
            for index in range(dialog.render_pass_combo.count())
        ]
        assert pass_names == ["beauty", "depth", "object_id"]

        dialog.render_pass_combo.setCurrentIndex(1)
        assert dialog.get_settings()["render_pass"] == "depth"
    finally:
        dialog.close()


def test_cache_key_changes_when_upstream_aov_contract_changes():
    node = NodeSnapshot(
        node_id="out",
        node_type="output",
        parameters={},
        disabled=False,
        input_ports=[("image", "image")],
        output_ports=[],
    )
    manager = CacheManager.__new__(CacheManager)
    beauty_only = TextureResult(texture_id=10, width=640, height=360, revision=7)
    with_aov = _aov_result()

    assert manager.compute_key(node, {"image": beauty_only}) != manager.compute_key(
        node, {"image": with_aov})


def _mock_multi_pass_export_renderer(source: TextureResult):
    renderer = CompositeExportRenderer.__new__(CompositeExportRenderer)
    renderer._persistent = False
    renderer._ensure_gpu_resources = Mock()
    renderer._sync_render_nodes = Mock()
    renderer._evaluate_snapshot = Mock(return_value=source)
    renderer.cleanup = Mock()
    renderer._readback = Mock(side_effect=["beauty-data", "depth-data", "id-data"])
    return renderer


def test_export_renderer_evaluates_once_for_multiple_aov_passes():
    renderer = _mock_multi_pass_export_renderer(_aov_result())

    result = renderer.render_and_readback_passes(
        object(), ("beauty", "depth", "object_id"))

    assert result.error is None
    assert result.image == "beauty-data"
    assert result.passes == {
        "beauty": "beauty-data",
        "depth": "depth-data",
        "object_id": "id-data",
    }
    renderer._evaluate_snapshot.assert_called_once()
    assert renderer._readback.call_count == 3
    renderer.cleanup.assert_called_once()


def test_export_renderer_multi_pass_reports_missing_aov_without_partial_payload():
    renderer = _mock_multi_pass_export_renderer(_aov_result())

    result = renderer.render_and_readback_passes(
        object(), ("beauty", "normal"))

    assert result.image is None
    assert result.passes == {}
    assert "normal" in (result.error or "")
    renderer._evaluate_snapshot.assert_called_once()
    renderer._readback.assert_not_called()


def test_preflight_checks_enabled_model3d_resource_path():
    report = run_preflight_data({
        "nodes": [{
            "id": "model",
            "type": "model3d",
            "parameters": {"use_imported_model": True, "model_path": ""},
        }],
        "connections": [],
    }, check_fs=False)

    assert not report.ok
    assert any(issue.code == "source.missing_path" for issue in report.errors)


def _scene_to_file_output_graph(render_pass: str, **scene_settings):
    return {
        "nodes": [
            {"id": "scene", "type": "scene3d", "parameters": {}},
            {"id": "write", "type": "file_output",
             "parameters": {"file_path": "/tmp/out.exr",
                             "aov_pass": render_pass}},
        ],
        "connections": [{
            "src_node_id": "scene", "src_port_name": "display",
            "dst_node_id": "write", "dst_port_name": "image",
        }],
        "viewer_state": {"default_3d_scene": scene_settings},
    }


def test_preflight_blocks_unknown_render_pass_in_export_mode():
    report = run_preflight_data(
        _scene_to_file_output_graph("diffuse"),
        mode=MODE_EXPORT,
        check_fs=False,
    )

    assert not report.ok
    issue = next(item for item in report.issues
                 if item.code == "render_pass.unsupported")
    assert issue.severity == "error"
    assert issue.node_id == "write"


def test_preflight_blocks_disabled_scene_aov_in_export_mode():
    report = run_preflight_data(
        _scene_to_file_output_graph("position", aov_position=False),
        mode=MODE_EXPORT,
        check_fs=False,
    )

    assert not report.ok
    assert any(item.code == "render_pass.disabled"
               and item.severity == "error"
               for item in report.issues)


def test_preflight_accepts_enabled_scene_aov():
    report = run_preflight_data(
        _scene_to_file_output_graph("position", aov_position=True),
        mode=MODE_EXPORT,
        check_fs=False,
    )

    assert report.ok
    assert not any(item.code.startswith("render_pass.")
                   for item in report.issues)


def test_preflight_accepts_enabled_uv_aov():
    report = run_preflight_data(
        _scene_to_file_output_graph("uv", aov_uv=True),
        mode=MODE_EXPORT,
        check_fs=False,
    )

    assert report.ok
    assert not any(item.code.startswith("render_pass.")
                   for item in report.issues)


def test_preflight_accepts_enabled_material_id_aov():
    report = run_preflight_data(
        _scene_to_file_output_graph("material_id", aov_material_id=True),
        mode=MODE_EXPORT,
        check_fs=False,
    )

    assert report.ok
    assert not any(item.code.startswith("render_pass.")
                   for item in report.issues)


def test_preflight_only_warns_for_invalid_render_pass_in_preview():
    report = run_preflight_data(
        _scene_to_file_output_graph("diffuse"),
        check_fs=False,
    )

    issue = next(item for item in report.issues
                 if item.code == "render_pass.unsupported")
    assert report.ok
    assert issue.severity == "warning"


def test_preflight_blocks_disabled_connected_scene3d_aov_port():
    graph = _scene_to_file_output_graph("beauty", aov_normal=False)
    graph["connections"][0]["src_port_name"] = "normal"

    report = run_preflight_data(graph, mode=MODE_EXPORT, check_fs=False)

    assert not report.ok
    assert any(item.code == "render_pass.port_disabled"
               and item.node_id == "scene"
               and item.severity == "error"
               for item in report.issues)


def test_auxiliary_propagation_only_applies_to_same_size_unary_outputs():
    source = _aov_result()
    plain_output = TextureResult(texture_id=99, width=640, height=360, revision=8)
    propagated = BaseEvaluatorMixin._propagate_auxiliary_metadata(
        {"image": source}, {"result": plain_output})
    assert propagated["result"].depth_texture_id == 20
    assert propagated["result"].aov_texture_ids == source.aov_texture_ids
