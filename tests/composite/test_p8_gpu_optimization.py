"""P8：深度 GPU 优化测试。

覆盖：
- P8-0：perf 计数器接线（uniform_sets/shader_switches/vbo_cache_hit）+
  _is_transparent alpha 判定（MaterialData.alpha）
- P8-1：navigation shader 真简化（IBL gating：u_env_intensity <= 0 跳过 cubemap 采样；
  nav 下 u_has_*_map 强制清零）
- P8-2：_set_shared_uniforms 的 (program, view_sig) view 级缓存
- P8-3：backface culling 由 quality profile 驱动（NAVIGATION/FAST_SOLID=True）

注：真实 GL 渲染需 GL context；用源码级断言 + 纯逻辑单测，与 P0-P7 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest


# ──────────────────────────────────────────────────────────────────────────
# P8-0：perf 计数器接线
# ──────────────────────────────────────────────────────────────────────────

class TestPerfCountersWired:
    """uniform_sets / shader_switches / vbo_cache_hit 三个计数器接通（之前是盲区）。"""

    def test_set_shared_uniforms_has_stats_param(self):
        """_set_shared_uniforms 新增可选 _stats 参数，累加 uniform_sets。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        sig = inspect.signature(Scene3DNode._set_shared_uniforms)
        assert "_stats" in sig.parameters
        assert sig.parameters["_stats"].default is None

    def test_set_shared_uniforms_increments_uniform_sets(self):
        """_set_shared_uniforms 传入 _stats 时，uniform_sets 累加（22 个 shared uniform）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._set_shared_uniforms)
        # 关键：传入 _stats 时累加 22（shared uniform 数量）
        assert "_stats.uniform_sets += 22" in src

    def test_path_a_counts_shader_switches(self):
        """Path A draw loop 统计 shader_switches（program 切换次数）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.shader_switches += 1" in src
        # 用 _last_prog_id 追踪上一个 program
        assert "_last_prog_id" in src

    def test_path_a_counts_per_mesh_uniforms(self):
        """Path A 每 mesh 统计 5 个 per-mesh uniform（u_model + albedo/metallic/roughness/emissive）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.uniform_sets += 5" in src

    def test_vbo_pool_exposes_last_was_cached(self):
        """MeshVBOPool.acquire 后 last_was_cached 反映命中/未命中。"""
        from src.features.composite.render3d.mesh_vbo_pool import MeshVBOPool
        pool = MeshVBOPool()
        assert hasattr(pool, "last_was_cached")
        # 默认 False（未 acquire 过）
        assert pool.last_was_cached is False

    def test_scene3d_reads_vbo_cache_hit_to_pstats(self):
        """Scene3D 把 _last_vbo_cache_hit 写到 _pstats.vbo_cache_hit。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.vbo_cache_hit = self._last_vbo_cache_hit" in src

    def test_ensure_flat_meshes_gl_records_vbo_hit(self):
        """_ensure_flat_meshes_gl 读 pool.last_was_cached 汇总到 _last_vbo_cache_hit。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._ensure_flat_meshes_gl)
        assert "_pool.last_was_cached" in src
        # AUD-7309：命中/未命中都显式汇总（三态 None/True/False），
        # 取代旧"只置 True"的单向写法
        assert "self._last_vbo_cache_hit = merge_vbo_cache_hit(" in src


class TestMaterialDataAlpha:
    """MaterialData 新增 alpha 字段（透明判定，P8-0/P8-3 基础）。"""

    def test_material_has_alpha_field(self):
        from src.features.composite.render3d.scene_graph import MaterialData
        md = MaterialData()
        assert hasattr(md, "alpha")
        # 默认完全不透明
        assert md.alpha == 1.0

    def test_material_has_two_sided_field(self):
        from src.features.composite.render3d.scene_graph import MaterialData
        md = MaterialData()
        assert hasattr(md, "two_sided")

    def test_is_transparent_uses_alpha(self):
        """_sort_meshes_by_material 的 _is_transparent 读 MaterialData.alpha。<0.999 透明。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        # 透明 mesh（alpha=0.5）
        opaque_mesh = {'material': type('M', (), {'alpha': 1.0})()}
        transparent_mesh = {'material': type('M', (), {'alpha': 0.5})()}
        # 调用内部 _is_transparent（静态函数里定义，用源码断言）
        src = inspect.getsource(Scene3DNode._sort_meshes_by_material)
        assert "alpha = getattr(mat, \"alpha\", 1.0)" in src
        assert "float(alpha) < 0.999" in src


