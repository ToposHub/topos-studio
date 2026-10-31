"""Side-effect-free startup diagnostics for commercial AI editing (P0)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable
from urllib.parse import urlparse

from src.features.edit.ai_editing.capability_status import (
    CapabilityStatus,
    build_capability_matrix,
)


class CheckState(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class StartupCheck:
    check_id: str
    state: CheckState
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "state": self.state.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class StartupDiagnostics:
    checks: tuple[StartupCheck, ...]
    capabilities: tuple[CapabilityStatus, ...]

    @property
    def ok(self) -> bool:
        return not any(check.state == CheckState.FAIL for check in self.checks)

    @property
    def production_ready(self) -> bool:
        return self.ok and any(
            item.tier.value == "production"
            and item.available
            and item.capability_id != "offline_help"
            for item in self.capabilities
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "production_ready": self.production_ready,
            "checks": [check.to_dict() for check in self.checks],
            "capabilities": [item.to_dict() for item in self.capabilities],
        }

    def summary(self) -> str:
        failures = [check.message for check in self.checks if check.state == CheckState.FAIL]
        warnings = [check.message for check in self.checks if check.state == CheckState.WARN]
        if failures:
            return "启动自检失败：" + "；".join(failures)
        if warnings:
            return "启动自检通过（有提示）：" + "；".join(warnings)
        return "启动自检通过"


def _profile_check(profile: Any | None, credential_available: bool) -> StartupCheck:
    if profile is None:
        return StartupCheck("llm_profile", CheckState.WARN, "未配置活动 AI profile；文本 Agent 不可用")
    endpoint = str(getattr(profile, "endpoint", "") or "")
    model_id = str(getattr(profile, "model_id", "") or "")
    provider_kind = str(getattr(profile, "provider_kind", "") or "").lower()
    if not endpoint:
        return StartupCheck("llm_profile", CheckState.FAIL, "AI profile 未配置 endpoint")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return StartupCheck("llm_profile", CheckState.FAIL, "AI profile endpoint 不是有效 HTTP(S) 地址")
    if not model_id:
        return StartupCheck("llm_profile", CheckState.FAIL, "AI profile 未配置 model")
    if provider_kind not in {"ollama", "local_http", "openai_compat"} and not credential_available:
        return StartupCheck("llm_credentials", CheckState.FAIL, "AI profile 缺少凭据；不会静默降级到演示 planner")
    return StartupCheck("llm_profile", CheckState.PASS, f"AI profile 配置完整（{provider_kind}/{model_id}）")


def run_startup_diagnostics(
    *,
    profile: Any | None = None,
    credential_available: bool = False,
    feature_flags: Any | None = None,
    generation_providers: Iterable[Any] = (),
    analysis_provider_ids: Iterable[str] = (),
    model_pack_manager: Any | None = None,
    offline: bool = False,
    blocked_capabilities: Iterable[str] = (),
    failed_capabilities: dict[str, str] | Iterable[str] = (),
) -> StartupDiagnostics:
    """Validate local configuration without making a paid/network request."""

    checks = [_profile_check(profile, credential_available)]
    if feature_flags is not None and not bool(getattr(feature_flags, "is_enabled", lambda _: True)("ai_assistant")):
        checks.append(StartupCheck("ai_assistant_flag", CheckState.FAIL, "ai_assistant feature flag 已关闭"))
    matrix = build_capability_matrix(
        feature_flags=feature_flags,
        profile=profile,
        credential_available=credential_available,
        generation_providers=generation_providers,
        analysis_provider_ids=analysis_provider_ids,
        model_pack_manager=model_pack_manager,
        offline=offline,
        blocked_capabilities=blocked_capabilities,
        failed_capabilities=failed_capabilities,
    )
    return StartupDiagnostics(tuple(checks), matrix)


__all__ = ["CheckState", "StartupCheck", "StartupDiagnostics", "run_startup_diagnostics"]
