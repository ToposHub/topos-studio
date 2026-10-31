"""Milestone D / P3: 材质与纹理状态缓存测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §7 (P3)：
1. MaterialStateKey：稳定可哈希、相同材质相同 key、不同 override 不同 key
2. MaterialStateCache：命中/未命中、reset、统计
3. Scene3D 集成（_material_cache 字段、连续同材质跳过 bind、每帧 reset）
4. _bind_null_material_samplers 的 sampler-loc 缓存（每 program 一次）
5. render_diagnostics 含 material_cache_hit_rate

注：真实 GL bind 需 GL；用源码级断言 + 纯逻辑单测，与 P0/P1/P2 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from src.features.composite.render3d.material_state import (
    MaterialStateKey, MaterialStateCache,
)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: MaterialStateKey
# ──────────────────────────────────────────────────────────────────────────

class _FakeTex:
    """模拟 TextureDescriptor（只需 path 字段）。"""
    def __init__(self, path: str):
        self.path = path


class _FakeMat:
    """模拟 MaterialData（含 *_tex TextureDescriptor）。"""
    def __init__(self):
        self.base_color_tex = _FakeTex("base.png")
        self.orm_tex = _FakeTex("orm.png")
        self.normal_tex = None
        self.ao_tex = None
        self.emissive_tex = None


_PARAMS = {
    "material_mode": "imported",
    "texture_filter": "linear",
    "texture_wrap": "clamp",
    "emissive_intensity": 1.0,
    "normal_format": "OpenGL",
}


class TestMaterialStateKey:
    """MaterialStateKey 稳定可哈希 + 等价判定。"""

    def test_hashable(self):
        k = MaterialStateKey()
        hash(k)  # 不抛

    def test_same_material_same_key(self):
        mat = _FakeMat()
        k1 = MaterialStateKey.from_material(mat, _PARAMS, {})
        k2 = MaterialStateKey.from_material(mat, _PARAMS, {})
        assert k1 == k2
        assert hash(k1) == hash(k2)

    def test_different_override_different_key(self):
        mat = _FakeMat()
        k1 = MaterialStateKey.from_material(mat, _PARAMS, {})
        k2 = MaterialStateKey.from_material(mat, _PARAMS, {"base_color_in": 5})
        assert k1 != k2

    def test_different_filter_different_key(self):
        mat = _FakeMat()
        k1 = MaterialStateKey.from_material(mat, {**_PARAMS, "texture_filter": "linear"}, {})
        k2 = MaterialStateKey.from_material(mat, {**_PARAMS, "texture_filter": "nearest"}, {})
        assert k1 != k2

    def test_none_material(self):
        k = MaterialStateKey.from_material(None, _PARAMS, {})
        assert k.material_id is None

    def test_override_order_independent(self):
        """override_glos 排序后比较，dict 顺序不影响 key。"""
        mat = _FakeMat()
        k1 = MaterialStateKey.from_material(mat, _PARAMS, {"base_color_in": 5, "ao_in": 7})
        k2 = MaterialStateKey.from_material(mat, _PARAMS, {"ao_in": 7, "base_color_in": 5})
        assert k1 == k2


# ──────────────────────────────────────────────────────────────────────────
# Part 2: MaterialStateCache
# ──────────────────────────────────────────────────────────────────────────

class TestMaterialStateCache:
    """MaterialStateCache 命中/未命中/reset/统计。"""

    def test_first_always_miss(self):
        cache = MaterialStateCache()
        k = MaterialStateKey()
        assert not cache.matches(k)
        assert cache.hits == 0

    def test_same_key_after_update_is_hit(self):
        cache = MaterialStateCache()
        k = MaterialStateKey()
        cache.update(k)
        assert cache.matches(k)
        assert cache.hits == 1

    def test_different_key_is_miss(self):
        cache = MaterialStateCache()
        k1 = MaterialStateKey(material_mode="imported")
        k2 = MaterialStateKey(material_mode="override")
        cache.update(k1)
        assert not cache.matches(k2)

    def test_reset_clears_last_key(self):
        cache = MaterialStateCache()
        k = MaterialStateKey()
        cache.update(k)
        cache.reset()
        assert not cache.matches(k)  # reset 后又 miss

    def test_hit_rate(self):
        cache = MaterialStateCache()
        k = MaterialStateKey()
        cache.update(k)         # miss
        cache.matches(k)        # hit
        cache.matches(k)        # hit
        assert cache.hits == 2
        assert cache.misses == 1
        assert cache.hit_rate == pytest.approx(2.0 / 3.0)

    def test_empty_cache_rate_zero(self):
        cache = MaterialStateCache()
        assert cache.hit_rate == 0.0


# ──────────────────────────────────────────────────────────────────────────
# Part 3: Scene3D 集成（源码级）
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DMaterialCacheIntegration:
    """Scene3D 用 MaterialStateCache 跳过连续同材质 bind（§7.3 P3-2）。"""

    def test_init_creates_cache(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "MaterialStateCache" in src
        assert "_material_cache" in src

    def test_do_evaluate_resets_cache_per_frame(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_material_cache.reset()" in src

    def test_do_evaluate_uses_cache_in_mesh_loop(self):
        """mesh 循环用 MaterialStateKey + matches/update 跳过 rebind。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "MaterialStateKey.from_material" in src
        assert "_material_cache.matches" in src
        assert "_material_cache.update" in src

    def test_render_ortho_views_resets_cache_per_view(self):
        """_render_ortho_views 每视图开头 reset 缓存（避免跨视图串场）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "_material_cache.reset()" in src

    def test_diagnostics_includes_hit_rate(self):
        """render_diagnostics 含 material_cache_hit_rate。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "material_cache_hit_rate" in src

    def test_material_switches_incremented_on_miss(self):
        """未命中时 _pstats.material_switches +1（命中时不变）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_pstats.material_switches += 1" in src

    def test_cached_material_state_is_not_unbound_per_mesh(self):
        """材质缓存命中依赖上一 mesh 的 sampler 仍绑定，不能每个 mesh 画完就解绑。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        cache_idx = src.index("_material_cache.matches")
        stats_idx = src.index("_pstats.perspective_ms +=")
        mesh_loop = src[cache_idx:stats_idx]
        assert "_unbind_material_textures()" not in mesh_loop


# ──────────────────────────────────────────────────────────────────────────
# Part 4: sampler-loc 缓存（P3-4）
# ──────────────────────────────────────────────────────────────────────────

class TestSamplerLocCache:
    """_bind_null_material_samplers 的 sampler uniform 赋值每 program 一次。"""

    def test_init_has_sampler_loc_set(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_sampler_loc_set_progs" in src

    def test_bind_uses_cache(self):
        """_bind_null_material_samplers 用 _sampler_loc_set_progs 跳过重复赋值。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_null_material_samplers)
        assert "_sampler_loc_set_progs" in src

    def test_cleanup_clears_sampler_set(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode.cleanup)
        assert "_sampler_loc_set_progs.clear()" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
