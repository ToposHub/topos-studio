"""F13 非破坏变形器单测（master_plan §16 / §21.16）。

覆盖：
1. FieldSpec：linear/radial/box 权重计算、falloff 曲线、invert。
2. DeformerSpec：signature 稳定性。
3. Bend/Twist/Bulge/Taper/Stretch/Noise：形变数学正确性。
4. DeformerStack：串联执行、顺序依赖、enable/skip、非破坏（不改原 mesh）。
5. selection mask 局部影响。

纯 numpy，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d.fields import (
    FieldSpec, linear_field, radial_field, box_field,
    FALLOFF_NONE, FALLOFF_LINEAR, FALLOFF_SMOOTH,
)
from src.features.composite.render3d.deformers import (
    DeformerSpec, DeformerStack, DeformerResult,
    evaluate_deformer,
    DEFORMER_BEND, DEFORMER_TWIST, DEFORMER_BULGE,
    DEFORMER_TAPER, DEFORMER_STRETCH, DEFORMER_NOISE,
)

pytestmark = pytest.mark.composite_core


# ── FieldSpec ────────────────────────────────────────────────────────

class TestFieldSpec:
    def test_radial_center_strong_edge_weak(self):
        """径向场：中心权重=1，远处→0。"""
        f = radial_field(center=(0, 0, 0), radius=2.0)
        verts = np.array([[0, 0, 0], [2, 0, 0], [4, 0, 0]], dtype=np.float32)
        w = f.evaluate(verts)
        assert w[0] >= 0.99  # 中心
        assert w[1] < 0.05   # 边缘（半径=2）
        assert w[2] < 0.05   # 外

    def test_linear_along_axis(self):
        """线性场：沿 Y 轴，center 处=1，远处→0。"""
        f = linear_field(center=(0, 0, 0), axis=(0, 1, 0), range_=2.0)
        verts = np.array([[0, 0, 0], [0, 1, 0], [0, 2, 0]], dtype=np.float32)
        w = f.evaluate(verts)
        assert w[0] >= 0.99  # center
        assert w[1] > w[2]   # 近>远

    def test_box_inside_outside(self):
        """盒体场：盒内=1，盒外衰减。"""
        f = box_field(center=(0, 0, 0), size=(1, 1, 1))
        verts = np.array([[0, 0, 0], [3, 3, 3]], dtype=np.float32)
        w = f.evaluate(verts)
        assert w[0] >= 0.99  # 盒内
        assert w[1] < 0.1    # 盒外

    def test_invert(self):
        """反转权重。"""
        f = radial_field(center=(0, 0, 0), radius=1.0, invert=True)
        verts = np.array([[0, 0, 0], [2, 0, 0]], dtype=np.float32)
        w = f.evaluate(verts)
        assert w[0] < 0.1    # 中心反转后→弱
        assert w[1] > 0.9    # 远处反转后→强

    def test_axis_normalized(self):
        """axis 自动归一化。"""
        f = linear_field(axis=(0, 5, 0))
        assert abs(float(np.linalg.norm(f.axis)) - 1.0) < 1e-5

    def test_empty_vertices(self):
        f = radial_field()
        assert f.evaluate(np.zeros((0, 3), dtype=np.float32)).shape == (0,)

    def test_falloff_none_hard_cutoff(self):
        """falloff=none：硬切。"""
        f = radial_field(center=(0, 0, 0), radius=1.0, falloff=FALLOFF_NONE)
        verts = np.array([[0.5, 0, 0], [1.5, 0, 0]], dtype=np.float32)
        w = f.evaluate(verts)
        assert w[0] == 1.0   # 内（<radius）
        assert w[1] == 0.0   # 外（>radius）


# ── DeformerSpec signature ───────────────────────────────────────────

class TestDeformerSpecSignature:
    def test_same_spec_same_signature(self):
        s1 = DeformerSpec(deformer_type=DEFORMER_BEND, strength=0.5, axis="y")
        s2 = DeformerSpec(deformer_type=DEFORMER_BEND, strength=0.5, axis="y")
        assert s1.signature() == s2.signature()

    def test_different_strength_different_signature(self):
        s1 = DeformerSpec(strength=0.5)
        s2 = DeformerSpec(strength=0.8)
        assert s1.signature() != s2.signature()

    def test_field_in_signature(self):
        s1 = DeformerSpec(field=radial_field(radius=1.0))
        s2 = DeformerSpec(field=radial_field(radius=2.0))
        assert s1.signature() != s2.signature()


# ── Bend ─────────────────────────────────────────────────────────────

class TestBend:
    def test_bend_moves_vertices(self):
        """Bend 应改变顶点位置。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_BEND, strength=1.0,
                            axis="y", params={"angle": 45.0})
        new_v, new_n = evaluate_deformer(spec, cube.vertices, cube.normals)
        assert not np.allclose(new_v, cube.vertices)
        assert new_v.shape == cube.vertices.shape

    def test_bend_zero_strength_no_change(self):
        """strength=0 → 无变形。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_BEND, strength=0.0)
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        assert np.allclose(new_v, cube.vertices, atol=1e-6)

    def test_bend_disabled_no_change(self):
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_BEND, enabled=False)
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        assert np.allclose(new_v, cube.vertices)

    def test_bend_field_limits_to_region(self):
        """带 field 的 bend 只影响局部（field 权重≈0 处不变）。"""
        cube = prim.cube(1.0)
        # linear field 沿 Y 轴：center=0 处权重=1，y=±2 处=0。
        # cube 顶点在 y∈[-1,1]，所以底部 y=-1 处有显著衰减。
        spec = DeformerSpec(deformer_type=DEFORMER_BEND, strength=1.0,
                            axis="y", params={"angle": 60.0},
                            field=linear_field(center=(0, 1, 0), axis=(0, 1, 0),
                                               range_=1.5))
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        # 顶部（y≈1，靠近 field center）权重最大 → 变形最大
        top_mask = cube.vertices[:, 1] > 0.5
        bot_mask = cube.vertices[:, 1] < -0.5
        top_delta = np.max(np.abs(new_v[top_mask] - cube.vertices[top_mask]))
        bot_delta = np.max(np.abs(new_v[bot_mask] - cube.vertices[bot_mask]))
        assert top_delta > bot_delta  # 顶部变形 > 底部变形


# ── Twist ────────────────────────────────────────────────────────────

class TestTwist:
    def test_twist_rotates_cross_section(self):
        """Twist 绕 axis 旋转横截面。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_TWIST, strength=1.0,
                            axis="y", params={"angle": 90.0})
        new_v, new_n = evaluate_deformer(spec, cube.vertices, cube.normals)
        # 顶部的 XZ 坐标应旋转（不等于原值）
        top_mask = cube.vertices[:, 1] > 0.5
        assert not np.allclose(new_v[top_mask][:, [0, 2]],
                               cube.vertices[top_mask][:, [0, 2]], atol=0.05)
        # 底部（h≈-1，t≈-1，扭转角大）也应变；中心（h≈0）不变
        center_mask = np.abs(cube.vertices[:, 1]) < 0.01
        assert np.allclose(new_v[center_mask], cube.vertices[center_mask], atol=0.1)


