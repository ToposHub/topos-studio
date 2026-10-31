"""B5 批次（性能与稳定性——本会话实施部分）focused 回归测试。

对应 2026-10-14 复合页审计计划：N-14（高关键帧量快照性能）与
QA-03（基准脚本可复现性）。P-01~05 的阶段归因与素材基准属实测任务，
不在此离线覆盖。
"""
from __future__ import annotations

import os
import statistics
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core


def _animated_graph(node_count: int, tracks_per_node: int,
                    keys_per_track: int):
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import Keyframe, KeyframeTrack

    graph = NodeGraph()
    params = ["translate_x", "translate_y", "scale", "rotation", "opacity"]
    for i in range(node_count):
        node = TransformNode(node_id=f"perf{i}")
        for t in range(tracks_per_node):
            track = KeyframeTrack(
                params[t % len(params)], min_val=-10000.0, max_val=10000.0)
            for k in range(keys_per_track):
                track.add_keyframe(Keyframe(frame=k * 2, value=float(k)))
            node.set_keyframe_track(params[t % len(params)], track)
        graph.add_node(node)
    return graph


class TestSnapshotPerformance:
    def test_n14_heavy_graph_snapshot_within_budget(self):
        """50 节点×5 轨道×120 键（30k 键）快照中位应远低于帧预算。

        审计原测中位 120.9ms（独占复测）；修复（编译轨道缓存/浅拷参数/
        按需别名/快照序列化缓存）后按计划目标 ≤10ms 验收。CI 机器波动
        用 3× 容差防 flake。
        """
        graph = _animated_graph(50, 5, 120)
        graph.snapshot(frame_number=0)  # warmup（构建缓存）
        samples = []
        for frame in range(1, 21):
            t0 = time.perf_counter()
            graph.snapshot(frame_number=frame)
            samples.append((time.perf_counter() - t0) * 1000)
        median = statistics.median(samples)
        assert median <= 30.0, f"snapshot median {median:.2f}ms exceeds budget"

    def test_n14_cache_invalidates_on_track_edit(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        from src.shared.keyframe import Keyframe, KeyframeTrack

        graph = NodeGraph()
        node = TransformNode(node_id="t")
        graph.add_node(node)
        track = KeyframeTrack("translate_x", min_val=-10000.0,
                              max_val=10000.0)
        track.add_keyframe(Keyframe(frame=0, value=0.0))
        track.add_keyframe(Keyframe(frame=10, value=10.0))
        node.set_keyframe_track("translate_x", track)
        assert graph.snapshot(
            frame_number=5).nodes["t"].parameters["translate_x"] == 5.0

        track2 = KeyframeTrack("translate_x", min_val=-10000.0,
                               max_val=10000.0)
        track2.add_keyframe(Keyframe(frame=0, value=100.0))
        track2.add_keyframe(Keyframe(frame=10, value=200.0))
        node.set_keyframe_track("translate_x", track2)

        snap = graph.snapshot(frame_number=5)
        assert snap.nodes["t"].parameters["translate_x"] == 150.0
        assert "translate_x" in snap.nodes["t"].keyframe_tracks

        node.remove_keyframe_track("translate_x")
        snap = graph.snapshot(frame_number=5)
        assert "translate_x" not in snap.nodes["t"].keyframe_tracks


class TestBenchScriptReproducibility:
    def test_qa03_bench_scripts_locate_this_checkout(self):
        import importlib.util

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__)))
        for rel in ("../../tools/bench_composite_playback.py",
                    "../../tools/bench_composite_scrub.py"):
            path = os.path.normpath(os.path.join(repo_root, rel))
            spec = importlib.util.spec_from_file_location(
                "bench_mod_" + os.path.basename(path)[:-3], path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            resolved_root = os.path.normpath(str(module._REPO_ROOT))
            # QA-03 意图修正：脚本必须解析**所在检出**的根（旧断言把
            # sibling 名字 topos-color02 写死，换检出名即崩——按意图
            # 改为等于本测试文件所在仓库根）。
            this_root = os.path.normpath(os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")))
            assert resolved_root == this_root, (resolved_root, this_root)
