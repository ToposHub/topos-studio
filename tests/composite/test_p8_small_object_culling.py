"""P8 高阶优化补充测试：small object culling。

覆盖 §12.2 的 small object culling（远距离极小物体跳过）：
- aabb_screen_size：AABB 屏幕投影估算
- Scene3D Path A 在 frustum cull 后做 small object culling
- nav 态阈值更激进（2px vs 静止态 0.5px）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import inspect


class TestAabbScreenSize:
    """aabb_screen_size：AABB 屏幕投影估算。"""

    def test_close_large_object_high_pixels(self):
        """近处大物体 → 高像素。"""
        from src.features.composite.render3d.frustum import aabb_screen_size
        # 1x1x1 立方体在距离 5 处
        px = aabb_screen_size(
            np.array([0, 0, 0]), np.array([1, 1, 1]),
            np.array([0, 0, 5]), viewport_height_px=600, cam_fov_deg=50.0)
        assert px > 5.0  # 近处大物体应显著

    def test_far_small_object_low_pixels(self):
        """远处小物体 → 低像素。"""
        from src.features.composite.render3d.frustum import aabb_screen_size
        # 0.1x0.1x0.1 立方体在距离 1000 处
        px = aabb_screen_size(
            np.array([0, 0, 0]), np.array([0.1, 0.1, 0.1]),
            np.array([0, 0, 1000]), viewport_height_px=600, cam_fov_deg=50.0)
        assert px < 1.0  # 远处小物体应 < 1 像素

    def test_zero_size_returns_zero(self):
        """零尺寸 AABB → 0。"""
        from src.features.composite.render3d.frustum import aabb_screen_size
        px = aabb_screen_size(
            np.array([1, 1, 1]), np.array([1, 1, 1]),
            np.array([0, 0, 5]), viewport_height_px=600)
        assert px == 0.0

    def test_proportional_to_distance(self):
        """像素数应随距离反比减小。"""
        from src.features.composite.render3d.frustum import aabb_screen_size
        bmin = np.array([0, 0, 0])
        bmax = np.array([1, 1, 1])
        px_near = aabb_screen_size(bmin, bmax, np.array([0, 0, 10]), 600)
        px_far = aabb_screen_size(bmin, bmax, np.array([0, 0, 100]), 600)
        # 远处应比近处小（约 1/10）
        assert px_far < px_near
        assert px_near / px_far > 5.0  # 距离 10x → 像素约 1/10


class TestSmallObjectCullingInScene3D:
    """Scene3D Path A 的 small object culling 逻辑。"""

    def test_path_a_imports_aabb_screen_size(self):
        """Path A 导入 aabb_screen_size。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "aabb_screen_size" in src

    def test_path_a_has_small_obj_threshold(self):
        """Path A 有 _small_obj_px 阈值（nav 态更激进）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_small_obj_px" in src
        assert "self._lod_active" in src  # nav 态判定

    def test_path_a_culls_small_objects(self):
        """Path A 在 frustum cull 后做 small object culling。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # aabb_screen_size 调用 + continue 跳过
        assert "_screen_px = aabb_screen_size(" in src
        assert "_screen_px < _small_obj_px" in src
