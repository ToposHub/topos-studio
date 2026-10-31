"""ChatRunDriver integration coverage for next-turn Inbox delivery."""

from __future__ import annotations

from src.features.edit.ai_editing.agent_inbox import AgentInbox, InboxItemKind
from src.features.edit.ai_editing.agent_presets import create_default_agent_preset_registry
from src.features.edit.ai_editing.agent_models import AgentEventKind
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.assistant_capability_router import AssistantCapabilityRouter
from src.features.edit.ai_editing.chat_run_driver import ChatRunDriver
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    ProfileRegistry,
    ProviderLimits,
)
from src.features.edit.ai_editing.request_recovery import RequestRecoveryPolicy


class _Client:
    def __init__(self, inbox: AgentInbox) -> None:
        self._inbox = inbox
        self.run_id = ""
        self.messages_seen: list[list[dict]] = []
        self.calls = 0
        self.last_usage = None

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages_seen.append(list(messages))
        if self.calls == 1:
            self._inbox.enqueue(self.run_id, InboxItemKind.FOLLOW_UP, "请继续给出剪辑建议")
            return "第一轮建议"
        return "第二轮建议"


def test_followup_arriving_during_request_is_delivered_at_next_pre_step() -> None:
    registry = ProfileRegistry()
    registry.add(AiProviderProfile(
        profile_id="lang1",
        display_name="Test",
        provider_kind="openai",
        capabilities=("language",),
        limits=ProviderLimits(max_context_tokens=8000),
    ))
    registry.set_active_for_capability("language", "lang1")
    orchestrator = AgentOrchestrator()
    inbox = AgentInbox()
    client = _Client(inbox)

    def capture_run(event) -> None:
        if event.kind == AgentEventKind.RUN_CREATED:
            client.run_id = event.run_id

    orchestrator.subscribe(capture_run)
    driver = ChatRunDriver(
        orchestrator,
        AssistantCapabilityRouter(registry),
        client_factory=lambda profile: client,
        profile_registry=registry,
        request_recovery_policy=RequestRecoveryPolicy(max_retries=0),
        inbox=inbox,
    )

    result = driver.run_chat(
        session_id="s1",
        user_text="请分析这段视频",
        messages=[{"role": "user", "content": "请分析这段视频"}],
        stream=False,
    )

    assert result.succeeded
    assert result.reply == "第二轮建议"
    assert client.calls == 2
    assert any(
        item.get("role") == "assistant" and item.get("content") == "第一轮建议"
        for item in client.messages_seen[1]
    )
    assert any(
        item.get("content") == "请继续给出剪辑建议"
        for item in client.messages_seen[1]
    )
    event_kinds = [event.kind for event in orchestrator.get_events(result.run_id)]
    assert AgentEventKind.RUN_INBOX_ENQUEUED in event_kinds
    assert AgentEventKind.RUN_PRE_STEP in event_kinds


def test_chat_driver_applies_preset_system_instruction() -> None:
    registry = ProfileRegistry()
    registry.add(AiProviderProfile(
        profile_id="lang1",
        display_name="Test",
        provider_kind="openai",
        capabilities=("language",),
        limits=ProviderLimits(max_context_tokens=8000),
    ))
    registry.set_active_for_capability("language", "lang1")
    orchestrator = AgentOrchestrator()
    seen: list[list[dict]] = []

    class Client:
        last_usage = None

        def complete(self, messages, **kwargs):
            seen.append(list(messages))
            return "preset reply"

    driver = ChatRunDriver(
        orchestrator,
        AssistantCapabilityRouter(registry),
        client_factory=lambda profile: Client(),
        profile_registry=registry,
        preset_registry=create_default_agent_preset_registry(),
        request_recovery_policy=RequestRecoveryPolicy(max_retries=0),
    )
    result = driver.run_chat(
        session_id="s1",
        user_text="检查字幕",
        messages=[{"role": "user", "content": "检查字幕"}],
        preset_id="subtitle_quality",
    )
    assert result.succeeded
    assert seen[0][0]["role"] == "system"
    assert "字幕质检助理" in seen[0][0]["content"]
