"""Structured tool results + approval derivation (plan §A2 §8-9).

Every tool invocation that flows through the ``ToolFacade`` returns a
structured :class:`ToolResult` instead of a bare ``dict`` or string, so the
agent loop, the Run Drawer and the audit log all share one shape for success,
partial success, failure and permission denial (plan §A2 §8).

Hard rules (plan §A2 §9 / §8):

- **Auditable but redacted.**  ``to_dict`` runs every payload through
  ``sanitize_for_storage`` so an audit event never leaks a secret, an
  absolute path or a full sensitive prompt.
- **Diagnostics are layered.**  A result carries structured diagnostics
  (severity / code / message) so the UI can show a short reason and hide the
  technical detail behind an expander (plan §A3 §6).
- **Denial is explicit.**  A permission or approval rejection is a distinct
  outcome (``DENIED``), not folded into a generic failure, so retry logic can
  tell "the tool broke" from "the user/host said no".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.edit.ai_editing.tool_facade import ToolPermission


# ---------------------------------------------------------------------- #
# Outcomes
# ---------------------------------------------------------------------- #


class ToolOutcome:
    """The four terminal outcomes a tool invocation can report."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    #: Permission or approval rejection — never retried automatically.
    DENIED = "denied"

    KNOWN: frozenset[str] = frozenset({SUCCESS, PARTIAL, FAILURE, DENIED})
    FAILURE_LIKE: frozenset[str] = frozenset({FAILURE, DENIED})

    @staticmethod
    def is_failure(outcome: str) -> bool:
        return outcome in ToolOutcome.FAILURE_LIKE

    @staticmethod
    def is_success(outcome: str) -> bool:
        return outcome == ToolOutcome.SUCCESS


# ---------------------------------------------------------------------- #
# Diagnostic
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class Diagnostic:
    """One layered diagnostic entry (plan §A3 §6 short reason + expander)."""

    severity: str = "info"  # info / warning / error
    code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": sanitize_for_storage(self.message),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Diagnostic":
        return Diagnostic(
            severity=str(d.get("severity", "info")),
            code=str(d.get("code", "")),
            message=str(d.get("message", "")),
        )


# ---------------------------------------------------------------------- #
# ToolResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolResult:
    """Structured result of one tool invocation (plan §A2 §8)."""

    tool_name: str
    outcome: str
    data: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("tool_name cannot be empty")
        outcome = str(self.outcome or "").strip().lower()
        if outcome not in ToolOutcome.KNOWN:
            outcome = ToolOutcome.FAILURE  # fail safe
        object.__setattr__(self, "outcome", outcome)

    @property
    def succeeded(self) -> bool:
        return self.outcome == ToolOutcome.SUCCESS

    @property
    def failed(self) -> bool:
        return ToolOutcome.is_failure(self.outcome)

    @property
    def is_denied(self) -> bool:
        return self.outcome == ToolOutcome.DENIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "outcome": self.outcome,
            "data": sanitize_for_storage(dict(self.data or {})),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            # AUD-14803：artifact_refs 同样过 sanitize_for_storage——模块红线
            # "审计事件绝不泄漏绝对路径/secret"对 refs 一视同仁（handle 可能
            # 是宿主文件路径形状）。
            "artifact_refs": [sanitize_for_storage(str(ref)) for ref in self.artifact_refs],
            "error_code": self.error_code,
            "error_message": sanitize_for_storage(self.error_message),
            "duration_ms": self.duration_ms,
        }

    # ---------------- factories ---------------- #

    @staticmethod
    def success(
        tool_name: str,
        *,
        data: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
        diagnostics: tuple[Diagnostic, ...] = (),
        duration_ms: float = 0.0,
    ) -> "ToolResult":
        return ToolResult(
            tool_name=tool_name,
            outcome=ToolOutcome.SUCCESS,
            data=dict(data or {}),
            artifact_refs=artifact_refs,
            diagnostics=diagnostics,
            duration_ms=duration_ms,
        )

    @staticmethod
    def partial(
        tool_name: str,
        *,
        data: dict[str, Any] | None = None,
        diagnostics: tuple[Diagnostic, ...] = (),
        error_message: str = "",
        duration_ms: float = 0.0,
    ) -> "ToolResult":
        return ToolResult(
            tool_name=tool_name,
            outcome=ToolOutcome.PARTIAL,
            data=dict(data or {}),
            diagnostics=diagnostics,
            error_message=error_message,
            duration_ms=duration_ms,
        )

    @staticmethod
    def failure(
        tool_name: str,
        *,
        error_code: str = "TOOL_FAILED",
        error_message: str = "",
        diagnostics: tuple[Diagnostic, ...] = (),
        duration_ms: float = 0.0,
    ) -> "ToolResult":
        return ToolResult(
            tool_name=tool_name,
            outcome=ToolOutcome.FAILURE,
            error_code=error_code,
            error_message=error_message,
            diagnostics=diagnostics,
            duration_ms=duration_ms,
        )

    @staticmethod
    def denied(
        tool_name: str,
        *,
        reason: str = "",
        error_code: str = "TOOL_DENIED",
    ) -> "ToolResult":
        return ToolResult(
            tool_name=tool_name,
            outcome=ToolOutcome.DENIED,
            error_code=error_code,
            error_message=reason or "工具调用未被授权",
        )


# ---------------------------------------------------------------------- #
# Permission -> approval derivation (plan §A2 §5-6)
# ---------------------------------------------------------------------- #


#: ToolPermission tiers that require an explicit host approval grant before
#: execution (plan §A2 permission matrix).  Read / preview tiers never do.
#: Stored as raw string values so the check works whether the caller passes a
#: ``ToolPermission`` member or a plain string.
_APPROVAL_REQUIRED_PERMISSIONS: frozenset[str] = frozenset(
    {
        ToolPermission.PROPOSAL.value,
        ToolPermission.WRITE.value,
        ToolPermission.EXPENSIVE.value,
        ToolPermission.FILE_WRITE.value,
    }
)


def _permission_value(permission: object) -> str:
    """Normalise a ToolPermission member or raw string to its string value."""

    if isinstance(permission, ToolPermission):
        return permission.value
    return str(permission)


def permission_requires_approval(permission: object) -> bool:
    """Whether a tool's permission tier needs an explicit approval grant."""

    return _permission_value(permission) in _APPROVAL_REQUIRED_PERMISSIONS


def is_read_only_permission(permission: object) -> bool:
    """Whether a tool's permission tier is side-effect free."""

    return _permission_value(permission) == ToolPermission.READ.value


__all__ = [
    "Diagnostic",
    "ToolOutcome",
    "ToolResult",
    "is_read_only_permission",
    "permission_requires_approval",
]
