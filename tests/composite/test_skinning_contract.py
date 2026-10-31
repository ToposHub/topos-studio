"""P1-24 CPU/GPU skinning contract tests."""

import os
import sys
import inspect

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d.scene_graph import SkinData
from src.features.composite.render3d.skinning import (
    SkinningError,
    compute_skin_joint_matrices,
    skin_mesh_cpu,
    validate_skinning_contract,
)


def _translation(x: float, y: float, z: float = 0.0) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float32)
    matrix[:3, 3] = (x, y, z)
    return matrix


def _make_two_bone_mesh() -> tuple[prim.MeshData, SkinData]:
    mesh = prim.MeshData(
        vertices=np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ], dtype=np.float32),
        normals=np.tile([0.0, 0.0, 1.0], (3, 1)),
        uvs=np.zeros((3, 2), dtype=np.float32),
        indices=np.array([0, 1, 2], dtype=np.uint32),
        bone_indices=np.array([
            [0, 1, 0, 0],
            [1, 0, 0, 0],
            [0, 0, 0, 0],
        ], dtype=np.uint32),
        bone_weights=np.array([
            [0.5, 0.5, 0.0, 0.0],
            [1.0, 0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0, 0.0],
        ], dtype=np.float32),
    )
    skin = SkinData(
        bone_names=["root", "arm"],
        inverse_bind_matrices=np.stack([
            np.eye(4, dtype=np.float32),
            np.eye(4, dtype=np.float32),
        ]),
        bone_indices=mesh.bone_indices,
        bone_weights=mesh.bone_weights,
    )
    return mesh, skin


def test_cpu_skinning_uses_multi_bone_joint_matrices():
    mesh, skin = _make_two_bone_mesh()
    worlds = {"root": _translation(2.0, 0.0), "arm": _translation(0.0, 4.0)}

    matrices = compute_skin_joint_matrices(skin, worlds)
    posed = skin_mesh_cpu(mesh, skin, worlds)

    assert matrices.shape == (64, 4, 4)
    assert np.allclose(posed.vertices[0], [1.0, 2.0, 0.0])
    assert np.allclose(posed.vertices[1], [1.0, 4.0, 0.0])
    assert np.allclose(posed.vertices[2], [2.0, 1.0, 0.0])
    assert not posed.is_skinned


def test_missing_animation_channel_stays_in_bind_pose():
    mesh, skin = _make_two_bone_mesh()
    posed = skin_mesh_cpu(mesh, skin, {"root": _translation(2.0, 0.0)})

    # arm has identity IBM, so a missing channel is exactly bind pose.
    assert np.allclose(posed.vertices[1], [1.0, 0.0, 0.0])


def test_invalid_skinning_attributes_raise_stable_error():
    mesh, skin = _make_two_bone_mesh()
    mesh.bone_weights = np.zeros((3, 3), dtype=np.float32)

    with pytest.raises(SkinningError, match=r"shape \(N, 4\)"):
        skin_mesh_cpu(mesh, skin, {})


def test_skinning_contract_reports_truncation_bad_indices_and_weights():
    mesh, skin = _make_two_bone_mesh()
    skin.bone_names.extend(f"extra-{index}" for index in range(64))
    skin.inverse_bind_matrices = np.concatenate(
        (skin.inverse_bind_matrices,
         np.tile(np.eye(4, dtype=np.float32), (64, 1, 1))), axis=0)
    mesh.bone_indices[0, 0] = 99
    mesh.bone_weights[0, 0] = 0.25
    diagnostics = validate_skinning_contract(mesh, skin)
    assert diagnostics == (
        "skinning.bones_truncated",
        "skinning.influence_index_ignored",
        "skinning.weights_not_normalized",
    )


def test_skinning_contract_reports_missing_skin_without_throwing():
    mesh, _skin = _make_two_bone_mesh()
    assert validate_skinning_contract(mesh, None) == ("skinning.missing_skin",)


def test_scene3d_uses_gpu_skinning_uniform_upload_for_all_required_passes():
    from src.features.composite.nodes.scene3d_node import Scene3DNode

    for method_name in (
        "_do_evaluate", "_render_depth_pass", "_render_aov_pass",
        "_render_motion_pass", "_render_id_pass",
    ):
        method = getattr(Scene3DNode, method_name, None)
        assert method is not None
        assert "_upload_bone_matrices" in inspect.getsource(method)


class _Uniform:
    def __init__(self):
        self.value = None


class _Program(dict):
    pass


def test_scene3d_gpu_upload_uses_same_joint_matrix_contract():
    _, skin = _make_two_bone_mesh()
    worlds = {"root": _translation(2.0, 0.0), "arm": _translation(0.0, 4.0)}
    program = _Program(u_bone_matrices=_Uniform())
    mesh_gl = {"skin": skin}

    Scene3DNode._upload_bone_matrices(
        object(), program, mesh_gl, worlds)

    uploaded = np.asarray(program["u_bone_matrices"].value, dtype=np.float32)
    expected = compute_skin_joint_matrices(skin, worlds)
    assert uploaded.shape == (64, 16)
    assert np.allclose(uploaded.reshape(64, 4, 4), expected, atol=1.0e-6)
