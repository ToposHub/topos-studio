"""Milestone B / P1: Navigation LOD 质量档位测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §5 (P1)：
1. ViewportQualityProfile enum + QUALITY_PROFILE_PARAMS 降载参数
2. Scene3DNode._resolve_quality_profile 决策（FINAL/PREVIEW/NAVIGATION/FAST_SOLID）
3. Scene3D _do_evaluate 按 profile 降载（MSAA/IBL/网格 opacity）
4. CompositePage 3D 导航交互态（_nav3d_active → interaction_active）
5. 大模型自动 FAST_SOLID（LARGE_MODEL_TRIANGLE_THRESHOLD）

注：真实渲染需 GL；用源码级断言 + 决策函数单测，与 test_viewport3d_perf_stats.py 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from src.features.composite.ui.viewport3d_state import (
    ViewportQualityProfile, QUALITY_PROFILE_PARAMS,
    LARGE_MODEL_TRIANGLE_THRESHOLD,
)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: ViewportQualityProfile enum + 降载参数
# ──────────────────────────────────────────────────────────────────────────

class TestQualityProfileEnum:
    """ViewportQualityProfile 四档 + 各档降载参数（§2.2 表）。"""

    def test_four_profiles(self):
        assert ViewportQualityProfile.FINAL.value == "final"
        assert ViewportQualityProfile.PREVIEW.value == "preview"
        assert ViewportQualityProfile.NAVIGATION.value == "navigation"
        assert ViewportQualityProfile.FAST_SOLID.value == "fast_solid"

    def test_params_cover_all_profiles(self):
        for p in ViewportQualityProfile:
            assert p in QUALITY_PROFILE_PARAMS

    def test_params_fields(self):
        """每档都有 msaa_factor/use_ibl/resolution_scale/pbr_mode 字段。"""
        for p, params in QUALITY_PROFILE_PARAMS.items():
            assert "msaa_factor" in params
            assert "use_ibl" in params
            assert "resolution_scale" in params
            assert "pbr_mode" in params

    def test_navigation_disables_msaa(self):
        """NAVIGATION 强制关 MSAA（msaa_factor=0）。"""
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.NAVIGATION]["msaa_factor"] <= 0.0
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FAST_SOLID]["msaa_factor"] <= 0.0

    def test_navigation_disables_ibl(self):
        """NAVIGATION 关 IBL（省 cubemap 采样）。"""
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.NAVIGATION]["use_ibl"] is False
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FAST_SOLID]["use_ibl"] is False

    def test_navigation_lowers_resolution(self):
        """NAVIGATION 降分辨率（0.5~0.75x）。"""
        nav_scale = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.NAVIGATION]["resolution_scale"]
        fast_scale = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FAST_SOLID]["resolution_scale"]
        assert 0.0 < nav_scale < 1.0
        assert 0.0 < fast_scale < 1.0
        # FAST_SOLID 比 NAVIGATION 更激进（更低分辨率）
        assert fast_scale <= nav_scale

    def test_final_full_quality(self):
        """FINAL 完整质量（不降载）。"""
        p = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FINAL]
        assert p["msaa_factor"] == 1.0
        assert p["use_ibl"] is True
        assert p["resolution_scale"] == 1.0

    def test_threshold_value(self):
        """大模型阈值 = 500k（§15.1）。"""
        assert LARGE_MODEL_TRIANGLE_THRESHOLD == 500_000


# ──────────────────────────────────────────────────────────────────────────
# Part 2: _resolve_quality_profile 决策
# ──────────────────────────────────────────────────────────────────────────

class _FakeCtx:
    """模拟 CompositeEvalContext（只需 interaction_active 字段）。"""
    def __init__(self, interaction_active: bool):
        self.interaction_active = interaction_active


class _FakeModelPayload:
    """模拟 ModelPayload（has_model + flat_meshes）。"""
    def __init__(self, triangles: int, has_model: bool = True):
        self.has_model = has_model
        self.flat_meshes = [{"index_count": triangles * 3}]


class TestResolveQualityProfile:
    """_resolve_quality_profile 决策表（§5.4）。"""

    def test_full_export_always_final(self):
        """合成输出（full/export）绝不被导航降载污染（§5.5 注意点 2）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(True), "full", None) == ViewportQualityProfile.FINAL
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(True), "export", None) == ViewportQualityProfile.FINAL

    def test_not_interacting_preview(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(False), "preview", None) == ViewportQualityProfile.PREVIEW

    def test_interacting_no_model_navigation(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(True), "preview", None) == ViewportQualityProfile.NAVIGATION

    def test_interacting_small_model_navigation(self):
        """小模型（<500k）导航态 → NAVIGATION。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        small = [_FakeModelPayload(100_000)]  # 100k < 500k
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(True), "preview", small) == ViewportQualityProfile.NAVIGATION

    def test_interacting_large_model_fast_solid(self):
        """大模型（>=500k）导航态 → FAST_SOLID（§15.1 Auto LOD）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        large = [_FakeModelPayload(600_000)]  # 600k >= 500k
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(True), "preview", large) == ViewportQualityProfile.FAST_SOLID

    def test_threshold_boundary(self):
        """阈值边界：正好 500k → FAST_SOLID。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        at = [_FakeModelPayload(500_000)]
        assert Scene3DNode._resolve_quality_profile(
            _FakeCtx(True), "preview", at) == ViewportQualityProfile.FAST_SOLID

    def test_none_ctx_preview(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert Scene3DNode._resolve_quality_profile(
            None, "preview", None) == ViewportQualityProfile.PREVIEW


# ──────────────────────────────────────────────────────────────────────────
# Part 3: Scene3D _do_evaluate 按 profile 降载（源码级）
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DProfileDegradation:
    """Scene3D _do_evaluate 按 profile 应用降载（MSAA/IBL/网格）。"""

    def test_do_evaluate_uses_resolve_quality_profile(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_resolve_quality_profile" in src
        assert "QUALITY_PROFILE_PARAMS" in src
        assert "_qp_params" in src

    def test_do_evaluate_lod_active_set_by_profile(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # _lod_active 在 NAVIGATION/FAST_SOLID 时为 True
        assert "ViewportQualityProfile.NAVIGATION" in src
        assert "ViewportQualityProfile.FAST_SOLID" in src
        assert "_lod_active" in src

    def test_do_evaluate_ibl_degradation(self):
        """use_ibl 在 NAVIGATION/FAST_SOLID 关闭。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert 'use_ibl and bool(_qp_params["use_ibl"])' in src

    def test_do_evaluate_grid_opacity_degradation(self):
        """导航态简化地面网格 opacity（§5.3 表格）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "grid_opacity" in src
        assert "FAST_SOLID" in src
        assert "NAVIGATION" in src

    def test_do_evaluate_fast_solid_refine_after_payload(self):
        """收集 model payload 后 refine 到 FAST_SOLID（大模型自动降级）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "LARGE_MODEL_TRIANGLE_THRESHOLD" in src
        # refine 块在 model_payload 收集后
        assert "_qp == ViewportQualityProfile.NAVIGATION" in src

    def test_perf_stats_uses_profile_value(self):
        """perf_stats.quality_profile 用 _qp.value（不是 lod_active 二选一）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.quality_profile = _qp.value" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 4: CompositePage 3D 导航交互态
# ──────────────────────────────────────────────────────────────────────────

class TestCompositePageNav3D:
    """CompositePage 追踪 3D 导航交互态并传给 request（源码级）。"""

    def test_page_has_nav3d_active_flag(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage.__init__)
        assert "_nav3d_active" in src

    def test_nav_started_sets_flag(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_started)
        assert "self._nav3d_active = True" in src

    def test_nav_committed_schedules_restore(self):
        """P1-5：nav_committed 启动延迟恢复 timer（不立即清 _nav3d_active）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_committed)
        assert "_schedule_nav3d_restore" in src
        # 恢复 timer 的实际清空逻辑在 _on_nav3d_restore
        src2 = inspect.getsource(CompositePage._on_nav3d_restore)
        assert "self._nav3d_active = False" in src2

    def test_evaluate_graph_combines_flags(self):
        """interaction_active = _interaction_active OR _nav3d_active。

        组合逻辑随 P6 view-signature 重构移入 _view_signature_non_cam
        （交互态变化必须触发重渲染）；_do_evaluate_graph 仍引用 _nav3d_active。
        """
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._build_render_request)
        assert "_nav3d_active" in src
        sig_src = inspect.getsource(CompositePage._view_signature_non_cam)
        assert "_interaction_active or self._nav3d_active" in sig_src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