# ──────────────────────────────────────────────────────────────────────────
# P8-1：navigation shader 真简化
# ──────────────────────────────────────────────────────────────────────────

class TestNavigationShaderSimplification:
    """navigation profile 下 IBL 采样被 u_env_intensity gating，贴图采样被强制清零。"""

    def test_ibl_sampling_gated_by_env_intensity(self):
        """_MESH_PBR_FRAGMENT 的 IBL 采样包进 if (u_env_intensity > 0.0)。"""
        from src.features.composite.core import shaders
        src = shaders._MESH_PBR_FRAGMENT
        # 关键：IBL 块被条件分支保护
        assert "if (u_env_intensity > 0.0)" in src
        # irradiance/prefilter/brdf 三个采样在 if 块内
        ibl_block_start = src.index("if (u_env_intensity > 0.0)")
        ibl_block = src[ibl_block_start:]
        assert "texture(u_irradiance_map" in ibl_block
        assert "textureLod(u_prefilter_map" in ibl_block
        assert "texture(u_brdf_lut" in ibl_block

    def test_nav_profile_forces_has_map_zero(self):
        """Path A cache-miss 时，FAST_SOLID 强制 u_has_*_map = 0（NAVIGATION 保留贴图）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # P8-1 修订：_nav_skip_maps 只在 FAST_SOLID 启用（NAVIGATION 保留材质）
        assert "_nav_skip_maps = (_qp == ViewportQualityProfile.FAST_SOLID)" in src
        # base_color/orm/ao/emissive 都用 _nav_skip_maps 条件
        assert "0 if _nav_skip_maps else int(_has_map[\"base_color\"])" in src
        assert "0 if _nav_skip_maps else int(_has_map[\"orm\"])" in src

    def test_navigation_profile_disables_ibl(self):
        """NAVIGATION/FAST_SOLID profile 的 use_ibl=False → env_int_val=0。"""
        from src.features.composite.ui.viewport3d_state import QUALITY_PROFILE_PARAMS, ViewportQualityProfile
        nav_params = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.NAVIGATION]
        fast_params = QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FAST_SOLID]
        assert nav_params["use_ibl"] is False
        assert fast_params["use_ibl"] is False
        # 静止态保留 IBL
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FINAL]["use_ibl"] is True


# ──────────────────────────────────────────────────────────────────────────
# P8-2：shared uniforms view 级缓存
# ──────────────────────────────────────────────────────────────────────────

class TestSharedUniformsViewCache:
    """_set_shared_uniforms 按 (program, view_sig) 缓存，避免每 mesh 重发 22 个 uniform。"""

    def test_scene3d_has_cache_fields(self):
        """Scene3DNode 有 _shared_uniforms_cache 和 _shared_uniforms_view_sig 字段。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "self._shared_uniforms_cache" in src
        assert "self._shared_uniforms_view_sig" in src

    def test_set_shared_uniforms_checks_cache(self):
        """_set_shared_uniforms 先查缓存命中，命中则跳过全量上传。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._set_shared_uniforms)
        assert "_shared_uniforms_view_sig" in src
        assert "_cache_hit" in src
        # 命中时 return（跳过 22 个 uniform）
        assert "if _cache_hit:" in src

    def test_do_evaluate_computes_view_sig(self):
        """_do_evaluate 在 Path A 前计算 view_sig 并清空缓存。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "self._shared_uniforms_view_sig = _view_sig" in src
        assert "self._shared_uniforms_cache.clear()" in src

    def test_set_shared_uniforms_passes_stats_in_path_a(self):
        """Path A 的 _set_shared_uniforms 调用传入 _stats=_pstats。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_stats=_pstats" in src


# ──────────────────────────────────────────────────────────────────────────
# P8-3：backface culling by profile
# ──────────────────────────────────────────────────────────────────────────

class TestBackfaceCullingByProfile:
    """backface culling 由 quality profile 驱动。NAVIGATION/FAST_SOLID 启用。"""

    def test_profile_params_have_backface_culling(self):
        """QUALITY_PROFILE_PARAMS 每 profile 都有 backface_culling 字段。"""
        from src.features.composite.ui.viewport3d_state import QUALITY_PROFILE_PARAMS, ViewportQualityProfile
        for profile in ViewportQualityProfile:
            assert "backface_culling" in QUALITY_PROFILE_PARAMS[profile]

    def test_navigation_profiles_enable_culling(self):
        """NAVIGATION/FAST_SOLID 的 backface_culling=True。"""
        from src.features.composite.ui.viewport3d_state import QUALITY_PROFILE_PARAMS, ViewportQualityProfile
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.NAVIGATION]["backface_culling"] is True
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FAST_SOLID]["backface_culling"] is True

    def test_static_profiles_disable_culling(self):
        """FINAL/PREVIEW 的 backface_culling=False（保守，避免法线问题）。"""
        from src.features.composite.ui.viewport3d_state import QUALITY_PROFILE_PARAMS, ViewportQualityProfile
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.FINAL]["backface_culling"] is False
        assert QUALITY_PROFILE_PARAMS[ViewportQualityProfile.PREVIEW]["backface_culling"] is False

    def test_do_evaluate_reads_backface_cull_flag(self):
        """_do_evaluate 从 _qp_params 读 backface_culling 到 _backface_cull。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_backface_cull" in src
        assert "_qp_params.get(\"backface_culling\", False)" in src

    def test_path_a_enables_cull_face_conditionally(self):
        """Path A 在 _backface_cull 时 ctx.enable(CULL_FACE) + cull_face='back'。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "if _backface_cull:" in src
        assert "ctx.enable(moderngl.CULL_FACE)" in src
        # 用字符串 'back'（moderngl 没有 BACK 常量）
        assert "ctx.cull_face = 'back'" in src

    def test_path_a_disables_cull_face_after_render(self):
        """Path A 渲染结束后 disable(CULL_FACE)（避免污染后续 pass）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "ctx.disable(moderngl.CULL_FACE)" in src


