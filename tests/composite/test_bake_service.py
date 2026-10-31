"""A6 Render-in-Place (bake subtree) service tests.

The service is renderer-independent (render_fn injected), so the graph
rewrite, sequence naming, undo payload and unbake round-trip are all
verified without a GL context.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.bake_service import (
    BAKED_TITLE_SUFFIX,
    apply_bake_rewrite,
    apply_unbake_rewrite,
    build_sequence_path,
    make_undo_action,
    plan_bake,
    plan_unbake,
    render_sequence,
    sequence_pattern_for,
)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.invert_node import InvertNode

pytestmark = pytest.mark.composite_core


def _make_graph():
    """constant → invert → blur, with output as the blur's consumer."""
    graph = NodeGraph()
    src = ConstantNode("src")
    inv = InvertNode("inv")
    blur = BlurNode("blur")
    out = ImageInputNode("viewer")  # any consumer stand-in
    out.user_label = "Viewer stand-in"
    for node in (src, inv, blur, out):
        graph.add_node(node)
    graph.connect("src", "output", "inv", "image")
    graph.connect("inv", "result", "blur", "image")
    graph.connect("blur", "result", "out", "image")
    return graph


class TestSequenceNaming:
    def test_frame_path(self):
        p = build_sequence_path("/cache/x", "bake_inv", 3)
        assert p.endswith("bake_inv.0003.exr")

    def test_pattern(self):
        p = sequence_pattern_for("/cache/x", "bake_inv")
        assert p.endswith("bake_inv.####.exr")


class TestRenderSequence:
    def test_renders_each_frame_via_callback(self, tmp_path):
        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3,
                         base_name="bake_inv")
        calls = []

        def render_fn(node_id, frame, out_path):
            calls.append((node_id, frame, out_path))
            with open(out_path, "wb") as f:
                f.write(b"x")
            return True

        written, error = render_sequence(graph, plan, str(tmp_path),
                                         "bake_inv", render_fn)
        assert error == ""
        assert len(written) == 3
        assert [c[1] for c in calls] == [1, 2, 3]
        assert all(c[0] == "inv" for c in calls)

    def test_failure_reports_frame_and_partial_files(self, tmp_path):
        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 5,
                         base_name="bake_inv")

        def render_fn(node_id, frame, out_path):
            return frame <= 2  # frame 3 fails

        written, error = render_sequence(graph, plan, str(tmp_path),
                                         "bake_inv", render_fn)
        assert "frame 3" in error
        assert len(written) == 2

    def test_cancellation(self, tmp_path):
        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 9,
                         base_name="bake_inv")
        written, error = render_sequence(
            graph, plan, str(tmp_path), "bake_inv",
            lambda *a: True, is_cancelled=lambda: True)
        assert error == "cancelled"
        assert written == []


class TestGraphRewrite:
    def test_bake_rewires_consumers_and_disables_target(self, tmp_path):
        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)

        bake_node = graph.get_node(plan.bake_node_id)
        assert bake_node is not None
        assert bake_node.node_type == "image_input"
        assert bake_node.parameters["file_path"].endswith("####.exr")
        # Target disabled + suffix-labelled, still present for unbake.
        inv = graph.get_node("inv")
        assert inv.disabled is True
        assert inv.display_title.endswith(BAKED_TITLE_SUFFIX)
        # The consumer (blur) is now fed by the bake node.
        blur_inputs = [c for c in graph.connections
                       if c.dst_node_id == "blur"]
        assert [(c.src_node_id, c.src_port_name) for c in blur_inputs] \
            == [(plan.bake_node_id, "image")]
        # Serialized bake node keeps its params (undo/redo safe).
        data = json.loads(json.dumps(bake_node.to_dict()))
        assert data["parameters"]["file_path"].endswith("####.exr")

    def test_undo_action_restores_graph(self, tmp_path):
        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)

        restored = []

        def applier(data):
            new_graph = NodeGraph.from_dict(data)
            restored.append(new_graph)
            return new_graph

        action = make_undo_action(plan, applier)
        action.data["before"] = plan.before_graph
        action.data["after"] = plan.after_graph
        # Undo → original graph shape.
        action.undo_fn(action.data)
        g = restored[-1]
        assert g.get_node(plan.bake_node_id) is None
        assert g.get_node("inv").disabled is False
        # Redo → baked shape again.
        action.redo_fn(action.data)
        g = restored[-1]
        assert g.get_node(plan.bake_node_id) is not None
        assert g.get_node("inv").disabled is True


