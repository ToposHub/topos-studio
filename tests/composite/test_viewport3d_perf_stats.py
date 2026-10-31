"""Milestone A / P0: 3D 视口性能统计测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §4 (P0)：
1. Viewport3DPerfStats dataclass：字段、默认值、reset、序列化
2. ScopedTimer 计时累加
3. StatsLogger 节流（每秒最多一次）
4. format_overlay / format_quad_overlay 文本格式
5. Scene3D _do_evaluate 集成（源码级 + perf_stats 进 render_diagnostics）
6. MaterialTextureLoader.was_last_cached 标志
7. composite_renderer 把 render_diagnostics 进 metadata
8. CompositeViewer perf overlay 方法（set_render_diagnostics/toggle_perf_overlay）

注：真实渲染需 GL；用源码级断言 + dataclass 字段验证，与 test_3d_phase7_performance.py 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import logging
import time

import pytest

from src.features.composite.render3d.perf_stats import (
    Viewport3DPerfStats, ScopedTimer, StatsLogger,
    format_overlay, format_quad_overlay,
)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: Viewport3DPerfStats dataclass
# ──────────────────────────────────────────────────────────────────────────

class TestViewport3DPerfStats:
    """Viewport3DPerfStats 字段与序列化（计划 §4.3 指标字段）。"""

    def test_default_fields(self):
        s = Viewport3DPerfStats()
        assert s.frame_id == 0
        assert s.quality_profile == "final"
        assert s.viewport_layout == "single"
        assert s.active_view == "perspective"
        assert s.total_cpu_ms == 0.0
        assert s.perspective_ms == 0.0
        assert s.top_ms == 0.0
        assert s.right_ms == 0.0
        assert s.front_ms == 0.0
        assert s.draw_calls == 0
        assert s.triangles_submitted == 0
        assert s.meshes_visible == 0
        assert s.meshes_culled == 0
        assert s.texture_binds == 0
        assert s.texture_uploads == 0
        assert s.material_switches == 0
        assert s.shader_switches == 0
        assert s.uniform_sets == 0
        assert s.fbo_switches == 0
        assert s.navigation_active is False
        assert s.lod_level == 0
        assert s.internal_resolution_scale == 1.0

    def test_covers_plan_fields(self):
        """字段覆盖计划 §4.3 列出的全部指标。"""
        s = Viewport3DPerfStats()
        expected = {
            "frame_id", "quality_profile", "viewport_layout", "active_view",
            "total_cpu_ms", "perspective_ms", "top_ms", "right_ms", "front_ms",
            "draw_calls", "triangles_submitted", "meshes_visible", "meshes_culled",
            "texture_binds", "texture_uploads", "material_switches",
            "shader_switches", "uniform_sets", "fbo_switches",
            "model_cache_hit", "texture_cache_hit", "vbo_cache_hit",
            "navigation_active", "lod_level", "internal_resolution_scale",
        }
        assert expected.issubset(set(s.to_dict().keys()))

    def test_reset_clears_counters(self):
        s = Viewport3DPerfStats()
        s.draw_calls = 5
        s.triangles_submitted = 1000
        s.texture_binds = 3
        s.perspective_ms = 12.5
        s.lod_level = 2
        s.reset(frame_id=42)
        assert s.frame_id == 42
        assert s.draw_calls == 0
        assert s.triangles_submitted == 0
        assert s.texture_binds == 0
        assert s.perspective_ms == 0.0
        assert s.lod_level == 0
        # reset 后 resolution 也清零
        assert s.resolution == (0, 0)

    def test_to_dict_serializable(self):
        """to_dict 返回纯 dict（可放入 render_diagnostics / JSON）。"""
        s = Viewport3DPerfStats(frame_id=7, draw_calls=12,
                                triangles_submitted=524288,
                                total_cpu_ms=16.5)
        d = s.to_dict()
        assert isinstance(d, dict)
        assert d["frame_id"] == 7
        assert d["draw_calls"] == 12
        assert d["triangles_submitted"] == 524288
        assert d["total_cpu_ms"] == 16.5


# ──────────────────────────────────────────────────────────────────────────
# Part 2: ScopedTimer
# ──────────────────────────────────────────────────────────────────────────

class TestScopedTimer:
    """ScopedTimer 累加到指定字段。"""

    def test_accumulates_ms(self):
        s = Viewport3DPerfStats()
        with ScopedTimer(s, "perspective_ms"):
            time.sleep(0.01)
        assert s.perspective_ms > 0.0
        # 至少 5ms（sleep 10ms + 开销）
        assert s.perspective_ms >= 5.0

    def test_multiple_entries_accumulate(self):
        s = Viewport3DPerfStats()
        for _ in range(3):
            with ScopedTimer(s, "top_ms"):
                time.sleep(0.005)
        assert s.top_ms >= 10.0  # 三段累加


# ──────────────────────────────────────────────────────────────────────────
# Part 3: StatsLogger 节流
# ──────────────────────────────────────────────────────────────────────────

class TestStatsLogger:
    """StatsLogger 每 interval 秒最多输出一次。"""

    def test_disabled_logger_no_output(self):
        records = []

        class _Logger:
            def debug(self, msg, *a):
                records.append(msg)
        lg = StatsLogger(_Logger(), interval=1.0, enabled=False)
        lg.log(Viewport3DPerfStats())
        lg.log(Viewport3DPerfStats())
        assert records == []

    def test_throttles_to_interval(self):
        records = []

        class _Logger:
            def debug(self, msg, *a):
                records.append(msg)
        lg = StatsLogger(_Logger(), interval=10.0, enabled=True)
        s = Viewport3DPerfStats()
        lg.log(s)  # 第一次：输出
        assert len(records) == 1
        lg.log(s)  # 立刻第二次：被节流
        assert len(records) == 1


# ──────────────────────────────────────────────────────────────────────────
# Part 4: Overlay 文本格式
# ──────────────────────────────────────────────────────────────────────────

class TestOverlayFormat:
    """format_overlay / format_quad_overlay 文本。"""

    def test_format_overlay_includes_key_metrics(self):
        s = Viewport3DPerfStats(total_cpu_ms=16.5, draw_calls=12,
                                triangles_submitted=524288)
        txt = format_overlay(s, fps=60.0)
        assert "60.0" in txt  # FPS
        assert "16.5" in txt  # ms
        assert "12" in txt    # draws
        assert "524k" in txt  # tris (k 单位)

    def test_format_overlay_million_tris(self):
        s = Viewport3DPerfStats(triangles_submitted=1_500_000)
        txt = format_overlay(s)
        assert "1.5M" in txt

    def test_format_overlay_navigation(self):
        s = Viewport3DPerfStats(navigation_active=True, lod_level=2,
                                internal_resolution_scale=0.65)
        txt = format_overlay(s)
        assert "nav:LOD2" in txt
        assert "0.65x" in txt

    def test_format_quad_overlay_single_empty(self):
        s = Viewport3DPerfStats(viewport_layout="single")
        assert format_quad_overlay(s) == ""

    def test_format_quad_overlay_shows_views(self):
        s = Viewport3DPerfStats(viewport_layout="quad",
                                perspective_ms=5.0, top_ms=2.0,
                                right_ms=2.0, front_ms=2.0)
        txt = format_quad_overlay(s)
        assert "P:" in txt
        assert "T:" in txt
        assert "R:" in txt
        assert "F:" in txt


# ──────────────────────────────────────────────────────────────────────────
# Part 5: Scene3D 集成（源码级）
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DPerfIntegration:
    """Scene3D _do_evaluate 集成 perf_stats（源码级断言）。"""

    def test_scene3d_has_perf_stats_field(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode.__new__(Scene3DNode)
        # __init__ 会创建 _perf_stats；这里验证类引用了 perf_stats 模块
        src = inspect.getsource(Scene3DNode.__init__)
        assert "_perf_stats" in src
        assert "Viewport3DPerfStats" in src
        assert "StatsLogger" in src

    def test_do_evaluate_records_timing(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # 整段计时起点
        assert "_do_evaluate_t0" in src
        # 透视渲染段计时
        assert "perspective_ms" in src
        # draw call 计数
        assert "_pstats.draw_calls" in src
        # 三角形计数
        assert "_pstats.triangles_submitted" in src
        # 剔除计数
        assert "_pstats.meshes_culled" in src
        assert "_pstats.meshes_visible" in src
        # 最终 total_cpu_ms 结算
        assert "_pstats.total_cpu_ms" in src

    def test_do_evaluate_writes_perf_to_diagnostics(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert '"perf_stats"' in src
        assert "_pstats.to_dict()" in src

    def test_ortho_view_per_view_timing(self):
        """_render_ortho_views 记录各视图耗时。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "_v_t0" in src
        assert "self._perf_stats.top_ms" in src
        assert "self._perf_stats.right_ms" in src
        assert "self._perf_stats.front_ms" in src

    def test_material_bind_counts_texture_binds(self):
        """_bind_material_textures 累加 texture_binds。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_material_textures)
        assert "self._perf_stats.texture_binds" in src
        assert "self._perf_stats.texture_uploads" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 6: MaterialTextureLoader.was_last_cached
# ──────────────────────────────────────────────────────────────────────────

class TestMaterialTextureLoaderCacheFlag:
    """MaterialTextureLoader 暴露 was_last_cached 供 perf stats 读。"""

    def test_field_exists(self):
        from src.features.composite.render3d.material_texture_loader import (
            MaterialTextureLoader)
        loader = MaterialTextureLoader()
        assert hasattr(loader, "was_last_cached")
        assert loader.was_last_cached is False


# ──────────────────────────────────────────────────────────────────────────
# Part 7: composite_renderer metadata 含 render_diagnostics
# ──────────────────────────────────────────────────────────────────────────

class TestRendererMetadataDiagnostics:
    """composite_renderer 把 render_diagnostics 附进 metadata_ready。"""

    def test_metadata_includes_diagnostics(self):
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer)
        assert "render_diagnostics" in src
        assert "metadata_ready.emit" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 8: CompositeViewer perf overlay 方法
# ──────────────────────────────────────────────────────────────────────────

class TestViewerPerfOverlay:
    """CompositeViewer 暴露 perf overlay API（源码级，无需构造 Qt）。"""

    def test_viewer_has_perf_methods(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "set_render_diagnostics")
        assert hasattr(CompositeViewer, "toggle_perf_overlay")
        assert hasattr(CompositeViewer, "_draw_perf_overlay")

    def test_set_render_diagnostics_signature(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.set_render_diagnostics)
        assert "self._perf_diagnostics" in src
        # FPS 估算
        assert "_perf_fps" in src

    def test_toggle_perf_overlay_returns_state(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.toggle_perf_overlay)
        assert "_perf_show_overlay" in src

    def test_draw_perf_overlay_uses_formatter(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_perf_overlay)
        assert "format_overlay" in src
        assert "format_quad_overlay" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
