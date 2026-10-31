"""Unified RunContext for all Agent entry points (AG-02).

AG-02 requires that chat, quick-skill and workflow-canvas entries share the
same scope semantics.  Today each entry maintains its own scope dataclass
(``ContextBuildRequest`` / ``WorkflowInvocationContext`` / ``SkillInput`` /
``AgentContext``) with misaligned fields, so "same input" can produce
different scope decisions depending on which door the user walked in.

This module defines the canonical :class:`RunContext` — the one typed object
every entry point derives its scope from.  It does not replace the existing
dataclasses (that would be a large refactor); instead it provides the
**alignment contract** they can be built from, plus adapter helpers that
construct the existing types from a RunContext so the migration is gradual.

Design rules (AG-02 acceptance):
* every field the plan lists (project / timeline / selected assets / brief /
  analysis fingerprint / profile / permission / budget / delivery profile)
  is present and typed;
* missing project / expired revision / out-of-scope asset / out-of-budget
  all fail closed;
* the RunContext is JSON-safe and Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Permission tiers (AG-02 / AG-07 link)
# --------------------------------------------------------------------------- #

PERMISSION_READ_ONLY = "read_only"
PERMISSION_PROPOSAL = "proposal"            # may suggest, not write
PERMISSION_CONFIRM_EACH = "confirm_each"    # write after per-action confirmation
PERMISSION_RESTRICTED_AUTO = "restricted_auto"  # bounded auto within scope

ALL_PERMISSIONS: frozenset[str] = frozenset({
    PERMISSION_READ_ONLY, PERMISSION_PROPOSAL,
    PERMISSION_CONFIRM_EACH, PERMISSION_RESTRICTED_AUTO,
})


# --------------------------------------------------------------------------- #
# RunContext
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunContext:
    """The canonical scope every Agent entry point should derive from.

    Attributes:
        run_id: Stable identifier for this run.
        project_id: Required — scope never crosses projects.
        timeline_id: Target timeline (None = no timeline bound).
        base_revision: Timeline revision the run was planned against.
        selected_asset_ids: Assets the user explicitly allowed in scope.
            ``None`` means the user explicitly allowed the whole project;
            an empty tuple means nothing is in scope (fail-closed).
        brief_id / brief_revision: The creative brief this run serves.
        analysis_fingerprint: Hash of the analysis evidence snapshot.
        delivery_profile_id: Delivery profile (aspect / codec / platform).
        permission: One of :data:`ALL_PERMISSIONS` — drives what the run may
            do without asking again.
        budget_units: Cost ceiling the user accepted.
        max_duration_seconds: Optional duration cap.
        allowed_hosts: Network hosts the run may contact (cloud upload).
        user_id: Sanitized user id for cohort / flag evaluation.
        metadata: Extra typed hints (selection range, locked clip ids, etc.).
    """

    run_id: str
    project_id: str
    timeline_id: str | None = None
    base_revision: int | None = None
    # AUD-14310：None = 显式"不限素材"（用户明确授权整工程作用域）；
    # () = 空集（拒绝全部素材，fail-closed）；非空 tuple = 显式圈定。
    selected_asset_ids: tuple[str, ...] | None = None
    brief_id: str | None = None
    brief_revision: int | None = None
    analysis_fingerprint: str | None = None
    delivery_profile_id: str | None = None
    permission: str = PERMISSION_PROPOSAL
    budget_units: float | None = None
    max_duration_seconds: float | None = None
    allowed_hosts: tuple[str, ...] = ()
    user_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("RunContext.run_id is required")
        if not self.project_id:
            raise ValueError("RunContext.project_id is required")
        if self.permission not in ALL_PERMISSIONS:
            raise ValueError(
                f"Unknown permission: {self.permission!r}; "
                f"expected one of {sorted(ALL_PERMISSIONS)}"
            )
        if self.base_revision is not None and self.base_revision < 0:
            raise ValueError("base_revision must be non-negative")
        if self.budget_units is not None and self.budget_units < 0:
            raise ValueError("budget_units cannot be negative")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if self.brief_revision is not None and self.brief_revision < 1:
            raise ValueError("brief_revision must be >= 1")

    # ---------------- derivation helpers ---------------- #

    @property
    def may_write(self) -> bool:
        return self.permission in {PERMISSION_CONFIRM_EACH, PERMISSION_RESTRICTED_AUTO}

    @property
    def may_auto_write(self) -> bool:
        return self.permission == PERMISSION_RESTRICTED_AUTO

    def asset_in_scope(self, asset_id: str) -> bool:
        """AUD-14310：仅当显式声明"不限"（None）时全工程在作用域内。

        空 tuple 是"用户圈定了空集"——拒绝全部素材（fail-closed），不再
        被解释为"全工程素材均授权"。
        """
        if self.selected_asset_ids is None:
            return True
        return asset_id in self.selected_asset_ids

    def host_allowed(self, host: str) -> bool:
        if not self.allowed_hosts:
            return False
        return host.lower().strip() in {h.lower().strip() for h in self.allowed_hosts}

    def fingerprint(self) -> str:
        """Stable hash of the semantic scope (excludes run_id / metadata)."""
        payload = {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_revision": self.base_revision,
            "selected_asset_ids": (
                sorted(self.selected_asset_ids)
                if self.selected_asset_ids is not None else None
            ),
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "analysis_fingerprint": self.analysis_fingerprint,
            "delivery_profile_id": self.delivery_profile_id,
            "permission": self.permission,
            "budget_units": self.budget_units,
            "max_duration_seconds": self.max_duration_seconds,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_revision": self.base_revision,
            "selected_asset_ids": (
                list(self.selected_asset_ids)
                if self.selected_asset_ids is not None else None
            ),
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "analysis_fingerprint": self.analysis_fingerprint,
            "delivery_profile_id": self.delivery_profile_id,
            "permission": self.permission,
            "budget_units": self.budget_units,
            "max_duration_seconds": self.max_duration_seconds,
            "allowed_hosts": list(self.allowed_hosts),
            "user_id": self.user_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunContext":
        # AUD-14310：键缺失或显式 null → None（显式不限）；空列表 → ()（拒绝
        # 全部，fail-closed）；畸形类型 → ()（外部数据须验证，不猜成不限）。
        raw_assets = data.get("selected_asset_ids")
        if raw_assets is None:
            assets_scope: tuple[str, ...] | None = None
        elif isinstance(raw_assets, (list, tuple)):
            assets_scope = tuple(str(a) for a in raw_assets)
        else:
            assets_scope = ()
        hosts = data.get("allowed_hosts")
        return cls(
            run_id=str(data.get("run_id", "")),
            project_id=str(data.get("project_id", "")),
            timeline_id=data.get("timeline_id"),
            base_revision=data.get("base_revision"),
            selected_asset_ids=assets_scope,
            brief_id=data.get("brief_id"),
            brief_revision=data.get("brief_revision"),
            analysis_fingerprint=data.get("analysis_fingerprint"),
            delivery_profile_id=data.get("delivery_profile_id"),
            permission=str(data.get("permission", PERMISSION_PROPOSAL)),
            budget_units=data.get("budget_units"),
            max_duration_seconds=data.get("max_duration_seconds"),
            allowed_hosts=tuple(str(h) for h in hosts) if isinstance(hosts, (list, tuple)) else (),
            user_id=data.get("user_id"),
            metadata=dict(data.get("metadata", {})),
        )


# --------------------------------------------------------------------------- #
# Scope validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunContextIssue:
    code: str
    message: str
    field: str = ""


def validate_run_context(ctx: RunContext, *, observed_revision: int | None = None) -> tuple[RunContextIssue, ...]:
    """Return issues that would make a RunContext unsafe to execute against.

    When ``observed_revision`` is provided (the *current* timeline revision),
    a mismatch with ``base_revision`` is reported as stale — the plan must be
    regenerated before it can be applied.
    """

    issues: list[RunContextIssue] = []
    if observed_revision is not None and ctx.base_revision is not None:
        if observed_revision != ctx.base_revision:
            issues.append(RunContextIssue(
                code="STALE_REVISION",
                field="base_revision",
                message=f"计划基于 revision {ctx.base_revision}，当前已变为 {observed_revision}。",
            ))
    if ctx.brief_id and not ctx.brief_revision:
        issues.append(RunContextIssue(
            code="BRIEF_REVISION_MISSING",
            field="brief_revision",
            message="指定了 brief_id 但缺少 brief_revision。",
        ))
    if ctx.permission == PERMISSION_RESTRICTED_AUTO and ctx.budget_units is None:
        issues.append(RunContextIssue(
            code="BUDGET_REQUIRED_FOR_AUTO",
            field="budget_units",
            message="受限自动模式必须设定预算上限。",
        ))
    # AUD-14310：受限自动写（RESTRICTED_AUTO）必须显式圈定素材范围——
    # None（整工程不限）与"受限"语义矛盾，Agent 可改动用户从未圈定的素材。
    if ctx.permission == PERMISSION_RESTRICTED_AUTO and ctx.selected_asset_ids is None:
        issues.append(RunContextIssue(
            code="SCOPE_REQUIRED_FOR_AUTO",
            field="selected_asset_ids",
            message="受限自动模式必须显式圈定素材范围（selected_asset_ids）。",
        ))
    return tuple(issues)


__all__ = [
    "RunContext",
    "RunContextIssue",
    "validate_run_context",
    "ALL_PERMISSIONS",
    "PERMISSION_READ_ONLY",
    "PERMISSION_PROPOSAL",
    "PERMISSION_CONFIRM_EACH",
    "PERMISSION_RESTRICTED_AUTO",
]
