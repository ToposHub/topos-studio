"""P2-3：per-view 降质测试（非活动正交视图 solid 渲染）。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §6.3 (P2-3)：
- _render_ortho_views 支持 solid_views 参数（降质渲染，与 skip_views 互补）
- quad_view_quality=active_textured 时非活动正交视图进 solid_views
- solid 视图强制 u_has_*_map=0，跳过材质贴图绑定

注：用源码级断言，与 P0-P8 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect


class TestSolidViewsParameter:
    """_render_ortho_views 新增 solid_views 参数。"""

    def test_render_ortho_views_has_solid_views_param(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        sig = inspect.signature(Scene3DNode._render_ortho_views)
        assert "solid_views" in sig.parameters
        assert sig.parameters["solid_views"].default is None

    def test_render_ortho_views_docstring_describes_solid(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "solid_views" in src
        assert "P2-3" in src


class TestSolidViewsLogic:
    """_render_ortho_views 的 solid 降质逻辑。"""

    def test_ortho_mesh_render_checks_solid_views(self):
        """正交视图渲染 mesh 时检查 key in solid_views。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "_is_solid_view = bool(solid_views and key in solid_views)" in src

    def test_solid_view_forces_has_map_zero(self):
        """solid 视图强制 u_has_*_map=0（跳过贴图）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        # solid 分支设 0
        assert "mesh_prog['u_has_base_color_map'].value = 0" in src
        assert "mesh_prog['u_has_orm_map'].value = 0" in src
        assert "mesh_prog['u_has_emissive_map'].value = 0" in src

    def test_solid_view_skips_bind_material_textures(self):
        """solid 视图跳过 _bind_material_textures（在 else 分支才调）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        # _bind_material_textures 在 else 分支（非 solid）
        assert "else:" in src
        assert "_has_map = self._bind_material_textures(" in src

    def test_solid_view_skips_unbind(self):
        """solid 视图不解绑贴图（没绑过）。unbind 条件含 not _is_solid_view。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "if not _is_solid_view and any(_has_map.values())" in src


class TestSolidViewsComputation:
    """_do_evaluate 计算 solid_views（active_textured 时非活动视图降质）。"""

    def test_do_evaluate_computes_solid_views(self):
        """_do_evaluate 计算 _solid_views（active_textured 模式）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_solid_views" in src
        assert "_quad_quality == \"active_textured\"" in src

    def test_solid_views_passed_to_render_ortho(self):
        """_solid_views 传给 _render_ortho_views。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "solid_views=_solid_views" in src

    def test_non_active_ortho_goes_to_solid(self):
        """非活动正交视图进 _solid_views（活动视图除外）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_non_active_ortho" in src
        assert "_solid_views = set(_non_active_ortho)" in src
