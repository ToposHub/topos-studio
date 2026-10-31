"""Tests for observable, cancellable model request recovery."""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import AgentEventKind
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.llm_client import LLMError
from src.features.edit.ai_editing.request_recovery import (
    ModelRequestRecovery,
    RequestRecoveryPolicy,
)


def _recovery(orchestrator: AgentOrchestrator) -> ModelRequestRecovery:
    return ModelRequestRecovery(
        orchestrator,
        policy=RequestRecoveryPolicy(
            max_retries=1,
            initial_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
        ),
    )


def _run(orchestrator: AgentOrchestrator) -> str:
    from src.features.edit.ai_editing.agent_models import AgentTask

    return orchestrator.start_task(
        AgentTask(session_id="s1", title="request", user_goal="request"),
        mode="chat",
    )


def test_retries_retryable_request_and_emits_event() -> None:
    orchestrator = AgentOrchestrator()
    run_id = _run(orchestrator)
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMError("temporary", code="LLM_NETWORK_ERROR", retryable=True)
        return "ok"

    assert _recovery(orchestrator).call(operation, run_id=run_id) == "ok"
    assert calls == 2
    events = [e for e in orchestrator.get_events(run_id) if e.kind == AgentEventKind.RUN_REQUEST_RETRY]
    assert len(events) == 1
    assert events[0].payload["attempt"] == 1
    assert events[0].payload["operation"] == "model_request"


def test_non_retryable_request_is_not_replayed() -> None:
    orchestrator = AgentOrchestrator()
    run_id = _run(orchestrator)
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        raise LLMError("bad request", code="LLM_INVALID_RESPONSE")

    with pytest.raises(LLMError, match="bad request"):
        _recovery(orchestrator).call(operation, run_id=run_id)
    assert calls == 1
    assert not [e for e in orchestrator.get_events(run_id) if e.kind == AgentEventKind.RUN_REQUEST_RETRY]


def test_cancel_during_backoff_stops_before_second_attempt() -> None:
    orchestrator = AgentOrchestrator()
    run_id = _run(orchestrator)
    calls = 0
    cancelled = False

    def operation() -> str:
        nonlocal calls
        calls += 1
        raise LLMError("temporary", code="LLM_NETWORK_ERROR", retryable=True)

    def cancel_check() -> bool:
        nonlocal cancelled
        cancelled = True
        return True

    policy = RequestRecoveryPolicy(
        max_retries=1,
        initial_delay_seconds=0,
        max_delay_seconds=0,
        jitter_ratio=0,
    )
    with pytest.raises(LLMError, match="已取消") as raised:
        ModelRequestRecovery(orchestrator, policy=policy).call(
            operation,
            run_id=run_id,
            cancel_check=cancel_check,
        )
    assert raised.value.code == "LLM_CANCELLED"
    assert cancelled
    assert calls == 0
