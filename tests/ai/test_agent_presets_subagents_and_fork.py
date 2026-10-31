"""Final DeepSeek Harness-inspired runtime capability tests."""

from __future__ import annotations

import pytest

from src.features.ai_harness.session_fork import SessionEventLog, SessionForkError
from src.features.ai_harness.run_trace import RunTraceStore
from src.features.ai_harness.subagent_runtime import (
    SubagentDefinition,
    SubagentError,
    SubagentPolicy,
    SubagentRegistry,
    SubagentSupervisor,
)
from src.features.edit.ai_editing.agent_presets import (
    AgentPreset,
    AgentPresetRegistry,
    create_default_agent_preset_registry,
)


def test_default_presets_are_bounded_and_replaceable() -> None:
    registry = create_default_agent_preset_registry()
    preset = registry.get("subtitle_quality")
    assert preset is not None
    assert preset.max_tool_calls > 0
    assert "topos.apply_edit_plan" not in preset.tool_allowlist
    assert registry.register(preset) is False
    assert registry.register(AgentPreset("custom", "Custom"), replace=True)


def test_subagent_enforces_lineage_depth_and_continuation_rounds() -> None:
    calls: list[tuple[str, int, bool]] = []
    events: list[tuple[str, dict]] = []

    def runner(request):
        calls.append((request.parent_run_id, request.depth, request.allow_write_tools))
        return f"round {request.round}"

    registry = SubagentRegistry()
    registry.register(SubagentDefinition("qc", "QC", runner, continuable=True))
    supervisor = SubagentSupervisor(
        registry,
        policy=SubagentPolicy(max_depth=1, max_rounds=2, allow_write_tools=False),
        event_sink=lambda run_id, kind, payload: events.append((kind, payload)),
    )
    result = supervisor.run(
        parent_run_id="parent",
        session_id="s1",
        agent_id="qc",
        prompt="检查字幕",
        parent_allows_write=True,
    )
    assert result.text == "round 1"
    assert result.lineage == ("parent",)
    assert calls == [("parent", 1, False)]
    continued = supervisor.continue_child(result.child_run_id, "继续检查")
    assert continued.round == 2
    assert [kind for kind, _ in events].count("subagent.finished") == 2
    assert all("继续检查" not in str(payload) for _, payload in events)
    with pytest.raises(SubagentError, match="maximum rounds"):
        supervisor.continue_child(result.child_run_id, "再检查")
    with pytest.raises(SubagentError, match="maximum depth"):
        supervisor.run(
            parent_run_id="child",
            session_id="s1",
            agent_id="qc",
            prompt="nested",
            parent_depth=1,
        )


def test_session_fork_replays_only_a_known_event_boundary() -> None:
    log = SessionEventLog()
    first = log.append("s1", "user.message", payload={"text": "开始"})
    log.append("s1", "assistant.reply", payload={"text": "好的"})
    fork = log.fork("s1", at_seq=first.seq, fork_session_id="s2")
    assert fork.event_count == 1
    assert [event.kind for event in log.events("s2")] == ["user.message"]
    child_event = log.append("s2", "user.message", payload={"text": "改成片尾"})
    assert child_event.seq == first.seq + 1
    with pytest.raises(SessionForkError):
        log.fork("s1", at_seq=99)


def test_run_trace_store_exposes_event_boundary_fork() -> None:
    trace = RunTraceStore()
    first = trace.append(
        session_id="s1", run_id="r1", source="assistant", kind="run.created",
    )
    trace.append(
        session_id="s1", run_id="r1", source="assistant", kind="run.completed",
    )
    fork = trace.fork_session("s1", at_seq=first.seq, fork_session_id="s2")
    assert fork.event_count == 1
    assert len(trace.events(session_id="s2")) == 1
