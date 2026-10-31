"""P14 plugin lifecycle → RunTrace projection tests."""

from __future__ import annotations

from src.features.ai_harness.plugin_status import PluginEventJournal
from src.features.ai_harness.plugin_trace import PluginRunTraceAdapter
from src.features.ai_harness.run_trace import RunTraceStore


def test_plugin_events_project_to_tool_trace_and_keep_plugin_diagnostic() -> None:
    journal = PluginEventJournal()
    trace_store = RunTraceStore()
    adapter = PluginRunTraceAdapter(
        trace_store,
        session_id_for_run=lambda run_id: f"session-for-{run_id}",
    )
    adapter.attach(journal)
    try:
        journal.record(
            "topos.isolated",
            "plugin.tool_started",
            {
                "tool_name": "topos.isolated.inspect",
                "invocation_id": "inv-1",
                "run_id": "run-trace-1",
            },
        )
        journal.record(
            "topos.isolated",
            "plugin.tool_timed_out",
            {
                "tool_name": "topos.isolated.inspect",
                "invocation_id": "inv-1",
                "run_id": "run-trace-1",
                "code": "PLUGIN_TIMEOUT",
            },
        )
        projection = trace_store.project(
            session_id="session-for-run-trace-1",
            run_id="run-trace-1",
        )
        assert projection.tool_call_count == 1
        assert projection.tool_completed_count == 1
        assert projection.tool_failed_count == 1
        assert projection.pending_tool_calls == ()
        finished = trace_store.events(
            session_id="session-for-run-trace-1",
            run_id="run-trace-1",
        )[-1]
        assert finished.kind == "run.tool_finished"
        assert finished.payload["plugin_event_kind"] == "plugin.tool_timed_out"
        assert finished.payload["code"] == "PLUGIN_TIMEOUT"
    finally:
        adapter.close()


def test_plugin_trace_adapter_unsubscribes_without_affecting_journal() -> None:
    journal = PluginEventJournal()
    trace_store = RunTraceStore()
    adapter = PluginRunTraceAdapter(trace_store)
    adapter.attach(journal)
    adapter.close()
    journal.record(
        "topos.plugin",
        "plugin.tool_started",
        {"run_id": "run-after-close", "tool_name": "inspect"},
    )
    assert trace_store.events(session_id="default", run_id="run-after-close") == ()
    assert journal.recent(run_id="run-after-close", limit=1)

