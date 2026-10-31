"""
F6 骨架与蒙皮数据层测试。

覆盖：
- SkeletonData / BoneData 数据结构
- build_skeleton_from_joints：从 glTF joints 构建（rest_world 累乘）
- compute_joint_matrices：当前 pose → GPU joint matrices
- unique_bone_names：骨骼名去重
- build_skeleton_overlay：骨骼线段 + 关节点 buffer
- build_weight_colors：蒙皮权重热力色

纯 numpy 逻辑测试，无 GPU 依赖。
"""

import numpy as np
import pytest

from src.features.composite.render3d.scene_graph import SkinData, mat4_identity
from src.features.composite.render3d.skeleton import (
    MAX_BONES,
    BoneData,
    SkeletonData,
    _JointNode,
    build_all_bone_weights_summary,
    build_skeleton_from_joints,
    build_skeleton_overlay,
    build_weight_colors,
    compute_joint_matrices,
    rest_pose_worlds,
    unique_bone_names,
    weight_to_color,
)


# ──────────────────────────────────────────────────────────────────────
# BoneData / SkeletonData 基础结构
# ──────────────────────────────────────────────────────────────────────


class TestBoneData:
    def test_defaults(self):
        b = BoneData()
        assert b.name == ""
        assert b.index == 0
        assert b.parent_index == -1
        assert b.children_indices == []
        assert b.rest_local.shape == (4, 4)
        assert b.rest_world.shape == (4, 4)
        np.testing.assert_array_almost_equal(b.rest_local, np.eye(4))
        np.testing.assert_array_almost_equal(b.rest_world, np.eye(4))

    def test_post_init_float32_4x4(self):
        # 传入 list 也应规整成 float32 4x4
        m = np.eye(4, dtype=np.float64) * 2.0
        b = BoneData(rest_local=m, rest_world=m)
        assert b.rest_local.dtype == np.float32
        assert b.rest_world.dtype == np.float32
        assert b.rest_local.shape == (4, 4)


class TestSkeletonData:
    def test_empty_skeleton(self):
        skel = SkeletonData()
        assert skel.name == ""
        assert skel.bones == []
        assert skel.root_indices == []
        assert skel.bone_count == 0
        assert skel.is_empty is True

    def test_bone_count(self):
        skel = SkeletonData(bones=[BoneData(index=0), BoneData(index=1)])
        assert skel.bone_count == 2
        assert skel.is_empty is False

    def test_bone_index_by_name_found(self):
        skel = SkeletonData(bones=[
            BoneData(name="root", index=0),
            BoneData(name="spine", index=1),
        ])
        assert skel.bone_index_by_name("root") == 0
        assert skel.bone_index_by_name("spine") == 1

    def test_bone_index_by_name_not_found(self):
        skel = SkeletonData(bones=[BoneData(name="root")])
        assert skel.bone_index_by_name("missing") == -1

    def test_bone_index_by_name_duplicate_returns_first(self):
        skel = SkeletonData(bones=[
            BoneData(name="bone", index=0),
            BoneData(name="bone", index=1),
        ])
        assert skel.bone_index_by_name("bone") == 0

    def test_rest_world_matrices(self):
        skel = SkeletonData(bones=[
            BoneData(name="a", rest_world=np.eye(4) * 1.0),
            BoneData(name="b", rest_world=np.eye(4) * 2.0),
        ])
        out = skel.rest_world_matrices()
        assert out.shape == (2, 4, 4)
        assert out.dtype == np.float32
        np.testing.assert_array_almost_equal(out[0], np.eye(4))

    def test_rest_world_matrices_empty(self):
        skel = SkeletonData()
        out = skel.rest_world_matrices()
        assert out.shape == (0, 4, 4)


# ──────────────────────────────────────────────────────────────────────
# build_skeleton_from_joints：glTF joints → SkeletonData
# ──────────────────────────────────────────────────────────────────────


