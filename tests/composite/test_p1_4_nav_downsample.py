"""P1-4：navigation 真降采样测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §5 (P1-4)：
- navigation 态渲染目标降到 0.65x，停止恢复 1.0x
- _gpu_texture（输出）始终全尺寸，渲染后上采样（copy_framebuffer）
- nav_res_scale 变化触发 _rebuild_targets（nav↔static 切换）
- resolve 条件扩展：msaa=0 + nav 降采样也要 blit

注：真实渲染需 GL；用源码级断言，与 P0-P8 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect


class TestNavDownsampleFields:
    """Scene3DNode 有 navigation 降采样渲染目标的字段。"""

    def test_has_nav_render_fields(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "self._nav_render_color" in src
        assert "self._nav_render_fbo" in src
        assert "self._nav_render_scale" in src

    def test_release_render_target_releases_nav(self):
        """_release_render_target 释放 nav 降采样资源。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._release_render_target)
        assert "_nav_render_color" in src
        assert "_nav_render_fbo" in src
        assert "self._nav_render_scale = 1.0" in src


class TestNavDownsampleLogic:
    """_do_evaluate 的降采样尺寸计算 + rebuild 判定 + resolve 条件。"""

    def test_rebuild_targets_includes_nav_scale_change(self):
        """nav_res_scale 变化触发 _rebuild_targets。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_nav_scale_changed" in src
        assert "_rebuild_targets" in src
        # nav_scale 变化纳入 rebuild 判定
        assert "_nav_scale_changed" in src

    def test_nav_downsample_computes_rw_rh(self):
        """nav 态（_nav_res_scale<1.0）计算降采样尺寸 _rw/_rh。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_rw = max(2, int(w * _nav_res_scale))" in src
        assert "_rh = max(2, int(h * _nav_res_scale))" in src

    def test_nav_msaa_uses_downsampled_size(self):
        """MSAA color/depth 用 _rw/_rh（nav 态降采样）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # MSAA color 创建用 _rw,_rh
        assert "ctx.texture((_rw, _rh), 4, dtype='f4', samples=msaa)" in src
        assert "ctx.depth_renderbuffer((_rw, _rh), samples=msaa)" in src

    def test_nav_no_msaa_separates_render_target(self):
        """msaa=0 + nav 降采样时，渲染目标用 _nav_render_color，分离 _gpu_texture。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "if _nav_res_scale < 1.0:" in src
        assert "self._nav_render_color = ctx.texture((_rw, _rh)" in src
        assert "self._nav_render_fbo = ctx.framebuffer(" in src
        # _render_fbo 指向 nav_render_fbo
        assert "self._render_fbo = self._nav_render_fbo" in src
        # resolve FBO 绑定全尺寸 _gpu_texture（上采样目标）
        assert "color_attachments=[self._gpu_texture])" in src

    def test_resolve_condition_includes_nav_downsample(self):
        """resolve 条件扩展：msaa=0 + nav 降采样也要 blit 上采样。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_need_resolve" in src
        # 条件包含 nav_render_fbo
        assert "self._nav_render_fbo is not None" in src
        # 实际 blit 调用
        assert "ctx.copy_framebuffer(self._resolve_fbo, self._render_fbo)" in src


class TestNavDownsampleProfileParams:
    """quality profile 的 resolution_scale 配置（nav 降采样的数据源）。"""

    def test_navigation_profile_has_resolution_scale_below_1(self):
        """NAVIGATION/FAST_SOLID 的 resolution_scale < 1.0（触发降采样）。"""
        from src.features.composite.ui.viewport3d_state import (
            QUALITY_PROFILE_PARAMS, ViewportQualityProfile)
        nav = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.NAVIGATION]
        fast = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FAST_SOLID]
        assert nav["resolution_scale"] < 1.0
        assert fast["resolution_scale"] < 1.0

    def test_static_profile_resolution_scale_is_1(self):
        """FINAL/PREVIEW 的 resolution_scale = 1.0（不降采样）。"""
        from src.features.composite.ui.viewport3d_state import (
            QUALITY_PROFILE_PARAMS, ViewportQualityProfile)
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FINAL]["resolution_scale"] == 1.0
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.PREVIEW]["resolution_scale"] == 1.0