# ── Bulge ────────────────────────────────────────────────────────────

class TestBulge:
    def test_bulge_along_normal(self):
        """Bulge 沿法线推出顶点（远离原位置）。"""
        sphere = prim.sphere(1.0, 16, 8)
        spec = DeformerSpec(deformer_type=DEFORMER_BULGE, strength=0.5,
                            params={"mode": "normal"})
        new_v, _ = evaluate_deformer(spec, sphere.vertices, sphere.normals)
        # 沿法线推出 → 到原点距离增大
        old_dist = np.linalg.norm(sphere.vertices, axis=1)
        new_dist = np.linalg.norm(new_v, axis=1)
        assert np.mean(new_dist) > np.mean(old_dist)

    def test_bulge_radial_from_center(self):
        """radial 模式：从 center 向外推。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_BULGE, strength=0.3,
                            center=np.zeros(3, dtype=np.float32),
                            params={"mode": "radial"})
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        old_dist = np.linalg.norm(cube.vertices, axis=1)
        new_dist = np.linalg.norm(new_v, axis=1)
        assert np.mean(new_dist) >= np.mean(old_dist) - 1e-6


# ── Taper / Stretch ──────────────────────────────────────────────────

class TestTaperStretch:
    def test_taper_changes_cross_section(self):
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_TAPER, strength=1.0,
                            axis="y", params={"amount": 2.0})
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        # 顶部（y>0）XZ 跨度应比原来大
        top = cube.vertices[:, 1] > 0.5
        bot = cube.vertices[:, 1] < -0.5
        old_top_span = np.ptp(cube.vertices[top][:, 0])
        new_top_span = np.ptp(new_v[top][:, 0])
        assert new_top_span > old_top_span

    def test_stretch_axis_grows_perp_shrinks(self):
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_STRETCH, strength=1.0,
                            axis="y", params={"amount": 1.5})
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        # Y 跨度增大
        assert np.ptp(new_v[:, 1]) > np.ptp(cube.vertices[:, 1])
        # X 跨度缩小（体积守恒近似）
        assert np.ptp(new_v[:, 0]) < np.ptp(cube.vertices[:, 0]) + 1e-6


# ── Noise ────────────────────────────────────────────────────────────

class TestNoise:
    def test_noise_deterministic(self):
        """相同 seed → 相同结果。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_NOISE, strength=1.0,
                            params={"seed": 1.0, "amplitude": 0.2})
        v1, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        v2, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        assert np.allclose(v1, v2)

    def test_noise_changes_vertices(self):
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_NOISE, strength=1.0,
                            params={"amplitude": 0.3})
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        assert not np.allclose(new_v, cube.vertices)