# ──────────────────────────────────────────────────────────────────────────
# P8-0：MeshVBOPool hit/miss 行为（逻辑级）
# ──────────────────────────────────────────────────────────────────────────

class TestMeshVBOPoolHitMiss:
    """MeshVBOPool.acquire 后 last_was_cached 反映本次是否命中缓存。"""

    def test_last_was_cached_default_false(self):
        from src.features.composite.render3d.mesh_vbo_pool import MeshVBOPool
        pool = MeshVBOPool()
        assert pool.last_was_cached is False

    def test_acquire_source_records_cached_flag(self):
        """acquire 源码：命中时 last_was_cached=True，未命中 False。"""
        from src.features.composite.render3d.mesh_vbo_pool import MeshVBOPool
        src = inspect.getsource(MeshVBOPool.acquire)
        assert "self.last_was_cached = False" in src
        assert "self.last_was_cached = True" in src


# ──────────────────────────────────────────────────────────────────────────
# 综合回归：确保 P8 不破坏 P0-P7 现有契约
# ──────────────────────────────────────────────────────────────────────────

class TestP8NoRegression:
    """P8 改动不应破坏现有 perf stats 字段和 profile 契约。"""

    def test_perf_stats_still_has_all_fields(self):
        """Viewport3DPerfStats 字段完整（P8 没删字段）。"""
        from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
        s = Viewport3DPerfStats()
        # P0-P7 字段都还在
        for f in ("draw_calls", "triangles_submitted", "texture_binds",
                  "material_switches", "shader_switches", "uniform_sets",
                  "scene_content_cache_hit", "model_cache_hit",
                  "texture_cache_hit", "vbo_cache_hit"):
            assert hasattr(s, f), f"missing field: {f}"

    def test_perf_stats_reset_clears_uniform_sets(self):
        """reset() 清零 uniform_sets（P8 接线的计数器也要 reset）。"""
        from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
        s = Viewport3DPerfStats()
        s.uniform_sets = 100
        s.shader_switches = 5
        s.vbo_cache_hit = True
        s.reset()
        assert s.uniform_sets == 0
        assert s.shader_switches == 0
        assert s.vbo_cache_hit is None

    def test_quality_profile_params_still_has_required_keys(self):
        """QUALITY_PROFILE_PARAMS 每 profile 仍有 msaa/use_ibl/resolution/pbr。"""
        from src.features.composite.ui.viewport3d_state import QUALITY_PROFILE_PARAMS, ViewportQualityProfile
        for profile in ViewportQualityProfile:
            params = QUALITY_PROFILE_PARAMS[profile]
            for key in ("msaa_factor", "use_ibl", "resolution_scale", "pbr_mode"):
                assert key in params, f"{profile} missing {key}"

    def test_material_data_backwards_compatible(self):
        """MaterialData 新增 alpha/two_sided 后，旧构造方式仍可用。"""
        from src.features.composite.render3d.scene_graph import MaterialData
        # 旧方式：只传 base_color/metallic/roughness（不传 alpha/two_sided）
        md = MaterialData(metallic=0.5, roughness=0.3)
        assert md.alpha == 1.0  # 默认不透明
        assert md.metallic == 0.5
