"""PostconditionChecker tests (plan §A6).

Validates that a run is only trusted when deterministic checks pass — the
model never self-reports success (plan §4.2 rule 4).

- Chat: a non-empty reply passes; an empty one fails.
- Motion Graphic: needs both a reply and an HTML artifact (browser-capture
  checks layer on later without changing the contract).
- Media generation: needs at least one produced asset.
- ``verified`` distinguishes "we proved it worked" from "nothing was checked",
  so a task kind with no registered postcondition is not silently trusted.
- A check that raises is treated as a failure, never crashing the run.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentTaskKind,
    ArtifactKind,
    ContextUsage,
)
from src.features.edit.ai_editing.postcondition_checker import (
    Postcondition,
    PostconditionChecker,
    VerificationContext,
    VerificationOutcome,
    VerificationResult,
    create_default_checker,
    no_tool_failure,
)
from src.features.edit.ai_editing.tool_result import ToolResult


# ---------------------------------------------------------------------- #
# Default checker per task kind
# ---------------------------------------------------------------------- #


class TestDefaultChecker:
    @pytest.fixture
    def checker(self):
        return create_default_checker()

    def test_chat_non_empty_reply_passes(self, checker):
        r = checker.check(VerificationContext(task_kind=AgentTaskKind.CHAT, reply="hello"))
        assert r.outcome == VerificationOutcome.PASSED
        assert r.verified

    def test_chat_empty_reply_fails(self, checker):
        r = checker.check(VerificationContext(task_kind=AgentTaskKind.CHAT, reply="   "))
        assert r.outcome == VerificationOutcome.FAILED
        assert "reply_non_empty" in r.failed
        assert r.reasons

    def test_motion_graphic_needs_html_artifact(self, checker):
        ok = checker.check(
            VerificationContext(
                task_kind=AgentTaskKind.MOTION_GRAPHIC,
                reply="done",
                artifact_refs=("a1",),
                artifact_kinds=(ArtifactKind.HTML_DOCUMENT,),
            )
        )
        assert ok.verified
        # reply ok but no artifact -> partial
        partial = checker.check(
            VerificationContext(
                task_kind=AgentTaskKind.MOTION_GRAPHIC, reply="done", artifact_refs=()
            )
        )
        assert partial.outcome == VerificationOutcome.PARTIAL
        assert "html_artifact_present" in partial.failed

    @pytest.mark.parametrize("kind", [AgentTaskKind.IMAGE, AgentTaskKind.VIDEO, AgentTaskKind.MUSIC])
    def test_media_needs_artifact(self, checker, kind):
        ok = checker.check(VerificationContext(task_kind=kind, artifact_refs=("x",)))
        assert ok.verified
        bad = checker.check(VerificationContext(task_kind=kind, artifact_refs=()))
        assert bad.outcome == VerificationOutcome.FAILED


# ---------------------------------------------------------------------- #
# verified vs unverified
# ---------------------------------------------------------------------- #


class TestVerifiedFlag:
    def test_no_postconditions_not_verified(self):
        checker = PostconditionChecker()
        r = checker.check(VerificationContext(task_kind="unknown", reply="x"))
        # AUD-14109：零检查 = 无法证明成功 → fail-closed 返回 FAILED
        # （消费者按 outcome != "failed" 判定成功，此处不能空过）。
        assert r.outcome == VerificationOutcome.FAILED
        assert r.verified is False
        assert r.all_passed is False
        assert r.reasons


# ---------------------------------------------------------------------- #
# Partial outcomes
# ---------------------------------------------------------------------- #


class TestPartialOutcomes:
    def test_partial_when_some_pass_some_fail(self):
        checker = PostconditionChecker()
        checker.register(
            "t",
            Postcondition(name="a", check=lambda ctx: True),
        )
        checker.register(
            "t",
            Postcondition(name="b", check=lambda ctx: False, failure_reason="b failed"),
        )
        r = checker.check(VerificationContext(task_kind="t"))
        assert r.outcome == VerificationOutcome.PARTIAL
        assert "a" in r.passed
        assert "b" in r.failed
        assert "b failed" in r.reasons

    def test_all_fail_is_failure_not_partial(self):
        checker = PostconditionChecker()
        checker.register("t", Postcondition(name="a", check=lambda ctx: False))
        checker.register("t", Postcondition(name="b", check=lambda ctx: False))
        r = checker.check(VerificationContext(task_kind="t"))
        assert r.outcome == VerificationOutcome.FAILED
        assert not r.passed


# ---------------------------------------------------------------------- #
# Exception safety + tool-failure check
# ---------------------------------------------------------------------- #


class TestSafety:
    def test_check_raising_treated_as_failure(self):
        def boom(_):
            raise RuntimeError("check exploded")

        checker = PostconditionChecker()
        checker.register("t", Postcondition(name="risky", check=boom))
        r = checker.check(VerificationContext(task_kind="t"))
        assert r.outcome == VerificationOutcome.FAILED
        assert "risky" in r.failed

    def test_no_tool_failure_condition(self):
        pc = no_tool_failure()
        # no tool results -> passes
        assert pc.check(VerificationContext(tool_results=()))
        # all succeed -> passes
        assert pc.check(
            VerificationContext(tool_results=(ToolResult.success("t"),))
        )
        # a failure -> fails
        assert not pc.check(
            VerificationContext(
                tool_results=(ToolResult.success("a"), ToolResult.failure("b"))
            )
        )
        # a denial -> fails
        assert not pc.check(
            VerificationContext(tool_results=(ToolResult.denied("c"),))
        )


# ---------------------------------------------------------------------- #
# Serialization
# ---------------------------------------------------------------------- #


class TestSerialization:
    def test_result_roundtrip(self):
        r = VerificationResult(
            outcome=VerificationOutcome.PARTIAL,
            passed=("a",),
            failed=("b",),
            reasons=("b failed",),
        )
        d = r.to_dict()
        assert d["outcome"] == VerificationOutcome.PARTIAL
        assert d["passed"] == ["a"]
        assert d["failed"] == ["b"]


# ---------------------------------------------------------------------- #
# Context carries usage / artifacts
# ---------------------------------------------------------------------- #


class TestVerificationContext:
    def test_context_holds_usage_and_artifacts(self):
        ctx = VerificationContext(
            task_kind=AgentTaskKind.CHAT,
            reply="hi",
            artifact_refs=("a1",),
            artifact_kinds=(ArtifactKind.TEXT,),
            usage=ContextUsage(actual_prompt=10),
        )
        assert ctx.usage.actual_prompt == 10
        assert ctx.artifact_kinds == (ArtifactKind.TEXT,)
