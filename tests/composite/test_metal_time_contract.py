"""阶段 5.4 · Metal 合成链的历史与多帧契约测试（纯逻辑层，无 GPU）。

覆盖：

- 历史跨度登记（deflicker 精确跨度、其余时域类型只登记分类）。
- 确定性种子（相同输入序列 ⇒ 相同种子，与播放顺序无关）。
- 重置边界状态机：正放/倒放/seek/跨剪切/单帧重试/代际/显式重置。
- 预检对时域节点的「历史跨度」拒绝诊断（含节点 ID/类型与所需历史），
  以及对未解析动画/表达式的整图回退（关键帧由现有求值器采样契约）。
- 渲染器侧边界逻辑：时域图在任何边界都不产出（真机层见
  test_metal_composite_chain / export_bridge）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.composite.render.metal import (
    TemporalHistoryTracker,
    deterministic_seed,
    evaluate_temporal_requirements,
    evaluate_metal_support,
    graph_history_span,
)
from src.features.composite.render.metal.time_contract import (
    DEFLICKER_WINDOWS,
    deflicker_history_frames,
    temporal_fallback_reason,
)


# ── 图构造辅助（与 test_metal_composite_chain 同式） ────────────────────


def constant(nid: str, w: int, h: int, rgba: tuple, **kw) -> dict:
    params = {"width": w, "height": h, "mode": "solid", "color": rgba}
    params.update(kw)
    return {"id": nid, "type": "constant", "parameters": params}


def node(nid: str, ntype: str, **params) -> dict:
    return {"id": nid, "type": ntype, "parameters": dict(params)}


def conn(src: str, dst: str, src_port: str = "result",
         dst_port: str = "image") -> dict:
    return {"src_node_id": src, "src_port_name": src_port,
            "dst_node_id": dst, "dst_port_name": dst_port}


def graph(nodes: list, connections: list) -> dict:
    return {"nodes": nodes, "connections": connections, "graph_revision": 3}


# ── 历史跨度登记 ────────────────────────────────────────────────────────


class TestTemporalRequirements:
    def test_deflicker_history_matches_node_window_semantics(self):
        # DeflickerNode._trailing_frames：window=5 → start = frame - 4，
        # 共 5 帧结束于当前帧 → 先前帧数 = 4。
        for window in DEFLICKER_WINDOWS:
            assert deflicker_history_frames(window) == window - 1

    def test_deflicker_invalid_window_falls_back_to_default(self):
        assert deflicker_history_frames("bogus") == 4
        assert deflicker_history_frames(None) == 4

    def test_deflicker_requirement_in_contributing_cone(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("d", "deflicker", window=9),
             node("o", "output")],
            [conn("c", "d"), conn("d", "o")])
        reqs = evaluate_temporal_requirements(g)
        assert len(reqs) == 1
        req = reqs[0]
        assert (req.node_id, req.node_type) == ("d", "deflicker")
        assert req.history_frames == 8
        assert "8" in req.format()

    def test_unconnected_temporal_branch_not_listed(self):
        # 不参与渲染锥的时域节点不登记（与预检的参与锥语义一致）。
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)), node("o", "output"),
             node("d", "deflicker", window=3)],
            [conn("c", "o")])
        assert evaluate_temporal_requirements(g) == []

    def test_other_temporal_types_classified_without_fake_span(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("r", "retime"), node("f", "frame_interpolate"),
             node("o", "output")],
            [conn("c", "r"), conn("r", "f"), conn("f", "o")])
        reqs = {r.node_type: r for r in evaluate_temporal_requirements(g)}
        assert set(reqs) == {"retime", "frame_interpolate"}
        # 节点定义不足以无歧义声明跨度 → None（不伪造精度）。
        assert reqs["retime"].history_frames is None
        assert reqs["frame_interpolate"].history_frames is None

    def test_seeded_nodes_declare_zero_history(self):
        g = graph(
            [node("p", "particle", seed=7), node("o", "output")],
            [conn("p", "o")])
        reqs = evaluate_temporal_requirements(g)
        assert reqs and reqs[0].history_frames == 0

    def test_graph_history_span_is_max(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("d1", "deflicker", window=7),
             node("d2", "deflicker", window=3),
             node("o", "output")],
            [conn("c", "d1"), conn("d1", "d2"), conn("d2", "o")])
        assert graph_history_span(g) == 6

    def test_no_temporal_nodes_span_zero(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)), node("o", "output")],
            [conn("c", "o")])
        assert graph_history_span(g) == 0


# ── 确定性种子 ──────────────────────────────────────────────────────────


class TestDeterministicSeed:
    def test_same_inputs_same_seed(self):
        a = deterministic_seed(7, "n1", 42, "digest")
        b = deterministic_seed(7, "n1", 42, "digest")
        assert a == b

    def test_playback_order_independent(self):
        # 正放/倒放/seek 后重渲染同一帧 → 相同种子。
        forward = [deterministic_seed(7, "n1", f, "digest") for f in range(10)]
        reverse = [deterministic_seed(7, "n1", f, "digest")
                   for f in reversed(range(10))]
        assert list(reversed(reverse)) == forward
        seek_then_retry = deterministic_seed(7, "n1", 5, "digest")
        assert seek_then_retry == forward[5]

    def test_different_inputs_different_seed(self):
        base = deterministic_seed(7, "n1", 0, "digest")
        assert deterministic_seed(8, "n1", 0, "digest") != base   # 修订
        assert deterministic_seed(7, "n2", 0, "digest") != base   # 节点
        assert deterministic_seed(7, "n1", 1, "digest") != base   # 帧
        assert deterministic_seed(7, "n1", 0, "other") != base    # 参数

    def test_seed_domain(self):
        seed = deterministic_seed(0, "n", 0, "")
        assert 0 <= seed < 2 ** 32


# ── 重置边界状态机 ──────────────────────────────────────────────────────


class TestHistoryTracker:
    def test_forward_play_continues(self):
        tracker = TemporalHistoryTracker()
        first = tracker.note_request(0)
        assert first.reset and first.step is None          # 起点
        for frame in range(1, 5):
            decision = tracker.note_request(frame)
            assert not decision.reset
            assert decision.step == 1

    def test_reverse_play_continues_after_explicit_start(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(10)
        decisions = [tracker.note_request(f) for f in range(9, 4, -1)]
        assert all(not d.reset for d in decisions)
        assert all(d.step == -1 for d in decisions)

    def test_seek_jump_resets(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(10)
        tracker.note_request(11)
        decision = tracker.note_request(40)
        assert decision.reset and decision.step == 29
        assert "跳变" in decision.reason

    def test_direction_flip_resets(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(10)
        tracker.note_request(11)
        decision = tracker.note_request(10)                # +1 → -1
        assert decision.reset and decision.step == -1
        assert "方向翻转" in decision.reason

    def test_same_frame_retry_does_not_reset(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(7)
        decision = tracker.note_request(7)
        assert not decision.reset and decision.step == 0
        assert "重试" in decision.reason

    def test_generation_change_resets(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(5, generation=1)
        decision = tracker.note_request(6, generation=2)
        assert decision.reset
        assert "代际" in decision.reason

    def test_segment_change_resets(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(5, segment_id="cut-a")
        decision = tracker.note_request(6, segment_id="cut-b")
        assert decision.reset
        assert "段边界" in decision.reason

    def test_explicit_reset_before_segmented_export(self):
        tracker = TemporalHistoryTracker()
        tracker.note_request(5)
        tracker.note_request(6)
        tracker.reset("分段导出段边界")
        decision = tracker.note_request(7)
        assert decision.reset and decision.step is None
        assert decision.reason == "起点（首个帧请求）"


# ── 预检诊断（时域/动画） ───────────────────────────────────────────────


class TestPreflightTemporalDiagnostics:
    def test_deflicker_reports_history_span(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("df", "deflicker", window=9), node("o", "output")],
            [conn("c", "df"), conn("df", "o")])
        report = evaluate_metal_support(g)
        assert not report.supported
        issue = next(i for i in report.issues if i.node_id == "df")
        assert issue.code == "node.metal_history_span"
        assert "先前 8 帧" in issue.reason
        assert "deflicker" in report.fallback_summary

    def test_temporal_fallback_reason_lists_node_and_span(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("df", "deflicker", window=5), node("o", "output")],
            [conn("c", "df"), conn("df", "o")])
        reqs = evaluate_temporal_requirements(g)
        decision = TemporalHistoryTracker().note_request(0)
        reason = temporal_fallback_reason(reqs, decision)
        assert "[df] deflicker" in reason
        assert "先前 4 帧" in reason
        assert "不做静默近似" in reason

    def test_animated_node_blocked_until_resolved(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             {"id": "t", "type": "transform", "parameters": {"scale_x": 1.5},
              "keyframe_tracks": {"scale_x": {"keyframes": [
                  {"frame": 0, "value": 1.0}, {"frame": 10, "value": 2.0}]}}},
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        report = evaluate_metal_support(g)
        assert not report.supported
        issue = next(i for i in report.issues if i.node_id == "t")
        assert issue.code == "node.metal_animated_params"
        assert "求值器" in issue.reason

    def test_resolved_params_skip_animation_block(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             {"id": "t", "type": "transform", "parameters": {"scale_x": 1.5},
              "keyframe_tracks": {"scale_x": {"keyframes": [
                  {"frame": 0, "value": 1.0}]}}},
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        assert not evaluate_metal_support(g).supported
        assert evaluate_metal_support(g, params_resolved=True).supported

    def test_enabled_expression_blocked(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             {"id": "t", "type": "transform",
              "parameters": {"translate_x": 0.0},
              "expressions": {"translate_x": "frame"}, "expression_enabled": True},
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        report = evaluate_metal_support(g)
        issue = next(i for i in report.issues if i.node_id == "t")
        assert issue.code == "node.metal_animated_params"

    def test_disabled_expression_not_blocked(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             {"id": "t", "type": "transform",
              "parameters": {"translate_x": 0.0},
              "expressions": {"translate_x": "frame"},
              "expression_enabled": False},
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        assert evaluate_metal_support(g).supported

    def test_empty_keyframe_track_not_blocked(self):
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             {"id": "t", "type": "transform", "parameters": {"scale_x": 1.5},
              "keyframe_tracks": {"scale_x": {"keyframes": []}}},
             node("o", "output")],
            [conn("c", "t"), conn("t", "o")])
        assert evaluate_metal_support(g).supported


# ── 渲染器侧：时域图在任何边界都不产出（真机层跑得到 GPU 才有效） ────────


class TestRendererTemporalGate:
    def _graph(self) -> dict:
        return graph(
            [constant("c", 4, 4, (1, 1, 1, 1)),
             node("df", "deflicker", window=5), node("o", "output")],
            [conn("c", "df"), conn("df", "o")])

    def test_temporal_graph_refused_with_history_reason(self):
        from src.features.composite.render.metal.renderer import (
            MetalCompositeRenderer,
        )
        renderer = MetalCompositeRenderer()
        try:
            # 预检在时域节点处即整图回退；诊断含历史跨度。
            handle = renderer.render(self._graph())
            assert not handle.available
            assert "deflicker" in handle.fallback_reason
            assert renderer.last_history_reset_reason == ""  # 未到执行
        finally:
            renderer.cleanup()

    def test_static_graph_records_history_boundaries(self):
        pytest.importorskip("src.shared.gpu.metal")
        from src.shared.gpu.metal import MetalComputeContext
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        from src.features.composite.render.metal.renderer import (
            MetalCompositeRenderer,
        )
        g = graph(
            [constant("c", 4, 4, (1, 1, 1, 1)), node("o", "output")],
            [conn("c", "o")])
        renderer = MetalCompositeRenderer()
        try:
            h1 = renderer.render(g, timeline_frame=0)
            assert h1.available
            assert renderer.last_history_reset_reason  # 起点
            h2 = renderer.render(g, timeline_frame=1)
            assert h2.available
            assert renderer.last_history_reset_reason == ""  # 连续推进
            h2.release()
            h1.release()
        finally:
            renderer.cleanup()


# ── 缓存/种子一致性（相同输入序列 → 相同像素；真机层） ──────────────────


class TestSequenceConsistency:
    def test_seek_and_retry_produce_identical_cache_keys_and_pixels(self):
        from src.shared.gpu.metal import MetalComputeContext
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        from src.features.composite.render.metal.cpu_reference import (
            render_graph_cpu,
        )
        from src.features.composite.render.metal.renderer import (
            MetalCompositeRenderer,
        )
        from src.features.composite.render.metal.tile_cache import (
            composite_cache_key,
            graph_params_digest,
        )
        g = graph(
            [constant("fg", 16, 16, (1, 1, 1, 1), mode="checker",
                      color2=(0, 0, 0, 1), checker_size=4),
             node("t", "transform", translate_x=2.0, scale_x=1.25),
             node("o", "output")],
            [conn("fg", "t"), conn("t", "o")])
        digest = graph_params_digest(g)
        # 相同 (修订, 帧, 参数) 的键一致——播放顺序无关。
        key_a = composite_cache_key(
            graph_revision=3, frame=4, node_id="o", port="display",
            width=16, height=16, tile_rect=(0, 0, 16, 16),
            color_space="linear", alpha_premultiplied=False,
            encode_srgb=False, params_digest=digest)
        key_b = composite_cache_key(
            graph_revision=3, frame=4, node_id="o", port="display",
            width=16, height=16, tile_rect=(0, 0, 16, 16),
            color_space="linear", alpha_premultiplied=False,
            encode_srgb=False, params_digest=digest)
        assert key_a == key_b

        renderer = MetalCompositeRenderer()
        try:
            # 正放 0..4 → seek 回 2 → 帧内容与首次渲染 2 完全一致。
            forward = {f: renderer.render_readback(g, timeline_frame=f,
                                                   use_cache=True)
                       for f in range(5)}
            retry = renderer.render_readback(g, timeline_frame=2,
                                             use_cache=True)
            assert retry is not None
            np.testing.assert_array_equal(forward[2], retry)
            # 与 CPU 参考一致（内容寻址，与播放顺序无关）。
            ref2 = render_graph_cpu(g, timeline_frame=2)
            np.testing.assert_allclose(forward[2], ref2, atol=5e-3, rtol=0)
        finally:
            renderer.cleanup()

    def test_reverse_play_matches_forward_frames(self):
        from src.shared.gpu.metal import MetalComputeContext
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        from src.features.composite.render.metal.renderer import (
            MetalCompositeRenderer,
        )
        # 帧相关的静态图：constant 参数随图 dict 变化（不同 graph_revision
        # 的内容差异）；这里用 transform 平移不变、改 checker 位置不可行——
        # 直接用同一图不同帧 + 关缓存，验证倒放逐帧与正放逐帧一致。
        g = graph(
            [constant("fg", 12, 12, (1, 1, 1, 1), mode="checker",
                      color2=(0, 0, 0, 1), checker_size=3),
             node("o", "output")],
            [conn("fg", "o")])
        renderer = MetalCompositeRenderer()
        try:
            forward = {f: renderer.render_readback(g, timeline_frame=f,
                                                   use_cache=False)
                       for f in range(4)}
            backward = {f: renderer.render_readback(g, timeline_frame=f,
                                                    use_cache=False)
                        for f in reversed(range(4))}
            for f in range(4):
                np.testing.assert_array_equal(forward[f], backward[f])
        finally:
            renderer.cleanup()
