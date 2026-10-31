"""Focused acceptance tests for the commercial P0/P1 batch."""

from __future__ import annotations

import json
import urllib.error

import pytest

from src.features.edit.ai_editing.capability_status import CapabilityState, CapabilityTier
from src.features.edit.ai_editing.generation_provider import DeterministicGenerationProvider
from src.features.edit.ai_editing.llm_client import (
    LLMError,
    LLMRequestPolicy,
    OpenAICompatClient,
    OpenAIResponsesClient,
    _openai_chat_endpoint,
    make_client,
)
from src.features.edit.ai_editing.llm_cost import CostLedger, CostRecord
from src.features.edit.ai_editing.llm_variant_planner import LLMPlannerError, LLMVariantPlanner
from src.features.edit.ai_editing.model_router import (
    ModelRouteError,
    ModelRouteRequest,
    ModelRouter,
)
from src.features.edit.ai_editing.privacy_guard import check_media_upload, redact_mapping, redact_text
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
)
from src.features.edit.ai_editing.structured_output import (
    PLANNER_SCHEMA_VERSION,
    StructuredOutputError,
    validate_planner_output,
)
from src.features.edit.ai_editing.startup_diagnostics import CheckState, run_startup_diagnostics
from src.shared.feature_flags import FeatureFlags

pytestmark = pytest.mark.edit_core


def _profile(kind: str = "deepseek") -> AiProviderProfile:
    return AiProviderProfile(
        profile_id="deepseek-main",
        display_name="DeepSeek",
        provider_kind=kind,
        endpoint="https://api.example.test",
        model_id="deepseek-chat",
        credential_ref="topos:deepseek-main",
    )


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _StreamingResponse(_Response):
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        yield b'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        yield b"data: [DONE]\n\n"


class _ResponsesStreamingResponse(_Response):
    def __iter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"hello "}\n\n'
        yield b'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
        yield (
            b'data: {"type":"response.completed","response":'
            b'{"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'
        )
        yield b"data: [DONE]\n\n"