class TestBuildSkeletonFromJoints:
    def test_empty_joints(self):
        skel = build_skeleton_from_joints("skin0", [])
        assert skel.name == "skin0"
        assert skel.is_empty is True
        assert skel.bones == []

    def test_single_root_bone(self):
        joints = [_JointNode(name="root", local_matrix=np.eye(4))]
        skel = build_skeleton_from_joints("skin", joints)
        assert skel.bone_count == 1
        assert skel.root_indices == [0]
        assert skel.bones[0].name == "root"
        assert skel.bones[0].parent_index == -1
        # 根骨骼 rest_world == rest_local
        np.testing.assert_array_almost_equal(
            skel.bones[0].rest_world, skel.bones[0].rest_local)

    def test_parent_child_world_accumulation(self):
        """子骨骼的 rest_world = parent.rest_world @ child.rest_local。"""
        # parent 平移 (1,0,0)，child 相对 parent 平移 (0,2,0)
        parent_local = np.eye(4, dtype=np.float32)
        parent_local[0, 3] = 1.0
        child_local = np.eye(4, dtype=np.float32)
        child_local[1, 3] = 2.0

        joints = [
            _JointNode(name="parent", local_matrix=parent_local),
            _JointNode(name="child", local_matrix=child_local, parent_joint_index=0),
        ]
        skel = build_skeleton_from_joints("s", joints)

        assert skel.root_indices == [0]
        assert skel.bones[0].children_indices == [1]
        assert skel.bones[1].parent_index == 0

        # parent world translation = (1,0,0)
        np.testing.assert_array_almost_equal(
            skel.bones[0].rest_world[:3, 3], [1.0, 0.0, 0.0])
        # child world translation = (1,2,0)（parent world @ child local）
        np.testing.assert_array_almost_equal(
            skel.bones[1].rest_world[:3, 3], [1.0, 2.0, 0.0])

    def test_three_level_hierarchy(self):
        """三级链：root → spine → head，每级平移累乘。"""
        t = np.eye(4, dtype=np.float32)
        t[1, 3] = 1.0  # 每级沿 Y 上移 1 单位

        joints = [
            _JointNode(name="root", local_matrix=t),
            _JointNode(name="spine", local_matrix=t, parent_joint_index=0),
            _JointNode(name="head", local_matrix=t, parent_joint_index=1),
        ]
        skel = build_skeleton_from_joints("s", joints)

        np.testing.assert_array_almost_equal(skel.bones[0].rest_world[:3, 3], [0, 1, 0])
        np.testing.assert_array_almost_equal(skel.bones[1].rest_world[:3, 3], [0, 2, 0])
        np.testing.assert_array_almost_equal(skel.bones[2].rest_world[:3, 3], [0, 3, 0])

    def test_multiple_roots(self):
        """两个无父骨骼都是 root。"""
        joints = [
            _JointNode(name="r1", local_matrix=np.eye(4)),
            _JointNode(name="r2", local_matrix=np.eye(4)),
        ]
        skel = build_skeleton_from_joints("s", joints)
        assert sorted(skel.root_indices) == [0, 1]

    def test_children_indices_populated(self):
        joints = [
            _JointNode(name="root", local_matrix=np.eye(4)),
            _JointNode(name="c1", local_matrix=np.eye(4), parent_joint_index=0),
            _JointNode(name="c2", local_matrix=np.eye(4), parent_joint_index=0),
        ]
        skel = build_skeleton_from_joints("s", joints)
        assert skel.bones[0].children_indices == [1, 2]


# ──────────────────────────────────────────────────────────────────────
# compute_joint_matrices：当前 pose → GPU matrices
# ──────────────────────────────────────────────────────────────────────


def _make_simple_skeleton_and_skin():
    """构建 2 骨骼 + 对应 skin（ibm = rest_world 的逆）。"""
    parent_local = np.eye(4, dtype=np.float32)
    parent_local[0, 3] = 1.0
    child_local = np.eye(4, dtype=np.float32)
    child_local[1, 3] = 2.0
    joints = [
        _JointNode(name="parent", local_matrix=parent_local),
        _JointNode(name="child", local_matrix=child_local, parent_joint_index=0),
    ]
    skel = build_skeleton_from_joints("s", joints)

    # inverse bind = inverse of rest_world
    ibm = np.stack([
        np.linalg.inv(skel.bones[0].rest_world),
        np.linalg.inv(skel.bones[1].rest_world),
    ]).astype(np.float32)

    # 4 顶点，每顶点只受 bone 0 影响
    bone_indices = np.array([[0, 0, 0, 0]] * 4, dtype=np.uint32)
    bone_weights = np.array([[1, 0, 0, 0]] * 4, dtype=np.float32)
    skin = SkinData(
        bone_names=["parent", "child"],
        inverse_bind_matrices=ibm,
        bone_indices=bone_indices,
        bone_weights=bone_weights,
    )
    return skel, skin


