"""Agent Runtime V2 feature switch tests (plan §A0 step 1 / B0).

The flag defaults **on** (since B0), so ordinary chat flows through the
unified AgentOrchestrator path.  The dedicated env var and process-local
override allow opt-out without touching ``os.environ``.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing import agent_feature_flags as flags


@pytest.fixture(autouse=True)
def _reset_override():
    """Ensure no leaked override between tests (the override is process-global)."""

    flags.reset_agent_runtime_v2_override()
    yield
    flags.reset_agent_runtime_v2_override()


def test_defaults_on():
    """B0: V2 is the default chat path (AgentOrchestrator)."""

    assert flags.is_agent_runtime_v2_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on", "Enabled"])
def test_env_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv(flags.ENV_AGENT_RUNTIME_V2, value)
    assert flags.is_agent_runtime_v2_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "garbage"])
def test_env_falsy_values_disable(monkeypatch, value):
    """Falsy env values explicitly opt out of V2."""

    monkeypatch.setenv(flags.ENV_AGENT_RUNTIME_V2, value)
    assert flags.is_agent_runtime_v2_enabled() is False


def test_override_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv(flags.ENV_AGENT_RUNTIME_V2, "1")
    flags.set_agent_runtime_v2_override(False)
    assert flags.is_agent_runtime_v2_enabled() is False


def test_override_true_when_env_absent(monkeypatch):
    monkeypatch.delenv(flags.ENV_AGENT_RUNTIME_V2, raising=False)
    flags.set_agent_runtime_v2_override(True)
    assert flags.is_agent_runtime_v2_enabled() is True


def test_reset_returns_to_env(monkeypatch):
    monkeypatch.setenv(flags.ENV_AGENT_RUNTIME_V2, "1")
    flags.set_agent_runtime_v2_override(False)
    flags.reset_agent_runtime_v2_override()
    assert flags.is_agent_runtime_v2_enabled() is True


def test_env_absent_falls_back_to_default(monkeypatch):
    monkeypatch.delenv(flags.ENV_AGENT_RUNTIME_V2, raising=False)
    assert flags.is_agent_runtime_v2_enabled() is True


def test_override_does_not_mutate_environ(monkeypatch):
    monkeypatch.delenv(flags.ENV_AGENT_RUNTIME_V2, raising=False)
    flags.set_agent_runtime_v2_override(True)
    import os

    assert flags.ENV_AGENT_RUNTIME_V2 not in os.environ
