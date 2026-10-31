"""Safety tests for tool-operation deduplication and restart recovery."""

from __future__ import annotations

from src.features.edit.ai_editing.agent_models import AgentTask
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.tool_invocation import ToolInvocationRunner
from src.features.edit.ai_editing.tool_operation_ledger import (
    LedgerDecision,
    OperationStatus,
    ToolOperationLedger,
)


class _Facade:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, name, args, *, approval=None):
        self.calls += 1
        return {"ok": True, "revision": self.calls}


def _run(orch: AgentOrchestrator, suffix: str) -> str:
    return orch.start_task(AgentTask(session_id="s", title=suffix))


def test_content_key_replays_success_across_runs() -> None:
    ledger = ToolOperationLedger()
    orch = AgentOrchestrator()
    facade = _Facade()
    runner = ToolInvocationRunner(orch, facade, operation_ledger=ledger)

    run_one = _run(orch, "first")
    run_two = _run(orch, "retry")
    first = runner.invoke(
        run_one, "topos.apply_edit_plan", {"plan": {"id": "p1"}},
        approval="grant",
    )
    second = runner.invoke(
        run_two, "topos.apply_edit_plan", {"plan": {"id": "p1"}},
        approval="grant",
    )

    assert first.succeeded and second.succeeded
    assert facade.calls == 1
    finished = [event for event in orch.get_events(run_two) if event.payload.get("deduplicated")]
    assert finished


def test_interrupted_operation_restores_as_reconciliation_required() -> None:
    original = ToolOperationLedger()
    admission = original.begin("run", "call", "topos.apply_edit_plan", {"plan": {"id": "p"}})
    assert admission.decision == LedgerDecision.EXECUTE
    restored = ToolOperationLedger()
    assert restored.import_all(original.export_all()) == 1
    record = restored.records()[0]
    assert record.status == OperationStatus.UNKNOWN
    assert restored.begin("run", "call", "topos.apply_edit_plan", {"plan": {"id": "p"}}).decision == LedgerDecision.RECONCILE


def test_tool_exception_is_not_replayed() -> None:
    ledger = ToolOperationLedger()
    orch = AgentOrchestrator()
    facade = _Facade()

    def throw(name, args, *, approval=None):
        facade.calls += 1
        raise RuntimeError("side effect status unknown")

    facade.invoke = throw
    runner = ToolInvocationRunner(orch, facade, operation_ledger=ledger)
    run_id = _run(orch, "exception")
    first = runner.invoke(run_id, "topos.apply_edit_plan", {"plan": {"id": "p"}}, approval="grant")
    second = runner.invoke(run_id, "topos.apply_edit_plan", {"plan": {"id": "p"}}, approval="grant")

    assert first.error_code == "TOOL_EXCEPTION"
    assert second.error_code == "TOOL_OPERATION_RECONCILIATION_REQUIRED"
    assert facade.calls == 1
