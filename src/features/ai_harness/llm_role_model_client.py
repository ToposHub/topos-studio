"""把现有统一 LLM client 接到受控 RoleExecutor 的最小适配层。

该适配器只负责协议转换：system/user → ``LLMClient.complete``，并把公开 usage
映射为 ``ModelResponse``。鉴权、成本 ledger 和网络策略仍由 profile/client
负责；本模块不保存或记录 secret。

超时（AUD-22014）：``LLMClient.complete`` 协议本身不接受 per-request 超时
参数，而 RoleExecutor 完全依赖客户端执行 ``timeout_ms`` 预算（自身无墙钟
检查）。适配器因此用看门狗线程包一层：``timeout_ms`` 到期时 ``generate``
立即返回可见降级（``error="role_timeout"``，``finish_reason="timeout"``），
不再让慢端点把角色执行无限拖过 RoleBudget.timeout_ms。底层调用线程无法被
强杀，会作为 daemon 线程在后台自然结束，其结果被丢弃。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from src.features.edit.ai_editing.llm_client import LLMClient, make_client
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile, CredentialStore

from src.features.ai_harness.llm_role_executor import ModelResponse


class _RoleGenerationTimeoutError(RuntimeError):
    """适配器侧超时看门狗的内部标记（不外泄给调用方）。"""


@dataclass
class LlmRoleModelClient:
    """将 ``LLMClient`` 暴露为 ``LlmRoleExecutor.ModelClient``。"""

    client: LLMClient

    @classmethod
    def from_profile(
        cls, profile: AiProviderProfile, credentials: CredentialStore,
    ) -> "LlmRoleModelClient":
        return cls(make_client(profile, credentials))

    def generate(
        self,
        *,
        system: str,
        user: str,
        role: str,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
    ) -> ModelResponse:
        if timeout_ms is not None and timeout_ms <= 0:
            return ModelResponse(error="invalid_role_timeout")
        timeout_seconds: float | None = (
            timeout_ms / 1000.0 if timeout_ms is not None else None
        )
        try:
            text = self._complete(
                system=system, user=user,
                max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            )
        except _RoleGenerationTimeoutError:
            # AUD-22014：超时预算必须真实生效——超时是可见降级，不是假装
            # 已执行完毕。executor 侧对 error/timeout 统一走降级记账。
            return ModelResponse(error="role_timeout", finish_reason="timeout")
        except Exception as exc:  # noqa: BLE001 — executor records visible degradation
            return ModelResponse(error=f"{type(exc).__name__}")

        usage: Any = getattr(self.client, "last_usage", None)
        tokens = _as_int(getattr(usage, "total_tokens", 0))
        cost = _as_float(getattr(usage, "cost_units", 0.0))
        return ModelResponse(
            text=str(text or ""), finish_reason="stop",
            tokens_used=tokens, cost_units=cost,
        )

    def _complete(
        self, *, system: str, user: str,
        max_tokens: int | None, timeout_seconds: float | None,
    ) -> str:
        """执行 ``LLMClient.complete``；给了预算就用看门狗线程限时等待。"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if timeout_seconds is None:
            return self.client.complete(
                messages, json_mode=True, temperature=0.3, max_tokens=max_tokens,
            )
        outcome: dict[str, Any] = {}

        def _call() -> None:
            try:
                outcome["text"] = self.client.complete(
                    messages, json_mode=True, temperature=0.3,
                    max_tokens=max_tokens,
                )
            except BaseException as exc:  # noqa: BLE001 — 原样转交等待方
                outcome["error"] = exc

        worker = threading.Thread(
            target=_call, name="llm-role-client-timeout", daemon=True,
        )
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            raise _RoleGenerationTimeoutError(
                f"role model call exceeded timeout budget ({timeout_seconds:.3f}s)",
            )
        if "error" in outcome:
            error: BaseException = outcome["error"]
            raise error
        return str(outcome.get("text") or "")


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["LlmRoleModelClient"]
