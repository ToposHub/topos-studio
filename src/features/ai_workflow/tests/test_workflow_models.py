"""P5.1 测试：WorkflowDefinition 数据模型（plan §3.2 / §4.2 schema）。

验证 :class:`WorkflowGraph` 的核心契约（plan §3.2 最小 schema）：

- ``schema`` = "topos.workflow/1"；``workflow_id`` 唯一；``schema_version``。
- ``nodes``：每个 node 有 ``node_id``（唯一）/ ``spec_id`` / ``spec_version`` /
  ``params`` / ``ui``（layout）。
- ``edges``：``from``/``to`` 是 ``PortRef(node_id, port)``；端口名 + 方向明确。
- ``ui``：viewport / notes / groups（布局元数据）。
- ``policy``：``network`` / ``timeline_write``（默认 ask / approval_required）。
- ``exposed_inputs``：把节点内部端口暴露为 workflow 顶层输入（plan §3.2）。

红线（plan §3.1 / §1.4）：

- 端口不传 Qt 对象 / OpenGL texture / 模型实例 / 线程对象 / API key / 绝对路径。
- WorkflowGraph 是 frozen + 可序列化为 plan §3.2 的 JSON 形态。
- 节点 ``spec_id`` 必须非空；``node_id`` 在同一 graph 内唯一。
- DAG 校验在 :class:`WorkflowValidator`（P5.2），本模型只做基本字段校验。

本模型与 Composite 的 ``NodeGraph``（GPU 渲染语义）是**两套东西**，不复用。
"""
from __future__ import annotations

import pytest

from src.features.ai_workflow.core.workflow_models import (
    PortRef,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPolicy,
    WorkflowUi,
    parse_workflow_dict,
)


# ---------------------------------------------------------------------- #
# PortRef
# ---------------------------------------------------------------------- #


class TestPortRef:
    def test_construction_and_equality(self) -> None:
        a = PortRef(node_id="n1", port="assets")
        b = PortRef(node_id="n1", port="assets")
        c = PortRef(node_id="n1", port="transcript")
        assert a == b
        assert a != c
        assert hash(a) == hash(b)

    def test_to_dict_roundtrip(self) -> None:
        ref = PortRef(node_id="input", port="assets")
        d = ref.to_dict()
        assert d == {"node_id": "input", "port": "assets"}
        assert PortRef.from_dict(d) == ref

    def test_empty_node_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            PortRef(node_id="", port="x")


# ---------------------------------------------------------------------- #
# WorkflowNode
# ---------------------------------------------------------------------- #


def _make_node(node_id: str = "n1", spec_id: str = "topos.media.selected_assets") -> WorkflowNode:
    return WorkflowNode(node_id=node_id, spec_id=spec_id)


class TestWorkflowNode:
    def test_basic_construction(self) -> None:
        n = WorkflowNode(
            node_id="input",
            spec_id="topos.media.selected_assets",
            spec_version="1.0.0",
            params={"media_kind": "video"},
            ui={"x": 120, "y": 200},
        )
        assert n.node_id == "input"
        assert n.spec_id == "topos.media.selected_assets"
        assert n.params == {"media_kind": "video"}
        assert n.ui == {"x": 120, "y": 200}

    def test_empty_node_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowNode(node_id="", spec_id="topos.x")

    def test_empty_spec_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowNode(node_id="n1", spec_id="")

    def test_to_dict_roundtrip(self) -> None:
        n = _make_node()
        d = n.to_dict()
        back = WorkflowNode.from_dict(d)
        assert back == n

    def test_default_spec_version_is_1_0_0(self) -> None:
        n = _make_node()
        assert n.spec_version == "1.0.0"

    def test_default_ui_is_empty(self) -> None:
        n = _make_node()
        assert n.ui == {}


# ---------------------------------------------------------------------- #
# WorkflowEdge
# ---------------------------------------------------------------------- #


