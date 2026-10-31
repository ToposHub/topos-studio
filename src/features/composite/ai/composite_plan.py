"""Composite AI contracts: brief, plan, composability analysis, quality gate (CP-01~CP-09).

The audit found that ``features/composite/`` has a mature node-graph engine
and ``CompositeEditPlan`` (keyframe operations), but is missing the agent-
facing layer: no CompositeBrief, no composability analysis, no composite
quality gate, no tracking evidence.  This module fills CP-01 through CP-09.

It composes with the existing ``CompositeEditPlan`` / ``LayerStackValidator``
/ node engine rather than replacing them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping


def _freeze_mapping(value: Any) -> Mapping[str, Any]:
    """Defensively copy and wrap a Mapping in a read-only proxy (AUD-4500).

    Frozen dataclasses only freeze attribute rebinding; a caller-supplied
    dict kept by reference could be mutated after construction, silently
    changing ``plan_hash()``/``to_dict()`` output. Deep-copying at
    construction and wrapping in :class:`types.MappingProxyType` pins the
    value for the object's lifetime.
    """
    if isinstance(value, Mapping):
        return MappingProxyType(copy.deepcopy(dict(value)))
    if value is None:
        return MappingProxyType({})
    raise TypeError(
        f"expected a mapping, got {type(value).__name__}")


# --------------------------------------------------------------------------- #
# Composite brief (CP-01)
# --------------------------------------------------------------------------- #


EFFECT_HUD = "hud_overlay"
EFFECT_SCREEN_REPLACEMENT = "screen_replacement"
EFFECT_BACKGROUND_REPLACE = "background_replace"
EFFECT_TEXT_OVERLAY = "text_overlay"
EFFECT_LOGO_INSERT = "logo_insert"
EFFECT_CLEANUP = "cleanup_removal"

SUPPORTED_COMPOSITE_EFFECTS: frozenset[str] = frozenset({
    EFFECT_HUD, EFFECT_SCREEN_REPLACEMENT, EFFECT_BACKGROUND_REPLACE,
    EFFECT_TEXT_OVERLAY, EFFECT_LOGO_INSERT, EFFECT_CLEANUP,
})


@dataclass(frozen=True)
class CompositeBrief:
    """Structured request for a composite operation (CP-01)."""

    brief_id: str
    project_id: str
    timeline_id: str
    shot_scope: tuple[str, ...]              # clip ids the comp affects
    desired_effect: str
    background_ref: str | None = None
    alpha_source_ref: str | None = None
    output_spec: Mapping[str, Any] = field(default_factory=dict)
    keyframes: tuple[Mapping[str, Any], ...] = ()
    risks: tuple[str, ...] = ()
    quality_checks: tuple[str, ...] = ()
    source: str = "llm"

    def __post_init__(self) -> None:
        if not self.brief_id:
            raise ValueError("CompositeBrief.brief_id is required")
        if self.desired_effect not in SUPPORTED_COMPOSITE_EFFECTS:
            raise ValueError(
                f"Unsupported composite effect: {self.desired_effect!r}"
            )
        if not self.shot_scope:
            raise ValueError("CompositeBrief.shot_scope cannot be empty")
        # AUD-4500: pin Mapping payloads at construction time.
        object.__setattr__(
            self, "output_spec", _freeze_mapping(self.output_spec))
        object.__setattr__(
            self, "keyframes",
            tuple(_freeze_mapping(k) for k in self.keyframes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "shot_scope": list(self.shot_scope),
            "desired_effect": self.desired_effect,
            "background_ref": self.background_ref,
            "alpha_source_ref": self.alpha_source_ref,
            "output_spec": dict(self.output_spec),
            "keyframes": [dict(k) for k in self.keyframes],
            "risks": list(self.risks),
            "quality_checks": list(self.quality_checks),
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# Node-graph operation (CP-01 / CP-03)
# --------------------------------------------------------------------------- #


NODE_OP_ADD_NODE = "add_node"
NODE_OP_CONNECT = "connect"
NODE_OP_DISCONNECT = "disconnect"
NODE_OP_SET_PARAM = "set_param"
NODE_OP_REMOVE_NODE = "remove_node"
NODE_OP_GROUP = "group"
NODE_OP_UNGROUP = "ungroup"
# T-P1-60 批次 B：图结构 AI 操作扩展
NODE_OP_DUPLICATE_NODE = "duplicate_node"
NODE_OP_SET_OUTPUT_TARGET = "set_output_target"

SUPPORTED_NODE_OPS: frozenset[str] = frozenset({
    NODE_OP_ADD_NODE, NODE_OP_CONNECT, NODE_OP_DISCONNECT,
    NODE_OP_SET_PARAM, NODE_OP_REMOVE_NODE, NODE_OP_GROUP, NODE_OP_UNGROUP,
    NODE_OP_DUPLICATE_NODE, NODE_OP_SET_OUTPUT_TARGET,
})


@dataclass(frozen=True)
class NodeGraphOperation:
    """One typed node-graph mutation in a composite plan."""

    op_id: str
    kind: str
    node_type: str = ""               # for add_node
    node_id: str = ""                 # target node
    from_port: str | None = None      # for connect
    to_port: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    requires_gpu: bool = False
    preview_safe: bool = True
    export_safe: bool = True

    def __post_init__(self) -> None:
        if not self.op_id:
            raise ValueError("NodeGraphOperation.op_id is required")
        if self.kind not in SUPPORTED_NODE_OPS:
            raise ValueError(f"Unsupported node op: {self.kind!r}")
        # AUD-4500: pin the parameters payload at construction time so the
        # plan hash cannot drift after approval.
        object.__setattr__(
            self, "parameters", _freeze_mapping(self.parameters))


@dataclass(frozen=True)
class CompositePlan:
    """The dry-run, hash-bound composite execution plan (CP-01/CP-03)."""

    plan_id: str
    brief_id: str
    project_id: str
    timeline_id: str
    base_generation: int
    operations: tuple[NodeGraphOperation, ...]
    render_cost_estimate: float = 0.0
    risk_flags: tuple[str, ...] = ()
    quality_checks: tuple[str, ...] = ()
    source: str = "llm"

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("CompositePlan.plan_id is required")
        if not self.operations:
            raise ValueError("CompositePlan.operations cannot be empty")

    def plan_hash(self) -> str:
        payload = {
            "brief_id": self.brief_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_generation": self.base_generation,
            "operations": [
                {"op_id": op.op_id, "kind": op.kind, "node_id": op.node_id,
                 "node_type": op.node_type, "parameters": dict(op.parameters)}
                for op in self.operations
            ],
            "source": self.source,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def requires_gpu(self) -> bool:
        return any(op.requires_gpu for op in self.operations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "brief_id": self.brief_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_generation": self.base_generation,
            "operations": [
                {"op_id": op.op_id, "kind": op.kind, "node_type": op.node_type,
                 "node_id": op.node_id, "from_port": op.from_port, "to_port": op.to_port,
                 "parameters": dict(op.parameters), "requires_gpu": op.requires_gpu,
                 "preview_safe": op.preview_safe, "export_safe": op.export_safe}
                for op in self.operations
            ],
            "render_cost_estimate": self.render_cost_estimate,
            "risk_flags": list(self.risk_flags),
            "quality_checks": list(self.quality_checks),
            "source": self.source,
            "plan_hash": self.plan_hash(),
        }


# --------------------------------------------------------------------------- #
# Composability analysis (CP-02)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComposabilityFinding:
    aspect: str               # alpha_quality / spill / edge / hair / motion_blur / noise / exposure_diff / occlusion / track_points
    severity: str             # error / warning / info
    score: float              # 0..1 (1 = excellent)
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # AUD-4500: pin the details payload at construction time.
        object.__setattr__(self, "details", _freeze_mapping(self.details))


@dataclass(frozen=True)
class ComposabilityReport:
    """Whether a clip is suitable for the requested composite effect (CP-02)."""

    clip_id: str
    desired_effect: str
    findings: tuple[ComposabilityFinding, ...] = ()
    overall_score: float = 0.0
    recommended_approach: str = ""         # e.g. "needs_rotoscope" / "needs_track" / "direct_key"

    @property
    def is_blocker(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def errors(self) -> tuple[ComposabilityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "desired_effect": self.desired_effect,
            "overall_score": self.overall_score,
            "recommended_approach": self.recommended_approach,
            "is_blocker": self.is_blocker,
            "findings": [
                {"aspect": f.aspect, "severity": f.severity, "score": f.score,
                 "message": f.message, "details": dict(f.details)}
                for f in self.findings
            ],
        }


# --------------------------------------------------------------------------- #
# Composite quality gate (CP-09)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CompositeQualityFinding:
    check: str               # missing_input / alpha_error / premult_error / black_edge / edge_flicker / frame_jitter / color_space / render_error / output_size / output_duration / hash_mismatch
    severity: str
    code: str
    message: str
    node_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # AUD-4500: pin the details payload at construction time.
        object.__setattr__(self, "details", _freeze_mapping(self.details))


@dataclass(frozen=True)
class CompositeQualityReport:
    """Post-render quality gate for composite output (CP-09)."""

    findings: tuple[CompositeQualityFinding, ...] = ()
    checks_run: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[CompositeQualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def is_deliverable(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_deliverable": self.is_deliverable,
            "error_count": len(self.errors),
            "checks_run": list(self.checks_run),
            "findings": [
                {"check": f.check, "severity": f.severity, "code": f.code,
                 "message": f.message, "node_id": f.node_id, "details": dict(f.details)}
                for f in self.findings
            ],
        }


# Ops whose execution resolves a target node through ``op.node_id``.
# ``add_node`` is excluded: it *creates* the node and its node_id is the new
# node's id (may be unset when the executor assigns one).
_TARGET_NODE_OPS: frozenset[str] = frozenset({
    NODE_OP_REMOVE_NODE, NODE_OP_SET_PARAM, NODE_OP_DUPLICATE_NODE,
    NODE_OP_GROUP, NODE_OP_UNGROUP, NODE_OP_SET_OUTPUT_TARGET,
    NODE_OP_CONNECT, NODE_OP_DISCONNECT,
})


def validate_composite_plan(
    plan: CompositePlan,
    *,
    available_node_types: Iterable[str] = (),
    gpu_available: bool = True,
    existing_node_ids: Iterable[str] = (),
) -> CompositeQualityReport:
    """Pre-render validation of a composite plan (CP-03/CP-09).

    Checks that every ``add_node`` declares an existing node type (when a
    registry is supplied) and a non-empty ``node_type``; operations that
    resolve a target node declare a non-empty ``node_id`` (and, when
    ``existing_node_ids`` is supplied, reference an existing node);
    connect/disconnect declare both ports; and GPU-requiring operations
    have GPU available.
    """

    findings: list[CompositeQualityFinding] = []
    checks: list[str] = [
        "node_type_exists", "gpu_available", "port_references",
        "node_id_references",
    ]
    available = set(available_node_types)
    existing = set(existing_node_ids)

    for op in plan.operations:
        if op.kind == NODE_OP_ADD_NODE:
            if not op.node_type:
                findings.append(CompositeQualityFinding(
                    check="node_type_exists", severity="error",
                    code="MISSING_NODE_TYPE",
                    message=f"操作 {op.op_id} 是 add_node 但未声明 node_type。",
                    node_id=op.node_id,
                ))
            elif available and op.node_type not in available:
                findings.append(CompositeQualityFinding(
                    check="node_type_exists", severity="error",
                    code="UNKNOWN_NODE_TYPE",
                    message=f"节点类型 {op.node_type!r} 不在可用列表中。",
                    node_id=op.node_id,
                ))
        if op.requires_gpu and not gpu_available:
            findings.append(CompositeQualityFinding(
                check="gpu_available", severity="error",
                code="GPU_REQUIRED_UNAVAILABLE",
                message=f"操作 {op.op_id} 需要 GPU，但当前环境不可用。",
                node_id=op.node_id,
            ))
        if op.kind in (NODE_OP_CONNECT, NODE_OP_DISCONNECT) and (not op.from_port or not op.to_port):
            findings.append(CompositeQualityFinding(
                check="port_references", severity="error",
                code="MISSING_PORT_REFERENCE",
                message=f"操作 {op.op_id} 缺少 from_port 或 to_port。",
                node_id=op.node_id,
            ))
        if op.kind in _TARGET_NODE_OPS and not op.node_id:
            findings.append(CompositeQualityFinding(
                check="node_id_references", severity="error",
                code="MISSING_NODE_ID",
                message=f"操作 {op.op_id}（{op.kind}）需要目标节点，但 node_id 为空。",
                node_id=op.node_id,
            ))
        elif (existing and op.node_id and op.kind != NODE_OP_ADD_NODE
                and op.node_id not in existing):
            findings.append(CompositeQualityFinding(
                check="node_id_references", severity="error",
                code="UNKNOWN_NODE_REFERENCE",
                message=f"操作 {op.op_id} 引用了不存在的节点 {op.node_id!r}。",
                node_id=op.node_id,
            ))

    return CompositeQualityReport(
        findings=tuple(findings),
        checks_run=tuple(dict.fromkeys(checks)),
    )


__all__ = [
    "CompositeBrief",
    "CompositePlan",
    "NodeGraphOperation",
    "ComposabilityFinding",
    "ComposabilityReport",
    "CompositeQualityFinding",
    "CompositeQualityReport",
    "SUPPORTED_COMPOSITE_EFFECTS",
    "SUPPORTED_NODE_OPS",
    "EFFECT_HUD",
    "EFFECT_SCREEN_REPLACEMENT",
    "EFFECT_BACKGROUND_REPLACE",
    "EFFECT_TEXT_OVERLAY",
    "EFFECT_LOGO_INSERT",
    "EFFECT_CLEANUP",
    "NODE_OP_ADD_NODE",
    "NODE_OP_CONNECT",
    "NODE_OP_DISCONNECT",
    "NODE_OP_SET_PARAM",
    "NODE_OP_REMOVE_NODE",
    "NODE_OP_GROUP",
    "NODE_OP_UNGROUP",
    "validate_composite_plan",
]
