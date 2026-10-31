"""Tests for claim-once Agent Inbox / pre-step rendering."""

from __future__ import annotations

from src.features.edit.ai_editing.agent_inbox import (
    AgentInbox,
    InboxItemKind,
)


def test_claim_is_atomic_and_steering_uses_latest_value() -> None:
    inbox = AgentInbox()
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "继续检查字幕")
    inbox.enqueue("run-1", InboxItemKind.STEER, "优先处理片头")
    inbox.enqueue("run-1", InboxItemKind.STEER, "改为优先处理片尾")
    inbox.enqueue("run-1", InboxItemKind.INJECT, "当前时间线为 30fps")

    step = inbox.claim("run-1")
    assert len(step.item_ids) == 4
    assert step.follow_ups == ("继续检查字幕",)
    assert step.steering == "改为优先处理片尾"
    messages = step.to_messages()
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "继续检查字幕"
    assert inbox.claim("run-1").empty


def test_inbox_trace_metadata_does_not_include_raw_content() -> None:
    events: list[tuple[str, str, dict]] = []
    inbox = AgentInbox(event_sink=lambda run, kind, payload: events.append((run, kind, payload)))
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "secret/project/path")
    assert events[0][1] == "run.inbox_enqueued"
    assert "secret/project/path" not in str(events[0][2])
    assert events[0][2]["content_len"] > 0


def test_clear_run_drops_only_unclaimed_items() -> None:
    inbox = AgentInbox()
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "one")
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "two")
    inbox.claim("run-1")
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "three")
    assert inbox.clear_run("run-1") == 1
    assert not inbox.has_pending("run-1")
