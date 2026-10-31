"""视锥剔除（frustum culling）单元测试（阶段六）。

覆盖：extract_frustum_planes（Gribb-Hartmann 行主序提取）、aabb_in_frustum
（保守可见策略）、transform_aabb（local→world AABB）。纯 numpy，无 GL 依赖。
"""
import numpy as np
import pytest

from src.features.composite.render3d import frustum, math_utils as m3d

pytestmark = pytest.mark.composite_core


@pytest.fixture
def front_facing_frustum():
    """相机在 (0,0,5) 看向原点 -Z，FOV 50°，near 0.1 far 1000 的视锥平面。"""
    proj = m3d.perspective(50.0, 1.0, 0.1, 1000.0)
    eye = m3d.vec3(0, 0, 5)
    view = m3d.look_at(eye, m3d.vec3(0, 0, 0), m3d.vec3(0, 1, 0))
    return frustum.extract_frustum_planes(proj @ view)


class TestFrustumExtraction:
    def test_planes_shape(self, front_facing_frustum):
        planes = front_facing_frustum
        assert planes.shape == (6, 4)

    def test_planes_normalized(self, front_facing_frustum):
        """每个平面法向应单位长度（归一化后）。"""
        planes = front_facing_frustum
        norms = np.linalg.norm(planes[:, :3], axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)


class TestAABBInFrustum:
    def test_origin_cube_visible(self, front_facing_frustum):
        bmin, bmax = np.array([-1, -1, -1], dtype=np.float32), np.array([1, 1, 1], dtype=np.float32)
        assert frustum.aabb_in_frustum(front_facing_frustum, bmin, bmax) is True

    def test_within_far_plane_visible(self, front_facing_frustum):
        """z=-500 距相机 505 < far 1000，应可见。"""
        bmin, bmax = np.array([-1, -1, -501], dtype=np.float32), np.array([1, 1, -499], dtype=np.float32)
        assert frustum.aabb_in_frustum(front_facing_frustum, bmin, bmax) is True

    def test_beyond_far_plane_culled(self, front_facing_frustum):
        """z=-2000 超出 far，应剔除。"""
        bmin, bmax = np.array([-1, -1, -2001], dtype=np.float32), np.array([1, 1, -1999], dtype=np.float32)
        assert frustum.aabb_in_frustum(front_facing_frustum, bmin, bmax) is False

    def test_behind_camera_culled(self, front_facing_frustum):
        """相机 z=5 看 -Z，z=+500 在身后，应剔除。"""
        bmin, bmax = np.array([-1, -1, 499], dtype=np.float32), np.array([1, 1, 501], dtype=np.float32)
        assert frustum.aabb_in_frustum(front_facing_frustum, bmin, bmax) is False

    def test_side_out_of_view_culled(self, front_facing_frustum):
        """x=1000 在视野侧外，应剔除。"""
        bmin, bmax = np.array([999, -1, -1], dtype=np.float32), np.array([1001, 1, 1], dtype=np.float32)
        assert frustum.aabb_in_frustum(front_facing_frustum, bmin, bmax) is False

    def test_partially_intersecting_visible(self, front_facing_frustum):
        """部分相交（保守策略）应算可见。"""
        # 立方体一半在视野内（x 跨越视野边界）
        bmin, bmax = np.array([-0.1, -1, -1], dtype=np.float32), np.array([50, 1, 1], dtype=np.float32)
        assert frustum.aabb_in_frustum(front_facing_frustum, bmin, bmax) is True


class TestTransformAABB:
    def test_identity_unchanged(self):
        bmin, bmax = np.array([-1, -1, -1], dtype=np.float32), np.array([1, 1, 1], dtype=np.float32)
        m = np.eye(4, dtype=np.float32)
        wbmin, wbmax = frustum.transform_aabb(bmin, bmax, m)
        assert np.allclose(wbmin, bmin)
        assert np.allclose(wbmax, bmax)

    def test_translation(self):
        bmin, bmax = np.array([-1, -1, -1], dtype=np.float32), np.array([1, 1, 1], dtype=np.float32)
        m = m3d.translate(10, 20, 30)
        wbmin, wbmax = frustum.transform_aabb(bmin, bmax, m)
        # 平移后 AABB 中心 = (10,20,30)
        center = (wbmin + wbmax) / 2
        assert np.allclose(center, [10, 20, 30], atol=1e-5)
        # 尺寸不变
        assert np.allclose(wbmax - wbmin, [2, 2, 2], atol=1e-5)

    def test_rotation_no_excess_growth(self):
        """45° 旋转后 AABB 适度放大（精确角点变换，非放大估计）。"""
        bmin, bmax = np.array([-1, -1, -1], dtype=np.float32), np.array([1, 1, 1], dtype=np.float32)
        # 绕 Z 旋转 45°（角点到轴距离 = sqrt(2)）
        m = m3d.rotate_z(45.0)
        wbmin, wbmax = frustum.transform_aabb(bmin, bmax, m)
        # 旋转后边长应 = 2*sqrt(2) ≈ 2.83
        extent = wbmax - wbmin
        assert np.allclose(extent, [2 * 2**0.5, 2 * 2**0.5, 2], atol=1e-4)


class TestMeshLocalAABB:
    def test_cube_aabb(self):
        from src.features.composite.render3d import primitives as prim
        c = prim.cube(1.0)
        bmin, bmax = c.local_aabb()
        assert np.allclose(bmin, [-1, -1, -1], atol=1e-5)
        assert np.allclose(bmax, [1, 1, 1], atol=1e-5)

    def test_cached(self):
        from src.features.composite.render3d import primitives as prim
        c = prim.cube(1.0)
        a1 = c.local_aabb()
        a2 = c.local_aabb()
        assert a1 is a2  # 同一对象（缓存）
