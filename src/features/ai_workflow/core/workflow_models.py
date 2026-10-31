"""P5.1 AI Workflow 数据模型（plan §3.2 / §4.2 schema）。

定义 :class:`WorkflowGraph` —— AI Workflow 节点图的**声明式数据模型**。

设计边界（红线，遵循 plan §3.1 / §1.4 / §10.7）：

- **不复用 Composite 的 ``NodeGraph``**（GPU 渲染语义，与 ModernGL/纹理强耦合）。
  本模型是纯数据（frozen dataclass + JSON 可序列化），不含任何 Qt / OpenGL / 模型实例。
- **端口不传危险对象**（plan §3.1）：不传 Qt 对象 / OpenGL texture / 模型实例 /
  线程对象 / API key / 绝对路径。``params`` 是 JSON-safe dict（``str/int/float/bool/
  list/dict``）；运行时由 adapter 把 artifact 引用填进端口。
- **DAG / 环 / 端口兼容 / 模型 / 权限校验** 由 :class:`WorkflowValidator`（P5.2）做，
  本模型只做**基本字段完整性**（node_id 唯一 / spec_id 非空 / edge 不引用 ghost node）。

与既有模块的关系：

- 序列化形态匹配 plan §3.2 的 ``topos.workflow/1`` JSON schema。
- :class:`WorkflowCompiler`（P5.3）把本图编译为 ``Invocation`` 调用
  :class:`HarnessRuntime`，保证 chat / 快捷卡 / workflow 三入口同底层。
- 本图最终编译为 :class:`TaskGraph`（M1）执行；node spec（P5.2 注册表）声明每个
  节点的输入/输出端口、所需能力、executor_id。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Literal, Tuple, Union

# JSON-safe 值类型（端口参数只能是这些，不能是对象/路径/secret）
JsonValue = Union[None, bool, int, float, str, List[Any], Dict[str, Any]]


# ---------------------------------------------------------------------- #
# 端口方向（plan §3.3 输入/输出端口）
# ---------------------------------------------------------------------- #


class PortDirection:
    """端口方向（plan §3.3）。"""

    INPUT = "input"
    OUTPUT = "output"

    ALL: Tuple[str, ...] = (INPUT, OUTPUT)


# ---------------------------------------------------------------------- #
# PortRef —— 端口引用（节点 ID + 端口名）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PortRef:
    """节点端口的引用（``node_id`` + ``port`` 名）。

    用于 :class:`WorkflowEdge` 的 from/to、``exposed_inputs`` 的内部端口绑定。
    红线：``node_id`` 非空；``port`` 非空。
    """

    node_id: str
    port: str

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("PortRef.node_id cannot be empty")
        if not self.port:
            raise ValueError("PortRef.port cannot be empty")

    def to_dict(self) -> Dict[str, str]:
        return {"node_id": self.node_id, "port": self.port}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PortRef":
        return PortRef(
            node_id=str(d.get("node_id", "")),
            port=str(d.get("port", "")),
        )


# ---------------------------------------------------------------------- #
# WorkflowNode —— 节点实例
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowNode:
    """节点实例（plan §3.2 ``nodes[]`` 单项）。

    Attributes:
        node_id: 节点唯一 ID（同一 graph 内唯一）。
        spec_id: 节点 spec ID（如 ``topos.media.selected_assets``）。
        spec_version: spec 语义版本（默认 ``1.0.0``）。
        params: 节点参数（JSON-safe；不含 secret / 路径）。
        ui: 布局元数据（``{"x":..,"y":..}`` 等）。
    """

    node_id: str
    spec_id: str
    spec_version: str = "1.0.0"
    params: Dict[str, JsonValue] = field(default_factory=dict)
    ui: Dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("WorkflowNode.node_id cannot be empty")
        if not self.spec_id:
            raise ValueError("WorkflowNode.spec_id cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "spec_id": self.spec_id,
            "spec_version": self.spec_version,
            "params": dict(self.params),
            "ui": dict(self.ui),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WorkflowNode":
        return WorkflowNode(
            node_id=str(d.get("node_id", "")),
            spec_id=str(d.get("spec_id", "")),
            spec_version=str(d.get("spec_version", "1.0.0")),
            params=dict(d.get("params", {}) or {}),
            ui=dict(d.get("ui", {}) or {}),
        )


# ---------------------------------------------------------------------- #
# WorkflowEdge —— 连接（from 输出端口 → to 输入端口）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowEdge:
    """节点间的连接（plan §3.2 ``edges[]`` 单项）。

    红线：from/to 不能完全相同（自连）。
    """

    from_port: PortRef
    to_port: PortRef

    def __post_init__(self) -> None:
        if self.from_port == self.to_port:
            raise ValueError(
                f"WorkflowEdge self-loop not allowed: {self.from_port}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from": self.from_port.to_dict(),
            "to": self.to_port.to_dict(),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WorkflowEdge":
        return WorkflowEdge(
            from_port=PortRef.from_dict(d.get("from", {})),
            to_port=PortRef.from_dict(d.get("to", {})),
        )


# ---------------------------------------------------------------------- #
# WorkflowPolicy —— 权限 / 预算策略（plan §3.2 ``policy``）
# ---------------------------------------------------------------------- #


NetworkPolicy = Literal["ask", "allow", "forbidden"]
TimelineWritePolicy = Literal["approval_required", "forbidden"]


@dataclass(frozen=True)
class WorkflowPolicy:
    """workflow 级权限 / 预算策略（plan §3.2 ``policy`` / §6.4）。

    红线：``timeline_write`` 默认 ``approval_required``（不绕过审批）；
    ``allow_model_download`` / ``allow_generation`` 默认 False（独立 consent）。
    """

    network: NetworkPolicy = "ask"
    timeline_write: TimelineWritePolicy = "approval_required"
    cost_budget: float | None = None
    allow_model_download: bool = False
    allow_generation: bool = False

    def __post_init__(self) -> None:
        if self.network not in ("ask", "allow", "forbidden"):
            raise ValueError(f"invalid network policy: {self.network!r}")
        if self.timeline_write not in ("approval_required", "forbidden"):
            raise ValueError(f"invalid timeline_write policy: {self.timeline_write!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "network": self.network,
            "timeline_write": self.timeline_write,
            "cost_budget": self.cost_budget,
            "allow_model_download": self.allow_model_download,
            "allow_generation": self.allow_generation,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WorkflowPolicy":
        return WorkflowPolicy(
            network=d.get("network", "ask"),  # type: ignore[arg-type]
            timeline_write=d.get("timeline_write", "approval_required"),  # type: ignore[arg-type]
            cost_budget=d.get("cost_budget"),
            allow_model_download=bool(d.get("allow_model_download", False)),
            allow_generation=bool(d.get("allow_generation", False)),
        )


# ---------------------------------------------------------------------- #
# WorkflowUi —— 布局元数据（viewport / notes / groups）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowUi:
    """布局元数据（plan §3.2 ``ui``）。纯展示，不影响执行语义。"""

    viewport: Dict[str, JsonValue] = field(
        default_factory=lambda: {"zoom": 1.0, "center": [0, 0]},
    )
    notes: Tuple[Tuple[str, str], ...] = ()        # (note_id, text)
    groups: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()  # (group_id, (node_ids,))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "viewport": dict(self.viewport),
            "notes": [list(n) for n in self.notes],
            "groups": [[g[0], list(g[1])] for g in self.groups],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WorkflowUi":
        notes_raw = d.get("notes", []) or []
        groups_raw = d.get("groups", []) or []
        notes = tuple(
            (str(n[0]), str(n[1])) for n in notes_raw
            if isinstance(n, (list, tuple)) and len(n) >= 2
        )
        groups = tuple(
            (str(g[0]), tuple(str(x) for x in g[1]))
            for g in groups_raw
            if isinstance(g, (list, tuple)) and len(g) >= 2
        )
        return WorkflowUi(
            viewport=dict(d.get("viewport", {}) or {"zoom": 1.0, "center": [0, 0]}),
            notes=notes,
            groups=groups,
        )


# ---------------------------------------------------------------------- #
# WorkflowGraph —— 核心
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowGraph:
    """AI Workflow 节点图的声明式数据模型（plan §3.2）。

    frozen + 可序列化为 ``topos.workflow/1`` JSON。``content_hash`` 由语义内容
    计算（不含 hash 本身），稳定可复现。

    红线（plan §3.1 / §1.4）：

    - 端口/参数不传 Qt/OpenGL/模型/API key/绝对路径。
    - ``node_id`` 唯一；``spec_id`` 非空；edge 不引用 ghost node。
    - DAG / 环 / 端口兼容 / 模型 / 权限校验在 :class:`WorkflowValidator`（P5.2）。
    """

    # 类常量（不是 dataclass 字段；用 ClassVar 避免 dataclass 把它当字段）
    SCHEMA: ClassVar[str] = "topos.workflow/1"

    workflow_id: str
    name: str = ""
    schema_version: int = 1
    nodes: Tuple[WorkflowNode, ...] = ()
    edges: Tuple[WorkflowEdge, ...] = ()
    ui: WorkflowUi = field(default_factory=WorkflowUi)
    policy: WorkflowPolicy = field(default_factory=WorkflowPolicy)
    #: 把节点内部端口暴露为 workflow 顶层输入：(exposed_name, node_id, port)
    exposed_inputs: Tuple[Tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.workflow_id:
            raise ValueError("WorkflowGraph.workflow_id cannot be empty")
        # node_id 唯一
        node_ids = [n.node_id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            dupes = [x for x in node_ids if node_ids.count(x) > 1]
            raise ValueError(f"duplicate node_id in graph: {sorted(set(dupes))}")
        # edge 引用的 node 必须存在
        existing = set(node_ids)
        for e in self.edges:
            if e.from_port.node_id not in existing:
                raise ValueError(
                    f"edge references unknown from-node: {e.from_port.node_id!r}"
                )
            if e.to_port.node_id not in existing:
                raise ValueError(
                    f"edge references unknown to-node: {e.to_port.node_id!r}"
                )
        # exposed_inputs 引用的 node 必须存在
        for exposed_name, node_id, port in self.exposed_inputs:
            if node_id not in existing:
                raise ValueError(
                    f"exposed_input {exposed_name!r} references unknown node: {node_id!r}"
                )

    # ---------------- 查询 ---------------- #

    def node(self, node_id: str) -> WorkflowNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def require_node(self, node_id: str) -> WorkflowNode:
        n = self.node(node_id)
        if n is None:
            raise KeyError(f"node not found: {node_id!r}")
        return n

    def edges_into(self, node_id: str) -> Tuple[WorkflowEdge, ...]:
        return tuple(e for e in self.edges if e.to_port.node_id == node_id)

    def edges_out_of(self, node_id: str) -> Tuple[WorkflowEdge, ...]:
        return tuple(e for e in self.edges if e.from_port.node_id == node_id)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    # ---------------- 序列化 ---------------- #

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "topos.workflow/1",
            "workflow_id": self.workflow_id,
            "name": self.name,
            "schema_version": self.schema_version,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "ui": self.ui.to_dict(),
            "policy": self.policy.to_dict(),
            "exposed_inputs": [
                {"name": n, "node_id": nid, "port": p}
                for (n, nid, p) in self.exposed_inputs
            ],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WorkflowGraph":
        schema = str(d.get("schema", "topos.workflow/1"))
        if schema != "topos.workflow/1":
            raise ValueError(
                f"unsupported workflow schema: {schema!r} (expected 'topos.workflow/1')"
            )
        nodes = tuple(
            WorkflowNode.from_dict(n) for n in (d.get("nodes", []) or [])
        )
        edges = tuple(
            WorkflowEdge.from_dict(e) for e in (d.get("edges", []) or [])
        )
        exposed_raw = d.get("exposed_inputs", []) or []
        exposed = tuple(
            (str(x.get("name", "")), str(x.get("node_id", "")), str(x.get("port", "")))
            for x in exposed_raw if isinstance(x, dict)
        )
        return WorkflowGraph(
            workflow_id=str(d.get("workflow_id", "")),
            name=str(d.get("name", "")),
            schema_version=int(d.get("schema_version", 1)),
            nodes=nodes,
            edges=edges,
            ui=WorkflowUi.from_dict(d.get("ui", {}) or {}),
            policy=WorkflowPolicy.from_dict(d.get("policy", {}) or {}),
            exposed_inputs=exposed,
        )

    # ---------------- 内容 hash ---------------- #

    @property
    def content_hash(self) -> str:
        """workflow 内容的稳定 sha256（不含 hash 本身）。

        用于发布产物完整性自检（plan §5.3）与 cache-key（plan §6.3）。
        """
        payload = {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "schema_version": self.schema_version,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "policy": self.policy.to_dict(),
            "exposed_inputs": [
                [n, nid, p] for (n, nid, p) in self.exposed_inputs
            ],
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# 便捷：从 dict 构造（含 schema 校验）
# ---------------------------------------------------------------------- #


def parse_workflow_dict(d: Dict[str, Any]) -> WorkflowGraph:
    """从 dict 构造 :class:`WorkflowGraph`（校验 schema）。

    若 ``schema`` 缺失，默认 ``topos.workflow/1``；不匹配则 ``ValueError``。
    """
    schema = str(d.get("schema", "topos.workflow/1"))
    if schema != "topos.workflow/1":
        raise ValueError(
            f"unsupported workflow schema: {schema!r} (expected 'topos.workflow/1')"
        )
    return WorkflowGraph.from_dict(d)


__all__ = [
    "JsonValue",
    "PortDirection",
    "PortRef",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowPolicy",
    "WorkflowUi",
    "WorkflowGraph",
    "parse_workflow_dict",
]
