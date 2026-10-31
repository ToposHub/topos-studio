"""Bounded AI execution policy for suggestion, semi-automatic and restricted modes.

The policy is deliberately independent from Qt and from the model client.  It
is the decision layer used by an Agent gate before a tool is invoked.  A
connected provider never implies permission to write a timeline, upload media,
spend money or publish a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


class ExecutionMode(str, Enum):
    SUGGESTION = "suggestion"
    SEMI_AUTOMATIC = "semi_automatic"
    RESTRICTED_AUTOMATIC = "restricted_automatic"


class ExecutionAction(str, Enum):
    READ = "read"
    ANALYZE_LOCAL = "analyze_local"
    PLAN = "plan"
    PREVIEW = "preview"
    LOCAL_EDIT = "local_edit"
    TIMELINE_WRITE = "timeline_write"
    CLOUD_UPLOAD = "cloud_upload"
    PAID_GENERATION = "paid_generation"
    FINAL_EXPORT = "final_export"
    AUTO_PUBLISH = "auto_publish"


@dataclass(frozen=True)
class ExecutionScope:
    """The project, assets, budget and network boundaries for auto execution."""

    project_id: str
    timeline_id: str | None = None
    base_revision: int | None = None
    asset_ids: tuple[str, ...] = ()
    template_ids: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_file_extensions: tuple[str, ...] = ()
    budget_units: float | None = None
    max_duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("execution scope project_id is required")
        if self.base_revision is not None and self.base_revision < 0:
            raise ValueError("execution scope base_revision must be non-negative")
        if self.budget_units is not None and self.budget_units < 0:
            raise ValueError("execution scope budget_units must be non-negative")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("execution scope max_duration_seconds must be positive")

    def allows_host(self, host_or_url: str) -> bool:
        """Return whether a network host is explicitly allow-listed."""
        if not self.allowed_hosts:
            return False
        raw = str(host_or_url).strip().lower()
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower().rstrip(".")
        return bool(host) and host in {
            item.strip().lower().rstrip(".") for item in self.allowed_hosts if item.strip()
        }

    def allows_extension(self, extension: str) -> bool:
        if not self.allowed_file_extensions:
            return True
        value = str(extension).strip().lower()
        if value and not value.startswith("."):
            value = f".{value}"
        return value in {
            item.strip().lower() if item.strip().startswith(".") else f".{item.strip().lower()}"
            for item in self.allowed_file_extensions
            if item.strip()
        }


@dataclass(frozen=True)
class ActionRequest:
    action: ExecutionAction | str
    project_id: str
    timeline_id: str | None = None
    base_revision: int | None = None
    asset_ids: tuple[str, ...] = ()
    template_id: str | None = None
    host: str | None = None
    file_extension: str | None = None
    cost_units: float = 0.0
    duration_seconds: float = 0.0
    risk_flags: tuple[str, ...] = ()

    def normalized_action(self) -> ExecutionAction | None:
        try:
            return self.action if isinstance(self.action, ExecutionAction) else ExecutionAction(str(self.action))
        except ValueError:
            return None


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""
    action: str = ""


_TOOL_ACTIONS: dict[str, ExecutionAction] = {
    "read": ExecutionAction.READ,
    "observe": ExecutionAction.READ,
    "analyze": ExecutionAction.ANALYZE_LOCAL,
    "analyze_local": ExecutionAction.ANALYZE_LOCAL,
    "plan": ExecutionAction.PLAN,
    "preview": ExecutionAction.PREVIEW,
    "render_preview": ExecutionAction.PREVIEW,
    "local_edit": ExecutionAction.LOCAL_EDIT,
    "timeline_write": ExecutionAction.TIMELINE_WRITE,
    "commit_plan": ExecutionAction.TIMELINE_WRITE,
    "cloud_upload": ExecutionAction.CLOUD_UPLOAD,
    "paid_generation": ExecutionAction.PAID_GENERATION,
    "generate_media": ExecutionAction.PAID_GENERATION,
    "final_export": ExecutionAction.FINAL_EXPORT,
    "export_file": ExecutionAction.FINAL_EXPORT,
    "auto_publish": ExecutionAction.AUTO_PUBLISH,
}


class ExecutionPolicy:
    """Evaluate one action against a bounded automation mode and scope."""

    def __init__(
        self,
        mode: ExecutionMode,
        scope: ExecutionScope,
        *,
        cloud_upload_enabled: bool = False,
        generation_enabled: bool = False,
        auto_export_enabled: bool = False,
        permission_tier: str | None = None,
    ) -> None:
        self.mode = mode
        self.scope = scope
        self.cloud_upload_enabled = cloud_upload_enabled
        self.generation_enabled = generation_enabled
        self.auto_export_enabled = auto_export_enabled
        # AG-07: optional four-tier permission override.  When set, it takes
        # precedence over the coarse 3-mode system for write decisions.
        self.permission_tier = str(permission_tier).lower() if permission_tier else None

    @classmethod
    def from_permission_tier(
        cls,
        tier: str,
        scope: ExecutionScope,
        *,
        cloud_upload_enabled: bool = False,
        generation_enabled: bool = False,
        auto_export_enabled: bool = False,
    ) -> "ExecutionPolicy":
        """AG-07: construct from a four-tier permission string.

        Maps the RunContext permission tier to the closest ExecutionMode
        while preserving the tier for fine-grained write decisions.
        """
        t = str(tier or "").strip().lower()
        mode_map = {
            "read_only": ExecutionMode.SUGGESTION,
            "proposal": ExecutionMode.SUGGESTION,
            "confirm_each": ExecutionMode.SEMI_AUTOMATIC,
            "restricted_auto": ExecutionMode.RESTRICTED_AUTOMATIC,
        }
        mode = mode_map.get(t, ExecutionMode.SUGGESTION)
        return cls(
            mode, scope,
            cloud_upload_enabled=cloud_upload_enabled,
            generation_enabled=generation_enabled,
            auto_export_enabled=auto_export_enabled,
            permission_tier=t,
        )

    @classmethod
    def from_feature_flags(cls, flags: Any, scope: ExecutionScope) -> "ExecutionPolicy":
        """Map existing safe-by-default feature flags to the three policy modes."""
        def _enabled(name: str) -> bool:
            if isinstance(flags, Mapping):
                return bool(flags.get(name, False))
            checker = getattr(flags, "is_enabled", None)
            if callable(checker):
                try:
                    return bool(checker(name))
                except Exception:  # noqa: BLE001 - a broken flag object is fail-safe
                    return False
            return bool(getattr(flags, name, False))

        auto_mode = _enabled("agent_auto_mode")
        auto_export = _enabled("auto_export")
        mode = (
            ExecutionMode.RESTRICTED_AUTOMATIC
            if auto_mode and auto_export
            else ExecutionMode.SEMI_AUTOMATIC
            if auto_mode
            else ExecutionMode.SUGGESTION
        )
        return cls(
            mode,
            scope,
            cloud_upload_enabled=_enabled("cloud_upload"),
            generation_enabled=_enabled("generation"),
            auto_export_enabled=auto_export,
        )

    def evaluate(self, request: ActionRequest, *, approval_granted: bool = False) -> PolicyDecision:
        action = request.normalized_action()
        action_name = str(request.action)
        if action is None:
            return PolicyDecision(False, reason="tool/action is not allow-listed", action=action_name)
        if request.project_id != self.scope.project_id:
            return PolicyDecision(False, reason="project scope mismatch", action=action.value)
        if (
            self.scope.timeline_id is not None
            and request.timeline_id is not None
            and request.timeline_id != self.scope.timeline_id
        ):
            return PolicyDecision(False, reason="timeline scope mismatch", action=action.value)
        if (
            self.scope.base_revision is not None
            and request.base_revision is not None
            and request.base_revision != self.scope.base_revision
        ):
            return PolicyDecision(False, reason="base revision changed; re-plan is required", action=action.value)
        if self.scope.asset_ids and not set(request.asset_ids).issubset(set(self.scope.asset_ids)):
            return PolicyDecision(False, reason="asset is outside the approved scope", action=action.value)
        if (
            self.scope.template_ids
            and request.template_id is not None
            and request.template_id not in self.scope.template_ids
        ):
            return PolicyDecision(False, reason="workflow template is outside the approved scope", action=action.value)
        if action == ExecutionAction.TIMELINE_WRITE and self.scope.timeline_id and not request.timeline_id:
            return PolicyDecision(False, reason="timeline scope is required for a write", action=action.value)
        if action == ExecutionAction.TIMELINE_WRITE and self.scope.base_revision is not None and request.base_revision is None:
            return PolicyDecision(False, reason="base revision is required for a write", action=action.value)
        if (
            action in {ExecutionAction.TIMELINE_WRITE, ExecutionAction.PAID_GENERATION, ExecutionAction.FINAL_EXPORT}
            and self.scope.template_ids
            and request.template_id is None
        ):
            return PolicyDecision(False, reason="approved workflow template is required", action=action.value)
        if request.cost_units < 0 or request.duration_seconds < 0:
            return PolicyDecision(False, reason="negative cost or duration is invalid", action=action.value)
        if self.scope.budget_units is not None and request.cost_units > self.scope.budget_units:
            return PolicyDecision(False, reason="cost exceeds the approved budget", action=action.value)
        if (
            self.scope.max_duration_seconds is not None
            and request.duration_seconds > self.scope.max_duration_seconds
        ):
            return PolicyDecision(False, reason="duration exceeds the approved limit", action=action.value)
        if action == ExecutionAction.CLOUD_UPLOAD:
            if not self.cloud_upload_enabled:
                return PolicyDecision(False, reason="cloud upload is disabled", action=action.value)
            if not request.host or not self.scope.allows_host(request.host):
                return PolicyDecision(False, reason="network host is not allow-listed", action=action.value)
        if action == ExecutionAction.PAID_GENERATION and not self.generation_enabled:
            return PolicyDecision(False, reason="media generation is disabled", action=action.value)
        if request.file_extension and not self.scope.allows_extension(request.file_extension):
            return PolicyDecision(False, reason="file type is outside the approved scope", action=action.value)

        # AG-07: four-tier permission check takes precedence over mode.
        if self.permission_tier and action in WRITE_ACTIONS:
            if not permission_tier_allows_write(
                self.permission_tier, approval_granted=approval_granted,
            ):
                if self.permission_tier in ("read_only", "proposal"):
                    return PolicyDecision(
                        False,
                        reason=f"permission tier '{self.permission_tier}' does not allow writes",
                        action=action.value,
                    )
                if self.permission_tier not in PERMISSION_TIERS:
                    # Unknown tier → fail closed.
                    return PolicyDecision(
                        False,
                        reason=f"unknown permission tier '{self.permission_tier}'",
                        action=action.value,
                    )
                # confirm_each without approval falls through to the mode-based
                # approval logic below.
                approval_required_override = True
            else:
                approval_required_override = False
        else:
            approval_required_override = None

        approval_required = self._approval_required(action)
        if approval_required_override is True:
            approval_required = True
        elif (
            approval_required_override is False
            and self.permission_tier == "restricted_auto"
            # AUTO_PUBLISH keeps its own consent boundary: the restricted_auto
            # tier may relax generic write approvals, but never publishing
            # (the tier check above already verified the write itself).
            and action != ExecutionAction.AUTO_PUBLISH
        ):
            approval_required = False
        if request.risk_flags:
            approval_required = True
        if approval_required and not approval_granted:
            return PolicyDecision(
                False,
                needs_approval=True,
                reason="explicit user approval is required for this action",
                action=action.value,
            )
        return PolicyDecision(True, action=action.value)

    def check_plan(self, context: Any, steps: Iterable[Any]) -> PolicyDecision:
        """Check an AgentPlan without importing the Agent loop (no cycle)."""
        approval_granted = bool(getattr(context, "approval_granted", False))
        for step in steps:
            tool_name = str(getattr(step, "tool_name", "")).strip().lower()
            action = _TOOL_ACTIONS.get(tool_name)
            if action is None:
                return PolicyDecision(False, reason=f"tool {tool_name!r} is not allow-listed", action=tool_name)
            if bool(getattr(step, "writes_timeline", False)):
                action = ExecutionAction.TIMELINE_WRITE
            raw_request = getattr(step, "policy_request", None) or {}
            if not isinstance(raw_request, Mapping):
                return PolicyDecision(
                    False,
                    reason="step policy_request must be an object",
                    action=action.value,
                )

            def _tuple(name: str, fallback: Iterable[str] = ()) -> tuple[str, ...]:
                value = raw_request.get(name, fallback)
                if isinstance(value, str):
                    return (value,) if value else ()
                if isinstance(value, (list, tuple, set)):
                    return tuple(str(item) for item in value if str(item).strip())
                return tuple(str(item) for item in fallback)

            def _number(name: str, fallback: float = 0.0) -> float:
                value = raw_request.get(name, fallback)
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return -1.0

            decision = self.evaluate(
                ActionRequest(
                    action=action,
                    project_id=str(raw_request.get("project_id", getattr(context, "project_id", ""))),
                    timeline_id=raw_request.get("timeline_id", getattr(context, "timeline_id", None)),
                    base_revision=raw_request.get("base_revision", getattr(context, "base_revision", None)),
                    asset_ids=_tuple("asset_ids", getattr(context, "asset_ids", ())),
                    template_id=raw_request.get("template_id", getattr(context, "template_id", None)),
                    host=raw_request.get("host"),
                    file_extension=raw_request.get("file_extension"),
                    cost_units=_number("cost_units"),
                    duration_seconds=_number("duration_seconds"),
                    risk_flags=_tuple("risk_flags"),
                ),
                approval_granted=approval_granted,
            )
            if not decision.allowed:
                return decision
        return PolicyDecision(True, action="plan")

    def _approval_required(self, action: ExecutionAction) -> bool:
        if action == ExecutionAction.AUTO_PUBLISH:
            return True  # publishing has an independent consent boundary
        if self.mode == ExecutionMode.SUGGESTION:
            return action not in {
                ExecutionAction.READ,
                ExecutionAction.ANALYZE_LOCAL,
                ExecutionAction.PLAN,
                ExecutionAction.PREVIEW,
            }
        if self.mode == ExecutionMode.SEMI_AUTOMATIC:
            return action in {
                ExecutionAction.TIMELINE_WRITE,
                ExecutionAction.CLOUD_UPLOAD,
                ExecutionAction.PAID_GENERATION,
                ExecutionAction.FINAL_EXPORT,
            }
        if action == ExecutionAction.FINAL_EXPORT and not self.auto_export_enabled:
            return True
        return False


__all__ = [
    "ActionRequest",
    "ExecutionAction",
    "ExecutionMode",
    "ExecutionPolicy",
    "ExecutionScope",
    "PolicyDecision",
    "PERMISSION_TIERS",
    "WRITE_ACTIONS",
    "permission_tier_allows_write",
]


# --------------------------------------------------------------------------- #
# AG-07: Four-tier permission system (linked to run_context.ALL_PERMISSIONS)
# --------------------------------------------------------------------------- #

#: All actions that modify the project / timeline / files / cloud.
WRITE_ACTIONS: frozenset[ExecutionAction] = frozenset({
    ExecutionAction.LOCAL_EDIT,
    ExecutionAction.TIMELINE_WRITE,
    ExecutionAction.CLOUD_UPLOAD,
    ExecutionAction.PAID_GENERATION,
    ExecutionAction.FINAL_EXPORT,
    ExecutionAction.AUTO_PUBLISH,
})

#: The four permission tiers from AG-07 / run_context.py.
PERMISSION_TIERS = {
    "read_only",
    "proposal",
    "confirm_each",
    "restricted_auto",
}


def permission_tier_allows_write(tier: str, *, approval_granted: bool = False) -> bool:
    """Check whether a permission tier permits a write action at all.

    * ``read_only``  — never.
    * ``proposal``   — never (may suggest plans, but cannot write even with approval).
    * ``confirm_each`` — only when ``approval_granted`` is True.
    * ``restricted_auto`` — always (bounded by scope/budget checks elsewhere).
    """
    t = str(tier or "").strip().lower()
    if t == "read_only":
        return False
    if t == "proposal":
        return False
    if t == "confirm_each":
        return approval_granted
    if t == "restricted_auto":
        return True
    # Unknown tier → fail closed.
    return False
