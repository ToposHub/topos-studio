"""AI Assistant Agent Runtime V2 feature switch (plan §A0 step 1).

This module owns the *single* gate that decides whether the unified
``AgentOrchestrator`` path is allowed to take over ordinary chat.  It is a
deliberately tiny module so the rest of the codebase imports one stable name
(``is_agent_runtime_v2_enabled``) instead of every call site re-reading the
environment.

Design rules (plan §A0 / §10.2 回滚 / B0 接线):

- **Default on (since B0).**  Ordinary chat now flows through the unified
  ``AgentOrchestrator`` path by default.  The legacy ``_ChatWorker`` remains
  as the automatic fallback when the V2 driver cannot be constructed (e.g.
  missing profile registry).  ``TOPOS_TEST_MODE`` still short-circuits to the
  synchronous legacy path so existing assertion-style tests are unaffected.
- **Explicit opt-out.**  The flag is read from a dedicated environment
  variable and an optional process-local override.  It is **never** inferred
  from ``ai_assistant``, ``agent_auto_mode`` or any product preset.
- **Fail closed.**  When V2 is enabled but an executor is not wired yet, the
  caller must surface "Agent V2 尚未启用" rather than silently falling back to
  a half-implemented path (plan §A0 退出条件).
- **Testable.**  A process-local override lets unit tests flip the flag
  without polluting ``os.environ`` across the process.
"""

from __future__ import annotations

import os
import threading

#: Environment variable that opts the process into the V2 agent runtime.
#: Mirrors the project's ``TOPOS_FEATURE_*`` convention but uses a dedicated
#: name so V2 stays decoupled from the registered capability flags.
ENV_AGENT_RUNTIME_V2: str = "TOPOS_AI_ASSISTANT_AGENT_RUNTIME_V2"

_DEFAULT: bool = True

# Process-local override (tests).  ``None`` means "defer to the environment".
_override_lock = threading.Lock()
_override: bool | None = None


def _truthy(value: object) -> bool:
    """Return whether *value* reads as an enabled flag."""

    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "enabled")


def is_agent_runtime_v2_enabled() -> bool:
    """Return whether the unified Agent Runtime V2 path may be used.

    Precedence (highest first):

    1. Process-local override set via :func:`set_agent_runtime_v2_override`
       (used by tests).
    2. ``TOPOS_AI_ASSISTANT_AGENT_RUNTIME_V2`` environment variable.
    3. The compile-time default (``True``, since B0).
    """

    with _override_lock:
        override = _override
    if override is not None:
        return override
    env_value = os.environ.get(ENV_AGENT_RUNTIME_V2)
    if env_value is not None:
        return _truthy(env_value)
    return _DEFAULT


def set_agent_runtime_v2_override(value: bool | None) -> None:
    """Set a process-local override for the V2 flag (testing only).

    Passing ``None`` clears the override so the environment/default is
    consulted again.  This never mutates ``os.environ``.
    """

    global _override
    with _override_lock:
        _override = bool(value) if value is not None else None


def reset_agent_runtime_v2_override() -> None:
    """Clear the process-local override (alias for ``set_override(None)``)."""

    set_agent_runtime_v2_override(None)


__all__ = [
    "ENV_AGENT_RUNTIME_V2",
    "is_agent_runtime_v2_enabled",
    "reset_agent_runtime_v2_override",
    "set_agent_runtime_v2_override",
]
