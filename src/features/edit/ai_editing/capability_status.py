"""Production capability matrix for AI editing (commercial plan P0).

The matrix is deliberately provider-neutral and side-effect free.  It is used by
startup diagnostics, the capability manifest and UI adapters so a deterministic
test provider can never be presented as a production media generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

#: 本地模型包清单（capability_id, label, pack_id）——
#: capability 矩阵与 RuntimeCapabilitySnapshot（T-P0-00）共用同一份定义。
LOCAL_MODEL_PACKS: tuple[tuple[str, str, str], ...] = (
    ("asr_local", "本地语音转录", "qwen3_asr"),
    ("alignment_local", "词级对齐", "qwen3_align"),
    ("diarization_local", "说话人分离", "nemo_sortformer"),
    ("ocr_local", "本地 OCR", "rapidocr_ocr"),
    ("visual_caption_local", "本地镜头描述与标签", "qwen3_vl_8b_awq"),
    ("visual_embedding_local", "本地画面语义检索", "siglip2_base"),
    ("tts_local", "本地 TTS", "qwen3_tts_customvoice"),
)

#: 本地模型包 pack_id 集合（RuntimeCapabilitySnapshot 用）。
_LOCAL_PACK_IDS: frozenset[str] = frozenset(p for _, _, p in LOCAL_MODEL_PACKS)


class CapabilityTier(str, Enum):
    DEMO = "demo"
    BETA = "beta"
    PRODUCTION = "production"


class CapabilityState(str, Enum):
    """The only user-facing states emitted by the capability matrix.

    The older ``disabled``/``not_configured``/``unavailable`` values are kept
    as enum members for API compatibility with extensions, but the matrix uses
    the more actionable states below.  In particular, ``DEMO`` is *not*
    available: a deterministic result must never look like a real provider.
    """

    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"
    OFFLINE = "offline"
    DEMO = "demo"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    FAILED = "failed"
    # A configured network profile must not be represented as available until
    # its explicit connection test has succeeded. This is intentionally
    # separate from OFFLINE/FAILED: no network call has been attempted yet.
    UNVERIFIED = "unverified"

    # Compatibility values for older callers.  New code should use the
    # actionable states above.
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CapabilityStatus:
    """Observable status of one user-facing AI capability."""

    capability_id: str
    label: str
    tier: CapabilityTier
    state: CapabilityState
    provider_id: str = ""
    reason: str = ""
    requires_consent: bool = False
    requires_credentials: bool = False
    source: str = ""
    next_step: str = ""
    retryable: bool = False

    @property
    def available(self) -> bool:
        return self.state == CapabilityState.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "tier": self.tier.value,
            "state": self.state.value,
            "available": self.available,
            "provider_id": self.provider_id,
            "reason": self.reason,
            "requires_consent": self.requires_consent,
            "requires_credentials": self.requires_credentials,
            "source": self.source,
            "next_step": self.next_step,
            "retryable": self.retryable,
        }


def _flag_enabled(feature_flags: Any | None, name: str, default: bool = False) -> bool:
    if feature_flags is None:
        return default
    if isinstance(feature_flags, Mapping):
        return bool(feature_flags.get(name, default))
    checker = getattr(feature_flags, "is_enabled", None)
    if callable(checker):
        return bool(checker(name))
    return bool(getattr(feature_flags, name, default))


def _provider_id(provider: Any) -> str:
    return str(getattr(provider, "provider_id", "") or "")


def _provider_is_demo(provider: Any) -> bool:
    return bool(getattr(provider, "is_demo", False)) or _provider_id(provider) in {
        "deterministic-stub",
        "deterministic",
    }


def _provider_is_available(provider: Any) -> bool:
    checker = getattr(provider, "is_available", None)
    return bool(checker()) if callable(checker) else True


def _pack_state(
    manager: Any | None,
    pack_id: str,
) -> tuple[CapabilityState, str, str, bool]:
    """Return an honest state for a local model pack.

    A missing manager means the host did not wire local model capabilities;
    that is the same user action as an uninstalled pack.  A recorded pack with
    a failed verification is a failed installation, not an available
    fallback.
    """
    if manager is None:
        return CapabilityState.NOT_INSTALLED, "未安装本地模型包", f"安装模型包 {pack_id}", False
    release_required = getattr(manager, "needs_release_manifest", None)
    if callable(release_required) and release_required(pack_id):
        return (
            CapabilityState.NOT_INSTALLED,
            "缺少受控发布清单中的真实 SHA-256，不能安全安装或使用模型包",
            "安装受控模型发布清单后重试",
            False,
        )
    try:
        status = manager.get_status(pack_id)
    except (AttributeError, KeyError, OSError, RuntimeError) as exc:
        return CapabilityState.FAILED, f"模型包状态读取失败：{type(exc).__name__}", "重试模型包检查", True
    if bool(getattr(status, "is_demo", False)):
        return CapabilityState.DEMO, "仅目录存在校验的演示模型，不可用于生产", "加载发布清单并重新安装", False
    if bool(getattr(status, "usable", False)):
        return CapabilityState.AVAILABLE, "", "", False
    if bool(getattr(status, "installed", False)):
        return CapabilityState.FAILED, "模型包完整性校验失败", "删除并重新安装模型包", True
    return CapabilityState.NOT_INSTALLED, "模型包未安装", f"安装模型包 {pack_id}", False


def _failure_reason(failed: Any, capability_id: str) -> str:
    if isinstance(failed, Mapping):
        return str(failed.get(capability_id, ""))
    return "" if capability_id not in set(failed or ()) else "provider 执行失败"


def build_capability_matrix(
    *,
    feature_flags: Any | None = None,
    profile: Any | None = None,
    credential_available: bool = False,
    generation_providers: Iterable[Any] = (),
    analysis_provider_ids: Iterable[str] = (),
    model_pack_manager: Any | None = None,
    offline: bool = False,
    blocked_capabilities: Iterable[str] = (),
    failed_capabilities: Mapping[str, str] | Iterable[str] = (),
    connection_results: Mapping[str, bool | str | None] | None = None,
) -> tuple[CapabilityStatus, ...]:
    """Build statuses from live configuration and provider registries.

    No network call is made.  A separate connection test can update the startup
    report after the local configuration gate has passed.
    """

    statuses: list[CapabilityStatus] = []
    profile_ready = bool(
        profile is not None
        and getattr(profile, "endpoint", "")
        and getattr(profile, "model_id", "")
        and (credential_available or getattr(profile, "provider_kind", "") in {"ollama", "local_http", "openai_compat"})
    )
    blocked = set(blocked_capabilities)
    profile_failed = _failure_reason(failed_capabilities, "deepseek_text_agent")
    if "deepseek_text_agent" in blocked:
        llm_state = CapabilityState.BLOCKED_BY_POLICY
        llm_reason = "当前策略禁止使用文本 Agent"
    elif profile_failed:
        llm_state = CapabilityState.FAILED
        llm_reason = profile_failed
    elif profile is None:
        llm_state = CapabilityState.NOT_CONFIGURED
        llm_reason = "未配置活动 AI profile"
    elif not profile_ready:
        llm_state = CapabilityState.BLOCKED
        llm_reason = "endpoint、model 或凭据引用不可用"
    elif not _flag_enabled(feature_flags, "ai_assistant", True):
        llm_state = CapabilityState.BLOCKED_BY_POLICY
        llm_reason = "ai_assistant feature flag 已关闭"
    elif offline and str(getattr(profile, "endpoint", "")).startswith(("http://", "https://")):
        llm_state = CapabilityState.OFFLINE
        llm_reason = "当前处于离线模式，云端 profile 暂不可用"
    elif str(getattr(profile, "endpoint", "")).startswith(("http://", "https://")):
        connection = (connection_results or {}).get(str(getattr(profile, "profile_id", "")))
        if connection is True:
            llm_state = CapabilityState.AVAILABLE
            llm_reason = "连接测试已通过"
        elif isinstance(connection, str) and connection.strip():
            llm_state = CapabilityState.FAILED
            llm_reason = f"连接测试失败：{connection.strip()}"
        elif connection is False:
            llm_state = CapabilityState.FAILED
            llm_reason = "连接测试失败"
        else:
            llm_state = CapabilityState.UNVERIFIED
            llm_reason = "配置已保存，尚未完成连接测试"
    else:
        llm_state = CapabilityState.AVAILABLE
        llm_reason = ""
    statuses.append(CapabilityStatus(
        capability_id="deepseek_text_agent",
        label="DeepSeek 文本 Agent",
        tier=CapabilityTier.BETA,
        state=llm_state,
        provider_id=str(getattr(profile, "profile_id", "") or ""),
        reason=llm_reason,
        requires_consent=True,
        requires_credentials=True,
        source="profile",
        next_step=(
            "测试连接确认模型可达" if llm_state == CapabilityState.UNVERIFIED
            else "配置 profile 和系统凭据" if llm_state != CapabilityState.AVAILABLE else ""
        ),
        retryable=llm_state in {CapabilityState.OFFLINE, CapabilityState.FAILED, CapabilityState.UNVERIFIED},
    ))

    analysis_ids = set(analysis_provider_ids)
    analysis_enabled = _flag_enabled(feature_flags, "media_analysis", True)
    analysis_failed = _failure_reason(failed_capabilities, "local_media_analysis")
    if "local_media_analysis" in blocked:
        analysis_state = CapabilityState.BLOCKED_BY_POLICY
        analysis_reason = "当前策略禁止素材分析"
    elif analysis_failed:
        analysis_state = CapabilityState.FAILED
        analysis_reason = analysis_failed
    elif not analysis_enabled:
        analysis_state = CapabilityState.BLOCKED_BY_POLICY
        analysis_reason = "media_analysis feature flag 已关闭"
    elif not analysis_ids:
        analysis_state = CapabilityState.NOT_INSTALLED
        analysis_reason = "没有注册分析 provider"
    elif offline:
        analysis_state = CapabilityState.AVAILABLE
        analysis_reason = "仅使用本地分析 provider"
    else:
        analysis_state = CapabilityState.AVAILABLE
        analysis_reason = ""
    statuses.append(CapabilityStatus(
        capability_id="local_media_analysis",
        label="本地素材分析",
        tier=CapabilityTier.BETA,
        state=analysis_state,
        provider_id=", ".join(sorted(analysis_ids)),
        reason=analysis_reason,
        source="analysis_registry",
        next_step="安装或启用本地分析 provider" if analysis_state == CapabilityState.NOT_INSTALLED else "",
        retryable=analysis_state in {CapabilityState.OFFLINE, CapabilityState.FAILED},
    ))

    # Model-backed local capabilities are exposed individually so a user can
    # repair only the missing part of a recipe (ASR, alignment, diarization or
    # OCR), rather than seeing one opaque "analysis unavailable" message.
    local_packs = LOCAL_MODEL_PACKS
    for capability_id, label, pack_id in local_packs:
        if capability_id in blocked:
            state, reason, next_step, retryable = (
                CapabilityState.BLOCKED_BY_POLICY, "当前策略禁止该能力", "检查隐私/上传策略", False
            )
        else:
            state, reason, next_step, retryable = _pack_state(model_pack_manager, pack_id)
        failure = _failure_reason(failed_capabilities, capability_id)
        if failure:
            state, reason, next_step, retryable = CapabilityState.FAILED, failure, "重试分析", True
        statuses.append(CapabilityStatus(
            capability_id=capability_id,
            label=label,
            tier=CapabilityTier.BETA,
            state=state,
            provider_id="local",
            reason=reason,
            source=pack_id,
            next_step=next_step,
            retryable=retryable,
        ))

    statuses.append(CapabilityStatus(
        capability_id="offline_help",
        label="离线帮助与 Brief",
        tier=CapabilityTier.PRODUCTION,
        state=(CapabilityState.BLOCKED_BY_POLICY if "offline_help" in blocked else CapabilityState.AVAILABLE),
        reason="当前策略禁止本地帮助" if "offline_help" in blocked else "",
        source="local_help_index",
        next_step="检查隐私策略" if "offline_help" in blocked else "",
    ))

    providers = tuple(generation_providers)
    for modality, label in (
        ("image", "图片生成"),
        ("video", "视频生成"),
        ("music", "音乐生成"),
        ("voice", "配音/TTS"),
    ):
        matching = [
            p for p in providers
            if any(getattr(item, "value", item) == modality for item in getattr(p, "supported_modalities", ()))
        ]
        active = next((p for p in matching if _provider_is_available(p)), None)
        if not _flag_enabled(feature_flags, "generation", True):
            state = CapabilityState.BLOCKED_BY_POLICY
            reason = "generation feature flag 已关闭"
        elif active is None:
            state = CapabilityState.OFFLINE if offline else CapabilityState.NOT_INSTALLED
            reason = "没有可用的生成 provider"
        elif _provider_is_demo(active):
            state = CapabilityState.DEMO
            reason = "仅测试/演示 provider；不可宣称真实生产生成"
        else:
            state = CapabilityState.AVAILABLE
            reason = ""
        statuses.append(CapabilityStatus(
            capability_id=f"generation_{modality}",
            label=label,
            tier=CapabilityTier.DEMO if active is None or _provider_is_demo(active) else CapabilityTier.BETA,
            state=state,
            provider_id=_provider_id(active) if active is not None else "",
            reason=reason,
            requires_consent=True,
            requires_credentials=active is not None and not _provider_is_demo(active),
            source=_provider_id(active) if active is not None else "generation_registry",
            next_step="配置真实 provider" if state in {CapabilityState.DEMO, CapabilityState.NOT_INSTALLED, CapabilityState.OFFLINE} else "",
            retryable=state in {CapabilityState.OFFLINE, CapabilityState.FAILED},
        ))

    for capability_id, label, flag_name in (
        ("agent_auto_mode", "受限自动模式", "agent_auto_mode"),
        ("automatic_export", "自动导出", "auto_export"),
        ("cloud_upload", "云端素材上传", "cloud_upload"),
    ):
        enabled = _flag_enabled(feature_flags, flag_name, False)
        statuses.append(CapabilityStatus(
            capability_id=capability_id,
            label=label,
            tier=CapabilityTier.BETA,
            state=CapabilityState.AVAILABLE if enabled else CapabilityState.BLOCKED_BY_POLICY,
            reason="" if enabled else "默认安全关闭，需用户显式开启",
            requires_consent=True,
            source="feature_flags",
            next_step="在设置中显式开启" if not enabled else "",
        ))
    return tuple(statuses)


__all__ = [
    "CapabilityState",
    "CapabilityStatus",
    "CapabilityTier",
    "build_capability_matrix",
]
