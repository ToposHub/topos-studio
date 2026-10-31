"""B3 批次（时间与数据通道）focused 回归测试。

对应 2026-10-14 复合页审计计划的 N-04/05/06(短期)/07(短期)/08/09/10/11
与 R-23：表达式/关键帧共存政策、唯一参数准备层（父变换保留）、时间
重映射先于参数求值（FrameHold 冻结上游动画）、共享源时间分支冲突
显式化、Retime 折叠精度、Cryptomatte 位模式身份与安全 float、数据
AOV 整数格式显式阻止。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core

import numpy as np


def _linear_track(param: str, end_frame: int = 24, end_value: float = 24.0):
    from src.shared.keyframe import Keyframe, KeyframeTrack

    track = KeyframeTrack(param, min_val=-10000.0, max_val=10000.0)
    track.add_keyframe(Keyframe(frame=0, value=0.0))
    track.add_keyframe(Keyframe(frame=end_frame, value=end_value))
    return track


# ── N-04：表达式/关键帧共存 + loop/valueAtTime 采样 ────────────────────


class TestExpressionKeyframeCoexistence:
    def _graph_with_track(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode

        graph = NodeGraph()
        node = TransformNode(node_id="t")
        graph.add_node(node)
        node.set_keyframe_track("translate_x", _linear_track("translate_x"))
        return graph, node

    def test_n04_expression_keeps_track_and_loops(self):
        graph, node = self._graph_with_track()
        ok, err = graph.set_node_expression(
            "t", "translate_x", 'loopOut("cycle")')
        assert ok and err is None
        assert "translate_x" in node.keyframe_tracks

        vals = [graph.snapshot(frame_number=f).nodes["t"].parameters[
            "translate_x"] for f in (0, 12, 24, 36)]
        assert vals == [0.0, 12.0, 24.0, 12.0]

    def test_n04_disabled_expression_falls_back_to_base(self):
        graph, node = self._graph_with_track()
        graph.set_node_expression("t", "translate_x", 'loopOut("cycle")')
        graph.set_node_expression_enabled("t", "translate_x", False)
        snap = graph.snapshot(frame_number=36)
        assert snap.nodes["t"].parameters["translate_x"] == 24.0

    def test_n04_value_at_time_samples_own_track(self):
        graph, node = self._graph_with_track()
        ok, _ = graph.set_node_expression(
            "t", "translate_x", "valueAtTime(0.5)")
        assert ok
        snap = graph.snapshot(frame_number=10)
        assert snap.nodes["t"].parameters["translate_x"] == 12.0

    def test_n04_roundtrip_preserves_both(self):
        graph, node = self._graph_with_track()
        graph.set_node_expression("t", "translate_x", 'loopOut("cycle")')
        data = node.to_dict()
        from src.features.composite.nodes.transform_node import TransformNode

        restored = TransformNode.from_dict(data)
        assert "translate_x" in restored.keyframe_tracks
        assert restored.get_expression("translate_x") == 'loopOut("cycle")'


# ── N-05：快照参数为唯一准备层（父变换保留）────────────────────────────


class TestResolvedParametersLayer:
    def test_n05_parent_transform_preserved_for_animated_child(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )

        graph = NodeGraph()
        parent = TransformNode(node_id="p")
        child = TransformNode(node_id="c")
        graph.add_node(parent)
        graph.add_node(child)
        parent.set_parameter("translate_x", 100.0)
        child.parent_id = "p"
        child.set_keyframe_track("translate_x", _linear_track(
            "translate_x", end_frame=10, end_value=10.0))

        snap = graph.snapshot(frame_number=5)
        assert snap.nodes["c"].parameters["translate_x"] == 105.0

        class H(BaseEvaluatorMixin):
            pass

        host = H()
        host._snapshot = snap
        ephemeral = host._create_ephemeral_node(snap.nodes["c"])
        # 渲染侧不得用原始轨道(0→10@5=5)覆盖世界参数(105)
        assert ephemeral.parameters["translate_x"] == 105.0

    def test_n05_expression_result_not_overridden_by_renderer(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )

        graph = NodeGraph()
        node = TransformNode(node_id="t")
        graph.add_node(node)
        node.set_keyframe_track("translate_x", _linear_track("translate_x"))
        graph.set_node_expression("t", "translate_x", "value + 100")

        snap = graph.snapshot(frame_number=24)
        assert snap.nodes["t"].parameters["translate_x"] == 124.0

        class H(BaseEvaluatorMixin):
            pass

        host = H()
        host._snapshot = snap
        ephemeral = host._create_ephemeral_node(snap.nodes["t"])
        assert ephemeral.parameters["translate_x"] == 124.0


# ── N-06（短期）：FrameHold 冻结上游动画参数 ───────────────────────────


class TestTimeFreezeParameters:
    def test_n06_frame_hold_freezes_upstream_animated_params(self):
        from src.features.composite.core.node_engine import (
            NodeGraph, NODE_REGISTRY,
        )
        from src.features.composite.nodes.transform_node import TransformNode
        from src.features.composite.nodes.output_node import OutputNode

        graph = NodeGraph()
        anim = TransformNode(node_id="anim")
        hold = NODE_REGISTRY["frame_hold"](node_id="hold")
        out = OutputNode(node_id="out")
        for n in (anim, hold, out):
            graph.add_node(n)
        anim.set_keyframe_track("translate_x", _linear_track(
            "translate_x", end_frame=100, end_value=100.0))
        hold.set_parameter("hold_frame", 5)
        assert graph.connect("anim", "result", "hold", "input")
        assert graph.connect("hold", "result", "out", "image")

        snap = graph.snapshot(frame_number=20)
        # 帧 20 的上游 Transform 参数冻结在 hold 帧（5），不是 20
        assert snap.nodes["anim"].parameters["translate_x"] == 5.0
        assert "anim" in snap.frame_overrides


# ── N-07（短期）：共享源时间分支冲突显式化 ─────────────────────────────


class TestSharedSourceTimeConflict:
    def test_n07_conflict_surfaced_not_silent(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.video_frame_node import VideoFrameNode
        from src.features.composite.nodes.time_offset_node import TimeOffsetNode
        from src.features.composite.nodes.blend_node import MergeNode

        graph = NodeGraph()
        vid = VideoFrameNode(node_id="vid")
        a = TimeOffsetNode(node_id="a")
        b = TimeOffsetNode(node_id="b")
        m = MergeNode(node_id="m")
        for n in (vid, a, b, m):
            graph.add_node(n)
        a.set_parameter("frame_offset", 5)
        b.set_parameter("frame_offset", 9)
        assert graph.connect("vid", "image", "a", "input")
        assert graph.connect("vid", "image", "b", "input")
        assert graph.connect("a", "result", "m", "A")
        assert graph.connect("b", "result", "m", "B")

        snap = graph.snapshot(frame_number=10)
        # first-wins 仍是 v1 语义，但冲突必须可定位（不再静默）
        assert snap.time_conflicts
        assert "vid" in snap.time_conflicts[0]


# ── N-08：Retime 折叠精度 ──────────────────────────────────────────────


class TestRetimeFoldPrecision:
    def test_n08_fractional_speeds_chain_not_folded(self):
        from src.features.composite.core.time_remap import TimeRemap

        inner = TimeRemap(speed=0.5)
        outer = TimeRemap(speed=2.0)
        merged = inner.pushed_upstream(outer)
        sequential = [outer.apply(inner.apply(f)) for f in (1, 3, 5)]
        combined = [merged.apply(f) for f in (1, 3, 5)]
        assert sequential == combined == [0, 4, 4]
        assert merged.inner is not None  # 保留链，不折叠

    def test_n08_integer_speeds_still_fold(self):
        from src.features.composite.core.time_remap import TimeRemap

        merged = TimeRemap(speed=2.0, offset=1).pushed_upstream(
            TimeRemap(speed=3.0, offset=2))
        assert merged.inner is None
        assert merged.speed == 6.0
        for f in (0, 1, 5, 13):
            seq = TimeRemap(speed=3.0, offset=2).apply(
                TimeRemap(speed=2.0, offset=1).apply(f))
            assert merged.apply(f) == seq


# ── N-09/N-10/N-11：Cryptomatte 身份与安全 float ───────────────────────


class TestCryptomatteIdentity:
    def test_n10_ten_thousand_names_finite_unique(self):
        from src.features.composite.render3d.cryptomatte import (
            cryptomatte_float_id,
        )

        seen = set()
        for i in range(10000):
            value = cryptomatte_float_id(f"object_{i}")
            assert np.isfinite(value)
            key = float(value).hex()
            assert key not in seen, f"duplicate id for object_{i}"
            seen.add(key)

    def test_n09_tiny_ids_selectable_simultaneously(self):
        from src.features.composite.render3d.cryptomatte import (
            cryptomatte_float_id,
        )
        from src.features.composite.nodes.cryptomatte_select_node import (
            CryptomatteSelectNode,
        )

        node = CryptomatteSelectNode("t")
        id0 = cryptomatte_float_id("object_0")
        id3 = cryptomatte_float_id("object_3")
        node.add_selection(id0, "object_0")
        node.add_selection(id3, "object_3")
        selected = node.get_selected_ids()
        assert len(selected) == 2
        assert id0 in selected and id3 in selected

        node.remove_selection(id0)
        assert node.get_selected_ids() == [id3]

    def test_n11_bad_json_structures_return_empty(self):
        from src.features.composite.nodes.cryptomatte_select_node import (
            CryptomatteSelectNode,
        )

        node = CryptomatteSelectNode("t")
        node.parameters["selected_ids"] = "null"
        node.parameters["selected_names"] = "null"
        node.parameters["manifest"] = "[]"
        assert node.get_selected_ids() == []
        assert node.get_selected_names() == []
        assert node.get_manifest() == {}

    def test_n09_shader_uses_bit_pattern_compare(self):
        from src.features.composite.core.shaders import (
            _CRYPTOMATTE_SELECT_FRAGMENT,
        )

        source = _CRYPTOMATTE_SELECT_FRAGMENT
        assert "floatBitsToUint" in source
        # float_r 路径不再使用绝对 epsilon
        assert "matches(sample_value.r, 0.000001)" not in source


# ── R-23：数据 AOV 越界值显式阻止 ──────────────────────────────────────


class TestDataAovClipping:
    def test_r23_out_of_range_data_pass_blocked(self):
        from src.features.composite.render.composite_render_output import (
            _convert_for_image_ext,
        )

        img = np.array([[[-2.0, 0.5, 3.0]]], np.float32)
        with pytest.raises(ValueError, match="EXR|裁切"):
            _convert_for_image_ext(img, "png", None, None,
                                   render_pass="motion_vector")
        with pytest.raises(ValueError):
            _convert_for_image_ext(img, "tif", None, None,
                                   render_pass="position")

    def test_r23_in_range_data_pass_and_exr_ok(self):
        from src.features.composite.render.composite_render_output import (
            _convert_for_image_ext,
        )

        in_range = np.array([[[0.0, 0.5, 1.0]]], np.float32)
        out = _convert_for_image_ext(in_range, "png", None, None,
                                     render_pass="normal")
        assert out.dtype == np.uint8

        signed = np.array([[[-2.0, 0.5, 3.0]]], np.float32)
        preserved = _convert_for_image_ext(signed, "exr", None, None,
                                           render_pass="position")
        assert preserved[0, 0].tolist() == [-2.0, 0.5, 3.0]