class TestComputeJointMatrices:
    def test_empty_skeleton(self):
        skel = SkeletonData()
        skin = SkinData(
            bone_names=[],
            inverse_bind_matrices=np.zeros((0, 4, 4), dtype=np.float32),
            bone_indices=np.zeros((0, 4), dtype=np.uint32),
            bone_weights=np.zeros((0, 4), dtype=np.float32),
        )
        out = compute_joint_matrices(skel, skin, {})
        # 空骨架仍返回 MAX_BONES 个 identity（shader uniform 数组大小固定）
        assert out.shape == (MAX_BONES, 4, 4)
        for i in range(MAX_BONES):
            np.testing.assert_array_almost_equal(out[i], np.eye(4))

    def test_bind_pose_returns_identity(self):
        """bind pose（current = rest）时 joint_matrix = rest_world @ inv(rest_world) = I。"""
        skel, skin = _make_simple_skeleton_and_skin()
        worlds = rest_pose_worlds(skel)
        out = compute_joint_matrices(skel, skin, worlds)
        # 固定 MAX_BONES 行（前 2 行是骨骼，其余 identity 填充）
        assert out.shape == (MAX_BONES, 4, 4)
        # 前 2 根骨骼的 joint matrix 应接近单位矩阵（数值误差内）
        for i in range(2):
            np.testing.assert_array_almost_equal(out[i], np.eye(4), decimal=5)
        # 第 3+ 行应是 identity（未使用骨骼槽）
        for i in range(2, MAX_BONES):
            np.testing.assert_array_almost_equal(out[i], np.eye(4))

    def test_translated_pose(self):
        """骨骼平移后，joint_matrix 应反映该平移。"""
        skel, skin = _make_simple_skeleton_and_skin()
        # 把 parent 沿 X 再移 1 单位（总平移 (2,0,0)）
        moved = np.eye(4, dtype=np.float32)
        moved[0, 3] = 2.0
        worlds = {"parent": moved, "child": skel.bones[1].rest_world}
        out = compute_joint_matrices(skel, skin, worlds)
        # parent 的 joint matrix translation 应为 (1,0,0)（移动量）
        np.testing.assert_array_almost_equal(out[0][:3, 3], [1.0, 0.0, 0.0])

    def test_missing_bone_uses_rest_world(self):
        """current_worlds 缺某骨骼时 fallback 到 rest_world（bind pose）。"""
        skel, skin = _make_simple_skeleton_and_skin()
        # 只给 parent，不给 child
        worlds = {"parent": skel.bones[0].rest_world}
        out = compute_joint_matrices(skel, skin, worlds)
        # child 用 rest_world → joint matrix ≈ identity
        np.testing.assert_array_almost_equal(out[1], np.eye(4), decimal=5)

    def test_max_bones_truncation(self):
        """骨骼数 > MAX_BONES 时截断到 MAX_BONES（输出固定 MAX_BONES 行）。"""
        joints = [_JointNode(name=f"b{i}", local_matrix=np.eye(4))
                  for i in range(MAX_BONES + 5)]
        skel = build_skeleton_from_joints("s", joints)
        ibm = np.stack([np.eye(4, dtype=np.float32)] * (MAX_BONES + 5))
        skin = SkinData(
            bone_names=[f"b{i}" for i in range(MAX_BONES + 5)],
            inverse_bind_matrices=ibm,
            bone_indices=np.zeros((1, 4), dtype=np.uint32),
            bone_weights=np.zeros((1, 4), dtype=np.float32),
        )
        out = compute_joint_matrices(skel, skin, rest_pose_worlds(skel))
        # 固定 MAX_BONES 行（超出骨骼被截断，不补 identity 因为本身就满）
        assert out.shape == (MAX_BONES, 4, 4)

    def test_output_always_max_bones_padded(self):
        """骨骼数 < MAX_BONES 时尾部用 identity 填充（保证 shader 数组大小固定）。"""
        skel, skin = _make_simple_skeleton_and_skin()  # 2 骨骼
        out = compute_joint_matrices(skel, skin, rest_pose_worlds(skel))
        # 固定 MAX_BONES 行
        assert out.shape == (MAX_BONES, 4, 4)
        # 第 3+ 行应是 identity（未使用槽位，顶点 bone_id 命中这些时不动）
        for i in range(2, MAX_BONES):
            np.testing.assert_array_almost_equal(out[i], np.eye(4))


# ──────────────────────────────────────────────────────────────────────
# unique_bone_names
# ──────────────────────────────────────────────────────────────────────


