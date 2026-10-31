"""P5.2 WorkflowValidator（plan §12.1 单元测试范围）。

> WorkflowValidator：缺模型、缺权限、云上传、超预算、锁定轨道、stale revision。

检查项：

- **DAG 无环**（DFS 环检测）。
- **未知节点 spec**：node.spec_id 不在 registry → error。
- **端口存在**：edge 的 from/to 端口名在对应 spec 中必须存在 → 否则
  ``UNKNOWN_PORT`` error（AUD-23217：此前静默 continue，坏边直达运行时）。
- **端口兼容**：edge 的 from/to artifact type 兼容（同 type 或显式声明可连）。
- **必填输入有连接**：spec.inputs 中 required=True 的端口必须有 edge 连入或
  exposed_inputs 绑定（后者由 host 在运行时注入实际值）。
- **allow_multi**：声明单值的输入端口连入多条边 → ``MULTI_INPUT_NOT_ALLOWED``
  error（AUD-23218）。
- **节点 params 契约**：对照 spec.params_schema 校验必填缺失 / 类型不符
  （error）与未知键（warning）（AUD-23219）。
- **缺模型**：节点 required_capabilities 不在 host available_capabilities → error。
- **缺权限 / 云上传**：network_required 但 policy.network=forbidden → error。
- **超预算**：节点 estimated_cost 累加超 policy.cost_budget → warning。
- **timeline 写需 approval_token**：apply_approved_plan 缺 approval_token 输入 → error。
- **stale revision**：外部 revision 与期望不一致 → error。

红线（plan §6.4 / §1.4）：

- approval_token 必须来自 user.approval 节点的输出；apply 节点不得无 token 执行。
- 缺模型 / 缺权限 / cycle 是 error（硬拒）；超预算是 warning（提示用户）。

与既有模块的关系：

- 用 :class:`NodeSpecRegistry`（本文件同包）查 spec 端口契约。
- :class:`WorkflowCompiler`（P5.3）在编译前先调本 validator；不合法 → 拒绝编译。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from src.features.ai_workflow.core.node_specs import NodeSpecRegistry
from src.features.ai_workflow.core.workflow_models import WorkflowGraph

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.ai_workflow.core.node_specs import WorkflowNodeSpec


# ---------------------------------------------------------------------- #
# ValidationErrorCode
# ---------------------------------------------------------------------- #


class ValidationErrorCode:
    """validator 检查项的错误码（plan §12.1）。"""

    DAG_CYCLE = "DAG_CYCLE"
    UNKNOWN_NODE_SPEC = "UNKNOWN_NODE_SPEC"
    UNKNOWN_PORT = "UNKNOWN_PORT"
    PORT_TYPE_MISMATCH = "PORT_TYPE_MISMATCH"
    MISSING_REQUIRED_INPUT = "MISSING_REQUIRED_INPUT"
    MULTI_INPUT_NOT_ALLOWED = "MULTI_INPUT_NOT_ALLOWED"
    MISSING_REQUIRED_PARAM = "MISSING_REQUIRED_PARAM"
    INVALID_PARAM_TYPE = "INVALID_PARAM_TYPE"
    UNKNOWN_PARAM = "UNKNOWN_PARAM"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    NETWORK_FORBIDDEN = "NETWORK_FORBIDDEN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MISSING_APPROVAL_TOKEN = "MISSING_APPROVAL_TOKEN"
    STALE_REVISION = "STALE_REVISION"
    DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"


# ---------------------------------------------------------------------- #
# ValidationFinding
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ValidationFinding:
    """单个校验发现（error/warning/info）。"""

    severity: str          # error / warning / info
    code: str
    node_id: str = ""
    port: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "node_id": self.node_id,
            "port": self.port,
            "message": self.message,
        }


# ---------------------------------------------------------------------- #
# WorkflowValidator
# ---------------------------------------------------------------------- #


# artifact type 兼容表：from_type → {允许连入的 to_type}
# 同 type 总是兼容；额外允许 asset_ref → asset_set（单素材进集合）
_COMPATIBLE_TYPES: Dict[str, Set[str]] = {
    "asset_ref": {"asset_ref", "asset_set"},
    "asset_set": {"asset_set"},
    "timeline_ref": {"timeline_ref"},
    "transcript_ref": {"transcript_ref"},
    "analysis_ref": {"analysis_ref"},
    "subtitle_track_ref": {"subtitle_track_ref"},
    "text_document": {"text_document"},
    "edit_plan_ref": {"edit_plan_ref"},
    "media_generation_ref": {"media_generation_ref"},
    "report": {"report"},
    "approval_token": {"approval_token"},
    "selection_set": {"selection_set"},
    # report 的 any 输入端口接受任意（特殊处理）
}

#: report.run_summary 的 "any" 输入端口接受所有类型
_ANY_INPUT_PORT = "any"


class WorkflowValidator:
    """workflow graph 校验器（plan §12.1）。

    用法::

        v = WorkflowValidator(registry)
        findings = v.validate(graph, available_capabilities={"asr"})
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            raise ValueError("workflow invalid")
    """

    def __init__(self, registry: NodeSpecRegistry) -> None:
        self._registry = registry

    def validate(
        self,
        graph: WorkflowGraph,
        *,
        available_capabilities: Optional[Set[str]] = None,
        current_revision: Optional[int] = None,
        expected_revision: Optional[int] = None,
    ) -> Tuple[ValidationFinding, ...]:
        """返回所有校验发现（error/warning/info）。空 = 完全合法。

        Args:
            available_capabilities: host 当前可用的能力集（asr/ocr/tts/...）。
                None 表示不做能力检查（仅查 spec 声明）。
            current_revision: 当前 timeline revision（用于 stale 检查）。
            expected_revision: graph 锚定的期望 revision（来自 plan.base_revision）。
        """
        caps = available_capabilities or set()
        findings: List[ValidationFinding] = []

        # 1. 未知节点 spec
        specs = self._resolve_specs(graph, findings)

        # 2. DAG 无环
        self._check_dag_acyclic(graph, findings)

        # 3. 端口兼容 + 端口存在 + 必填输入 + allow_multi
        self._check_port_compatibility(graph, specs, findings)

        # 3.5 节点 params 对照 spec.params_schema（AUD-23219）
        self._check_node_params(graph, specs, findings)

        # 4. 缺模型
        if available_capabilities is not None:
            self._check_capabilities(graph, specs, caps, findings)

        # 5. 网络 / 云上传权限
        self._check_network_policy(graph, specs, findings)

        # 6. approval_token（apply 节点必须有 token 输入）
        self._check_approval_token(graph, specs, findings)

        # 7. 超预算（warning）
        self._check_budget(graph, specs, findings)

        # 8. stale revision
        if current_revision is not None and expected_revision is not None:
            if current_revision != expected_revision:
                findings.append(ValidationFinding(
                    severity="error", code=ValidationErrorCode.STALE_REVISION,
                    message=(
                        f"stale revision: current={current_revision} "
                        f"!= expected={expected_revision}"
                    ),
                ))

        return tuple(findings)

    # ---------------- 检查实现 ---------------- #

    def _resolve_specs(
        self, graph: WorkflowGraph, findings: List[ValidationFinding],
    ) -> Dict[str, Optional["WorkflowNodeSpec"]]:
        """解析每个 node 的 spec；未知 → error。返回 node_id → spec|None。"""
        result: Dict[str, Optional[WorkflowNodeSpec]] = {}
        for n in graph.nodes:
            spec = self._registry.get(n.spec_id)
            if spec is None:
                findings.append(ValidationFinding(
                    severity="error", code=ValidationErrorCode.UNKNOWN_NODE_SPEC,
                    node_id=n.node_id,
                    message=f"unknown node spec: {n.spec_id!r}",
                ))
            result[n.node_id] = spec
        return result

    def _check_dag_acyclic(
        self, graph: WorkflowGraph, findings: List[ValidationFinding],
    ) -> None:
        """DFS 环检测（Kahn 拓扑）。"""
        node_ids = [n.node_id for n in graph.nodes]
        adj: Dict[str, List[str]] = {nid: [] for nid in node_ids}
        indeg: Dict[str, int] = {nid: 0 for nid in node_ids}
        for e in graph.edges:
            f, t = e.from_port.node_id, e.to_port.node_id
            if f in adj and t in indeg:
                adj[f].append(t)
                indeg[t] += 1
        queue = [nid for nid in node_ids if indeg[nid] == 0]
        visited = 0
        while queue:
            n = queue.pop(0)
            visited += 1
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if visited != len(node_ids):
            # 找出环中的节点（indeg > 0 的）
            in_cycle = [nid for nid in node_ids if indeg[nid] > 0]
            findings.append(ValidationFinding(
                severity="error", code=ValidationErrorCode.DAG_CYCLE,
                message=f"cycle detected involving nodes: {in_cycle}",
            ))

    def _check_port_compatibility(
        self,
        graph: WorkflowGraph,
        specs: Dict[str, Optional["WorkflowNodeSpec"]],
        findings: List[ValidationFinding],
    ) -> None:
        """端口存在 + 类型兼容 + 必填输入有连接 + allow_multi 约束。"""
        # 收集每个 node 每个输入端口是否有连入。exposed_inputs 绑定同样满足
        # required 端口——它声明该输入由 host 在运行时注入（如 composite_graph），
        # 不是图内数据流缺口。
        connected_inputs: Dict[str, Set[str]] = {n.node_id: set() for n in graph.nodes}
        # AUD-23218：统计每个 (node_id, port) 的入边数，供 allow_multi 校验。
        incoming_edge_counts: Dict[Tuple[str, str], int] = {}
        for e in graph.edges:
            connected_inputs.setdefault(e.to_port.node_id, set()).add(e.to_port.port)
            key = (e.to_port.node_id, e.to_port.port)
            incoming_edge_counts[key] = incoming_edge_counts.get(key, 0) + 1
        for _exposed_name, node_id, port in graph.exposed_inputs:
            connected_inputs.setdefault(node_id, set()).add(port)

        for n in graph.nodes:
            spec = specs.get(n.node_id)
            if spec is None:
                continue
            # 必填输入必须有连接
            for p in spec.inputs:
                if p.direction == "input" and p.required:
                    if p.name not in connected_inputs.get(n.node_id, set()):
                        findings.append(ValidationFinding(
                            severity="error",
                            code=ValidationErrorCode.MISSING_REQUIRED_INPUT,
                            node_id=n.node_id, port=p.name,
                            message=(
                                f"required input port {p.name!r} of {n.node_id} "
                                f"has no connection"
                            ),
                        ))
            # AUD-23218：allow_multi=False 的输入端口只允许一条入边
            for p in spec.inputs:
                if p.direction != "input" or p.allow_multi:
                    continue
                count = incoming_edge_counts.get((n.node_id, p.name), 0)
                if count > 1:
                    findings.append(ValidationFinding(
                        severity="error",
                        code=ValidationErrorCode.MULTI_INPUT_NOT_ALLOWED,
                        node_id=n.node_id, port=p.name,
                        message=(
                            f"input port {p.name!r} of {n.node_id} declares "
                            f"allow_multi=False but has {count} incoming edges"
                        ),
                    ))

        # 端口存在（AUD-23217）+ 类型兼容
        for e in graph.edges:
            from_spec = specs.get(e.from_port.node_id)
            to_spec = specs.get(e.to_port.node_id)
            if from_spec is None or to_spec is None:
                continue
            from_type = self._output_port_type(from_spec, e.from_port.port)
            to_type = self._input_port_type(to_spec, e.to_port.port)
            if from_type is None:
                findings.append(ValidationFinding(
                    severity="error", code=ValidationErrorCode.UNKNOWN_PORT,
                    node_id=e.from_port.node_id, port=e.from_port.port,
                    message=(
                        f"edge references unknown output port "
                        f"{e.from_port.node_id}.{e.from_port.port} "
                        f"(spec {from_spec.spec_id})"
                    ),
                ))
                continue
            if to_type is None:
                findings.append(ValidationFinding(
                    severity="error", code=ValidationErrorCode.UNKNOWN_PORT,
                    node_id=e.to_port.node_id, port=e.to_port.port,
                    message=(
                        f"edge references unknown input port "
                        f"{e.to_port.node_id}.{e.to_port.port} "
                        f"(spec {to_spec.spec_id})"
                    ),
                ))
                continue
            # report.any 接受任意
            if e.to_port.port == _ANY_INPUT_PORT:
                continue
            allowed = _COMPATIBLE_TYPES.get(from_type, {from_type})
            if to_type not in allowed:
                findings.append(ValidationFinding(
                    severity="error",
                    code=ValidationErrorCode.PORT_TYPE_MISMATCH,
                    node_id=e.to_port.node_id, port=e.to_port.port,
                    message=(
                        f"port type mismatch: {e.from_port.node_id}.{e.from_port.port}"
                        f"({from_type}) → {e.to_port.node_id}.{e.to_port.port}"
                        f"({to_type})"
                    ),
                ))

    def _check_capabilities(
        self,
        graph: WorkflowGraph,
        specs: Dict[str, Optional["WorkflowNodeSpec"]],
        caps: Set[str],
        findings: List[ValidationFinding],
    ) -> None:
        for n in graph.nodes:
            spec = specs.get(n.node_id)
            if spec is None:
                continue
            for cap in spec.required_capabilities:
                if cap not in caps:
                    findings.append(ValidationFinding(
                        severity="error",
                        code=ValidationErrorCode.MISSING_CAPABILITY,
                        node_id=n.node_id,
                        message=(
                            f"node {n.node_id} requires capability {cap!r} "
                            f"not in available {sorted(caps)}"
                        ),
                    ))

    def _check_network_policy(
        self,
        graph: WorkflowGraph,
        specs: Dict[str, Optional["WorkflowNodeSpec"]],
        findings: List[ValidationFinding],
    ) -> None:
        if graph.policy.network != "forbidden":
            return
        for n in graph.nodes:
            spec = specs.get(n.node_id)
            if spec is None:
                continue
            if spec.network_required:
                findings.append(ValidationFinding(
                    severity="error",
                    code=ValidationErrorCode.NETWORK_FORBIDDEN,
                    node_id=n.node_id,
                    message=(
                        f"node {n.node_id} ({n.spec_id}) requires network "
                        f"but policy.network=forbidden"
                    ),
                ))

    def _check_approval_token(
        self,
        graph: WorkflowGraph,
        specs: Dict[str, Optional["WorkflowNodeSpec"]],
        findings: List[ValidationFinding],
    ) -> None:
        """红线：apply_approved_plan 节点必须有 approval_token 输入连接。"""
        for n in graph.nodes:
            if n.spec_id != "topos.timeline.apply_approved_plan":
                continue
            # 检查是否有 approval_token 类型的输入连接
            has_token = False
            for e in graph.edges:
                if e.to_port.node_id != n.node_id:
                    continue
                from_spec = specs.get(e.from_port.node_id)
                if from_spec is None:
                    continue
                from_type = self._output_port_type(from_spec, e.from_port.port)
                if from_type == "approval_token":
                    has_token = True
                    break
            if not has_token:
                findings.append(ValidationFinding(
                    severity="error",
                    code=ValidationErrorCode.MISSING_APPROVAL_TOKEN,
                    node_id=n.node_id,
                    message=(
                        f"apply_approved_plan node {n.node_id} has no "
                        f"approval_token input (plan §6.4 红线)"
                    ),
                ))

    def _check_budget(
        self,
        graph: WorkflowGraph,
        specs: Dict[str, Optional["WorkflowNodeSpec"]],
        findings: List[ValidationFinding],
    ) -> None:
        budget = graph.policy.cost_budget
        if budget is None:
            return
        total = 0.0
        for n in graph.nodes:
            spec = specs.get(n.node_id)
            if spec is None:
                continue
            total += spec.estimated_cost
        if total > budget:
            findings.append(ValidationFinding(
                severity="warning",
                code=ValidationErrorCode.BUDGET_EXCEEDED,
                message=(
                    f"estimated cost {total:.3f} exceeds budget {budget:.3f}"
                ),
            ))

    # ---------------- 端口查找辅助 ---------------- #

    #: JSON Schema 基本类型 → Python 类型检查（bool 必须先于 int 判定）。
    _JSON_TYPE_CHECKS = {
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
    }

    def _check_node_params(
        self,
        graph: WorkflowGraph,
        specs: Dict[str, Optional["WorkflowNodeSpec"]],
        findings: List[ValidationFinding],
    ) -> None:
        """AUD-23219：节点 params 对照 spec.params_schema 校验。

        - required 缺失 → ``MISSING_REQUIRED_PARAM`` error。
        - 类型不符 → ``INVALID_PARAM_TYPE`` error。
        - 未知键（不在 properties）→ ``UNKNOWN_PARAM`` warning。

        spec 未声明 params_schema（空 dict）时不校验（无契约可依）。
        """
        for n in graph.nodes:
            spec = specs.get(n.node_id)
            if spec is None:
                continue
            schema = spec.params_schema
            if not isinstance(schema, dict) or not schema.get("properties"):
                continue  # spec 未声明参数契约
            properties = schema.get("properties") or {}
            required_keys = schema.get("required") or ()
            # params 可能为空（必填参数完全缺失正是要检查的情形）
            params = dict(n.params or {})
            for key in required_keys:
                if key not in params:
                    findings.append(ValidationFinding(
                        severity="error",
                        code=ValidationErrorCode.MISSING_REQUIRED_PARAM,
                        node_id=n.node_id,
                        message=(
                            f"node {n.node_id} ({spec.spec_id}) missing required "
                            f"param {key!r}"
                        ),
                    ))
            for key, value in params.items():
                if key not in properties:
                    findings.append(ValidationFinding(
                        severity="warning",
                        code=ValidationErrorCode.UNKNOWN_PARAM,
                        node_id=n.node_id,
                        message=(
                            f"node {n.node_id} ({spec.spec_id}) sets param "
                            f"{key!r} not declared in spec params_schema"
                        ),
                    ))
                    continue
                prop = properties.get(key)
                if not isinstance(prop, dict):
                    continue
                json_type = prop.get("type")
                checker = self._JSON_TYPE_CHECKS.get(str(json_type or ""))
                if checker is not None and not checker(value):
                    findings.append(ValidationFinding(
                        severity="error",
                        code=ValidationErrorCode.INVALID_PARAM_TYPE,
                        node_id=n.node_id,
                        message=(
                            f"node {n.node_id} ({spec.spec_id}) param {key!r} "
                            f"expects {json_type}, got "
                            f"{type(value).__name__}: {value!r}"
                        ),
                    ))

    def _output_port_type(
        self, spec: "WorkflowNodeSpec", port_name: str,
    ) -> Optional[str]:
        for p in spec.outputs:
            if p.direction == "output" and p.name == port_name:
                return p.artifact_type
        return None

    def _input_port_type(
        self, spec: "WorkflowNodeSpec", port_name: str,
    ) -> Optional[str]:
        for p in spec.inputs:
            if p.direction == "input" and p.name == port_name:
                return p.artifact_type
        return None


__all__ = [
    "ValidationErrorCode",
    "ValidationFinding",
    "WorkflowValidator",
]
