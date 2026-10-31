"""scene_graph 场景图单元测试（阶段五）。

覆盖：flatten_scene 父子变换链、normalize_scene 居中缩放、
四元数 slerp、动画采样与循环。纯 numpy，无 GL/PyQt 依赖，可独立运行。
"""
import numpy as np
import pytest

from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d import primitives as prim

pytestmark = pytest.mark.composite_core


def _cube_node(name, translate, scale=1.0, material=None):
    """造一个带平移的 cube SceneNode。"""
    local = np.eye(4, dtype=np.float32)
    local[0, 3] = float(translate[0])
    local[1, 3] = float(translate[1])
    local[2, 3] = float(translate[2])
    return sg.SceneNode(name=name, mesh=prim.cube(scale), material=material,
                        local_transform=local)


class TestFlattenScene:
    def test_single_node_world_equals_local(self):
        node = _cube_node("solo", (1.0, 2.0, 3.0))
        flat = sg.flatten_scene(node)
        assert len(flat) == 1
        origin = flat[0].world_matrix @ np.array([0, 0, 0, 1], dtype=np.float32)
        assert np.allclose(origin[:3], [1, 2, 3], atol=1e-5)

    def test_parent_child_chain(self):
        """父子联动：child world = parent_local @ child_local。"""
        parent = _cube_node("parent", (5.0, 0.0, 0.0))
        child = _cube_node("child", (0.0, 2.0, 0.0))
        parent.children.append(child)
        flat = sg.flatten_scene(parent)
        assert len(flat) == 2
        names = {f.node_name for f in flat}
        assert names == {"parent", "child"}
        child_fm = next(f for f in flat if f.node_name == "child")
        origin = child_fm.world_matrix @ np.array([0, 0, 0, 1], dtype=np.float32)
        # 期望 (5,2,0) = parent.translate(5,0,0) @ child.translate(0,2,0)
        assert np.allclose(origin[:3], [5, 2, 0], atol=1e-5)

    def test_three_level_chain(self):
        """三层嵌套：累乘。"""
        gp = _cube_node("gp", (1.0, 0.0, 0.0))
        p = _cube_node("p", (2.0, 0.0, 0.0))
        c = _cube_node("c", (3.0, 0.0, 0.0))
        p.children.append(c)
        gp.children.append(p)
        flat = sg.flatten_scene(gp)
        c_fm = next(f for f in flat if f.node_name == "c")
        origin = c_fm.world_matrix @ np.array([0, 0, 0, 1], dtype=np.float32)
        assert np.allclose(origin[:3], [6, 0, 0], atol=1e-5)  # 1+2+3

    def test_non_mesh_node_passes_transform(self):
        """非叶子节点（无 mesh）只传递变换。"""
        empty = sg.SceneNode(name="empty", local_transform=sg.mat4_identity())
        empty.local_transform[0, 3] = 4.0
        child = _cube_node("c", (1.0, 0.0, 0.0))
        empty.children.append(child)
        flat = sg.flatten_scene(empty)
        assert len(flat) == 1
        origin = flat[0].world_matrix @ np.array([0, 0, 0, 1], dtype=np.float32)
        assert np.allclose(origin[:3], [5, 0, 0], atol=1e-5)


class TestNormalizeScene:
    def test_centers_and_scales(self):
        node = _cube_node("big", (5.0, 5.0, 5.0), scale=10.0)
        root = sg.SceneGraphRoot(root=node)
        sg.normalize_scene(root, target_size=2.0)
        bmin, bmax = sg.compute_bbox(root.root)
        extent = bmax - bmin
        # 归一化后最长边 ≈ 2.0
        assert abs(float(np.max(extent)) - 2.0) < 0.1
        # 居中（中心 ≈ 原点附近，归一化把中心移到原点）
        center = (bmin + bmax) * 0.5
        assert np.allclose(center, [0, 0, 0], atol=0.1)

    def test_degenerate_no_crash(self):
        """退化几何（无顶点）不应崩。"""
        empty = sg.SceneNode(name="e")
        root = sg.SceneGraphRoot(root=empty)
        sg.normalize_scene(root)  # 不应抛


