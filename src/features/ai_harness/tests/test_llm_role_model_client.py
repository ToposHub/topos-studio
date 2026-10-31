"""LLM client → RoleExecutor adapter contract tests."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.llm_role_model_client import LlmRoleModelClient


class _Usage:
    total_tokens = 42
    cost_units = 0.125


class _Client:
    """Fake LLMClient that *records* its call contract instead of asserting.

    AUD-22608: asserting inside the fake made any contract violation escape as
    ``AssertionError`` through the adapter's ``except Exception`` — the test
    then failed later on ``response.text`` with the real cause masked as
    ``error == "AssertionError"``.  Recording the calls and asserting in the
    test body surfaces the actual mismatch.
    """

    last_usage = _Usage()

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append({
            "roles": [m.get("role") for m in messages],
            "json_mode": json_mode,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return '{"approved":true}'


class _FailingClient:
    last_usage = None

    def complete(self, *_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("secret must not escape")


def test_adapter_maps_structured_call_and_public_usage() -> None:
    client = _Client()
    response = LlmRoleModelClient(client).generate(
        system="system", user="user", role="qa_reviewer", max_tokens=100,
    )
    # 调用契约在测试正文断言（AUD-22608）：消息顺序 / json_mode / max_tokens。
    assert len(client.calls) == 1, "adapter must issue exactly one complete() call"
    call = client.calls[0]
    assert call["roles"] == ["system", "user"]
    assert call["json_mode"] is True
    assert call["max_tokens"] == 100
    assert response.text == '{"approved":true}'
    assert response.tokens_used == 42
    assert response.cost_units == 0.125
    assert response.error == ""


def test_adapter_returns_redacted_error_and_rejects_invalid_timeout() -> None:
    failed = LlmRoleModelClient(_FailingClient()).generate(
        system="s", user="u", role="qa_reviewer",
    )
    assert failed.error == "RuntimeError"
    assert "secret" not in failed.error
    invalid_client = _Client()
    invalid = LlmRoleModelClient(invalid_client).generate(
        system="s", user="u", role="qa_reviewer", timeout_ms=0,
    )
    assert invalid.error == "invalid_role_timeout"
    # 超时预算非法时必须在触达底层 client 之前拒绝（补强断言）。
    assert invalid_client.calls == []
