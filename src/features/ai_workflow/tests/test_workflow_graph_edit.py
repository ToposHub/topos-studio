"""P10.1b 测试：WorkflowGraphEdit（冻结模型变更层）。

> :class:`WorkflowGraph` 全 ``frozen=True``（``workflow_models.py:268``），无 mutation
> API。画布编辑（拖拽/连线/参数）需要改 graph。本批建纯 Python 服务
> :class:`WorkflowGraphEdit`，用 ``dataclasses.replace`` 产**新** graph（原 graph 不变），
> 复用 ``WorkflowGraph.__post_init__`` 的校验（重复 node_id / edge ghost / 自连）。

验证契约：

- ``move_node`` 后新 graph 的 node.ui.x/y 更新；**原 graph 不变**。
- ``set_node_param`` 后 params 更新；非 JSON-safe value 抛 ValueError。
- ``add_edge`` 成功；重复边抛 ValueError；自连抛 ValueError（靠 WorkflowEdge 校验）。
- ``remove_edge`` 后 edges 少一条；不存在的边 no-op（返回 graph 不变）。
- ``move_nodes`` 批量。
- 非法 node_id 抛 KeyError（require_node）。
- 红线：源码不 import moderngl / composite.core / Qt。
- 冻结模型不变：原 graph 引用字段不变。
"""
import os

import pytest

from src.features.ai_workflow.core.workflow_models import (
    PortRef, WorkflowEdge, WorkflowGraph, WorkflowNode,
)


# ---------------------------------------------------------------------- #
# 红线：源码扫描
# ---------------------------------------------------------------------- #


_SRC = os.path.join(
    os.path.dirname(__file__), "..", "core", "workflow_graph_edit.py",
)


def test_source_does_not_import_gpu_or_qt_or_composite() -> None:
    """红线：edit 层纯 Python，不 import moderngl / composite.core / Qt。"""
    with open(_SRC, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite" not in source
    assert "TextureResult" not in source
    assert "from PyQt6" not in source
    assert "import PyQt6" not in source


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


def _node(node_id: str, *, x: float = 0.0, y: float = 0.0, params=None) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id, spec_id="topos.test.node",
        params=dict(params or {}), ui={"x": x, "y": y},
    )


@pytest.fixture
def simple_graph() -> WorkflowGraph:
    """两节点 + 一边的最小 graph。"""
    n1 = _node("n1", x=10.0, y=20.0)
    n2 = _node("n2", x=100.0, y=20.0)
    edge = WorkflowEdge(
        from_port=PortRef(node_id="n1", port="out"),
        to_port=PortRef(node_id="n2", port="in"),
    )
    return WorkflowGraph(workflow_id="wf-test", nodes=(n1, n2), edges=(edge,))


@pytest.fixture
def edit():
    from src.features.ai_workflow.core.workflow_graph_edit import WorkflowGraphEdit
    return WorkflowGraphEdit()


# ---------------------------------------------------------------------- #
# move_node
# ---------------------------------------------------------------------- #


def test_move_node_updates_ui(edit, simple_graph):
    new = edit.move_node(simple_graph, "n1", 50.0, 60.0)
    n1 = new.require_node("n1")
    assert n1.ui["x"] == 50.0
    assert n1.ui["y"] == 60.0


def test_move_node_does_not_mutate_original(edit, simple_graph):
    """冻结模型红线：原 graph 不变。"""
    original_n1_x = simple_graph.require_node("n1").ui["x"]
    edit.move_node(simple_graph, "n1", 999.0, 999.0)
    # 原 graph 的 n1.x 仍是原值
    assert simple_graph.require_node("n1").ui["x"] == original_n1_x


def test_move_node_returns_new_instance(edit, simple_graph):
    """move 后返回的是新 WorkflowGraph 实例（id 不同）。"""
    new = edit.move_node(simple_graph, "n1", 1.0, 2.0)
    assert new is not simple_graph


def test_move_node_unknown_raises(edit, simple_graph):
    with pytest.raises(KeyError):
        edit.move_node(simple_graph, "nonexistent", 0.0, 0.0)


def test_move_node_preserves_other_nodes_and_edges(edit, simple_graph):
    new = edit.move_node(simple_graph, "n1", 50.0, 60.0)
    # n2 不变
    assert new.require_node("n2").ui["x"] == 100.0
    # edges 不变
    assert len(new.edges) == 1
    assert new.edges[0].from_port.node_id == "n1"
    assert new.edges[0].to_port.node_id == "n2"