class TestUnbake:
    def test_plan_and_rewrite(self, tmp_path):
        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 5, 9)
        apply_bake_rewrite(graph, plan)

        unbake = plan_unbake(graph, plan.bake_node_id)
        assert unbake is not None
        assert unbake.target_id == "inv"
        assert unbake.frame_start == 5 and unbake.frame_end == 9
        assert not unbake.original_title.endswith(BAKED_TITLE_SUFFIX)
        assert unbake.original_title == "Invert"

        apply_unbake_rewrite(graph, unbake)
        assert graph.get_node(plan.bake_node_id) is None
        inv = graph.get_node("inv")
        assert inv.disabled is False
        assert not inv.display_title.endswith(BAKED_TITLE_SUFFIX)
        # The consumer is back on the original output.
        blur_inputs = [c.src_node_id for c in graph.connections
                       if c.dst_node_id == "blur"]
        assert blur_inputs == ["inv"]

    def test_plan_unbake_missing_nodes(self, tmp_path):
        graph = _make_graph()
        assert plan_unbake(graph, "nope_bake") is None


class TestBakeSourceSignature:
    """F7.3 · 烘焙绑定源签名——更新原图不悄悄播放旧烘焙。"""

    def test_fresh_bake_not_stale(self, tmp_path):
        from src.features.composite.bake_service import bake_is_stale

        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)
        assert bake_is_stale(graph, plan.bake_node_id) is False

    def test_upstream_param_change_marks_stale(self, tmp_path):
        """上游参数变化 → 签名不匹配（烘焙过期，不再被信任）。"""
        from src.features.composite.bake_service import bake_is_stale

        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)
        # 改被烘焙目标自身的参数（禁用态参数仍在）
        graph.get_node("inv").set_parameter("amount", 0.9)
        assert bake_is_stale(graph, plan.bake_node_id) is True

    def test_deep_upstream_change_marks_stale(self, tmp_path):
        """上游锥的深层参数变化同样过期。"""
        from src.features.composite.bake_service import bake_is_stale

        graph = _make_graph()
        plan = plan_bake(graph, "blur", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)
        # blur 的上游锥 = inv→blur（fixture 实际连通边）
        graph.get_node("inv").set_parameter("amount", 0.77)
        assert bake_is_stale(graph, plan.bake_node_id) is True

    def test_unrelated_node_change_not_stale(self, tmp_path):
        """锥外节点变化不影响烘焙（精确失效域）。"""
        from src.features.composite.bake_service import bake_is_stale

        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)
        graph.get_node("blur").set_parameter("radius", 9.0)  # 下游
        assert bake_is_stale(graph, plan.bake_node_id) is False

    def test_legacy_bake_without_signature_returns_none(self, tmp_path):
        """无签名的旧烘焙 → None（不冒称新鲜/过期）。"""
        from src.features.composite.bake_service import (
            BAKE_SOURCE_SIGNATURE_PARAM,
            bake_is_stale,
        )

        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)
        bake_node = graph.get_node(plan.bake_node_id)
        bake_node.parameters.pop(BAKE_SOURCE_SIGNATURE_PARAM, None)
        assert bake_is_stale(graph, plan.bake_node_id) is None

    def test_signature_survives_serialization(self, tmp_path):
        """签名随 bake 节点序列化（重开后过期检测仍可用）。"""
        import json as _json

        from src.features.composite.bake_service import (
            BAKE_SOURCE_SIGNATURE_PARAM,
        )

        graph = _make_graph()
        plan = plan_bake(graph, "inv", str(tmp_path), 1, 3)
        apply_bake_rewrite(graph, plan)
        data = _json.loads(_json.dumps(graph.to_dict()))
        restored = NodeGraph.from_dict(data)
        bake_node = restored.get_node(plan.bake_node_id)
        assert bake_node.parameters.get(BAKE_SOURCE_SIGNATURE_PARAM)


# ================================================================
# M08.1（S10）· 签名含连线/端口
# ================================================================

