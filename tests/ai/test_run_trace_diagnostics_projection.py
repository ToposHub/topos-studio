"""Trace projection details used by the Run Drawer diagnostics action."""

from __future__ import annotations

from src.features.ai_harness.run_trace import RunTraceStore


def test_failed_tool_projection_exposes_error_code_and_message() -> None:
    store = RunTraceStore()
    store.append(
        session_id="s", run_id="r", source="assistant", event_id="start",
        kind="run.tool_started", payload={"tool_name": "topos.apply_edit_plan", "call_id": "c1"},
    )
    store.append(
        session_id="s", run_id="r", source="assistant", event_id="finish",
        kind="run.tool_finished", payload={
            "tool_name": "topos.apply_edit_plan", "call_id": "c1",
            "outcome": "failure", "error_code": "STALE_PLAN",
            "error_message": "当前时间线已变化",
        },
    )
    projection = store.project(session_id="s", run_id="r")
    assert projection.tool_failed_count == 1
    assert projection.last_diagnostic["error_code"] == "STALE_PLAN"
    assert projection.last_diagnostic["error_message"] == "当前时间线已变化"
