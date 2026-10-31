"""
Render3DStats + PerfMonitor 3D 统计集成测试（F12 性能面板增强）。

覆盖：
- Render3DStats 数据结构 + summary_lines
- PerfMonitor.record_3d_stats / get_snapshot 3D 统计传递
- begin_frame 清除 3D 统计
- PerfSnapshot.render3d 字段

纯逻辑测试，无 GPU 依赖。
"""

import pytest

from src.features.composite.utils.perf_monitor import (
    PerfMonitor,
    PerfSnapshot,
    Render3DStats,
    build_perf_report,
)


class TestRender3DStatsDefaults:
    def test_defaults(self):
        s = Render3DStats()
        assert s.draw_calls == 0
        assert s.triangles == 0
        assert s.vertices == 0
        assert s.render_passes == 1
        assert s.msaa_samples == 1
        assert s.lod_level == 0
        assert s.render_ms == 0.0

    def test_custom_values(self):
        s = Render3DStats(
            draw_calls=15, triangles=12000, vertices=8000,
            bone_binds=45, skin_count=2, render_ms=3.5,
        )
        assert s.draw_calls == 15
        assert s.triangles == 12000
        assert s.bone_binds == 45
        assert s.skin_count == 2
        assert s.render_ms == 3.5


class TestRender3DStatsSummary:
    def test_summary_lines_count(self):
        s = Render3DStats()
        lines = s.summary_lines()
        assert len(lines) == 11  # 所有字段各一行

    def test_summary_contains_draw_calls(self):
        s = Render3DStats(draw_calls=42)
        lines = s.summary_lines()
        assert any("42" in line and "Draw" in line for line in lines)

    def test_summary_triangles_formatted(self):
        """三角面用千分位格式。"""
        s = Render3DStats(triangles=1234567)
        lines = s.summary_lines()
        assert any("1,234,567" in line for line in lines)

    def test_summary_vertices_formatted(self):
        s = Render3DStats(vertices=987654)
        lines = s.summary_lines()
        assert any("987,654" in line for line in lines)

    def test_summary_bones_shows_skin_count(self):
        s = Render3DStats(bone_binds=45, skin_count=3)
        lines = s.summary_lines()
        bone_line = [l for l in lines if "Bones" in l]
        assert len(bone_line) == 1
        assert "45" in bone_line[0]
        assert "3 skins" in bone_line[0]

    def test_summary_msaa(self):
        s = Render3DStats(msaa_samples=4)
        lines = s.summary_lines()
        assert any("4x" in line and "MSAA" in line for line in lines)

    def test_summary_render_ms(self):
        s = Render3DStats(render_ms=5.23)
        lines = s.summary_lines()
        assert any("5.2ms" in line for line in lines)


class TestPerfMonitor3DStats:
    def test_snapshot_default_no_3d(self):
        m = PerfMonitor()
        m.begin_frame()
        snap = m.get_snapshot()
        assert snap.render3d is None

    def test_record_and_retrieve(self):
        m = PerfMonitor()
        m.begin_frame()
        stats = Render3DStats(draw_calls=10, triangles=5000)
        m.record_3d_stats(stats)
        snap = m.get_snapshot()
        assert snap.render3d is not None
        assert snap.render3d.draw_calls == 10
        assert snap.render3d.triangles == 5000

    def test_begin_frame_clears_3d(self):
        m = PerfMonitor()
        m.begin_frame()
        m.record_3d_stats(Render3DStats(draw_calls=5))
        m.begin_frame()  # 新帧清除
        snap = m.get_snapshot()
        assert snap.render3d is None

    def test_3d_stats_persist_across_snapshots(self):
        """3D 统计不被 get_snapshot 清除（与 frame 不同；frame 记录时清 dropped）。"""
        m = PerfMonitor()
        m.begin_frame()
        m.record_3d_stats(Render3DStats(draw_calls=7))
        snap1 = m.get_snapshot()
        snap2 = m.get_snapshot()
        assert snap1.render3d is not None
        assert snap2.render3d is not None
        assert snap2.render3d.draw_calls == 7

    def test_snapshot_has_render3d_field(self):
        """PerfSnapshot 有 render3d 字段。"""
        snap = PerfSnapshot()
        assert hasattr(snap, 'render3d')
        assert snap.render3d is None

    def test_thread_safety_lock(self):
        """record_3d_stats 在锁内（并发安全；验证不崩溃）。"""
        import threading
        m = PerfMonitor()
        m.begin_frame()
        errors = []

        def writer():
            try:
                for _ in range(100):
                    m.record_3d_stats(Render3DStats(draw_calls=1))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    m.get_snapshot()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []


class TestPerfTraceReport:
    def test_history_is_bounded_and_detached(self):
        monitor = PerfMonitor(history_size=2)
        for request_id in (1, 2, 3):
            monitor.begin_frame()
            monitor.record_node_eval(
                "blur", "blur", float(request_id), False, request_id == 3
            )
            monitor.record_frame(
                request_id=request_id,
                total_eval_ms=float(request_id),
                cache_hits=request_id,
                cache_misses=1,
                cache_evictions=0,
                active_textures=request_id,
                vram_mb=float(request_id * 10),
            )

        history = monitor.get_history()
        assert [entry.frame.request_id for entry in history] == [2, 3]
        history[0].nodes["blur"].eval_time_ms = 999.0
        assert monitor.get_history()[0].nodes["blur"].eval_time_ms == 2.0

    def test_snapshot_and_report_are_json_safe_and_aggregate_hot_nodes(self):
        monitor = PerfMonitor()
        monitor.begin_frame()
        monitor.record_node_eval("slow", "blur", 12.5, False, True)
        monitor.record_node_eval("fast", "grade", 1.0, True, False)
        monitor.record_3d_stats(
            Render3DStats(
                draw_calls=8, triangles=1200, mesh_count=3, render_ms=4.5
            )
        )
        monitor.record_frame(
            request_id=7,
            total_eval_ms=13.5,
            cache_hits=4,
            cache_misses=2,
            cache_evictions=1,
            active_textures=6,
            vram_mb=128.0,
        )

        snapshot = monitor.get_history()[0]
        raw = snapshot.to_dict()
        assert raw["frame"]["request_id"] == 7
        assert raw["nodes"]["slow"]["was_dirty"] is True
        assert raw["render3d"]["triangles"] == 1200

        report = build_perf_report([snapshot])
        assert report["schema_version"] == 1
        assert report["frame_summary"]["peak_vram_mb"] == 128.0
        assert report["render3d_summary"]["peak_draw_calls"] == 8
        assert report["hot_nodes"][0]["node_id"] == "slow"
        assert report["hot_nodes"][0]["dirty_frames"] == 1