class TestSignatureIncludesConnections:
    from types import SimpleNamespace as NS

    @staticmethod
    def _conns():
        NS = TestSignatureIncludesConnections.NS
        return [
            NS(src_node_id="a", src_port_name="out",
               dst_node_id="merge", dst_port_name="A"),
            NS(src_node_id="b", src_port_name="out",
               dst_node_id="merge", dst_port_name="B"),
        ]

    @classmethod
    def _graph(cls, connections=None):
        NS = cls.NS
        nodes = {i: NS(parameters={"v": i}, enabled=True)
                 for i in ("a", "b", "merge")}
        return NS(
            connections=cls._conns() if connections is None else connections,
            get_node=nodes.get)

    def test_port_swap_changes_signature(self):
        from src.features.composite.bake_service import _bake_source_signature
        conns = self._conns()
        first = _bake_source_signature(self._graph(conns), "merge")
        conns[0].dst_port_name, conns[1].dst_port_name = "B", "A"
        # 交换 Merge A/B 端口 = 上游结构变化 → 签名必须变（陈旧可检测）
        assert first != _bake_source_signature(self._graph(conns), "merge")

    def test_same_structure_same_signature(self):
        from src.features.composite.bake_service import _bake_source_signature
        assert (_bake_source_signature(self._graph(), "merge")
                == _bake_source_signature(self._graph(), "merge"))

    def test_param_change_changes_signature(self):
        from src.features.composite.bake_service import _bake_source_signature
        graph = self._graph()
        first = _bake_source_signature(graph, "merge")
        graph.get_node("a").parameters["v"] = 99
        assert first != _bake_source_signature(graph, "merge")


class TestAuditBakeStaleness:
    from types import SimpleNamespace as NS

    def test_audit_reports_stale_and_fresh(self):
        from src.features.composite.bake_service import (
            BAKE_SOURCE_SIGNATURE_PARAM, _bake_source_signature,
            audit_bake_staleness,
        )
        conns = lambda: [
            self.NS(src_node_id="a", src_port_name="out",
                    dst_node_id="merge", dst_port_name="A")]
        sig = _bake_source_signature(
            self.NS(connections=conns(), get_node={
                "a": self.NS(parameters={"v": 1}, enabled=True),
                "merge": self.NS(parameters={}, enabled=True)}.get),
            "merge")
        # 新鲜签名
        fresh = self.NS(nodes={
            "bake_input_merge": self.NS(parameters={
                BAKE_SOURCE_SIGNATURE_PARAM: sig}, enabled=True),
            "a": self.NS(parameters={"v": 1}, enabled=True),
            "merge": self.NS(parameters={}, enabled=True)},
            connections=conns(), get_node=None)
        # 适配：audit 的 snapshot shim 用 nodes/connections；构造 graph 形
        bake_node = self.NS(parameters={
            BAKE_SOURCE_SIGNATURE_PARAM: sig}, enabled=True)
        node_a = self.NS(parameters={"v": 1}, enabled=True)
        node_merge = self.NS(parameters={}, enabled=True)
        table = {"bake_input_merge": bake_node, "a": node_a,
                 "merge": node_merge}
        graph = self.NS(
            nodes=table,
            connections=[
                self.NS(src_node_id="a", src_port_name="out",
                        dst_node_id="merge", dst_port_name="A")],
            get_node=table.get)
        result = audit_bake_staleness(graph)
        assert len(result) == 1
        assert result[0]["stale"] is False
        # 上游变化 → stale
        node_a.parameters["v"] = 99
        result2 = audit_bake_staleness(graph)
        assert result2[0]["stale"] is True

    def test_audit_snapshot_form_supported(self):
        """GraphSnapshot 形态（无 get_node）也能审计。"""
        from src.features.composite.bake_service import (
            BAKE_SOURCE_SIGNATURE_PARAM, _bake_source_signature,
            audit_bake_staleness,
        )
        src_graph = self.NS(
            connections=[], get_node={
                "t": self.NS(parameters={"p": 1}, enabled=True)}.get)
        sig = _bake_source_signature(src_graph, "t")
        snap = self.NS(
            nodes={"bake_input_t": self.NS(parameters={
                BAKE_SOURCE_SIGNATURE_PARAM: sig}, enabled=True),
                "t": self.NS(parameters={"p": 1}, enabled=True)},
            connections=[])
        result = audit_bake_staleness(snap)
        assert result[0]["stale"] is False