def test_deepseek_is_openai_compatible_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeCredentialStore()
    store.set_secret("topos:deepseek-main", "secret-not-logged")
    ledger = CostLedger()
    calls: list[float] = []

    def _urlopen(request: object, *, timeout: float) -> _Response:
        calls.append(timeout)
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        assert "thinking" not in body
        return _Response({
            "id": "req-1",
            "choices": [{"message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost_units": 0.25},
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = make_client(
        _profile(), store,
        policy=LLMRequestPolicy(timeout_seconds=7.0, max_retries=0),
        cost_ledger=ledger,
        cost_budget=1.0,
    )

    assert client.complete([{"role": "user", "content": "ping"}]) == '{"ok":true}'
    assert calls == [7.0]
    assert ledger.total_cost() == pytest.approx(0.25)
    assert ledger.records()[0].total_tokens == 15


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://api.example.test", "https://api.example.test/v1/chat/completions"),
        ("https://api.example.test/v1", "https://api.example.test/v1/chat/completions"),
        ("https://api.example.test/v4", "https://api.example.test/v4/chat/completions"),
        (
            "https://api.example.test/v4/chat/completions",
            "https://api.example.test/v4/chat/completions",
        ),
        (
            "https://api.example.test/v4/chat/completions/",
            "https://api.example.test/v4/chat/completions",
        ),
    ],
)
def test_openai_compat_accepts_base_or_complete_endpoint(endpoint: str, expected: str) -> None:
    assert _openai_chat_endpoint(endpoint) == expected


def test_openai_compat_posts_to_complete_glm_coding_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _urlopen(request: object, *, timeout: float) -> _Response:
        del timeout
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    profile = AiProviderProfile(
        profile_id="glm-coding", display_name="GLM Coding", provider_kind="openai",
        endpoint="https://open.bigmodel.cn/api/coding/paas/v4",
        model_id="glm-5.2", credential_ref="glm-key",
    )
    client = OpenAICompatClient(profile, "key", policy=LLMRequestPolicy(max_retries=0))
    assert client.complete([{"role": "user", "content": "ping"}]) == "ok"
    assert captured["url"] == "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"


def test_deepseek_v4_flash_disables_reasoning_for_content_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeCredentialStore()
    profile = AiProviderProfile(
        profile_id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        provider_kind="openai",
        endpoint="https://api.deepseek.com",
        model_id="deepseek-v4-flash",
        credential_ref="topos:deepseek-v4-flash",
    )
    captured: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: float) -> _Response:
        captured.update(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return _Response({
            "choices": [{"message": {"content": '{"ok":true}'}}],
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(
        profile,
        "key",
        policy=LLMRequestPolicy(timeout_seconds=7.0, max_retries=0),
    )

    assert client.complete(
        [{"role": "user", "content": "return JSON"}], json_mode=True,
    ) == '{"ok":true}'
    assert captured["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("model_id", ["glm-5.2", "GLM-5.2", "glm-4.6"])
def test_glm_reasoning_model_disables_thinking(
    monkeypatch: pytest.MonkeyPatch, model_id: str,
) -> None:
    """glm-5.x / 4.6 reasoning models burn the entire max_tokens quota in
    their thinking channel on structured-output tasks (HTML/JSON), leaving
    ``message.content`` empty with ``finish_reason=length``.  Thinking must
    be disabled so callers receive visible content."""
    store = FakeCredentialStore()
    profile = AiProviderProfile(
        profile_id="glm-test",
        display_name="GLM",
        provider_kind="openai",
        endpoint="https://open.bigmodel.cn/api/coding/paas/v4",
        model_id=model_id,
        credential_ref="topos:glm",
    )
    captured: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: float) -> _Response:
        captured.update(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return _Response({"choices": [{"message": {"content": "<!DOCTYPE html>"}}]})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(
        profile, "key",
        policy=LLMRequestPolicy(timeout_seconds=7.0, max_retries=0),
    )
    assert client.complete([{"role": "user", "content": "make HTML"}]) == "<!DOCTYPE html>"
    assert captured["thinking"] == {"type": "disabled"}


def test_unknown_reasoning_model_auto_retries_with_thinking_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Models not in the prefix list (e.g. a future qwen3-thinking) may still
    burn the completion budget on their thinking channel.  When the first
    response comes back with empty content and ``finish_reason="length"``,
    ``complete`` must transparently retry once with ``thinking=disabled``
    rather than returning an empty string."""
    profile = AiProviderProfile(
        profile_id="qwen3", display_name="Qwen3 Thinking", provider_kind="openai",
        endpoint="https://api.example.test", model_id="qwen3-thinking",
        credential_ref="topos:qwen3",
    )
    request_bodies: list[dict[str, object]] = []
    call = 0

    def _urlopen(request: object, *, timeout: float) -> _Response:
        nonlocal call
        call += 1
        body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        request_bodies.append(body)
        if call == 1:
            # First attempt: thinking NOT disabled (not in prefix list),
            # reasoning eats the entire budget → empty content, finish=length.
            return _Response({
                "choices": [{
                    "message": {"content": ""},
                    "finish_reason": "length",
                }],
            })
        # Retry: thinking now disabled → real content.
        return _Response({
            "choices": [{
                "message": {"content": "real answer"},
                "finish_reason": "stop",
            }],
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(
        profile, "key", policy=LLMRequestPolicy(max_retries=0),
    )
    assert client.complete([{"role": "user", "content": "hi"}]) == "real answer"
    assert call == 2
    # First request must NOT contain thinking=disabled (model not in list).
    assert "thinking" not in request_bodies[0]
    # Retry request MUST inject thinking=disabled.
    assert request_bodies[1]["thinking"] == {"type": "disabled"}


def test_reasoning_model_no_retry_when_content_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``finish_reason="length"`` but content is non-empty the retry
    must NOT fire — partial content is still usable content."""
    profile = AiProviderProfile(
        profile_id="qwen3", display_name="Qwen3", provider_kind="openai",
        endpoint="https://api.example.test", model_id="qwen3-thinking",
        credential_ref="topos:qwen3",
    )
    call = 0

    def _urlopen(_request: object, *, timeout: float) -> _Response:
        nonlocal call
        call += 1
        return _Response({
            "choices": [{
                "message": {"content": "partial but valid"},
                "finish_reason": "length",
            }],
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(
        profile, "key", policy=LLMRequestPolicy(max_retries=0),
    )
    assert client.complete([{"role": "user", "content": "hi"}]) == "partial but valid"
    assert call == 1


class _EmptyThenOkStreamingResponse:
    """SSE that yields zero content deltas with finish_reason=length, then
    (on re-stream) yields real content."""

    def __init__(self) -> None:
        self._call = 0

    def __enter__(self) -> "_EmptyThenOkStreamingResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self):
        self._call += 1
        if self._call == 1:
            # Thinking channel burns the budget; no content, finish=length.
            yield b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
            yield b"data: [DONE]\n\n"
        else:
            yield b'data: {"choices":[{"delta":{"content":"real "}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"stream"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            yield b"data: [DONE]\n\n"


def test_unknown_reasoning_model_auto_retries_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming variant: when the first stream yields no content deltas and
    ``finish_reason="length"``, retry once with thinking disabled."""
    profile = AiProviderProfile(
        profile_id="qwen3", display_name="Qwen3", provider_kind="openai",
        endpoint="https://api.example.test", model_id="qwen3-thinking",
        credential_ref="topos:qwen3",
    )
    streaming = _EmptyThenOkStreamingResponse()
    request_bodies: list[dict[str, object]] = []

    def _urlopen(request: object, *, timeout: float) -> _EmptyThenOkStreamingResponse:
        request_bodies.append(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return streaming

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    deltas: list[str] = []
    client = OpenAICompatClient(
        profile, "key", policy=LLMRequestPolicy(max_retries=0),
    )
    assert client.stream_complete(
        [{"role": "user", "content": "hi"}], on_delta=deltas.append,
    ) == "real stream"
    assert deltas == ["real ", "stream"]
    assert "thinking" not in request_bodies[0]
    assert request_bodies[1]["thinking"] == {"type": "disabled"}


def test_complete_with_tools_sends_tools_and_parses_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: ``complete_with_tools`` must send the ``tools`` array and parse
    ``tool_calls`` from the response into ``ToolCallResponse.tool_calls``."""
    captured: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: float) -> _Response:
        captured.update(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return _Response({
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "topos.get_project_snapshot",
                            "arguments": '{"detail": "summary"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(
        _profile(), "key", policy=LLMRequestPolicy(max_retries=0),
    )
    resp = client.complete_with_tools(
        [{"role": "user", "content": "how many assets?"}],
        [{"type": "function", "function": {"name": "topos.get_project_snapshot"}}],
    )
    assert resp.text == ""
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].tool_name == "topos.get_project_snapshot"
    assert resp.tool_calls[0].call_id == "call_abc"
    assert resp.tool_calls[0].arguments == {"detail": "summary"}
    assert resp.finish is False
    # The request body must include tools + tool_choice.
    assert "tools" in captured
    assert captured["tool_choice"] == "auto"


def test_complete_with_tools_converges_without_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model returns content without tool_calls, finish must be True."""

    def _urlopen(request: object, *, timeout: float) -> _Response:
        return _Response({
            "choices": [{
                "message": {"content": "all done"},
                "finish_reason": "stop",
            }],
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(
        _profile(), "key", policy=LLMRequestPolicy(max_retries=0),
    )
    resp = client.complete_with_tools(
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "t"}}],
    )
    assert resp.text == "all done"
    assert resp.tool_calls == ()
    assert resp.finish is True


def test_tool_definition_to_openai_format() -> None:
    """B2: ToolDefinition.to_openai_tool() must produce valid OpenAI tools format."""
    from src.features.edit.ai_editing.tool_facade import (
        ToolDefinition,
        ToolPermission,
    )

    td = ToolDefinition(
        name="topos.test_tool",
        permission=ToolPermission.READ,
        description="A test tool.",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        output_description="Returns test data",
    )
    openai_tool = td.to_openai_tool()
    assert openai_tool["type"] == "function"
    assert openai_tool["function"]["name"] == "topos.test_tool"
    assert "test tool" in openai_tool["function"]["description"].lower()
    assert "[read]" in openai_tool["function"]["description"]
    assert openai_tool["function"]["parameters"]["type"] == "object"


def test_volcengine_responses_client_maps_output_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: float) -> _Response:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return _Response({
            "id": "resp-test",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "ARK_TEXT_OK"}],
            }],
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        })

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    profile = AiProviderProfile(
        profile_id="volcengine-text",
        display_name="Ark Responses",
        provider_kind="volcengine_responses",
        endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
        model_id="doubao-seed-2.0-lite",
        credential_ref="topos:volcengine-text",
    )
    client = make_client(
        profile,
        FakeCredentialStore(),
        policy=LLMRequestPolicy(max_retries=0),
    )

    assert isinstance(client, OpenAIResponsesClient)
    assert client.complete([{"role": "user", "content": "ping"}]) == "ARK_TEXT_OK"
    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/plan/v3/responses"
    assert captured["body"]["store"] is False  # type: ignore[index]
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 7


def test_volcengine_responses_stream_emits_text_deltas_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _urlopen(request: object, *, timeout: float) -> _ResponsesStreamingResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return _ResponsesStreamingResponse({})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    profile = AiProviderProfile(
        profile_id="volcengine-text",
        display_name="Ark Responses",
        provider_kind="volcengine_responses",
        endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
        model_id="doubao-seed-2.0-lite",
        credential_ref="topos:volcengine-text",
    )
    client = OpenAIResponsesClient(
        profile,
        "key",
        policy=LLMRequestPolicy(max_retries=0),
    )
    deltas: list[str] = []

    assert client.stream_complete(
        [{"role": "user", "content": "ping"}], on_delta=deltas.append,
    ) == "hello world"
    assert deltas == ["hello ", "world"]
    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/plan/v3/responses"
    assert captured["body"]["stream"] is True  # type: ignore[index]
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 5


def test_network_failure_retries_only_with_bounded_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def _urlopen(_request: object, *, timeout: float) -> _Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("temporary outage")
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setattr("src.features.edit.ai_editing.llm_client.time.sleep", lambda _: None)
    client = OpenAICompatClient(
        _profile("openai"),
        "key",
        policy=LLMRequestPolicy(max_retries=1, backoff_seconds=0.0),
    )

    assert client.complete([{"role": "user", "content": "ping"}]) == "ok"
    assert attempts == 2


def test_stream_complete_emits_deltas_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    deltas: list[str] = []

    def _urlopen(_request: object, *, timeout: float) -> _StreamingResponse:
        assert timeout == 120.0
        return _StreamingResponse({})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    client = OpenAICompatClient(_profile("deepseek"), "key")
    assert client.stream_complete(
        [{"role": "user", "content": "ping"}], on_delta=deltas.append,
    ) == "hello world"
    assert deltas == ["hello ", "world"]
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 5


def test_structured_planner_output_requires_versioned_envelope() -> None:
    raw = json.dumps({
        "schema_version": PLANNER_SCHEMA_VERSION,
        "variants": [{"label": "A", "decisions": []}],
    })
    envelope = validate_planner_output(raw)
    assert envelope.schema_version == PLANNER_SCHEMA_VERSION
    assert len(envelope.variants) == 1

    with pytest.raises(StructuredOutputError, match="schema_version"):
        validate_planner_output('{"variants": []}')

    planner = LLMVariantPlanner.__new__(LLMVariantPlanner)
    with pytest.raises(LLMPlannerError, match="PLANNER_SCHEMA_VERSION_MISMATCH"):
        planner._parse_variants('{"variants": []}', ("hook",))


def test_model_router_requires_explicit_fallback() -> None:
    primary = _profile("deepseek")
    local = AiProviderProfile(
        profile_id="local", display_name="Local", provider_kind="ollama",
        endpoint="http://localhost:11434", model_id="qwen", modalities=("text",),
    )
    router = ModelRouter((local, primary))
    assert router.route(
        ModelRouteRequest(task="plan"), active_profile_id=primary.profile_id,
    ).profile.profile_id == primary.profile_id
    with pytest.raises(ModelRouteError):
        router.route(ModelRouteRequest(task="plan"), active_profile_id="missing")
    fallback = router.route(
        ModelRouteRequest(task="plan", allow_fallback=True), active_profile_id="missing",
    )
    assert fallback.fallback_used is True


def test_privacy_guard_redacts_secrets_paths_and_upload_consent() -> None:
    text = "Bearer abcdefgh secret=topsecret path=/Users/testuser/clip.mov"
    redacted = redact_text(text)
    assert "topsecret" not in redacted
    assert "/Users/testuser" not in redacted
    assert redact_mapping({"api_key": "secret", "prompt": text})["api_key"] == "[REDACTED]"

    profile = AiProviderProfile(
        profile_id="cloud", display_name="Cloud", endpoint="https://example.test",
        model_id="m", credential_ref="ref", modalities=("text", "media_upload"),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
    )
    assert not check_media_upload(
        profile=profile, asset_ids=("a1",), user_confirmed=False,
    ).allowed
    assert check_media_upload(
        profile=profile, asset_ids=("a1",), user_confirmed=True,
    ).allowed


def test_budget_is_checked_before_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = CostLedger()
    ledger.record(CostRecord(
        request_id="old", provider_kind="deepseek", model_id="m",
        request_type="completion", cost_units=1.0,
    ))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("budget gate must block before HTTP"),
    )
    client = OpenAICompatClient(
        _profile(), "key", cost_ledger=ledger, cost_budget=1.0,
    )
    with pytest.raises(LLMError, match="成本预算") as exc_info:
        client.complete([{"role": "user", "content": "ping"}])
    assert exc_info.value.code == "LLM_BUDGET_EXCEEDED"


def test_startup_diagnostics_does_not_call_network_and_marks_stub_as_demo() -> None:
    flags = FeatureFlags(ai_assistant=True, media_analysis=True, generation=True)
    report = run_startup_diagnostics(
        profile=_profile(),
        credential_available=True,
        feature_flags=flags,
        generation_providers=(DeterministicGenerationProvider(),),
        analysis_provider_ids=("scene_cut_local",),
    )

    assert report.ok
    assert report.production_ready is False
    video = next(item for item in report.capabilities if item.capability_id == "generation_video")
    assert video.state == CapabilityState.DEMO
    assert video.tier == CapabilityTier.DEMO
    assert "演示" in video.reason


def test_startup_diagnostics_blocks_configured_profile_without_secret() -> None:
    report = run_startup_diagnostics(profile=_profile(), credential_available=False)
    assert report.ok is False
    assert any(check.state == CheckState.FAIL for check in report.checks)
    assert "不会静默降级" in report.summary()