class TestWorkflowEdge:
    def test_construction(self) -> None:
        e = WorkflowEdge(
            from_port=PortRef(node_id="input", port="assets"),
            to_port=PortRef(node_id="asr", port="assets"),
        )
        assert e.from_port.node_id == "input"
        assert e.to_port.port == "assets"

    def test_self_loop_rejected(self) -> None:
        """红线：edge 不能自连（同 node 同 port）。"""
        with pytest.raises(ValueError):
            WorkflowEdge(
                from_port=PortRef(node_id="n1", port="x"),
                to_port=PortRef(node_id="n1", port="x"),
            )

    def test_to_dict_roundtrip(self) -> None:
        e = WorkflowEdge(
            from_port=PortRef(node_id="input", port="assets"),
            to_port=PortRef(node_id="asr", port="assets"),
        )
        d = e.to_dict()
        back = WorkflowEdge.from_dict(d)
        assert back == e


# ---------------------------------------------------------------------- #
# WorkflowPolicy
# ---------------------------------------------------------------------- #


class TestWorkflowPolicy:
    def test_defaults(self) -> None:
        p = WorkflowPolicy()
        assert p.network == "ask"
        assert p.timeline_write == "approval_required"
        assert p.cost_budget is None
        assert p.allow_model_download is False
        assert p.allow_generation is False

    def test_invalid_network_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowPolicy(network="bogus")

    def test_invalid_timeline_write_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowPolicy(timeline_write="bogus")

    def test_to_dict_roundtrip(self) -> None:
        p = WorkflowPolicy(network="forbidden", timeline_write="forbidden")
        d = p.to_dict()
        back = WorkflowPolicy.from_dict(d)
        assert back == p


# ---------------------------------------------------------------------- #
# WorkflowUi
# ---------------------------------------------------------------------- #


class TestWorkflowUi:
    def test_defaults(self) -> None:
        ui = WorkflowUi()
        assert ui.viewport["zoom"] == 1.0
        assert ui.notes == ()
        assert ui.groups == ()

    def test_to_dict_roundtrip(self) -> None:
        ui = WorkflowUi(
            viewport={"zoom": 1.5, "center": [10, 20]},
            notes=(("note-1", "记得检查字幕"),),
            groups=(("group-1", ("n1", "n2")),),
        )
        d = ui.to_dict()
        back = WorkflowUi.from_dict(d)
        assert back == ui


# ---------------------------------------------------------------------- #
# WorkflowGraph —— 核心
# ---------------------------------------------------------------------- #


def _auto_caption_graph() -> WorkflowGraph:
    """plan §3.2 / §9.1 '自动字幕' 模板的简化版。"""
    return WorkflowGraph(
        workflow_id="wf-auto-caption",
        name="自动字幕（本地优先）",
        nodes=(
            WorkflowNode(
                node_id="input",
                spec_id="topos.media.selected_assets",
                params={"media_kind": "video_or_audio"},
                ui={"x": 120, "y": 200},
            ),
            WorkflowNode(
                node_id="asr",
                spec_id="topos.analysis.transcribe_auto",
                ui={"x": 360, "y": 200},
            ),
            WorkflowNode(
                node_id="caption",
                spec_id="topos.caption.propose_track",
                ui={"x": 600, "y": 200},
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="asr", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="asr", port="transcript"),
                to_port=PortRef(node_id="caption", port="transcript"),
            ),
        ),
        policy=WorkflowPolicy(),
    )


