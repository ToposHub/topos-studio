"""Deterministic postcondition verification (plan §A6).

Separates "the model returned text" from "the task actually succeeded".  Only
a deterministic :class:`PostconditionChecker` may promote a run to SUCCEEDED;
the model never self-reports success (plan §4.2 rule 4).

Each task kind registers the postconditions its success depends on (a
non-empty reply for chat, an HTML artifact for Motion Graphic, a generated
asset for media).  The checker runs them against a :class:`VerificationContext`
built from the run's real outputs and returns a structured
:class:`VerificationResult` the orchestrator maps to SUCCEEDED /
PARTIAL_SUCCESS / FAILED.

This module is the framework + the chat-level checks.  Motion Graphic's
browser-capture and per-page visibility checks (plan §A6 step 10-11) plug in
here later without changing the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.features.edit.ai_editing.agent_models import (
    AgentTaskKind,
    ArtifactKind,
    ContextUsage,
)
from src.features.edit.ai_editing.tool_result import ToolResult


# ---------------------------------------------------------------------- #
# Outcome + result
# ---------------------------------------------------------------------- #


class VerificationOutcome:
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"

    KNOWN: frozenset[str] = frozenset({PASSED, PARTIAL, FAILED})


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of running a task's postconditions."""

    outcome: str = VerificationOutcome.PASSED
    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        outcome = str(self.outcome or "").strip().lower()
        if outcome not in VerificationOutcome.KNOWN:
            outcome = VerificationOutcome.FAILED
        object.__setattr__(self, "outcome", outcome)

    @property
    def verified(self) -> bool:
        """True only when at least one check ran and none failed.

        Distinguishes "we proved it worked" from "nothing was checked" so a
        run with no registered postcondition is not silently trusted.
        """

        return bool(self.passed) and not self.failed

    @property
    def all_passed(self) -> bool:
        return self.outcome == VerificationOutcome.PASSED and self.verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "passed": list(self.passed),
            "failed": list(self.failed),
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------- #
# Postcondition + context
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerificationContext:
    """The real outputs a postcondition checks against."""

    task_kind: str = AgentTaskKind.CHAT
    reply: str = ""
    artifact_refs: tuple[str, ...] = ()
    #: Kind of each artifact in ``artifact_refs`` (parallel when available).
    artifact_kinds: tuple[str, ...] = ()
    usage: ContextUsage | None = None
    tool_results: tuple[ToolResult, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Postcondition:
    """One named, deterministic success check."""

    name: str
    check: Callable[[VerificationContext], bool]
    description: str = ""
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("postcondition name cannot be empty")
        if not self.failure_reason:
            object.__setattr__(self, "failure_reason", f"{self.name} 未通过")


# ---------------------------------------------------------------------- #
# Checker
# ---------------------------------------------------------------------- #


class PostconditionChecker:
    """Run the registered postconditions for a task kind (plan §A6 §6)."""

    def __init__(self) -> None:
        self._registry: dict[str, list[Postcondition]] = {}

    def register(self, task_kind: str, postcondition: Postcondition) -> None:
        self._registry.setdefault(str(task_kind), []).append(postcondition)

    def register_many(
        self, task_kind: str, postconditions: tuple[Postcondition, ...]
    ) -> None:
        for pc in postconditions:
            self.register(task_kind, pc)

    def postconditions_for(self, task_kind: str) -> tuple[Postcondition, ...]:
        return tuple(self._registry.get(str(task_kind), ()))

    def check(self, context: VerificationContext) -> VerificationResult:
        conditions = self.postconditions_for(context.task_kind)
        passed: list[str] = []
        failed: list[str] = []
        reasons: list[str] = []
        for pc in conditions:
            try:
                ok = bool(pc.check(context))
            except Exception:  # pragma: no cover - a check must never crash the run
                ok = False
            if ok:
                passed.append(pc.name)
            else:
                failed.append(pc.name)
                reasons.append(pc.failure_reason)
        # AUD-14109：未注册任何 postcondition 的任务种类必须 fail-closed。
        # 唯一消费者（chat_run_driver）按 ``outcome != "failed"`` 判定成功，
        # 若此处返回 PASSED，零检查即 SUCCEEDED，直接违背模块 docstring
        # "the model never self-reports success"（plan §4.2 rule 4）。
        if not conditions:
            return VerificationResult(
                outcome=VerificationOutcome.FAILED,
                reasons=(
                    f"任务种类 {context.task_kind!r} 未注册任何 postcondition，"
                    "无法验证成功",
                ),
            )
        if not failed:
            return VerificationResult(
                outcome=VerificationOutcome.PASSED,
                passed=tuple(passed),
            )
        if passed:
            return VerificationResult(
                outcome=VerificationOutcome.PARTIAL,
                passed=tuple(passed),
                failed=tuple(failed),
                reasons=tuple(reasons),
            )
        return VerificationResult(
            outcome=VerificationOutcome.FAILED,
            failed=tuple(failed),
            reasons=tuple(reasons),
        )


# ---------------------------------------------------------------------- #
# Built-in postconditions
# ---------------------------------------------------------------------- #


def reply_non_empty() -> Postcondition:
    """The model produced a non-empty reply (chat baseline)."""

    return Postcondition(
        name="reply_non_empty",
        description="模型回复非空",
        failure_reason="模型返回了空回复",
        check=lambda ctx: bool(ctx.reply and ctx.reply.strip()),
    )


def html_artifact_present() -> Postcondition:
    """An HTML artifact was staged (Motion Graphic baseline).

    Full browser-capture / per-page visibility verification (plan §A6 step 10)
    is layered on top of this during the Motion Graphic adapter wiring; this
    check only proves the artifact exists.
    """

    def _check(ctx: VerificationContext) -> bool:
        if ArtifactKind.HTML_DOCUMENT in ctx.artifact_kinds:
            return True
        return bool(ctx.artifact_refs)

    return Postcondition(
        name="html_artifact_present",
        description="已生成 HTML artifact",
        failure_reason="未生成 HTML artifact",
        check=_check,
    )


def generation_artifact_present() -> Postcondition:
    """A media generation produced at least one asset."""

    return Postcondition(
        name="generation_artifact_present",
        description="生成产物已就绪",
        failure_reason="生成未产生任何产物",
        check=lambda ctx: bool(ctx.artifact_refs),
    )


def no_tool_failure() -> Postcondition:
    """No tool invocation ended in failure or denial."""

    def _check(ctx: VerificationContext) -> bool:
        if not ctx.tool_results:
            return True
        return all(not r.failed for r in ctx.tool_results)

    return Postcondition(
        name="no_tool_failure",
        description="所有工具调用成功",
        failure_reason="存在失败或被拒绝的工具调用",
        check=_check,
    )


def create_default_checker() -> PostconditionChecker:
    """Build a checker with the built-in postconditions per task kind."""

    checker = PostconditionChecker()
    checker.register_many(AgentTaskKind.CHAT, (reply_non_empty(),))
    # AUD-14109：agent 模式经 chat_run_driver 驱动（_MODE_TO_TASK_KIND 含
    # AGENT），同样产出用户可见回复——注册同一基线检查，避免 fail-closed
    # 改动把 agent 模式一刀切为 FAILED。
    checker.register_many(AgentTaskKind.AGENT, (reply_non_empty(),))
    checker.register_many(
        AgentTaskKind.MOTION_GRAPHIC,
        (reply_non_empty(), html_artifact_present()),
    )
    for kind in (
        AgentTaskKind.IMAGE,
        AgentTaskKind.VIDEO,
        AgentTaskKind.MUSIC,
    ):
        checker.register(kind, generation_artifact_present())
    return checker


__all__ = [
    "Postcondition",
    "PostconditionChecker",
    "VerificationContext",
    "VerificationOutcome",
    "VerificationResult",
    "create_default_checker",
    "generation_artifact_present",
    "html_artifact_present",
    "no_tool_failure",
    "reply_non_empty",
]