class TestQuaternionSlerp:
    def test_identity_to_180z(self):
        q0 = np.array([0, 0, 0, 1], dtype=np.float32)  # identity
        q1 = np.array([0, 0, 1, 0], dtype=np.float32)  # 180° Z
        mid = sg._slerp(q0, q1, 0.5)
        # 中点应是 90° Z = (0,0, sin45, cos45)
        assert np.allclose(mid, [0, 0, 0.7071, 0.7071], atol=1e-3)
        # 单位长度
        assert abs(float(np.linalg.norm(mid)) - 1.0) < 1e-4

    def test_endpoints(self):
        q0 = np.array([0, 0, 0, 1], dtype=np.float32)
        q1 = np.array([0, 0, 1, 0], dtype=np.float32)
        assert np.allclose(sg._slerp(q0, q1, 0.0), q0, atol=1e-5)
        assert np.allclose(sg._slerp(q0, q1, 1.0), q1, atol=1e-5)

    def test_shortest_path(self):
        """双覆盖：dot<0 应取最短路径。

        用 dot 明确为负的一对（不是 dot=0 的退化情形）。
        q1=(0,0,-1,0) 与 q0=(0,0,0,1) 的 dot=0（退化），故这里改用
        两个明确反向的四元数：q0=identity, q1≈绕 Z 旋转 170°（dot 略正），
        与其负数版本 dot<0，验证 slerp 取反后走最短弧。
        """
        q0 = np.array([0, 0, 0, 1], dtype=np.float32)        # identity
        # 绕 Z 旋转 ~170°：sin(85°),cos(85°) ≈ (0.996, 0.087)
        q1_pos = np.array([0, 0, 0.996, 0.087], dtype=np.float32)
        q1_neg = -q1_pos  # 双覆盖等价但 dot<0
        assert float(np.dot(q0, q1_neg)) < 0  # 前置：确实 dot<0
        mid = sg._slerp(q0, q1_neg, 0.5)
        # 取反后走最短弧，中点 z 应为正
        assert mid[2] > 0

    def test_quat_to_mat4_90z(self):
        q = np.array([0, 0, 0.7071, 0.7071], dtype=np.float32)  # 90° Z
        m = sg.quat_to_mat4(q)
        # 90° Z 旋转：x→y, y→-x
        assert np.allclose(m[0, 0], 0.0, atol=1e-2)
        assert np.allclose(m[1, 0], 1.0, atol=1e-2)
        assert np.allclose(m[0, 1], -1.0, atol=1e-2)


class TestAnimationSampling:
    def _clip(self):
        ch = sg.AnimationChannel(
            node_name="node",
            times=np.array([0.0, 1.0], dtype=np.float32),
            translations=np.array([[0, 0, 0], [0, 10, 0]], dtype=np.float32),
            rotations=np.array([[0, 0, 0, 1], [0, 0, 0, 1]], dtype=np.float32),
            scales=np.array([[1, 1, 1], [1, 1, 1]], dtype=np.float32),
        )
        return sg.AnimationClip(name="a", duration=1.0, channels=[ch])

    def test_midpoint_interpolation(self):
        state = sg.sample_animation(self._clip(), 0.5)
        assert np.allclose(state["node"][1, 3], 5.0, atol=1e-4)  # T.y=5

    def test_loop_wrap(self):
        """时间超过 duration 应循环。"""
        state = sg.sample_animation(self._clip(), 1.5)
        # 1.5 % 1.0 = 0.5 → T.y 应为 5.0
        assert np.allclose(state["node"][1, 3], 5.0, atol=1e-4)

    def test_near_end_extrapolates(self):
        """接近末尾（duration 前）应接近末尾关键帧值。"""
        state = sg.sample_animation(self._clip(), 0.999)
        assert np.allclose(state["node"][1, 3], 9.99, atol=0.05)

    def test_apply_to_scene(self):
        """动画世界矩阵应替换静态 local。"""
        node = _cube_node("node", (0.0, 0.0, 0.0))
        root = sg.SceneGraphRoot(root=node)
        sampled = sg.sample_animation(self._clip(), 0.5)
        worlds = sg.apply_animation_to_scene(root.root, sampled)
        # node 被 anim 平移到 y=5，world 矩阵 T.y 应为 5
        assert np.allclose(worlds["node"][1, 3], 5.0, atol=1e-4)


class TestSkinData:
    def test_bone_count(self):
        skin = sg.SkinData(
            bone_names=["b0", "b1", "b2"],
            inverse_bind_matrices=np.tile(np.eye(4, dtype=np.float32), (3, 1, 1)),
            bone_indices=np.zeros((4, 4), dtype=np.uint32),
            bone_weights=np.zeros((4, 4), dtype=np.float32),
        )
        assert skin.bone_count == 3
