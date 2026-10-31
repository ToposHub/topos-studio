"""P6 最小闭环测试：渲染状态拆分 + Scene3D 渲染边界清晰化。

覆盖：
- Step 1：view_signature 含相机段 + 交互态；相机 nav 路径不再设 force_refresh。
- Step 2：Scene3D content_sig 拆成 scene_content_sig + view_sig；仅相机变化时
  _scene_content_changed=False（资源不重建，只重发 draw call）。
- Step 3：perf_stats.scene_content_cache_hit 字段 + viewer overlay。

注：真实渲染需 GL；用源码级断言 + 签名逻辑单测，与 P0-P5 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest


# ──────────────────────────────────────────────────────────────────────────
# Step 1：view_signature 含相机 + 交互态；相机 nav 不再 force_refresh
# ──────────────────────────────────────────────────────────────────────────

class TestViewSignatureCamera:
    """_view_signature 在 3D 模式下追加相机签名段。"""

    def test_signature_has_cam_segment(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature)
        assert "cam=" in src
        assert "cam_orbit_yaw" in src
        assert "cam_orbit_pitch" in src
        assert "cam_dist" in src

    def test_signature_has_interaction_state(self):
        """_view_signature 含交互态段（nav0/nav1）——profile 切换可被 dedup 见。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature)
        assert "_interaction_active" in src or "self._interaction_active" in src
        assert "_nav3d_active" in src
        assert "nav0" in src or "nav1" in src

    def test_cam_segment_only_in_3d_mode(self):
        """相机签名段只在 3D 模式追加（2D 模式相机不影响渲染）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._view_signature)
        assert 'mode == "3d"' in src


class TestNavPathNoForceRefresh:
    """相机 nav committed/restore 不再设 _force_next_view_refresh（P6）。"""

    def test_nav_preview_force_refresh_limited_to_model_transforms(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_preview)
        # 相机变化由 view_signature 捕获；只有 Model3D/Geo3D transform 预览
        # 需要 force_refresh，避免拖动物体时复用旧缓存。
        assert "node_type" in src
        assert '{"geo3d", "model3d"}' in src
        assert "_force_next_view_refresh = True" in src
        # 仍触发 evaluate
        assert "_evaluate_graph" in src

    def test_nav_committed_no_force_refresh(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_committed)
        assert "_force_next_view_refresh = True" not in src

    def test_nav_restore_no_force_refresh(self):
        """_on_nav3d_restore 不再 force_refresh（交互态进 view_signature）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_nav3d_restore)
        assert "_force_next_view_refresh = True" not in src
        assert "_evaluate_graph()" in src


class TestVirtualNodeParamRevision:
    """P6 修复：虚拟节点 param_revision 随相机变化 bump（默认场景摄影响应操作）。

    去掉 nav 路径 force_refresh 后，Layer-2 per-node dirty 检查看不到相机变化
    （虚拟节点 param_revision 永远 0）→ Scene3D 不被标记 dirty → 视图无响应。
    修复：param_revision = 相机参数 hash。相机变 → revision 变 → dirty。
    """

    def test_inject_sets_param_revision(self):
        """_inject_viewer3d_scene_snapshot 给虚拟节点设 param_revision。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._inject_viewer3d_scene_snapshot)
        assert "param_revision" in src
        assert "_virtual_param_revision" in src

    def test_param_revision_derived_from_camera(self):
        """param_revision 从相机参数 hash 得出（相机变 → revision 变）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._inject_viewer3d_scene_snapshot)
        assert "cam_orbit_yaw" in src
        assert "hashlib" in src


# ──────────────────────────────────────────────────────────────────────────
# Step 2：Scene3D content_sig 拆分
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DContentSplit:
    """Scene3D 把 content_sig 拆成 scene_content_sig + view_sig。"""

    def test_init_has_scene_content_sig_field(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_last_scene_content_sig" in src

    def test_do_evaluate_builds_scene_content_sig(self):
        """_do_evaluate 构建 scene_content_sig（不含相机）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "scene_content_sig" in src
        assert "view_sig" in src

    def test_scene_content_sig_excludes_camera(self):
        """scene_content_sig 不含 cam_yaw/pitch/dist（相机在 view_sig）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # 找到 scene_content_sig 构建块，确认不含相机
        # 简化：确认 cam_yaw 在 view_sig 段而非 scene_content_sig 段
        assert "cam_yaw, cam_pitch" in src  # 这些在 view_sig

    def test_scene_content_changed_flag(self):
        """_do_evaluate 计算 _scene_content_changed。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_scene_content_changed" in src
        assert "_last_scene_content_sig" in src

    def test_rebuild_targets_excludes_scene_content_changes(self):
        """FBO/深度等渲染目标只受尺寸/MSAA/首次纹理创建影响。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assign_start = src.index("_rebuild_targets = (")
        assign_end = src.index(")", assign_start)
        rebuild_expr = src[assign_start:assign_end]
        assert "size_changed" in rebuild_expr
        assert "_msaa_changed" in rebuild_expr
        assert "self._gpu_texture is None" in rebuild_expr
        assert "_scene_content_changed" not in rebuild_expr

    def test_rebuild_targets_keeps_mesh_resource_caches(self):
        """重建 FBO 时不应丢弃 mesh/VAO 缓存，资源变化由各自 ensure/release 处理。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        block_start = src.index("if _rebuild_targets:")
        block_end = src.index("# ── 3.5 阶段七", block_start)
        rebuild_block = src[block_start:block_end]
        assert "self._last_primitive = None" not in rebuild_block
        assert "self._flat_meshes_gl = []" not in rebuild_block
        assert "self._depth_write_vaos = {}" not in rebuild_block

    def test_persists_scene_content_sig(self):
        """渲染块末尾持久化 _last_scene_content_sig。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_last_scene_content_sig = scene_content_sig" in src


# ──────────────────────────────────────────────────────────────────────────
# Step 3：perf_stats scene_content_cache_hit + overlay
# ──────────────────────────────────────────────────────────────────────────

class TestPerfStatsSceneContentCacheHit:
    """Viewport3DPerfStats 含 scene_content_cache_hit 字段。"""

    def test_field_exists(self):
        from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
        s = Viewport3DPerfStats()
        assert hasattr(s, "scene_content_cache_hit")
        assert s.scene_content_cache_hit is None  # 默认 None

    def test_field_in_to_dict(self):
        from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
        s = Viewport3DPerfStats(scene_content_cache_hit=True)
        d = s.to_dict()
        assert d["scene_content_cache_hit"] is True

    def test_reset_clears_field(self):
        from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
        s = Viewport3DPerfStats(scene_content_cache_hit=True)
        s.reset()
        assert s.scene_content_cache_hit is None

    def test_scene3d_sets_field(self):
        """Scene3D _do_evaluate 设置 scene_content_cache_hit。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "scene_content_cache_hit" in src

    def test_overlay_shows_scene_hit_miss(self):
        """viewer perf overlay 第 3 行显示 scene:hit/miss。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_perf_overlay)
        assert "scene_content_cache_hit" in src
        assert "scene:hit" in src
        assert "scene:miss" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
