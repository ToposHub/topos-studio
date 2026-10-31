"""Strict Tool / Capability Catalog (OS-03).

OS-03 asks for a per-tool catalog entry that carries *everything* the planner,
gate and approval layer need to decide whether a tool may run: domain,
required evidence, budget estimate, idempotency strategy, cancel semantics,
quality gate, and compensation action.  The existing ``ToolDefinition`` in
``tool_facade.py`` has 5 fields; this module defines the richer
:class:`CapabilityCatalogEntry` that extends it without breaking the existing
type.

The catalog is the single source of truth: a tool not in the catalog is
refused by the planner.  This makes "model invents a tool" structurally
impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Domain vocabulary
# --------------------------------------------------------------------------- #

DOMAIN_EDITORIAL = "editorial"
DOMAIN_COLOR = "color"
DOMAIN_COMPOSITE = "composite"
DOMAIN_DELIVERY = "delivery"
DOMAIN_SYSTEM = "system"

SUPPORTED_DOMAINS: frozenset[str] = frozenset({
    DOMAIN_EDITORIAL, DOMAIN_COLOR, DOMAIN_COMPOSITE, DOMAIN_DELIVERY, DOMAIN_SYSTEM,
})


# --------------------------------------------------------------------------- #
# Cancel / idempotency / quality semantics
# --------------------------------------------------------------------------- #

CANCEL_COOPERATIVE = "cooperative"      # worker polls cancel token
CANCEL_ATOMIC = "atomic"                # whole step rolls back on cancel
CANCEL_NONE = "none"                    # cannot cancel once started

ALL_CANCEL_SEMANTICS: frozenset[str] = frozenset({CANCEL_COOPERATIVE, CANCEL_ATOMIC, CANCEL_NONE})

IDEMPOTENCY_NONE = "none"
IDEMPOTENCY_REQUEST_KEY = "request_key"    # dedupe by request fingerprint
IDEMPOTENCY_CONTENT_KEY = "content_key"    # dedupe by content hash

ALL_IDEMPOTENCY_STRATEGIES: frozenset[str] = frozenset({
    IDEMPOTENCY_NONE, IDEMPOTENCY_REQUEST_KEY, IDEMPOTENCY_CONTENT_KEY,
})


# --------------------------------------------------------------------------- #
# Catalog entry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CompensationAction:
    """How to undo a tool's effect if a later step fails."""

    strategy: str                         # undo / rollback_group / manual
    undo_tool: str | None = None
    description: str = ""


@dataclass(frozen=True)
class CapabilityCatalogEntry:
    """One tool's full declaration for the planner / gate / approval layer.

    This extends ``ToolDefinition`` (name / permission / description /
    input_schema / output_description) with the OS-03 fields:

    * ``domain`` — which domain the tool belongs to;
    * ``required_evidence`` — evidence kinds that must be present before the
      tool may be called;
    * ``budget_estimate`` — expected cost in abstract units;
    * ``idempotency_strategy`` — how duplicate calls are deduped;
    * ``cancel_semantics`` — cooperative / atomic / none;
    * ``quality_gates`` — quality-check ids to run after the tool;
    * ``compensation`` — how to undo the tool's effect;
    * ``preview_support`` / ``export_support`` — whether the tool's output is
      preview-safe / export-safe.
    """

    name: str
    domain: str
    permission: str                       # mirrors ToolPermission values
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_description: str = ""
    required_evidence: tuple[str, ...] = ()
    budget_estimate: float = 0.0
    idempotency_strategy: str = IDEMPOTENCY_NONE
    cancel_semantics: str = CANCEL_COOPERATIVE
    quality_gates: tuple[str, ...] = ()
    compensation: CompensationAction | None = None
    preview_support: bool = True
    export_support: bool = True
    writes_timeline: bool = False
    execution_mode: str = "exclusive"
    owner_plugin_id: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CapabilityCatalogEntry.name is required")
        if self.domain not in SUPPORTED_DOMAINS:
            raise ValueError(
                f"Unsupported domain: {self.domain!r}; "
                f"expected one of {sorted(SUPPORTED_DOMAINS)}"
            )
        if self.idempotency_strategy not in ALL_IDEMPOTENCY_STRATEGIES:
            raise ValueError(f"Unknown idempotency strategy: {self.idempotency_strategy!r}")
        if self.cancel_semantics not in ALL_CANCEL_SEMANTICS:
            raise ValueError(f"Unknown cancel semantics: {self.cancel_semantics!r}")
        if self.budget_estimate < 0:
            raise ValueError("budget_estimate cannot be negative")
        if self.execution_mode not in {"exclusive", "parallel"}:
            raise ValueError(f"Unknown execution mode: {self.execution_mode!r}")
        if self.owner_plugin_id and not str(self.owner_plugin_id).strip():
            raise ValueError("owner_plugin_id cannot be whitespace")

    @property
    def requires_approval(self) -> bool:
        return self.permission in {"write", "expensive", "file_write"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "permission": self.permission,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_description": self.output_description,
            "required_evidence": list(self.required_evidence),
            "budget_estimate": self.budget_estimate,
            "idempotency_strategy": self.idempotency_strategy,
            "cancel_semantics": self.cancel_semantics,
            "quality_gates": list(self.quality_gates),
            "compensation": (
                {"strategy": self.compensation.strategy, "undo_tool": self.compensation.undo_tool,
                 "description": self.compensation.description}
                if self.compensation else None
            ),
            "preview_support": self.preview_support,
            "export_support": self.export_support,
            "writes_timeline": self.writes_timeline,
            "execution_mode": self.execution_mode,
            "owner_plugin_id": self.owner_plugin_id,
        }


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


