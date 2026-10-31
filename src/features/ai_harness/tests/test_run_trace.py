"""RunTraceStore tests: event fold, bounds, persistence and fail-closed restore."""

from __future__ import annotations

from src.features.ai_harness.invocation_models import RunEvent
from src.features.ai_harness.run_trace import RunTraceStore


def test_trace_merges_runtime_and_agent_events_in_one_projection() -> None:
    store = RunTraceStore()
    runtime_created = RunEvent(
        event_id="runtime-created",
        run_id="run-1",
        kind="run.created",
        timestamp=10.0,
        payload={"scope": {"path": "/Users/testuser/project.topos"}},
    )
    store.append_runtime_event(runtime_created, session_id="session-1")
    store.append(
        session_id="session-1",
        run_id="run-1",
        source="assistant",
        event_id="tool-started",
        kind="run.tool_started",
        timestamp=11.0,
        payload={"tool_name": "topos.search_assets", "call_id": "call-1"},
    )
    store.append(
        session_id="session-1",
        run_id="run-1",
        source="assistant",
        event_id="tool-finished",
        kind="run.tool_finished",
        timestamp=12.0,
        payload={
            "tool_name": "topos.search_assets",
            "call_id": "call-1",
            "outcome": "success",
        },
    )
    store.append(
        session_id="session-1",
        run_id="run-1",
        source="assistant",
        event_id="usage",
        kind="run.usage_recorded",
        timestamp=13.0,
        payload={"usage": {"total_tokens": 42}},
    )
    store.append(
        session_id="session-1",
        run_id="run-1",
        source="runtime",
        event_id="failed",
        kind="run.failed",
        timestamp=14.0,
        payload={"code": "MODEL_TIMEOUT", "message": "retryable"},
    )

    projection = store.project(session_id="session-1", run_id="run-1")

    assert projection.as_of_seq == 4
    assert projection.event_count == 5
    assert projection.status == "failed"
    assert projection.phase == "tool"
    assert projection.tool_call_count == 1
    assert projection.tool_completed_count == 1
    assert projection.tool_failed_count == 0
    assert projection.pending_tool_calls == ()
    assert projection.total_tokens == 42
    assert projection.started_at == 10.0
    assert projection.ended_at == 14.0
    assert projection.last_diagnostic["code"] == "MODEL_TIMEOUT"

    stored = store.events(session_id="session-1")
    assert [event.seq for event in stored] == [0, 1, 2, 3, 4]
    assert stored[0].payload["scope"]["path"] == "<absolute-path>"


def test_trace_deduplicates_and_marks_bounded_history() -> None:
    store = RunTraceStore(max_events_per_session=2)
    for index in range(3):
        event = store.append(
            session_id="s",
            run_id="r",
            source="assistant",
            event_id=f"event-{index}",
            kind="run.tool_progress",
            payload={"detail": "x" * 5000},
        )
        assert event is not None
    assert store.append(
        session_id="s",
        run_id="r",
        source="assistant",
        event_id="event-2",
        kind="run.tool_progress",
    ) is None

    projection = store.project(session_id="s", run_id="r")
    assert projection.event_count == 2
    assert projection.as_of_seq == 2
    assert projection.trace_truncated is True
    assert projection.dropped_event_count == 1
    assert len(store.events(session_id="s")[0].payload["detail"]) < 1300


def test_trace_export_restore_is_sanitized_and_skips_corrupt_rows() -> None:
    source = RunTraceStore()
    source.append(
        session_id="s",
        run_id="r",
        source="runtime",
        event_id="ok",
        kind="diagnostic",
        payload={"token": "api_key=abcdefghijklmnop", "detail": "ok"},
    )
    snapshot = source.export_snapshot()
    snapshot["sessions"]["s"]["events"].append({"not": "an event"})

    restored = RunTraceStore()
    report = restored.restore_snapshot(snapshot)

    assert report.restored_sessions == ("s",)
    assert report.restored_events == 1
    # AUD-22713: 只断言跳过条目的位置契约，不再耦合 from_dict 内部
    # 抛出的具体异常类名（实现可自由选择 TypeError/ValueError/KeyError）。
    skipped = report.skipped_entries
    assert len(skipped) == 1 and skipped[0].startswith("s:event_1:"), skipped
    restored_event = restored.events(session_id="s")[0]
    assert restored_event.payload["token"] == "api_key=<redacted-secret>"


def test_trace_restore_rejects_unknown_schema_without_mutation() -> None:
    store = RunTraceStore()
    store.append(
        session_id="s",
        run_id="r",
        source="assistant",
        event_id="existing",
        kind="run.created",
    )
    report = store.restore_snapshot({"schema_version": 999, "sessions": {}})

    assert report.ok is False
    assert report.skipped_entries == ("snapshot_schema_unsupported",)
    assert len(store.events(session_id="s")) == 1


def test_trace_restore_bad_metadata_is_atomic_for_existing_store() -> None:
    store = RunTraceStore()
    store.append(
        session_id="s",
        run_id="old-run",
        source="assistant",
        event_id="old",
        kind="run.created",
    )
    report = store.restore_snapshot({
        "schema_version": 1,
        "sessions": {
            "s": {
                "next_seq": "corrupt",
                "events": [],
            },
        },
    })

    assert report.ok is False
    assert report.skipped_entries == ("s:metadata_invalid",)
    assert store.events(session_id="s")[0].run_id == "old-run"


def test_trace_clear_keeps_session_watermark_monotonic() -> None:
    store = RunTraceStore()
    store.append(
        session_id="s", run_id="r", source="assistant", event_id="e0", kind="run.created",
    )
    store.clear("s")
    event = store.append(
        session_id="s", run_id="r", source="assistant", event_id="e1", kind="run.created",
    )

    assert event is not None
    assert event.seq == 1