# ---------------------------------------------------------------------- #
# move_nodes（批量）
# ---------------------------------------------------------------------- #


def test_move_nodes_batch(edit, simple_graph):
    new = edit.move_nodes(simple_graph, {"n1": (1.0, 2.0), "n2": (3.0, 4.0)})
    assert new.require_node("n1").ui["x"] == 1.0
    assert new.require_node("n1").ui["y"] == 2.0
    assert new.require_node("n2").ui["x"] == 3.0
    assert new.require_node("n2").ui["y"] == 4.0


def test_move_nodes_empty_moves_is_noop(edit, simple_graph):
    new = edit.move_nodes(simple_graph, {})
    # 无移动：返回新实例但内容等价（nodes 数/edges 数不变）
    assert len(new.nodes) == len(simple_graph.nodes)
    assert len(new.edges) == len(simple_graph.edges)


def test_move_nodes_unknown_raises(edit, simple_graph):
    with pytest.raises(KeyError):
        edit.move_nodes(simple_graph, {"nope": (1.0, 2.0)})


# ---------------------------------------------------------------------- #
# set_node_param
# ---------------------------------------------------------------------- #


def test_set_node_param_updates_params(edit, simple_graph):
    new = edit.set_node_param(simple_graph, "n1", "threshold", 0.5)
    assert new.require_node("n1").params["threshold"] == 0.5


def test_set_node_param_preserves_other_params(edit):
    g = WorkflowGraph(
        workflow_id="wf",
        nodes=(_node("n1", params={"a": 1, "b": 2}),),
    )
    from src.features.ai_workflow.core.workflow_graph_edit import WorkflowGraphEdit
    e = WorkflowGraphEdit()
    new = e.set_node_param(g, "n1", "b", 99)
    assert new.require_node("n1").params["a"] == 1  # 保留
    assert new.require_node("n1").params["b"] == 99


def test_set_node_param_json_unsafe_raises(edit, simple_graph):
    """非 JSON-safe value（含对象）抛 ValueError。"""
    class NotJsonSafe:
        pass
    with pytest.raises(ValueError):
        edit.set_node_param(simple_graph, "n1", "bad", NotJsonSafe())


def test_set_node_param_unknown_raises(edit, simple_graph):
    with pytest.raises(KeyError):
        edit.set_node_param(simple_graph, "nope", "k", 1)


# ---------------------------------------------------------------------- #
# add_edge
# ---------------------------------------------------------------------- #


def test_add_edge_success(edit, simple_graph):
    """加一条新边 n2→n1（反向）。"""
    new = edit.add_edge(simple_graph, "n2", "out", "n1", "in")
    assert len(new.edges) == 2


def test_add_edge_self_loop_raises(edit, simple_graph):
    """真正的自连（同 node + 同 port）抛 ValueError（WorkflowEdge.__post_init__）。

    注：同节点不同 port（n1:out → n1:in）不是自连——Composite 也允许这种重排序
    节点；model 的 self-loop 校验只在 from_port == to_port（完整 PortRef 相等）时触发。
    """
    with pytest.raises(ValueError):
        edit.add_edge(simple_graph, "n1", "out", "n1", "out")  # 同 port 才是自连


def test_add_edge_duplicate_raises(edit, simple_graph):
    """重复边抛 ValueError（edit 层显式校验，model 不管重复边）。"""
    with pytest.raises(ValueError):
        edit.add_edge(simple_graph, "n1", "out", "n2", "in")  # 已存在


def test_add_edge_unknown_node_raises(edit, simple_graph):
    with pytest.raises(ValueError):
        # WorkflowGraph.__post_init__ 校验 ghost node → ValueError
        edit.add_edge(simple_graph, "n1", "out", "ghost", "in")


# ---------------------------------------------------------------------- #
# remove_edge
# ---------------------------------------------------------------------- #


def test_remove_edge_success(edit, simple_graph):
    new = edit.remove_edge(simple_graph, "n1", "out", "n2", "in")
    assert len(new.edges) == 0


def test_remove_nonexistent_edge_is_noop(edit, simple_graph):
    """不存在的边：no-op（返回内容等价的新 graph）。"""
    new = edit.remove_edge(simple_graph, "n2", "out", "n1", "in")
    assert len(new.edges) == 1  # 原边还在


def test_remove_edge_preserves_nodes(edit, simple_graph):
    new = edit.remove_edge(simple_graph, "n1", "out", "n2", "in")
    assert len(new.nodes) == 2