class CapabilityCatalog:
    """Append-only registry of declared tool capabilities.

    The planner asks :meth:`get` before calling a tool; a miss means
    "tool not declared, refuse".  :meth:`missing_evidence_for` reports which
    evidence kinds are absent so the planner can request analysis first.
    """

    def __init__(self, entries: Iterable[CapabilityCatalogEntry] = ()) -> None:
        self._entries: dict[str, CapabilityCatalogEntry] = {}
        for entry in entries:
            self.register(entry)

    def register(self, entry: CapabilityCatalogEntry, *, replace: bool = False) -> bool:
        if entry.name in self._entries and not replace:
            return False
        self._entries[entry.name] = entry
        return True

    def remove(self, name: str, *, owner_plugin_id: str = "") -> bool:
        """Remove an extension-owned entry without touching core entries.

        AUD-13112：core 条目（``owner_plugin_id`` 为空）永不删除；调用方还必须
        声明归属——传入的 ``owner_plugin_id`` 为空或与条目归属不符时拒绝删除，
        与本 docstring 的承诺一致（此前空 ``owner_plugin_id`` 可删除任何条目，
        包括 core 条目）。
        """

        entry = self._entries.get(str(name))
        if entry is None:
            return False
        owner = str(owner_plugin_id).strip()
        if not owner or not entry.owner_plugin_id:
            return False
        if entry.owner_plugin_id != owner:
            return False
        del self._entries[str(name)]
        return True

    def get(self, name: str) -> CapabilityCatalogEntry | None:
        return self._entries.get(name)

    def require(self, name: str) -> CapabilityCatalogEntry:
        entry = self.get(name)
        if entry is None:
            raise KeyError(f"Tool not declared in capability catalog: {name!r}")
        return entry

    def all_entries(self) -> tuple[CapabilityCatalogEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def entries_for_domain(self, domain: str) -> tuple[CapabilityCatalogEntry, ...]:
        return tuple(e for e in self._entries.values() if e.domain == domain)

    def missing_evidence_for(
        self,
        tool_name: str,
        available_evidence: Iterable[str],
    ) -> tuple[str, ...]:
        entry = self.get(tool_name)
        if entry is None:
            return ()
        available = set(available_evidence)
        return tuple(e for e in entry.required_evidence if e not in available)

    def tools_writing_timeline(self) -> tuple[CapabilityCatalogEntry, ...]:
        return tuple(e for e in self._entries.values() if e.writes_timeline)


# --------------------------------------------------------------------------- #
# Seeded catalog (mirrors the existing TAEP tool table)
# --------------------------------------------------------------------------- #


def create_default_capability_catalog() -> CapabilityCatalog:
    """Seed the catalog with every tool declared by ``tool_facade``.

    The richer entries below preserve the audited policy for high-impact
    operations.  The final reconciliation pass covers newly-added read and
    preview tools, so the runtime cannot accidentally expose a tool that is
    absent from the capability catalog.
    """
    catalog = CapabilityCatalog()

    catalog.register(CapabilityCatalogEntry(
        name="topos.get_capabilities", domain=DOMAIN_SYSTEM, permission="read",
        description="返回当前软件真正支持的能力、值域和限制。",
        execution_mode="parallel",
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.get_project_snapshot", domain=DOMAIN_SYSTEM, permission="read",
        description="返回脱敏的只读工程快照。",
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.analyze_asset", domain=DOMAIN_EDITORIAL, permission="read",
        description="对指定素材执行 ASR / 镜头 / OCR / 音频分析。",
        required_evidence=(),  # this tool *produces* evidence
        budget_estimate=0.5,
        cancel_semantics=CANCEL_COOPERATIVE,
        quality_gates=("analysis_complete",),
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.propose_edit_plan", domain=DOMAIN_EDITORIAL, permission="proposal",
        description="基于 brief 和证据生成可审阅编辑方案。",
        required_evidence=("transcript", "shot"),
        budget_estimate=1.0,
        idempotency_strategy=IDEMPOTENCY_CONTENT_KEY,
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.preview_edit_plan", domain=DOMAIN_EDITORIAL, permission="preview",
        description="编译方案并模拟差异，不写工程。",
        budget_estimate=0.3,
        cancel_semantics=CANCEL_COOPERATIVE,
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.apply_edit_plan", domain=DOMAIN_EDITORIAL, permission="write",
        description="经审批后把方案提交到时间线。",
        writes_timeline=True,
        budget_estimate=0.2,
        idempotency_strategy=IDEMPOTENCY_CONTENT_KEY,
        cancel_semantics=CANCEL_ATOMIC,
        compensation=CompensationAction(strategy="undo", undo_tool="topos.undo_last_commit",
                                        description="撤销最近一次时间线写入。"),
        quality_gates=("post_commit_verify",),
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.request_export", domain=DOMAIN_DELIVERY, permission="file_write",
        description="导出最终文件并经质量门验证。",
        budget_estimate=2.0,
        idempotency_strategy=IDEMPOTENCY_CONTENT_KEY,
        cancel_semantics=CANCEL_COOPERATIVE,
        quality_gates=("delivery_quality_gate",),
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.generate_media", domain=DOMAIN_SYSTEM, permission="expensive",
        description="付费生成图像/视频/音频素材。",
        required_evidence=(),
        budget_estimate=5.0,
        idempotency_strategy=IDEMPOTENCY_REQUEST_KEY,
        cancel_semantics=CANCEL_COOPERATIVE,
    ))
    catalog.register(CapabilityCatalogEntry(
        name="topos.undo_last_commit", domain=DOMAIN_EDITORIAL, permission="write",
        description="撤销最近一次时间线写入（补偿操作）。",
        writes_timeline=True,
        budget_estimate=0.1,
        cancel_semantics=CANCEL_ATOMIC,
    ))

    # Keep the catalog and the transport-facing tool table in lockstep.  This
    # is deliberately a local import: tool_facade may import catalog types for
    # runtime wiring, while catalog construction itself must remain cycle-free.
    from src.features.edit.ai_editing.tool_facade import list_tool_definitions

    def domain_for(tool_name: str) -> str:
        if "composite" in tool_name:
            return DOMAIN_COMPOSITE
        if any(token in tool_name for token in ("export", "render_preview")):
            return DOMAIN_DELIVERY
        if any(token in tool_name for token in ("help", "diagnostic", "capabilities", "project_snapshot")):
            return DOMAIN_SYSTEM
        return DOMAIN_EDITORIAL

    for definition in list_tool_definitions():
        permission = getattr(definition.permission, "value", definition.permission)
        definition_mode = getattr(
            getattr(definition, "execution_mode", None), "value",
            getattr(definition, "execution_mode", "exclusive"),
        )
        existing = catalog.get(definition.name)
        if existing is not None:
            if existing.execution_mode != definition_mode:
                catalog.register(
                    replace(existing, execution_mode=str(definition_mode)),
                    replace=True,
                )
            continue
        is_write = permission in {"write", "file_write"}
        catalog.register(CapabilityCatalogEntry(
            name=definition.name,
            domain=domain_for(definition.name),
            permission=str(permission),
            description=definition.description,
            input_schema=definition.input_schema,
            output_description=definition.output_description,
            budget_estimate=1.0 if permission in {"expensive", "file_write"} else 0.1,
            cancel_semantics=CANCEL_ATOMIC if is_write else CANCEL_COOPERATIVE,
            writes_timeline=definition.name in {
                "topos.apply_edit_plan", "topos.apply_composite_edit",
            },
            execution_mode=str(definition_mode),
        ))

    return catalog


__all__ = [
    "CapabilityCatalog",
    "CapabilityCatalogEntry",
    "CompensationAction",
    "SUPPORTED_DOMAINS",
    "ALL_CANCEL_SEMANTICS",
    "ALL_IDEMPOTENCY_STRATEGIES",
    "DOMAIN_EDITORIAL",
    "DOMAIN_COLOR",
    "DOMAIN_COMPOSITE",
    "DOMAIN_DELIVERY",
    "DOMAIN_SYSTEM",
    "CANCEL_COOPERATIVE",
    "CANCEL_ATOMIC",
    "CANCEL_NONE",
    "IDEMPOTENCY_NONE",
    "IDEMPOTENCY_REQUEST_KEY",
    "IDEMPOTENCY_CONTENT_KEY",
    "create_default_capability_catalog",
]