# ================================================================
# M08.2 · Bake 协调器（发布事务）
# ================================================================

class TestExecuteBake:
    # OpenCV 的 EXR 编解码需显式启用（仓库其他 EXR 测试同款处理）
    import os as _os
    _os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

    @staticmethod
    def _graph():
        from types import SimpleNamespace as NS
        nodes = {"t": NS(parameters={"p": 1}, enabled=True,
                         display_title="T", outputs=None,
                         user_label=None)}
        return NS(nodes=nodes, connections=[],
                  get_node=nodes.get, to_dict=lambda: {"nodes": {"t": {}}})

    @staticmethod
    def _render_ok(node_id, frame, out_path):
        import cv2
        import numpy as np
        # 真实 EXR 帧（默认校验走 cv2 实际解码）
        return cv2.imwrite(
            out_path,
            np.full((4, 4, 3), frame / 10.0, dtype=np.float32))

    def test_success_publishes_with_manifest_and_rewrites(self, tmp_path):
        import json as _json
        from src.features.composite.bake_service import execute_bake
        graph = self._graph()
        rewritten = []
        import src.features.composite.bake_service as bs
        orig_rewrite = bs.apply_bake_rewrite
        bs.apply_bake_rewrite = lambda g, p: rewritten.append(p.target_id)
        try:
            result = execute_bake(
                graph, "t", str(tmp_path), 0, 2, self._render_ok)
        finally:
            bs.apply_bake_rewrite = orig_rewrite
        assert result.ok, result.error
        assert len(result.published) == 3
        assert all(os.path.isfile(p) for p in result.published)
        assert os.path.isfile(result.manifest_path)
        manifest = _json.load(open(result.manifest_path))
        assert len(manifest["frames"]) == 3
        assert all(os.path.basename(p) in str(manifest["frames"]) or p in manifest["frames"]
                   for p in result.published)
        assert rewritten == ["t"]
        # 临时目录清理
        assert not any(n.startswith(".topos-") for n in os.listdir(tmp_path))

    def test_frame_failure_no_publish_no_partial(self, tmp_path):
        from src.features.composite.bake_service import execute_bake

        def render_fails_at_2(node_id, frame, out_path):
            import cv2
            import numpy as np
            if frame == 2:
                return False
            return cv2.imwrite(
                out_path, np.zeros((4, 4, 3), np.float32))

        graph = self._graph()
        result = execute_bake(
            graph, "t", str(tmp_path), 0, 2, render_fails_at_2)
        assert not result.ok and result.stage == "render"
        # 未发布任何序列文件/manifest；临时清理
        leftovers = [n for n in os.listdir(tmp_path)
                     if not n.startswith(".topos-")]
        assert leftovers == []
        assert not any(n.startswith(".topos-") for n in os.listdir(tmp_path))

    def test_old_success_preserved_on_verify_failure(self, tmp_path):
        from src.features.composite.bake_service import execute_bake
        # 旧成功产物
        old_frame = tmp_path / "bake_t.0001.exr"
        old_frame.write_bytes(b"OLD-SUCCESS")
        # 新渲染"成功"但校验不可读（verify_fn 拒绝）
        graph = self._graph()
        result = execute_bake(
            graph, "t", str(tmp_path), 1, 1, self._render_ok,
            verify_fn=lambda p: False)
        assert not result.ok and result.stage == "verify"
        # 旧文件原封不动
        assert old_frame.read_bytes() == b"OLD-SUCCESS"

    def test_source_change_mid_bake_aborts_publish(self, tmp_path):
        from src.features.composite.bake_service import execute_bake
        graph = self._graph()

        def render_mutates(node_id, frame, out_path):
            import cv2
            import numpy as np
            ok = cv2.imwrite(out_path, np.zeros((4, 4, 3), np.float32))
            # 烘焙期间源被改动（参数变化）
            graph.get_node("t").parameters["p"] = 99
            return ok

        result = execute_bake(
            graph, "t", str(tmp_path), 0, 1, render_mutates)
        assert not result.ok and result.stage == "source_changed"
        # 不发布
        assert not [n for n in os.listdir(tmp_path)
                    if not n.startswith(".topos-")]