class TestWorkflowGraph:
    def test_schema_constant(self) -> None:
        assert WorkflowGraph.SCHEMA == "topos.workflow/1"

    def test_basic_construction(self) -> None:
        g = _auto_caption_graph()
        assert g.workflow_id == "wf-auto-caption"
        assert g.schema_version == 1
        assert len(g.nodes) == 3
        assert len(g.edges) == 2

    def test_empty_workflow_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowGraph(workflow_id="", name="x")

    def test_duplicate_node_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowGraph(
                workflow_id="wf",
                name="x",
                nodes=(
                    WorkflowNode(node_id="dup", spec_id="topos.x"),
                    WorkflowNode(node_id="dup", spec_id="topos.y"),
                ),
            )

    def test_edge_dangling_node_rejected(self) -> None:
        """edge 引用不存在的 node → 拒绝（基本完整性；DAG/环在 validator）。"""
        with pytest.raises(ValueError):
            WorkflowGraph(
                workflow_id="wf", name="x",
                nodes=(WorkflowNode(node_id="n1", spec_id="topos.x"),),
                edges=(
                    WorkflowEdge(
                        from_port=PortRef(node_id="n1", port="a"),
                        to_port=PortRef(node_id="ghost", port="b"),  # 不存在
                    ),
                ),
            )

    def test_to_dict_matches_plan_schema(self) -> None:
        """序列化结果匹配 plan §3.2 的 JSON schema 形态。"""
        g = _auto_caption_graph()
        d = g.to_dict()
        assert d["schema"] == "topos.workflow/1"
        assert d["workflow_id"] == "wf-auto-caption"
        assert d["schema_version"] == 1
        assert d["name"] == "自动字幕（本地优先）"
        assert len(d["nodes"]) == 3
        assert d["nodes"][0]["node_id"] == "input"
        assert d["nodes"][0]["spec_id"] == "topos.media.selected_assets"
        assert d["edges"][0]["from"] == {"node_id": "input", "port": "assets"}
        assert d["policy"]["network"] == "ask"
        assert d["policy"]["timeline_write"] == "approval_required"

    def test_from_dict_roundtrip(self) -> None:
        g = _auto_caption_graph()
        d = g.to_dict()
        back = WorkflowGraph.from_dict(d)
        assert back == g

    def test_workflow_hash_stable(self) -> None:
        """内容 hash 稳定（同内容 → 同 hash；内容变 → hash 变）。"""
        g1 = _auto_caption_graph()
        g2 = _auto_caption_graph()
        assert g1.content_hash == g2.content_hash
        g3 = WorkflowGraph(
            workflow_id="wf-other", name="其它",
            nodes=g1.nodes, edges=g1.edges,
        )
        assert g3.content_hash != g1.content_hash


# ---------------------------------------------------------------------- #
# parse_workflow_dict —— 从 dict/JSON 构造
# ---------------------------------------------------------------------- #


class TestParseWorkflowDict:
    def test_parse_full_dict(self) -> None:
        raw = {
            "schema": "topos.workflow/1",
            "workflow_id": "wf-test",
            "schema_version": 1,
            "name": "测试",
            "nodes": [
                {"node_id": "n1", "spec_id": "topos.media.selected_assets",
                 "spec_version": "1.0.0", "params": {}, "ui": {}},
            ],
            "edges": [],
            "ui": {"viewport": {"zoom": 1.0}, "notes": [], "groups": []},
            "policy": {"network": "ask", "timeline_write": "approval_required"},
        }
        g = parse_workflow_dict(raw)
        assert g.workflow_id == "wf-test"
        assert len(g.nodes) == 1

    def test_wrong_schema_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema"):
            parse_workflow_dict({"schema": "other.schema/1", "workflow_id": "x", "name": "x"})

    def test_missing_schema_defaults_to_v1(self) -> None:
        g = parse_workflow_dict({"workflow_id": "x", "name": "x"})
        assert g.schema_version == 1


# ---------------------------------------------------------------------- #
# exposed_inputs —— 把节点内部端口暴露为 workflow 顶层输入（plan §3.2）
# ---------------------------------------------------------------------- #


class TestExposedInputs:
    def test_default_empty(self) -> None:
        g = _auto_caption_graph()
        assert g.exposed_inputs == ()

    def test_expose_input_port(self) -> None:
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),),
            exposed_inputs=(
                # (exposed_name, node_id, port)
                ("assets", "input", "assets"),
            ),
        )
        assert len(g.exposed_inputs) == 1
        assert g.exposed_inputs[0][0] == "assets"

    def test_exposed_input_dangling_node_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowGraph(
                workflow_id="wf", name="x",
                nodes=(WorkflowNode(node_id="input", spec_id="topos.x"),),
                exposed_inputs=(("assets", "ghost", "assets"),),
            )
