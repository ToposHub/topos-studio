"""Cancellable, observable recovery for one model request.

DeepSeek Harness separates provider request recovery from tool execution.  We
follow the same boundary here: retrying a failed model request is safe to
replay, while a tool body is never retried by this helper because it may have
already changed the project or started an external job.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from src.features.edit.ai_editing.agent_models import AgentEventKind
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.llm_client import LLMError


T = TypeVar("T")
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class RequestRecoveryPolicy:
    """Bounded policy for replaying a failed model request."""

    max_retries: int = 1
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2
    retryable_codes: tuple[str, ...] = (
        "LLM_NETWORK_ERROR",
        "LLM_HTTP_ERROR",
        "LLM_RATE_LIMITED",
        "LLM_RETRY_EXHAUSTED",
        "LLM_STREAM_INTERRUPTED",
    )

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delay values cannot be negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds cannot be below initial delay")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def can_retry(self, error: LLMError) -> bool:
        """Return whether the error is safe to replay at the model boundary."""

        return bool(error.retryable or error.code in self.retryable_codes)

    def delay_for(self, retry_number: int) -> float:
        """Return bounded exponential backoff for 1-based retry number."""

        base = min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (2 ** max(0, retry_number - 1)),
        )
        if base <= 0 or self.jitter_ratio <= 0:
            return base
        return max(0.0, base * random.uniform(1.0 - self.jitter_ratio, 1.0 + self.jitter_ratio))


class ModelRequestRecovery:
    """Execute one replayable model request with durable retry events."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        *,
        policy: RequestRecoveryPolicy | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._policy = policy or RequestRecoveryPolicy()

    def call(
        self,
        operation: Callable[[], T],
        *,
        run_id: str,
        operation_name: str = "model_request",
        cancel_check: CancelCheck | None = None,
    ) -> T:
        """Call ``operation`` and retry only retryable ``LLMError`` failures."""

        attempt = 0
        while True:
            self._raise_if_cancelled(cancel_check)
            try:
                return operation()
            except LLMError as exc:
                if attempt >= self._policy.max_retries or not self._policy.can_retry(exc):
                    raise
                retry_number = attempt + 1
                delay = self._policy.delay_for(retry_number)
                self._orchestrator.emit(
                    run_id,
                    AgentEventKind.RUN_REQUEST_RETRY,
                    payload={
                        "operation": operation_name,
                        "attempt": retry_number,
                        "max_retries": self._policy.max_retries,
                        "delay_seconds": round(delay, 3),
                        "error_code": exc.code,
                        "status_code": exc.status_code,
                    },
                )
                self._wait(delay, cancel_check)
                attempt = retry_number

    @staticmethod
    def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
        if cancel_check is not None and cancel_check():
            raise LLMError("模型请求已取消", code="LLM_CANCELLED")

    def _wait(self, delay: float, cancel_check: CancelCheck | None) -> None:
        """Sleep in short slices so a user cancel is observed promptly."""

        deadline = time.monotonic() + delay
        while True:
            self._raise_if_cancelled(cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))


__all__ = ["ModelRequestRecovery", "RequestRecoveryPolicy"]
