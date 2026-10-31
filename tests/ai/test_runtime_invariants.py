"""Runtime invariant installer tests."""

from __future__ import annotations

from src.features.ai_harness.runtime_invariants import RuntimeInvariantEngine
from src.features.edit.ai_editing.agent_models import AgentEventKind, AgentTask
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator


def _run(orchestrator: AgentOrchestrator) -> str:
    return orchestrator.start_task(
        AgentTask(session_id="s1", title="invariant", user_goal="invariant"),
        mode="chat",
    )


def test_invariant_engine_detects_tool_finish_without_start_and_uninstalls() -> None:
    orchestrator = AgentOrchestrator()
    engine = RuntimeInvariantEngine(orchestrator)
    uninstall = engine.install()
    run_id = _run(orchestrator)
    orchestrator.emit(
        run_id,
        AgentEventKind.RUN_TOOL_FINISHED,
        payload={"tool_name": "topos.search_assets", "call_id": "c1", "outcome": "success"},
    )
    assert engine.violations[0].code == "TOOL_FINISH_WITHOUT_START"
    uninstall()
    orchestrator.emit(
        run_id,
        AgentEventKind.RUN_TOOL_FINISHED,
        payload={"tool_name": "topos.search_assets", "call_id": "c2", "outcome": "success"},
    )
    assert len(engine.violations) == 1


def test_skipped_tool_result_is_allowed_without_start() -> None:
    orchestrator = AgentOrchestrator()
    engine = RuntimeInvariantEngine(orchestrator)
    engine.install()
    run_id = _run(orchestrator)
    orchestrator.emit(
        run_id,
        AgentEventKind.RUN_TOOL_FINISHED,
        payload={"tool_name": "topos.search_assets", "call_id": "c1", "error_code": "TOOL_NOT_STARTED"},
    )
    assert engine.violations == ()
