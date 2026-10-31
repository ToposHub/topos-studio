"""ChatRunDriver integration tests (plan §A1 §4-5).

Validates the end-to-end flow that turns an ordinary chat request into a
unified AgentRun, composing the capability router, orchestrator and context
budget with an injected fake model client (no network).

Key guarantees checked:

- A blocking capability never calls the model; the run ends FAILED with the
  router's reason.
- An empty reply ends FAILED, never SUCCEEDED (plan §4.2 rule 4).
- Real provider usage is bridged into the run; a missing usage keeps the
  estimate instead of faking a zero.
- A run cancelled at a phase boundary is never reported as a success.
- The event stream records the full CREATED → … → terminal lifecycle.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.assistant_capability_router import (
    AssistantCapabilityRouter,
)
from src.features.edit.ai_editing.chat_run_driver import (
    ChatRunDriver,
    ChatRunResult,
    classify_chat_result,
)
from src.features.edit.ai_editing.llm_client import LLMError, LLMUsage
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    ProfileRegistry,
    ProviderLimits,
)


# ---------------------------------------------------------------------- #
# fakes
# ---------------------------------------------------------------------- #


class FakeClient:
    def __init__(self, reply: str = "hello", usage=None, error=None) -> None:
        self._reply = reply
        self.last_usage = usage
        self._error = error
        self.calls: list = []

    def complete(self, messages, **kw):
        self.calls.append(("complete", messages))
        if self._error:
            raise self._error
        return self._reply

    def stream_complete(self, messages, **kw):
        self.calls.append(("stream", messages))
        if self._error:
            raise self._error
        on_delta = kw.get("on_delta")
        if on_delta and self._reply:
            on_delta(self._reply)
        return self._reply


@pytest.fixture
def language_registry() -> ProfileRegistry:
    reg = ProfileRegistry()
    reg.add(
        AiProviderProfile(
            profile_id="lang1",
            display_name="GLM",
            provider_kind="openai",
            capabilities=("language",),
            limits=ProviderLimits(max_context_tokens=8000),
        )
    )
    reg.set_active_for_capability("language", "lang1")
    return reg


@pytest.fixture
def driver_factory(language_registry):
    """Build a driver with an injectable fake client."""

    def _make(reply="hello", usage=None, error=None):
        orch = AgentOrchestrator()
        router = AssistantCapabilityRouter(language_registry)
        client = FakeClient(reply=reply, usage=usage, error=error)
        drv = ChatRunDriver(
            orch,
            router,
            client_factory=lambda profile: client,
            profile_registry=language_registry,
        )
        return drv, orch, client

    return _make


# ---------------------------------------------------------------------- #
# Success path
# ---------------------------------------------------------------------- #


class TestSuccessPath:
    def test_succeeded_with_reply_and_usage(self, driver_factory):
        drv, orch, _ = driver_factory(
            reply="hello world",
            usage=LLMUsage("r", "openai", "gpt", 100, 30, 130),
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        assert res.succeeded
        assert res.reply == "hello world"
        assert res.terminal_state == AgentRunState.SUCCEEDED
        assert res.usage is not None
        assert res.usage.actual_prompt == 100
        # real reading clears the estimated flag
        assert res.usage.window_prompt_is_estimated is False

    def test_full_event_lifecycle_recorded(self, driver_factory):
        drv, orch, _ = driver_factory(reply="ok")
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        events = [e.kind for e in orch.get_events(res.run_id)]
        assert AgentEventKind.RUN_CREATED in events
        assert AgentEventKind.RUN_CONTEXT_STARTED in events
        assert AgentEventKind.RUN_USAGE_RECORDED in events
        assert AgentEventKind.RUN_VERIFICATION_STARTED in events
        assert events[-1] == AgentEventKind.RUN_COMPLETED

    def test_usage_recorded_on_run(self, driver_factory):
        drv, orch, _ = driver_factory(
            usage=LLMUsage("r", "openai", "gpt", 50, 20, 70)
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        run = orch.get_run(res.run_id)
        assert run.context_usage is not None
        assert run.context_usage.actual_prompt == 50


# ---------------------------------------------------------------------- #
# Fail closed
# ---------------------------------------------------------------------- #


class TestFailClosed:
    @pytest.mark.parametrize("mode", ["video", "music", "image"])
    def test_media_mode_without_provider_blocked(self, driver_factory, mode):
        drv, orch, client = driver_factory(reply="should not be used")
        res = drv.run_chat(
            session_id="s1",
            user_text="make media",
            messages=[{"role": "user", "content": "x"}],
            mode=mode,
        )
        assert res.blocked
        assert res.terminal_state == AgentRunState.FAILED
        assert res.error  # actionable reason
        # the model was never called
        assert client.calls == []

    def test_blocked_run_still_recorded(self, driver_factory):
        drv, orch, _ = driver_factory()
        res = drv.run_chat(
            session_id="s1", user_text="x", messages=[{"role": "user", "content": "x"}],
            mode="video",
        )
        # Even a blocked run leaves an observable trail.
        run = orch.get_run(res.run_id)
        assert run is not None
        assert run.state == AgentRunState.FAILED


# ---------------------------------------------------------------------- #
# Empty reply + LLM error
# ---------------------------------------------------------------------- #


class TestFailurePaths:
    def test_empty_reply_is_failure(self, driver_factory):
        drv, _, _ = driver_factory(reply="   ")
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        assert res.terminal_state == AgentRunState.FAILED
        assert not res.succeeded

    def test_llm_error_recorded_with_code(self, driver_factory):
        drv, _, _ = driver_factory(
            error=LLMError("rate limited", code="RATE_LIMIT", retryable=True)
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        assert res.terminal_state == AgentRunState.FAILED
        assert res.error_code == "RATE_LIMIT"
        assert "rate limited" in res.error


# ---------------------------------------------------------------------- #
# Usage bridge (never fake a missing number)
# ---------------------------------------------------------------------- #


class TestUsageBridge:
    def test_missing_usage_keeps_estimate(self, driver_factory):
        drv, _, _ = driver_factory(reply="ok", usage=None)
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        assert res.succeeded
        assert res.usage.actual_prompt is None
        assert res.usage.window_prompt_is_estimated is True

    def test_window_limit_from_profile(self, driver_factory):
        drv, _, _ = driver_factory(reply="ok", usage=None)
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        # profile declares max_context_tokens=8000
        assert res.usage.window_limit == 8000


# ---------------------------------------------------------------------- #
# Streaming
# ---------------------------------------------------------------------- #


class TestStreaming:
    def test_stream_forwards_deltas(self, driver_factory):
        drv, _, client = driver_factory(reply="streamed reply")
        deltas: list[str] = []
        res = drv.run_chat(
            session_id="s1",
            user_text="hi",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            on_delta=deltas.append,
        )
        assert res.reply == "streamed reply"
        assert deltas == ["streamed reply"]
        assert client.calls[0][0] == "stream"


# ---------------------------------------------------------------------- #
# Cancellation
# ---------------------------------------------------------------------- #


class _CancellingOrchestrator(AgentOrchestrator):
    """Cancels the run right after planning, to exercise the cancel check."""

    def mark_plan_ready(self, run_id):
        result = super().mark_plan_ready(run_id)
        self.request_cancel(run_id)
        return result


class TestCancellation:
    def test_cancelled_at_phase_boundary(self, language_registry):
        orch = _CancellingOrchestrator()
        router = AssistantCapabilityRouter(language_registry)
        client = FakeClient(reply="should not arrive")
        drv = ChatRunDriver(
            orch,
            router,
            client_factory=lambda profile: client,
            profile_registry=language_registry,
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi", messages=[{"role": "user", "content": "hi"}]
        )
        assert res.cancelled
        assert res.terminal_state == AgentRunState.CANCELLED
        # model was never called (cancel happened before the call)
        assert client.calls == []

    def test_cancel_during_model_call_is_not_success(self, language_registry):
        """A cancel that lands *during* the HTTP call must not be recorded as
        SUCCEEDED.  The driver re-checks ``is_run_active`` after the call."""

        class _MidFlightCancelClient(FakeClient):
            """Cancels the run while the model "call" is in progress."""

            def __init__(self, orch, **kw):
                super().__init__(**kw)
                self._orch = orch

            def stream_complete(self, messages, **kw):
                # Simulate: the user hits cancel while the stream is open.
                self._orch.request_cancel_for_session("s1")
                on_delta = kw.get("on_delta")
                if on_delta:
                    on_delta("partial text the user abandoned")
                return "partial text the user abandoned"

        orch = AgentOrchestrator()
        router = AssistantCapabilityRouter(language_registry)
        client = _MidFlightCancelClient(orch, reply="partial")
        drv = ChatRunDriver(
            orch,
            router,
            client_factory=lambda profile: client,
            profile_registry=language_registry,
        )
        res = drv.run_chat(
            session_id="s1",
            user_text="hi",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        assert res.cancelled
        assert res.terminal_state == AgentRunState.CANCELLED
        assert not res.succeeded
        # The orchestrator agrees: the run is CANCELLED, not SUCCEEDED.
        assert orch.get_run(res.run_id).state == AgentRunState.CANCELLED


# ---------------------------------------------------------------------- #
# DEMO_ONLY media generation — fail closed (defense-in-depth)
# ---------------------------------------------------------------------- #


class TestDemoOnlyFailClosed:
    @pytest.mark.parametrize("mode", ["image", "video", "music"])
    def test_demo_only_media_mode_blocked(self, language_registry, mode):
        """A media mode backed only by a demo/stub provider must fail closed
        rather than fabricate a fake artifact (plan §P0 red line 3)."""

        from src.features.edit.ai_editing.provider_capabilities import (
            CapabilityAvailability,
            CapabilityResolution,
        )

        class _DemoMediaRouter(AssistantCapabilityRouter):
            """Force a DEMO_ONLY resolution for any media mode."""

            def resolve(self, mode, **kw):
                return CapabilityResolution(
                    mode=mode,
                    availability=CapabilityAvailability.DEMO_ONLY,
                    demo=True,
                    reason="only a demo provider is configured",
                )

        orch = AgentOrchestrator()
        router = _DemoMediaRouter(language_registry)
        client = FakeClient(reply="fake image data")
        drv = ChatRunDriver(
            orch,
            router,
            client_factory=lambda profile: client,
            profile_registry=language_registry,
        )
        res = drv.run_chat(
            session_id="s1",
            user_text="draw something",
            messages=[{"role": "user", "content": "x"}],
            mode=mode,
        )
        assert res.blocked
        assert res.terminal_state == AgentRunState.FAILED
        assert client.calls == []  # model never called


# ---------------------------------------------------------------------- #
# classify_chat_result (signal routing for the Qt worker)
# ---------------------------------------------------------------------- #


class TestClassifyChatResult:
    def test_success_is_reply(self):
        from src.features.edit.ai_editing.chat_run_driver import classify_chat_result

        r = ChatRunResult(run_id="r", reply="hi", terminal_state=AgentRunState.SUCCEEDED)
        assert classify_chat_result(r) == "reply"

    def test_failure_is_failed(self):
        from src.features.edit.ai_editing.chat_run_driver import classify_chat_result

        r = ChatRunResult(
            run_id="r", terminal_state=AgentRunState.FAILED, error="boom"
        )
        assert classify_chat_result(r) == "failed"

    def test_cancelled_is_cancelled(self):
        from src.features.edit.ai_editing.chat_run_driver import classify_chat_result

        r = ChatRunResult(
            run_id="r", cancelled=True, terminal_state=AgentRunState.CANCELLED
        )
        assert classify_chat_result(r) == "cancelled"

    def test_blocked_capability_is_failed(self):
        # A blocking capability resolution must surface as a failure with the
        # router's reason, not an empty reply.
        from src.features.edit.ai_editing.chat_run_driver import classify_chat_result

        r = ChatRunResult(
            run_id="r", blocked=True, terminal_state=AgentRunState.FAILED, error="no provider"
        )
        assert classify_chat_result(r) == "failed"


# ---------------------------------------------------------------------- #
# B2: Tool loop integration
# ---------------------------------------------------------------------- #


class FakeToolClient(FakeClient):
    """Client that supports ``complete_with_tools`` for tool-loop testing."""

    def __init__(self, *args, script=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._script = list(script or [])
        self._idx = 0
        self.tools_calls: list[list] = []

    def complete_with_tools(self, messages, tools, **kw):
        self.tools_calls.append(tools)
        if self._idx < len(self._script):
            resp = self._script[self._idx]
            self._idx += 1
            return resp
        from src.features.edit.ai_editing.tool_calling_client import (
            ToolCallResponse,
        )
        return ToolCallResponse(text=self._reply, finish=True)


class FakeToolFacade:
    """Minimal ToolFacade for testing the tool loop integration."""

    def __init__(self):
        self.invoke_calls: list[tuple[str, dict | None]] = []

    def list_tools(self, *, max_permission=None):
        from src.features.edit.ai_editing.tool_facade import list_tool_definitions
        return list_tool_definitions()

    def invoke(self, tool_name, args=None, *, approval=None, approval_token=None):
        self.invoke_calls.append((tool_name, args))
        return {"ok": True, "outcome": "success", "data": {"mock": tool_name}}


@pytest.fixture
def tool_registry(language_registry):
    """Same as language_registry but ensures tool_call modality is present."""

    return language_registry


class TestToolLoopIntegration:
    """B2: When a ToolFacade is injected, ordinary chat can call read-only tools."""

    def test_tool_loop_executes_tool_then_converges(self, tool_registry):
        from src.features.edit.ai_editing.tool_calling_client import (
            ToolCallRequest,
            ToolCallResponse,
        )

        orch = AgentOrchestrator()
        router = AssistantCapabilityRouter(tool_registry)
        facade = FakeToolFacade()
        client = FakeToolClient(
            reply="final answer",
            script=[
                ToolCallResponse(
                    text="",
                    tool_calls=(ToolCallRequest(
                        call_id="c1", tool_name="topos.get_project_snapshot",
                    ),),
                ),
                ToolCallResponse(text="found 3 assets", finish=True),
            ],
        )
        drv = ChatRunDriver(
            orch, router,
            client_factory=lambda profile: client,
            profile_registry=tool_registry,
            tool_facade=facade,
        )
        res = drv.run_chat(
            session_id="s1", user_text="how many assets?",
            messages=[{"role": "user", "content": "how many assets?"}],
        )
        assert res.succeeded
        assert "found 3 assets" in res.reply
        # The tool was invoked through the facade.
        assert len(facade.invoke_calls) == 1
        assert facade.invoke_calls[0][0] == "topos.get_project_snapshot"
        # Tool events were emitted (visible in Run Drawer via B1).
        events = [e.kind for e in orch.get_events(res.run_id)]
        assert AgentEventKind.RUN_REQUEST_HEADER in events
        assert AgentEventKind.RUN_TOOL_STARTED in events
        assert AgentEventKind.RUN_TOOL_FINISHED in events

        header = next(
            e for e in orch.get_events(res.run_id)
            if e.kind == AgentEventKind.RUN_REQUEST_HEADER
        )
        assert header.payload["provider_id"] == "openai"
        assert header.payload["request_mode"] == "tool_loop"
        assert "topos.get_project_snapshot" in header.payload["tool_names"]
        assert "how many assets?" not in str(header.payload)

    def test_no_facade_falls_back_to_normal_complete(self, tool_registry):
        """Without a ToolFacade, the driver must use the normal completion path."""

        orch = AgentOrchestrator()
        router = AssistantCapabilityRouter(tool_registry)
        client = FakeClient(reply="plain reply")
        drv = ChatRunDriver(
            orch, router,
            client_factory=lambda profile: client,
            profile_registry=tool_registry,
            tool_facade=None,  # no facade
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert res.succeeded
        assert res.reply == "plain reply"
        # Normal complete was used, not complete_with_tools.
        assert client.calls[0][0] == "complete"

        header = next(
            e for e in orch.get_events(res.run_id)
            if e.kind == AgentEventKind.RUN_REQUEST_HEADER
        )
        assert header.payload["request_mode"] == "completion"
        assert header.payload["tool_names"] == []

    def test_client_without_tools_method_falls_back(self, tool_registry):
        """If the client doesn't implement complete_with_tools, fall back."""

        orch = AgentOrchestrator()
        router = AssistantCapabilityRouter(tool_registry)
        facade = FakeToolFacade()
        client = FakeClient(reply="no tools support")  # no complete_with_tools
        drv = ChatRunDriver(
            orch, router,
            client_factory=lambda profile: client,
            profile_registry=tool_registry,
            tool_facade=facade,
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert res.succeeded
        assert res.reply == "no tools support"
        assert client.calls[0][0] == "complete"

    def test_profile_without_tool_call_modality_falls_back(self, language_registry):
        """If the profile doesn't have tool_call in modalities, no tool loop."""

        from src.features.edit.ai_editing.provider_profiles import (
            AiProviderProfile, ProfileRegistry, ProviderLimits,
        )
        reg = ProfileRegistry()
        reg.add(AiProviderProfile(
            profile_id="text-only", display_name="Text Only",
            provider_kind="openai", capabilities=("language",),
            limits=ProviderLimits(max_context_tokens=8000),
            modalities=("text",),  # explicitly no tool_call
        ))
        reg.set_active_for_capability("language", "text-only")

        orch = AgentOrchestrator()
        router = AssistantCapabilityRouter(reg)
        facade = FakeToolFacade()
        client = FakeClient(reply="text only reply")
        drv = ChatRunDriver(
            orch, router,
            client_factory=lambda profile: client,
            profile_registry=reg,
            tool_facade=facade,
        )
        res = drv.run_chat(
            session_id="s1", user_text="hi",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert res.succeeded
        assert res.reply == "text only reply"
        assert client.calls[0][0] == "complete"
