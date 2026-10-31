"""Shared CPU/GPU skinning math for the composite 3D pipeline.

The beauty, depth, shadow, AOV and motion-vector passes all consume the same
``joint_world @ inverse_bind`` convention.  Deformer3D also needs that
convention when it must bake a posed mesh before applying a CPU deformer.  Keep
the matrix construction and CPU reference implementation here so the two
paths cannot silently drift apart.

This module is deliberately free of ModernGL objects.  It is safe to use in
unit tests and on the render thread before a GPU uniform upload.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .scene_graph import MAX_BONES


class SkinningError(ValueError):
    """Raised when a mesh or skin cannot be evaluated safely."""


def validate_skinning_contract(
    mesh: Any,
    skin: Any,
    max_bones: int = MAX_BONES,
) -> tuple[str, ...]:
    """Return stable diagnostics for inputs shared by CPU and GPU skinning.

    The renderer deliberately keeps rendering possible when an imported file
    contains a malformed optional skin. These codes let the import report and
    render diagnostics explain when the shader will ignore invalid influences
    or truncate a skeleton, instead of silently producing a wrong pose.
    """
    if not bool(getattr(mesh, "is_skinned", False)):
        return ()
    diagnostics: list[str] = []
    if skin is None:
        return ("skinning.missing_skin",)
    try:
        bone_indices = np.asarray(mesh.bone_indices)
        bone_weights = np.asarray(mesh.bone_weights, dtype=np.float32)
        vertices = np.asarray(mesh.vertices)
    except (AttributeError, TypeError, ValueError):
        return ("skinning.attributes_invalid",)
    if (vertices.ndim != 2 or vertices.shape[1] != 3
            or bone_indices.shape != (vertices.shape[0], 4)
            or bone_weights.shape != (vertices.shape[0], 4)):
        return ("skinning.attributes_invalid",)
    try:
        inverse_bind = np.asarray(
            skin.inverse_bind_matrices, dtype=np.float32).reshape(-1, 4, 4)
    except (AttributeError, TypeError, ValueError):
        return ("skinning.inverse_bind_invalid",)
    bone_count = min(inverse_bind.shape[0], len(getattr(skin, "bone_names", ())))
    if bone_count <= 0:
        return ("skinning.no_usable_bones",)
    if bone_count > max(1, int(max_bones)):
        diagnostics.append("skinning.bones_truncated")
    positive = bone_weights > 0.0
    if not np.isfinite(bone_weights).all() or (bone_weights < 0.0).any():
        diagnostics.append("skinning.weights_invalid")
    if np.any(positive & ((bone_indices < 0) | (bone_indices >= bone_count))):
        diagnostics.append("skinning.influence_index_ignored")
    positive_sums = bone_weights.sum(axis=1)
    weighted_rows = (np.abs(bone_weights) > 1.0e-8).any(axis=1)
    if (weighted_rows.any()
            and not np.allclose(positive_sums[weighted_rows], 1.0, atol=1.0e-3)):
        diagnostics.append("skinning.weights_not_normalized")
    return tuple(diagnostics)


def compute_skin_joint_matrices(
    skin: Any,
    current_worlds: Mapping[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Build the fixed-size joint matrix array used by the skinning shader.

    ``current_worlds`` contains sampled animation world matrices.  A missing
    animation channel deliberately falls back to ``inverse(IBM)`` so that the
    corresponding joint remains in bind pose.  The output is always
    ``(MAX_BONES, 4, 4)`` and is padded with identity matrices.
    """
    if skin is None:
        raise SkinningError("skinned mesh is missing SkinData")

    try:
        inverse_bind = np.asarray(
            skin.inverse_bind_matrices, dtype=np.float32).reshape(-1, 4, 4)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SkinningError("inverse bind matrices must have shape (B, 4, 4)") from exc

    bone_names = list(getattr(skin, "bone_names", ()))
    bone_count = min(int(inverse_bind.shape[0]), len(bone_names), MAX_BONES)
    if bone_count <= 0:
        raise SkinningError("skinned mesh has no usable bones")

    identity = np.eye(4, dtype=np.float32)
    joint_matrices = np.broadcast_to(
        identity, (MAX_BONES, 4, 4)).copy()
    worlds = current_worlds or {}
    for bone_index in range(bone_count):
        inverse = inverse_bind[bone_index]
        current_world = worlds.get(str(bone_names[bone_index]))
        if current_world is None:
            try:
                current_world = np.linalg.inv(inverse).astype(np.float32)
            except np.linalg.LinAlgError:
                current_world = identity
        try:
            current_world = np.asarray(current_world, dtype=np.float32).reshape(4, 4)
        except (TypeError, ValueError) as exc:
            raise SkinningError(
                f"current world matrix for bone {bone_names[bone_index]!r} is invalid"
            ) from exc
        joint_matrices[bone_index] = current_world @ inverse
    return joint_matrices


