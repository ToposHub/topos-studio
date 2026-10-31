"""Milestone C / P2: 四视图调度优化测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §6 (P2)：
1. RenderRequest.quad_active_view 字段 + CompositeEvalContext.quad_active_view
2. CompositeViewer.get_quad_active_view（活动视图代码）
3. CompositePage._quad_active_view_code + 传入 RenderRequest
4. renderer/evaluator 同步 quad_active_view 到 eval_ctx
5. Scene3D _render_ortho_views skip_views 参数（非活动视图跳过）
6. Scene3D _do_evaluate 导航态 skip_views 计算
7. viewer3d_settings 新增 quad_view_quality / interactive_lod 设置

注：真实渲染需 GL；用源码级断言 + 字段验证，与 P0/P1 测试同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest


# ──────────────────────────────────────────────────────────────────────────
# Part 1: RenderRequest + EvalContext 字段
# ──────────────────────────────────────────────────────────────────────────

class TestQuadActiveViewField:
    """RenderRequest + CompositeEvalContext 携带 quad_active_view。"""

    def test_render_request_field_exists(self):
        from src.features.composite.render.render_scheduler import RenderRequest
        # dataclass 字段——构造（snapshot=None 兜底）
        req = RenderRequest.__new__(RenderRequest)
        # 字段在 dataclass 定义里
        src = inspect.getsource(RenderRequest)
        assert "quad_active_view" in src

    def test_eval_context_field_exists(self):
        from src.features.composite.core.eval_context import CompositeEvalContext
        ctx = CompositeEvalContext()
        assert hasattr(ctx, "quad_active_view")
        assert ctx.quad_active_view is None  # 默认 None


# ──────────────────────────────────────────────────────────────────────────
# Part 2: CompositeViewer.get_quad_active_view
# ──────────────────────────────────────────────────────────────────────────

class TestViewerQuadActiveView:
    """CompositeViewer 暴露 get_quad_active_view（活动视图代码）。"""

    def test_method_exists(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "get_quad_active_view")

    def test_returns_perspective_code_mapping(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.get_quad_active_view)
        # 映射 perspective→0, top→1, right→2, front→3
        assert "ViewportViewKind.PERSPECTIVE: 0" in src
        assert "ViewportViewKind.TOP: 1" in src
        assert "ViewportViewKind.RIGHT: 2" in src
        assert "ViewportViewKind.FRONT: 3" in src

    def test_returns_zero_when_not_quad(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.get_quad_active_view)
        # 非 quad 返回 0
        assert "is_quad()" in src
        assert "return 0" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 3: CompositePage _quad_active_view_code
# ──────────────────────────────────────────────────────────────────────────

class TestPageQuadActiveViewCode:
    """CompositePage._quad_active_view_code 读 viewer 状态。"""

    def test_method_exists(self):
        from src.features.composite.ui.composite_page import CompositePage
        assert hasattr(CompositePage, "_quad_active_view_code")

    def test_calls_viewer_get_quad_active_view(self):
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._quad_active_view_code)
        assert "get_quad_active_view" in src

    def test_request_includes_quad_active_view(self):
        """主 _do_evaluate_graph 的 RenderRequest 带 quad_active_view。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._build_render_request)
        assert "quad_active_view=" in src
        # 防御性 getattr 形式（teardown 安全）：仍调用 _quad_active_view_code
        assert "_quad_active_view_code" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 4: renderer + evaluator 同步
# ──────────────────────────────────────────────────────────────────────────

class TestRendererEvaluatorSync:
    """renderer → evaluator → eval_ctx 同步 quad_active_view。"""

    def test_renderer_passes_quad_active_view(self):
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer)
        assert "quad_active_view=getattr(request" in src

    def test_evaluator_signature_has_param(self):
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        sig = inspect.signature(BaseEvaluatorMixin._evaluate_snapshot)
        assert "quad_active_view" in sig.parameters

    def test_evaluator_syncs_to_eval_ctx(self):
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        src = inspect.getsource(BaseEvaluatorMixin._evaluate_snapshot)
        assert "eval_ctx_ref.quad_active_view" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 5: Scene3D skip_views（非活动视图跳过）
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DSkipViews:
    """Scene3D _render_ortho_views 支持 skip_views；_do_evaluate 计算 skip_views。"""

    def test_render_ortho_views_has_skip_views_param(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        sig = inspect.signature(Scene3DNode._render_ortho_views)
        assert "skip_views" in sig.parameters

    def test_render_ortho_views_skips_views(self):
        """skip_views 中的视图（且纹理已存在）跳过重渲染。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "skip_views" in src
        assert "continue" in src

    def test_do_evaluate_reads_quad_active_view(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "quad_active_view" in src

    def test_do_evaluate_skips_inactive_in_nav(self):
        """导航态计算 skip_views = 所有正交视图 − 活动正交视图。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_skip_views" in src
        # 全跳过分支（活动视图是透视）
        assert '"top", "right", "front"' in src

    def test_do_evaluate_respects_skip_setting(self):
        """受 inactive_view_skip_in_nav 设置控制。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "inactive_view_skip_in_nav" in src
        assert "quad_view_quality" in src

    def test_do_evaluate_passes_skip_views_to_ortho(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "skip_views=_skip_views" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 6: Scene3D interactive_lod 设置（P1 回补）
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DInteractiveLodSetting:
    """Scene3D 读 interactive_lod 设置（关闭时不降载）。"""

    def test_do_evaluate_reads_interactive_lod(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_interactive_lod" in src
        assert 'interactive_lod' in src


# ──────────────────────────────────────────────────────────────────────────
# Part 7: 3D Scene Settings 新增四视图/导航设置
# ──────────────────────────────────────────────────────────────────────────

class TestViewer3DSettingsP2:
    """DEFAULT_VIEWER3D_SCENE_SETTINGS 含 P2 四视图调度 + P1 导航 LOD 设置。"""

    def test_settings_have_interactive_lod(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "interactive_lod" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["interactive_lod"] is True

    def test_settings_have_nav_restore_delay(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "nav_restore_delay_ms" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["nav_restore_delay_ms"] == 150

    def test_settings_have_nav_resolution_scale(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "nav_resolution_scale" in DEFAULT_VIEWER3D_SCENE_SETTINGS

    def test_settings_have_quad_view_quality(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "quad_view_quality" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        # 默认 all_textured：四视图都显示材质；active_textured 作为可选性能模式。
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["quad_view_quality"] == "all_textured"

    def test_settings_have_inactive_view_skip(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "inactive_view_skip_in_nav" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["inactive_view_skip_in_nav"] is True

    def test_merged_settings_preserves_new_keys(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings)
        merged = merged_viewer3d_scene_settings({})
        assert "quad_view_quality" in merged
        assert "interactive_lod" in merged


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