class TestUniqueBoneNames:
    def test_no_duplicates(self):
        names = ["root", "spine", "head"]
        assert unique_bone_names(names) == ["root", "spine", "head"]

    def test_duplicates_get_suffix(self):
        names = ["bone", "bone", "bone"]
        out = unique_bone_names(names)
        assert out == ["bone", "bone_2", "bone_3"]

    def test_mixed(self):
        names = ["a", "b", "a", "c", "b", "a"]
        out = unique_bone_names(names)
        assert out == ["a", "b", "a_2", "c", "b_2", "a_3"]

    def test_empty(self):
        assert unique_bone_names([]) == []

    def test_collision_with_existing_suffixed_name(self):
        """生成名与后续原始名冲突时继续递增（避免碰撞）。"""
        # ["Arm","Arm","Arm_2"]: 第二个 Arm 生成 "Arm_2"，
        # 但第三个原始名也是 "Arm_2" → 必须继续递增到 "Arm_3"
        names = ["Arm", "Arm", "Arm_2"]
        out = unique_bone_names(names)
        assert len(out) == len(set(out))  # 全部唯一
        assert out[0] == "Arm"

    def test_output_all_unique(self):
        """任意输入 → 输出全部唯一（属性测试）。"""
        import random
        random.seed(42)
        for _ in range(20):
            names = [random.choice(["bone", "joint", "Arm"]) for _ in range(10)]
            out = unique_bone_names(names)
            assert len(out) == len(set(out)), f"duplicates in {out}"


# ──────────────────────────────────────────────────────────────────────
# build_skeleton_overlay：线段 + 关节点
# ──────────────────────────────────────────────────────────────────────


class TestBuildSkeletonOverlay:
    def test_empty_skeleton(self):
        skel = SkeletonData()
        lines, joints, sizes = build_skeleton_overlay(skel)
        assert lines.shape == (0, 3)
        assert joints.shape == (0, 3)
        assert sizes.shape == (0,)

    def test_single_bone_no_lines(self):
        """单根骨骼（无父）没有线段（线段连接 parent→child）。"""
        skel = build_skeleton_from_joints("s", [
            _JointNode(name="root", local_matrix=np.eye(4)),
        ])
        lines, joints, sizes = build_skeleton_overlay(skel)
        assert lines.shape == (0, 3)
        assert joints.shape == (1, 3)
        assert sizes.shape == (1,)

    def test_two_bones_one_line(self):
        parent_local = np.eye(4, dtype=np.float32)
        parent_local[0, 3] = 1.0
        joints = [
            _JointNode(name="p", local_matrix=parent_local),
            _JointNode(name="c", local_matrix=np.eye(4), parent_joint_index=0),
        ]
        skel = build_skeleton_from_joints("s", joints)
        lines, joints_pos, sizes = build_skeleton_overlay(skel)

        # 1 条线段 = 2 个端点
        assert lines.shape == (2, 3)
        # parent 在 (1,0,0)，child 在 (1,0,0)（child local = identity）
        np.testing.assert_array_almost_equal(lines[0], [1.0, 0.0, 0.0])
        np.testing.assert_array_almost_equal(lines[1], [1.0, 0.0, 0.0])
        assert joints_pos.shape == (2, 3)

    def test_root_bone_larger_size(self):
        skel = build_skeleton_from_joints("s", [
            _JointNode(name="root", local_matrix=np.eye(4)),
        ])
        _, _, sizes = build_skeleton_overlay(skel, bone_size=0.05)
        # 根骨骼大小 = bone_size * 1.5
        np.testing.assert_array_almost_equal(sizes, [0.075])

    def test_current_worlds_override(self):
        """传入 current_worlds 时用当前 pose 位置而非 rest。"""
        skel = build_skeleton_from_joints("s", [
            _JointNode(name="root", local_matrix=np.eye(4)),
        ])
        moved = np.eye(4, dtype=np.float32)
        moved[2, 3] = 5.0
        _, joints, _ = build_skeleton_overlay(skel, current_worlds={"root": moved})
        np.testing.assert_array_almost_equal(joints[0], [0.0, 0.0, 5.0])


# ──────────────────────────────────────────────────────────────────────
# 权重热力色
# ──────────────────────────────────────────────────────────────────────