def skin_mesh_cpu(
    mesh: Any,
    skin: Any,
    current_worlds: Mapping[str, np.ndarray] | None = None,
) -> Any:
    """Return a posed, ordinary ``MeshData`` using the GPU shader contract.

    The implementation is the CPU reference for GPU skinning: positions use
    homogeneous transforms, normals use the upper-left 3x3 transform, and
    zero-weight vertices remain unchanged.  Bone attributes are intentionally
    removed from the result because a Deformer3D output must not be skinned a
    second time by Scene3D.
    """
    from .primitives import MeshData

    if not getattr(mesh, "is_skinned", False) or skin is None:
        raise SkinningError("skinned mesh is missing SkinData")

    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    normals_input = np.asarray(mesh.normals, dtype=np.float32)
    vertex_count = int(vertices.shape[0])
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        raise SkinningError("mesh vertices must have shape (N, 3)")
    if normals_input.shape != (vertex_count, 3):
        raise SkinningError("mesh normals must have shape (N, 3)")

    try:
        bone_indices = np.asarray(mesh.bone_indices, dtype=np.int64)
        bone_weights = np.asarray(mesh.bone_weights, dtype=np.float32)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SkinningError("skinning attributes must have shape (N, 4)") from exc
    if (bone_indices.shape != (vertex_count, 4)
            or bone_weights.shape != (vertex_count, 4)):
        raise SkinningError("skinning attributes must have shape (N, 4)")

    try:
        inverse_bind = np.asarray(
            skin.inverse_bind_matrices, dtype=np.float32).reshape(-1, 4, 4)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SkinningError("inverse bind matrices must have shape (B, 4, 4)") from exc
    bone_names = list(getattr(skin, "bone_names", ()))
    bone_count = min(int(inverse_bind.shape[0]), len(bone_names), MAX_BONES)
    if bone_count <= 0:
        raise SkinningError("skinned mesh has no usable bones")
    joint_matrices = compute_skin_joint_matrices(skin, current_worlds)

    valid = (bone_indices >= 0) & (bone_indices < bone_count)
    safe_indices = np.clip(bone_indices, 0, MAX_BONES - 1)
    transforms = joint_matrices[safe_indices]
    weights = bone_weights * valid.astype(np.float32)

    homogeneous = np.concatenate(
        (vertices, np.ones((vertex_count, 1), dtype=np.float32)), axis=1)
    transformed_positions = np.einsum(
        "nkim,nm->nki", transforms, homogeneous)
    positions = np.sum(
        transformed_positions[:, :, :3] * weights[:, :, None], axis=1)

    normal_transforms = transforms[:, :, :3, :3]
    transformed_normals = np.einsum(
        "nkij,nj->nki", normal_transforms, normals_input)
    normals = np.sum(transformed_normals * weights[:, :, None], axis=1)
    weight_sum = weights.sum(axis=1)
    unskinned = weight_sum <= 1.0e-8
    positions[unskinned] = vertices[unskinned]
    normals[unskinned] = normals_input[unskinned]

    normal_length = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(
        normals,
        np.maximum(normal_length, 1.0e-8),
        out=np.zeros_like(normals),
        where=normal_length > 1.0e-8,
    )
    normals[unskinned] = normals_input[unskinned]
    return MeshData(
        vertices=positions,
        normals=normals,
        uvs=np.asarray(mesh.uvs, dtype=np.float32).copy(),
        indices=np.asarray(mesh.indices, dtype=np.uint32).copy(),
    )


__all__ = [
    "SkinningError",
    "validate_skinning_contract",
    "compute_skin_joint_matrices",
    "skin_mesh_cpu",
]
