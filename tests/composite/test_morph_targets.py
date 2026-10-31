"""P1-24 morph/blend-shape data path tests."""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d.model_loader import (
    _convert_assimp_mesh,
    _convert_trimesh_geometry,
    _finalize_report,
)
from src.features.composite.render3d.model_import_report import (
    LOADER_BACKEND_TRIMESH,
)
from src.features.composite.render3d.morph import (
    apply_morph_targets,
    decode_morph_weights,
    encode_morph_weights,
)


def _base_mesh(**kwargs) -> prim.MeshData:
    return prim.MeshData(
        vertices=np.asarray([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ], dtype=np.float32),
        normals=np.tile([0.0, 0.0, 1.0], (3, 1)),
        uvs=np.zeros((3, 2), dtype=np.float32),
        indices=np.asarray([0, 1, 2], dtype=np.uint32),
        **kwargs,
    )


def test_apply_morph_targets_updates_positions_and_normals_without_mutating_source():
    mesh = _base_mesh(
        morph_targets={"smile": np.asarray([
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 0.5],
            [0.0, 0.0, 0.0],
        ], dtype=np.float32)},
        morph_normals={"smile": np.asarray([
            [0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ], dtype=np.float32)},
    )
    result = apply_morph_targets(mesh, {"smile": 0.5})

    assert np.allclose(result.vertices[0], [0.0, 0.0, 0.5])
    assert np.allclose(result.vertices[1], [1.0, 0.0, 0.25])
    assert np.allclose(np.linalg.norm(result.normals, axis=1), 1.0)
    assert result.morph_targets == {}
    assert np.allclose(mesh.vertices, [
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def test_morph_weight_document_is_deterministic_and_reports_invalid_values():
    weights, diagnostics = decode_morph_weights('{"b": 0.25, "a": 1}')
    assert diagnostics == ()
    assert list(weights) == ["a", "b"]
    assert encode_morph_weights(weights) == '{"a":1.0,"b":0.25}'

    _, diagnostics = decode_morph_weights('{"smile": "bad"}')
    assert diagnostics == ("model.morph_weights_invalid",)


def test_trimesh_metadata_targets_are_loaded_as_deltas():
    class FakeGeometry:
        vertices = np.asarray([
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        vertex_normals = np.tile([0.0, 0.0, 1.0], (3, 1))
        faces = np.asarray([[0, 1, 2]], dtype=np.uint32)
        visual = None
        metadata = {"morph_targets": {
            "smile": {"POSITION": np.asarray([
                [0.0, 0.0, 0.5], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]], dtype=np.float32),
            "NORMAL": np.zeros((3, 3), dtype=np.float32)},
        }}

    mesh = _convert_trimesh_geometry(FakeGeometry())
    assert mesh is not None
    assert mesh.has_morph_targets
    assert np.allclose(mesh.morph_targets["smile"][0], [0.0, 0.0, 0.5])


def test_assimp_anim_meshes_are_converted_to_deltas():
    class FakeAnimMesh:
        name = "blink"
        vertices = np.asarray([
            [0.0, 0.0, 0.25], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        normals = np.tile([0.0, 0.0, 1.0], (3, 1))

    class FakeMesh:
        vertices = np.asarray([
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        normals = np.tile([0.0, 0.0, 1.0], (3, 1))
        texcoords = None
        faces = np.asarray([[0, 1, 2]], dtype=np.uint32)
        animMeshes = [FakeAnimMesh()]

    mesh = _convert_assimp_mesh(FakeMesh())
    assert mesh is not None
    assert np.allclose(mesh.morph_targets["blink"][0], [0.0, 0.0, 0.25])


def test_import_report_counts_morph_targets():
    mesh = _base_mesh(morph_targets={"smile": np.zeros((3, 3), dtype=np.float32)})
    root = sg.SceneGraphRoot(root=sg.SceneNode(name="root", mesh=mesh))
    report = _finalize_report(root, LOADER_BACKEND_TRIMESH, "asset.glb")

    assert report.morph_target_count == 1
    assert report.summary_dict()["morph_target_count"] == 1
