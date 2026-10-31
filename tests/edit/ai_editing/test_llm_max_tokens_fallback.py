"""The Motion Graphic HTML path asks for the model's full output budget.

Providers cap ``max_tokens`` differently (8k/16k/64k/128k...).  When a
request exceeds a provider's cap the API answers HTTP 400 naming
``max_tokens``; the client must retry once on the server default instead of
failing the whole webpage generation.
"""

from __future__ import annotations

import pytest

import src.features.edit.ai_editing.llm_client as llm_client_module
from src.features.edit.ai_editing.llm_client import (
    LLMError,
    OpenAICompatClient,
    _is_max_tokens_rejection,
)
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile


def _client() -> OpenAICompatClient:
    profile = AiProviderProfile(
        profile_id="test", display_name="Test",
        provider_kind="openai",
        endpoint="https://example.invalid/v1", model_id="test-model",
    )
    return OpenAICompatClient(profile, "key")


def _ok_response(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {},
    }


def test_max_tokens_rejection_is_recognised() -> None:
    assert _is_max_tokens_rejection(
        LLMError("HTTP 400: max_tokens is too large: 65536", status_code=400)
    )
    assert _is_max_tokens_rejection(
        LLMError("HTTP 400: max output tokens exceeds the model limit", status_code=400)
    )
    # Auth problems must never be mistaken for a budget rejection.
    assert not _is_max_tokens_rejection(
        LLMError("鉴权失败（HTTP 401）invalid max_tokens token", status_code=401)
    )
    assert not _is_max_tokens_rejection(LLMError("HTTP 429: slow down"))


def test_complete_retries_on_provider_cap_after_cap_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    bodies: list[dict] = []

    def fake_post(url: str, headers: dict, body: dict, *, policy=None) -> dict:
        bodies.append(body)
        if body.get("max_tokens") == 65536:
            raise LLMError(
                "HTTP 400: max_tokens is too large: 65536 > 8192",
                status_code=400,
            )
        return _ok_response("<html>ok</html>")

    monkeypatch.setattr(llm_client_module, "_post_json", fake_post)
    reply = client.complete(
        [{"role": "user", "content": "design"}],
        temperature=1.0,
        max_tokens=65536,
    )
    assert reply == "<html>ok</html>"
    assert len(bodies) == 2
    assert bodies[0]["max_tokens"] == 65536
    # The retry uses the provider's real cap parsed from the message rather
    # than dropping to a tiny server default that would truncate the page.
    assert bodies[1]["max_tokens"] == 8192


def test_complete_falls_back_to_half_when_cap_not_parseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    bodies: list[dict] = []

    def fake_post(url: str, headers: dict, body: dict, *, policy=None) -> dict:
        bodies.append(body)
        if (body.get("max_tokens") or 0) > 1024:
            raise LLMError("HTTP 400: max_tokens exceeds the limit", status_code=400)
        return _ok_response("<html>ok</html>")

    monkeypatch.setattr(llm_client_module, "_post_json", fake_post)
    reply = client.complete(
        [{"role": "user", "content": "design"}],
        max_tokens=65536,
    )
    assert reply == "<html>ok</html>"
    # The ladder halves until the floor: 65536 -> 32768 -> ... -> 1024.
    assert bodies[1]["max_tokens"] == 32768
    assert bodies[-1]["max_tokens"] == 1024
    assert all(
        bodies[i]["max_tokens"] > bodies[i + 1]["max_tokens"]
        for i in range(len(bodies) - 1)
    )


def test_complete_surfaces_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    def fake_post(url: str, headers: dict, body: dict, *, policy=None) -> dict:
        raise LLMError("HTTP 500: upstream exploded", status_code=500)

    monkeypatch.setattr(llm_client_module, "_post_json", fake_post)
    with pytest.raises(LLMError):
        client.complete(
            [{"role": "user", "content": "design"}],
            max_tokens=65536,
        )