# ── DeformerStack ────────────────────────────────────────────────────

class TestDeformerStack:
    def test_empty_stack_no_change(self):
        """空栈 → 原样返回。"""
        cube = prim.cube(1.0)
        stack = DeformerStack()
        result = stack.evaluate(cube)
        assert np.allclose(result.vertices, cube.vertices)
        assert result.recomputed_normals is False

    def test_all_disabled_no_change(self):
        cube = prim.cube(1.0)
        stack = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BEND, enabled=False),
            DeformerSpec(deformer_type=DEFORMER_TWIST, enabled=False),
        ])
        result = stack.evaluate(cube)
        assert np.allclose(result.vertices, cube.vertices)

    def test_single_deformer_applies(self):
        cube = prim.cube(1.0)
        stack = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BEND, strength=1.0,
                         params={"angle": 45.0}),
        ])
        result = stack.evaluate(cube)
        assert not np.allclose(result.vertices, cube.vertices)

    def test_stack_order_matters(self):
        """Bend→Twist 与 Twist→Bend 结果不同（变形不满足交换律）。"""
        cube = prim.cube(1.0)
        bend = DeformerSpec(deformer_type=DEFORMER_BEND, strength=1.0,
                            axis="y", params={"angle": 45.0})
        twist = DeformerSpec(deformer_type=DEFORMER_TWIST, strength=1.0,
                             axis="y", params={"angle": 90.0})
        r1 = DeformerStack(deformers=[bend, twist]).evaluate(cube)
        r2 = DeformerStack(deformers=[twist, bend]).evaluate(cube)
        assert not np.allclose(r1.vertices, r2.vertices, atol=1e-4)

    def test_non_destructive(self):
        """变形器不修改原 MeshData。"""
        cube = prim.cube(1.0)
        orig_verts = cube.vertices.copy()
        stack = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BEND, strength=1.0,
                         params={"angle": 60.0}),
        ])
        stack.evaluate(cube)
        assert np.allclose(cube.vertices, orig_verts)

    def test_stack_signature(self):
        s1 = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BEND, strength=0.5),
        ])
        s2 = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BEND, strength=0.5),
        ])
        s3 = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BEND, strength=0.8),
        ])
        assert s1.signature() == s2.signature()
        assert s1.signature() != s3.signature()


# ── selection mask ───────────────────────────────────────────────────

class TestSelectionMask:
    def test_mask_limits_deformation(self):
        """selection mask 只影响选中顶点。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_BULGE, strength=0.5)
        # 只选前 4 个顶点
        mask = np.zeros(cube.vertex_count, dtype=bool)
        mask[:4] = True
        new_v, _ = evaluate_deformer(spec, cube.vertices, cube.normals,
                                      selection_mask=mask)
        # 未选中的顶点不变
        assert np.allclose(new_v[~mask], cube.vertices[~mask], atol=1e-6)
        # 选中的顶点变了
        assert not np.allclose(new_v[mask], cube.vertices[mask])
