"""Tests for the serialized composite asset dependency report."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.features.composite.core.asset_dependency import (
    AssetCollectError,
    AssetCollectService,
    AssetRelinkError,
    AssetRelinkService,
    ROLE_HDRI,
    ROLE_IES,
    ROLE_LUT,
    ROLE_MATERIAL_TEXTURE,
    ROLE_MEDIA,
    ROLE_MODEL,
    STATUS_MISSING,
    STATUS_MISSING_PATH,
    STATUS_RELINK_CANDIDATE,
    STATUS_UNVERIFIED,
    collect_asset_dependencies,
)
from src.features.composite.core.graph_preflight import run_preflight_data
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.undo_stack import UndoStack
from src.features.composite.nodes.model3d_node import Model3DNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode

pytestmark = pytest.mark.composite_core


def _node(node_id: str, node_type: str, **parameters: object) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "parameters": parameters,
        "disabled": False,
    }


def _graph(*nodes: dict) -> dict:
    return {"nodes": list(nodes), "connections": []}


def test_collects_direct_and_material_override_dependencies():
    with tempfile.TemporaryDirectory() as temp_dir:
        media_path = os.path.join(temp_dir, "plate.mov")
        model_path = os.path.join(temp_dir, "hero.gltf")
        lut_path = os.path.join(temp_dir, "show.cube")
        texture_path = os.path.join(temp_dir, "roughness.png")
        for path in (media_path, model_path, lut_path, texture_path):
            with open(path, "wb") as handle:
                handle.write(b"asset")

        overrides = json.dumps({
            "mat-body": {
                "slot_overrides": {
                    "roughness": {"path": texture_path, "uv_set": 0},
                },
            },
        })
        report = collect_asset_dependencies(_graph(
            _node("video", "video_frame", file_path=media_path),
            _node("model", "model3d", use_imported_model=True,
                  model_path=model_path, material_overrides=overrides),
            _node("grade", "lut", lut_path=lut_path),
        ))

    assert {item.role for item in report.references} == {
        ROLE_MEDIA, ROLE_MODEL, ROLE_LUT, ROLE_MATERIAL_TEXTURE,
    }
    assert not report.missing
    assert all(item.status == "resolved" for item in report.references)
    assert report.to_dict()["counts"]["total"] == 4


def test_missing_auxiliary_paths_are_reported_without_empty_optional_slots():
    report = collect_asset_dependencies(_graph(
        _node("light", "light3d", use_ibl=True, hdri_path="",
              ies_enabled=True, ies_profile=""),
        _node("scene", "scene3d", use_ibl=True, hdri_preset="__sky__"),
    ), check_fs=True)

    assert len(report.references) == 1
    reference = report.references[0]
    assert reference.role == ROLE_IES
    assert reference.status == STATUS_MISSING_PATH
    assert report.missing == (reference,)


def test_relative_path_resolution_reports_a_relink_candidate():
    with tempfile.TemporaryDirectory() as temp_dir:
        moved_dir = os.path.join(temp_dir, "assets")
        os.makedirs(moved_dir)
        moved_path = os.path.join(moved_dir, "plate.mov")
        with open(moved_path, "wb") as handle:
            handle.write(b"media")

        report = collect_asset_dependencies(
            _graph(_node("video", "video_frame", file_path="old/plate.mov")),
            project_dir=temp_dir,
            asset_search_roots=(moved_dir,),
        )

    reference = report.references[0]
    assert reference.role == ROLE_MEDIA
    assert reference.status == STATUS_RELINK_CANDIDATE
    assert reference.resolved_path == os.path.abspath(moved_path)
    assert report.relink_candidates == (reference,)


def test_check_fs_false_marks_paths_unverified():
    report = collect_asset_dependencies(
        _graph(_node("model", "model3d", use_imported_model=True,
                     model_path="missing/hero.gltf")),
        check_fs=False,
    )

    assert report.references[0].status == STATUS_UNVERIFIED
    assert not report.missing


def test_preflight_uses_asset_report_for_auxiliary_paths_and_lut_format():
    with tempfile.TemporaryDirectory() as temp_dir:
        bad_lut = os.path.join(temp_dir, "show.txt")
        with open(bad_lut, "wb") as handle:
            handle.write(b"not a lut")
        graph = _graph(
            _node("lut", "lut", lut_path=bad_lut),
            _node("light", "light3d", use_ibl=True,
                  hdri_path=os.path.join(temp_dir, "missing.hdr"),
                  ies_enabled=True, ies_profile=""),
        )
        report = run_preflight_data(graph, check_fs=True)

    assert {item.code for item in report.issues} >= {
        "asset.unsupported_format", "asset.file_not_found", "asset.missing_path",
    }
    assert len(report.asset_dependencies.by_role(ROLE_HDRI)) == 1
    assert report.asset_dependencies.by_role(ROLE_HDRI)[0].status == STATUS_MISSING
    assert report.asset_dependencies.by_role(ROLE_LUT)[0].status == "resolved"


def test_relink_service_applies_one_batch_and_undoes_it_as_one_action():
    with tempfile.TemporaryDirectory() as temp_dir:
        moved_dir = os.path.join(temp_dir, "moved")
        os.makedirs(moved_dir)
        moved_path = os.path.join(moved_dir, "plate.mov")
        with open(moved_path, "wb") as handle:
            handle.write(b"media")
        old_path = os.path.join(temp_dir, "old", "plate.mov")

        graph = NodeGraph()
        node = VideoFrameNode(node_id="video")
        node.set_parameter("file_path", old_path)
        graph.add_node(node)
        undo_stack = UndoStack()
        service = AssetRelinkService(graph, undo_stack=undo_stack)

        result = service.apply(
            {("video", "file_path"): moved_path},
            project_dir=temp_dir,
            asset_search_roots=(moved_dir,),
        )

        assert result.changed_count == 1
        assert graph.get_node("video").parameters["file_path"] == moved_path
        undo_stack.undo()
        assert graph.get_node("video").parameters["file_path"] == old_path
        undo_stack.redo()
        assert graph.get_node("video").parameters["file_path"] == moved_path


def test_relink_service_supports_material_override_slots():
    with tempfile.TemporaryDirectory() as temp_dir:
        moved_dir = os.path.join(temp_dir, "textures")
        os.makedirs(moved_dir)
        moved_path = os.path.join(moved_dir, "roughness.png")
        with open(moved_path, "wb") as handle:
            handle.write(b"texture")
        old_path = os.path.join(temp_dir, "old", "roughness.png")
        overrides = json.dumps({
            "mat-body": {
                "slot_overrides": {"roughness": {"path": old_path}},
            },
        })

        graph = NodeGraph()
        node = Model3DNode(node_id="model")
        node.set_parameter("use_imported_model", True)
        # The model itself is only needed to make the serialized node an
        # imported-model dependency; it is excluded from the replacement map.
        model_path = os.path.join(temp_dir, "hero.gltf")
        with open(model_path, "wb") as handle:
            handle.write(b"model")
        node.set_parameter("model_path", model_path)
        node.set_parameter("material_overrides", overrides)
        graph.add_node(node)
        service = AssetRelinkService(graph)

        parameter = (
            "material_overrides[mat-body].slot_overrides[roughness].path")
        result = service.apply(
            {("model", parameter): moved_path},
            project_dir=temp_dir,
            asset_search_roots=(moved_dir,),
        )

    assert result.changed_count == 1
    saved = json.loads(graph.get_node("model").parameters["material_overrides"])
    assert saved["mat-body"]["slot_overrides"]["roughness"]["path"] == moved_path


def test_relink_service_rejects_non_candidate_without_mutating_graph():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = os.path.join(temp_dir, "old.mov")
        candidate = os.path.join(temp_dir, "candidate.mov")
        for path in (old_path, candidate):
            with open(path, "wb") as handle:
                handle.write(b"media")
        graph = NodeGraph()
        node = VideoFrameNode(node_id="video")
        node.set_parameter("file_path", old_path)
        graph.add_node(node)

        service = AssetRelinkService(graph)
        with pytest.raises(AssetRelinkError):
            service.apply({("video", "file_path"): candidate})
        assert graph.get_node("video").parameters["file_path"] == old_path


def test_relink_service_can_store_candidate_as_project_relative_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        moved_dir = os.path.join(temp_dir, "assets")
        os.makedirs(moved_dir)
        moved_path = os.path.join(moved_dir, "plate.mov")
        with open(moved_path, "wb") as handle:
            handle.write(b"media")
        old_path = os.path.join(temp_dir, "old", "plate.mov")

        graph = NodeGraph()
        node = VideoFrameNode(node_id="video")
        node.set_parameter("file_path", old_path)
        graph.add_node(node)
        service = AssetRelinkService(graph)

        service.apply(
            {("video", "file_path"): moved_path},
            project_dir=temp_dir,
            asset_search_roots=(moved_dir,),
            store_relative_to_project=True,
        )

    assert graph.get_node("video").parameters["file_path"] == os.path.join(
        "assets", "plate.mov")


def test_collect_service_copies_dependencies_and_writes_portable_paths():
    with tempfile.TemporaryDirectory() as temp_dir:
        project_dir = os.path.join(temp_dir, "project")
        source_dir = os.path.join(temp_dir, "source")
        target_dir = os.path.join(project_dir, "assets")
        os.makedirs(source_dir)
        source_path = os.path.join(source_dir, "plate.mov")
        with open(source_path, "wb") as handle:
            handle.write(b"portable media")

        graph = NodeGraph()
        node = VideoFrameNode(node_id="video")
        node.set_parameter("file_path", source_path)
        graph.add_node(node)
        service = AssetCollectService(graph)

        plan = service.plan(
            target_dir, project_dir=project_dir,
            store_relative_to_project=True)
        assert plan.changed_count == 1
        assert plan.copies[0].source_path == os.path.abspath(source_path)
        assert plan.changes[0].new_path == os.path.join(
            "assets", "media", "plate.mov")

        result = service.apply(plan)
        collected_path = os.path.join(project_dir, plan.changes[0].new_path)
        assert result.copied_count == 1
        assert os.path.isfile(collected_path)
        assert graph.get_node("video").parameters["file_path"] == plan.changes[0].new_path


def test_collect_service_reuses_identical_content_and_supports_undo():
    with tempfile.TemporaryDirectory() as temp_dir:
        project_dir = os.path.join(temp_dir, "project")
        source_dir = os.path.join(temp_dir, "source")
        target_dir = os.path.join(project_dir, "assets")
        os.makedirs(source_dir)
        first_path = os.path.join(source_dir, "first.mov")
        with open(first_path, "wb") as handle:
            handle.write(b"same content")

        graph = NodeGraph()
        for node_id in ("first", "second"):
            node = VideoFrameNode(node_id=node_id)
            node.set_parameter("file_path", first_path)
            graph.add_node(node)
        undo_stack = UndoStack()
        service = AssetCollectService(graph, undo_stack=undo_stack)

        plan = service.plan(
            target_dir, project_dir=project_dir,
            store_relative_to_project=True)
        result = service.apply(plan)

        assert len(plan.copies) == 1
        assert result.copied_count == 1
        assert len(plan.changes) == 2
        assert len({change.new_path for change in plan.changes}) == 1
        undo_stack.undo()
        assert graph.get_node("first").parameters["file_path"] == first_path
        assert graph.get_node("second").parameters["file_path"] == first_path
        undo_stack.redo()
        assert graph.get_node("first").parameters["file_path"] in {
            change.new_path for change in plan.changes}


def test_collect_service_rejects_missing_dependencies_before_writing():
    with tempfile.TemporaryDirectory() as temp_dir:
        graph = NodeGraph()
        node = VideoFrameNode(node_id="video")
        node.set_parameter("file_path", os.path.join(temp_dir, "missing.mov"))
        graph.add_node(node)
        target_dir = os.path.join(temp_dir, "collected")

        with pytest.raises(AssetCollectError):
            AssetCollectService(graph).plan(target_dir)
        assert not os.path.exists(target_dir)