class TestWeightToColor:
    def test_zero_is_blue(self):
        c = weight_to_color(0.0)
        np.testing.assert_array_almost_equal(c, [0.0, 0.0, 1.0])

    def test_one_is_red(self):
        c = weight_to_color(1.0)
        np.testing.assert_array_almost_equal(c, [1.0, 0.0, 0.0])

    def test_half_is_green(self):
        c = weight_to_color(0.5)
        np.testing.assert_array_almost_equal(c, [0.0, 1.0, 0.0])

    def test_clamps_out_of_range(self):
        c_neg = weight_to_color(-0.5)
        c_hi = weight_to_color(1.5)
        np.testing.assert_array_almost_equal(c_neg, [0.0, 0.0, 1.0])
        np.testing.assert_array_almost_equal(c_hi, [1.0, 0.0, 0.0])


class TestBuildWeightColors:
    def test_empty_skin(self):
        skin = SkinData(
            bone_names=["b0"],
            inverse_bind_matrices=np.eye(4, dtype=np.float32).reshape(1, 4, 4),
            bone_indices=np.zeros((0, 4), dtype=np.uint32),
            bone_weights=np.zeros((0, 4), dtype=np.float32),
        )
        out = build_weight_colors(skin, bone_index=0)
        assert out.shape == (0, 3)

    def test_single_bone_full_weight(self):
        """4 顶点全部受 bone 0 影响，权重 1.0 → 全红。"""
        bone_indices = np.zeros((4, 4), dtype=np.uint32)
        bone_weights = np.zeros((4, 4), dtype=np.float32)
        bone_weights[:, 0] = 1.0  # 第一个 influence 权重 1.0
        skin = SkinData(
            bone_names=["b0"],
            inverse_bind_matrices=np.eye(4, dtype=np.float32).reshape(1, 4, 4),
            bone_indices=bone_indices,
            bone_weights=bone_weights,
        )
        out = build_weight_colors(skin, bone_index=0)
        assert out.shape == (4, 3)
        for i in range(4):
            np.testing.assert_array_almost_equal(out[i], [1.0, 0.0, 0.0])

    def test_no_influence_is_blue(self):
        """顶点不受 bone 1 影响 → 蓝色。"""
        bone_indices = np.zeros((2, 4), dtype=np.uint32)  # 全是 bone 0
        bone_weights = np.zeros((2, 4), dtype=np.float32)
        bone_weights[:, 0] = 1.0
        skin = SkinData(
            bone_names=["b0", "b1"],
            inverse_bind_matrices=np.stack([np.eye(4)] * 2).astype(np.float32),
            bone_indices=bone_indices,
            bone_weights=bone_weights,
        )
        out = build_weight_colors(skin, bone_index=1)
        assert out.shape == (2, 3)
        for i in range(2):
            np.testing.assert_array_almost_equal(out[i], [0.0, 0.0, 1.0])

    def test_partial_weight(self):
        """顶点受 bone 0 权重 0.5 → 绿色。"""
        bone_indices = np.array([[0, 1, 0, 0]], dtype=np.uint32)
        bone_weights = np.array([[0.5, 0.5, 0, 0]], dtype=np.float32)
        skin = SkinData(
            bone_names=["b0", "b1"],
            inverse_bind_matrices=np.stack([np.eye(4)] * 2).astype(np.float32),
            bone_indices=bone_indices,
            bone_weights=bone_weights,
        )
        out = build_weight_colors(skin, bone_index=0)
        assert out.shape == (1, 3)
        np.testing.assert_array_almost_equal(out[0], [0.0, 1.0, 0.0])


class TestBuildAllBoneWeightsSummary:
    def test_empty(self):
        skin = SkinData(
            bone_names=[],
            inverse_bind_matrices=np.zeros((0, 4, 4), dtype=np.float32),
            bone_indices=np.zeros((0, 4), dtype=np.uint32),
            bone_weights=np.zeros((0, 4), dtype=np.float32),
        )
        assert build_all_bone_weights_summary(skin) == {}

    def test_summary(self):
        bone_indices = np.array([[0, 1, 0, 0], [1, 0, 0, 0]], dtype=np.uint32)
        bone_weights = np.array([[0.7, 0.3, 0, 0], [0.4, 0.6, 0, 0]],
                                dtype=np.float32)
        skin = SkinData(
            bone_names=["b0", "b1"],
            inverse_bind_matrices=np.stack([np.eye(4)] * 2).astype(np.float32),
            bone_indices=bone_indices,
            bone_weights=bone_weights,
        )
        summary = build_all_bone_weights_summary(skin)
        # bone 0: 顶点0的0.7 + 顶点1的0.6 = 1.3
        assert abs(summary[0] - 1.3) < 1e-5
        # bone 1: 顶点0的0.3 + 顶点1的0.4 = 0.7
        assert abs(summary[1] - 0.7) < 1e-5
