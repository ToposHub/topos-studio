"""Explicit model routing with consent-preserving fallback semantics.

AUD-13923：旧版 ``ModelRouteRequest.reasoning_effort`` + ``_supports_reasoning``
读取 ``AiProviderProfile.reasoning_efforts`` —— 该字段在 profile 上不存在
（全库无定义），过滤退化为 no-op（low/medium/high 对所有 profile 一律放行，
其他值反而使所有 profile 不可用）。profile 当前不携带任何按模型的推理能力
数据，因此该过滤维度被整体移除，避免 API 表达不存在的约束；待
``AiProviderProfile`` 真正声明 reasoning 能力后再重新引入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.features.edit.ai_editing.provider_profiles import AiProviderProfile


class ModelRouteError(RuntimeError):
    """No profile can satisfy the requested model route."""


@dataclass(frozen=True)
class ModelRouteRequest:
    task: str
    modality: str = "text"
    allow_fallback: bool = False


@dataclass(frozen=True)
class ModelRoute:
    profile: AiProviderProfile
    fallback_used: bool = False
    reason: str = ""


class ModelRouter:
    """Choose profiles deterministically; fallback is never implicit."""

    def __init__(self, profiles: Iterable[AiProviderProfile]) -> None:
        self._profiles = tuple(profiles)

    def route(
        self,
        request: ModelRouteRequest,
        *,
        active_profile_id: str = "",
    ) -> ModelRoute:
        eligible = [
            profile for profile in self._profiles
            if request.modality in profile.modalities
        ]
        if active_profile_id:
            active = next((p for p in eligible if p.profile_id == active_profile_id), None)
            if active is not None:
                return ModelRoute(active)
            if not request.allow_fallback:
                raise ModelRouteError(
                    f"活动 profile {active_profile_id!r} 不支持任务 {request.task!r}"
                )
        if not request.allow_fallback:
            raise ModelRouteError(
                f"没有 profile 可执行任务 {request.task!r}；需显式允许 fallback"
            )
        if not eligible:
            raise ModelRouteError(f"没有 profile 支持 modality={request.modality!r}")
        selected = sorted(eligible, key=self._preference_key)[0]
        return ModelRoute(selected, fallback_used=True, reason="显式 fallback")

    @staticmethod
    def _preference_key(profile: AiProviderProfile) -> tuple[int, str]:
        kind = profile.provider_kind.lower()
        # DeepSeek/openai-compatible profiles are preferred for editorial text,
        # but only after the caller explicitly permits fallback.
        rank = 0 if kind in {"deepseek", "openai", "openai_compat"} else 1
        return rank, profile.profile_id


__all__ = ["ModelRoute", "ModelRouteError", "ModelRouteRequest", "ModelRouter"]
