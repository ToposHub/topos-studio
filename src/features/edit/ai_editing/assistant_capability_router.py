"""Assistant mode → capability resolution (plan §A1.5 / §2.8).

The bottom mode selector only expresses *intent*.  Before any model request
the orchestrator asks this router whether the selected mode can actually run,
and — if so — with which profile / provider.  The router is the single place
that turns "a checkbox was ticked" into a verified, explainable
:class:`CapabilityResolution`.

Hard guarantees (plan §2.8 路由原则 / §A1.5):

- **Fail closed.**  A missing provider never falls back to another modality
  and never produces a fake success.  Media generation without a real,
  non-demo provider returns a blocking resolution with an actionable reason.
- **Demo is labelled.**  A deterministic stub is reported as ``DEMO_ONLY`` so
  the UI can mark it; production paths must still surface that no real
  provider is configured.
- **Vision is split.**  ``vision_understanding`` (VLM / OCR) is a different
  capability from ``image_generation``.  A Seedream image profile can never
  satisfy a vision-understanding request (plan §2.8 §2).
- **Capability-specific profiles.**  Language / ASR / TTS / vision / embedding
  each use their own active profile; switching the language model does not
  silently change the image or TTS model.

The router adapts the *new* :class:`CapabilityKind` vocabulary to the
*existing* ``ProfileRegistry`` (legacy ``language/vision/asr/tts/embedding``
tags) and ``ProviderRegistry`` (``GenerationModality``).  It does **not**
mutate those registries or the persisted ``infer_capabilities`` output, so
importing it changes nothing until an orchestrator calls ``resolve``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.features.edit.ai_editing.assistant_modes import (
    DEFAULT_ASSISTANT_MODE,
    get_assistant_mode_specs,
)
from src.features.edit.ai_editing.generation_models import GenerationModality
from src.features.edit.ai_editing.provider_capabilities import (
    CapabilityAvailability,
    CapabilityKind,
    CapabilityResolution,
    EndpointStyle,
    HTML_GENERATION_PROTOCOL_VERSION,
    MODE_CAPABILITY_REQUIREMENTS,
    capabilities_for_mode,
    unavailable_resolution,
)

logger = logging.getLogger(__name__)

#: Every mode id the router can resolve — the union of the registered UI
#: modes and the routing table (which also covers future modes like
#: ``asr`` / ``tts`` / ``vision`` / ``embedding`` that have no selector yet).


def _known_modes() -> frozenset[str]:
    """Return built-in routing ids plus currently discovered plugin modes."""

    return frozenset(MODE_CAPABILITY_REQUIREMENTS.keys()) | frozenset(
        spec.mode_id for spec in get_assistant_mode_specs()
    )
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    ProfileRegistry,
)


# ---------------------------------------------------------------------- #
# Static routing tables
# ---------------------------------------------------------------------- #

#: New generation capability → existing ``GenerationModality``.
_CAPABILITY_TO_MODALITY: dict[str, GenerationModality] = {
    CapabilityKind.IMAGE_GENERATION: GenerationModality.IMAGE,
    CapabilityKind.VIDEO_GENERATION: GenerationModality.VIDEO,
    CapabilityKind.MUSIC_GENERATION: GenerationModality.MUSIC,
}

#: Legacy profile capability tag → new ``CapabilityKind`` for the
#: profile-driven (non-generation) capabilities.  ``vision`` is intentionally
#: absent here: it is ambiguous (image vs. understanding) and resolved
#: separately so a Seedream profile is never mistaken for a VLM.
_LEGACY_PROFILE_CAPABILITY: dict[str, str] = {
    "language": CapabilityKind.LANGUAGE,
    "tts": CapabilityKind.TEXT_TO_SPEECH,
    "asr": CapabilityKind.SPEECH_TO_TEXT,
    "embedding": CapabilityKind.EMBEDDING,
}

#: provider_kind → endpoint style for diagnostics (best-effort).
_PROVIDER_KIND_TO_ENDPOINT: dict[str, str] = {
    "openai": EndpointStyle.OPENAI_CHAT,
    "deepseek": EndpointStyle.OPENAI_CHAT,
    "ollama": EndpointStyle.OPENAI_CHAT,
    "local_http": EndpointStyle.OPENAI_CHAT,
    "openai_compat": EndpointStyle.OPENAI_CHAT,
    "openai_responses": EndpointStyle.OPENAI_RESPONSES,
    "volcengine_responses": EndpointStyle.OPENAI_RESPONSES,
    "anthropic": EndpointStyle.ANTHROPIC_MESSAGES,
    "claude": EndpointStyle.ANTHROPIC_MESSAGES,
    "volcengine_seedream": EndpointStyle.VOLCENGINE_SEEDREAM,
    "volcengine_plan_tts": EndpointStyle.VOLCENGINE_TTS,
    "local_qwen_tts": EndpointStyle.VOLCENGINE_TTS,
    "volcengine_plan_asr": EndpointStyle.VOLCENGINE_ASR,
    "local_qwen_asr": EndpointStyle.VOLCENGINE_ASR,
}


def endpoint_style_for_profile(profile: AiProviderProfile) -> str:
    """Best-effort endpoint style for a profile (used for diagnostics)."""

    return _PROVIDER_KIND_TO_ENDPOINT.get(
        str(profile.provider_kind or "").lower(), EndpointStyle.OTHER
    )


def _is_demo_provider(provider: Any) -> bool:
    """Return whether a generation provider is a non-production demo.

    Uses ``getattr`` so providers that do not declare the flag are treated as
    real (the flag is an opt-in marker on demo stubs).
    """

    return bool(getattr(provider, "is_demo", False)) or str(
        getattr(provider, "capability_tier", "")
    ).lower() == "demo"


# ---------------------------------------------------------------------- #
# AssistantCapabilityRouter
# ---------------------------------------------------------------------- #


class AssistantCapabilityRouter:
    """Resolve a user-selected mode to a verified execution capability.

    Usage::

        router = AssistantCapabilityRouter(profile_registry,
                                           generation_registry=gen_registry,
                                           teaching_animation_available=True)
        resolution = router.resolve("video")
        if resolution.is_blocking:
            show_unavailable(resolution.reason)   # fail closed
    """

    def __init__(
        self,
        profile_registry: ProfileRegistry | None,
        *,
        generation_registry: Any | None = None,
        teaching_animation_available: bool = False,
    ) -> None:
        self._profiles = profile_registry
        self._generation = generation_registry
        self._teaching_animation_available = bool(teaching_animation_available)

    # ---------------- public API ---------------- #

    def resolve(self, mode: str) -> CapabilityResolution:
        """Resolve *mode* to a single, explainable capability.

        A mode may require several capabilities (Motion Graphic needs language
        *and* the teaching-animation adapter).  The returned resolution is
        blocking as soon as any required capability is unavailable, and lists
        every missing capability in ``missing_capabilities`` (plan §A1.5 §5).
        """

        normalized = self._normalize_mode(mode)
        required = capabilities_for_mode(normalized)
        # Deterministic order so reasons / missing_capabilities are stable
        # regardless of frozenset iteration order.
        ordered = sorted(required)
        probes_by_cap: dict[str, CapabilityResolution] = {
            cap: self._probe_capability(cap) for cap in ordered
        }
        missing = [cap for cap in ordered if probes_by_cap[cap].is_blocking]

        if missing:
            # Surface the first missing capability so the reported
            # availability / reason explains *why* the mode cannot run.
            first_missing = probes_by_cap[missing[0]]
            return CapabilityResolution(
                mode=normalized,
                capability=first_missing.capability,
                profile_id=first_missing.profile_id,
                provider_id=first_missing.provider_id,
                adapter=first_missing.adapter,
                endpoint_style=first_missing.endpoint_style,
                contract_version=first_missing.contract_version,
                availability=first_missing.availability,
                reason=self._missing_reason(normalized, missing, probes_by_cap),
                demo=any(p.demo for p in probes_by_cap.values()),
                missing_capabilities=tuple(missing),
            )

        # Every required capability is runnable: surface the primary one while
        # keeping the demo flag if any contributor is a demo.  The primary is
        # the capability whose reverse-mapped mode matches this mode (so Motion
        # Graphic reports its adapter, not the shared language capability).
        primary_cap = next(
            (cap for cap in ordered if _capability_to_mode(cap) == normalized),
            ordered[0],
        )
        primary = probes_by_cap[primary_cap]
        any_demo = any(p.demo for p in probes_by_cap.values())
        availability = primary.availability
        if any_demo and availability == CapabilityAvailability.AVAILABLE:
            availability = CapabilityAvailability.DEMO_ONLY
        return CapabilityResolution(
            mode=normalized,
            capability=primary.capability,
            profile_id=primary.profile_id,
            provider_id=primary.provider_id,
            adapter=primary.adapter,
            endpoint_style=primary.endpoint_style,
            contract_version=primary.contract_version,
            availability=availability,
            reason="",
            demo=any_demo,
        )

    def resolve_all(self) -> dict[str, CapabilityResolution]:
        """Resolve every registered mode (used to refresh the selector state)."""

        return {spec.mode_id: self.resolve(spec.mode_id) for spec in get_assistant_mode_specs()}

    def is_mode_runnable(self, mode: str) -> bool:
        return self.resolve(mode).is_runnable

    # ---------------- per-capability probes ---------------- #

    def _probe_capability(self, capability: str) -> CapabilityResolution:
        if capability == CapabilityKind.LANGUAGE:
            return self._probe_language()
        if capability in _CAPABILITY_TO_MODALITY:
            return self._probe_generation(capability)
        if capability == CapabilityKind.TEXT_TO_SPEECH:
            return self._probe_profile_capability(
                "tts", capability, "未配置语音合成（TTS）Profile"
            )
        if capability == CapabilityKind.SPEECH_TO_TEXT:
            return self._probe_profile_capability(
                "asr", capability, "未配置语音识别（ASR）Profile"
            )
        if capability == CapabilityKind.EMBEDDING:
            return self._probe_profile_capability(
                "embedding", capability, "未配置 Embedding Profile"
            )
        if capability == CapabilityKind.VISION_UNDERSTANDING:
            # Vision understanding needs a dedicated VLM profile.  Legacy
            # ``vision`` tags are ambiguous (they also cover image generation)
            # so they must not satisfy this capability (plan §2.8 §2).
            return self._probe_profile_capability(
                CapabilityKind.VISION_UNDERSTANDING,
                capability,
                "视觉理解需要 VLM / 本地视觉分析 Provider，图片生成 Profile 不能复用",
            )
        if capability == CapabilityKind.TEACHING_ANIMATION:
            return self._probe_teaching_animation()
        # Unknown capability: fail closed rather than guessing.
        return unavailable_resolution(
            "unknown",
            capability=capability,
            availability=CapabilityAvailability.NOT_INSTALLED,
            reason=f"未知能力 {capability!r}",
        )

    def _probe_language(self) -> CapabilityResolution:
        profile = self._active_profile("language")
        if profile is None:
            return unavailable_resolution(
                "chat",
                capability=CapabilityKind.LANGUAGE,
                availability=CapabilityAvailability.UNCONFIGURED,
                reason="未配置语言模型 Profile（普通对话/Motion Graphic 均依赖）",
            )
        return CapabilityResolution(
            mode="chat",
            capability=CapabilityKind.LANGUAGE,
            profile_id=profile.profile_id,
            provider_id=str(profile.provider_kind),
            endpoint_style=endpoint_style_for_profile(profile),
            availability=CapabilityAvailability.AVAILABLE,
        )

    def _probe_generation(self, capability: str) -> CapabilityResolution:
        modality = _CAPABILITY_TO_MODALITY[capability]
        if self._generation is None:
            return unavailable_resolution(
                _capability_to_mode(capability),
                capability=capability,
                availability=CapabilityAvailability.NOT_INSTALLED,
                reason="生成 Provider 注册表未装配",
            )
        provider = self._generation.preferred_for(modality)
        if provider is None:
            return unavailable_resolution(
                _capability_to_mode(capability),
                capability=capability,
                availability=CapabilityAvailability.UNCONFIGURED,
                reason=f"未配置 {modality.value} 生成 Provider",
            )
        if _is_demo_provider(provider):
            # A demo provider is reported explicitly so the UI can mark it;
            # production must still fail closed when no real provider exists.
            return CapabilityResolution(
                mode=_capability_to_mode(capability),
                capability=capability,
                provider_id=str(getattr(provider, "provider_id", "")),
                availability=CapabilityAvailability.DEMO_ONLY,
                demo=True,
                reason=f"仅有演示占位 {modality.value} Provider，未配置真实生成模型",
            )
        return CapabilityResolution(
            mode=_capability_to_mode(capability),
            capability=capability,
            provider_id=str(getattr(provider, "provider_id", "")),
            availability=CapabilityAvailability.AVAILABLE,
        )

    def _probe_profile_capability(
        self, legacy_tag: str, capability: str, missing_reason: str
    ) -> CapabilityResolution:
        profile = self._active_profile(legacy_tag)
        if profile is None:
            return unavailable_resolution(
                _capability_to_mode(capability),
                capability=capability,
                availability=CapabilityAvailability.UNCONFIGURED,
                reason=missing_reason,
            )
        return CapabilityResolution(
            mode=_capability_to_mode(capability),
            capability=capability,
            profile_id=profile.profile_id,
            provider_id=str(profile.provider_kind),
            endpoint_style=endpoint_style_for_profile(profile),
            availability=CapabilityAvailability.AVAILABLE,
        )

    def _probe_teaching_animation(self) -> CapabilityResolution:
        if not self._teaching_animation_available:
            return unavailable_resolution(
                "motion_graphic",
                capability=CapabilityKind.TEACHING_ANIMATION,
                availability=CapabilityAvailability.NOT_INSTALLED,
                reason="教学动画模块未装配",
            )
        return CapabilityResolution(
            mode="motion_graphic",
            capability=CapabilityKind.TEACHING_ANIMATION,
            adapter="TeachingAnimationAgentAdapter",
            availability=CapabilityAvailability.AVAILABLE,
            contract_version=HTML_GENERATION_PROTOCOL_VERSION,
        )

    # ---------------- helpers ---------------- #

    def _active_profile(self, capability: str) -> AiProviderProfile | None:
        if self._profiles is None:
            return None
        try:
            return self._profiles.active_profile_for_capability(capability)
        except Exception:  # noqa: BLE001 — fail-closed, but never silent (AUD-13001)
            # 注册表异常（配置损坏/迁移 bug/键类型错误）不得击穿路由，但也不能
            # 伪装成"未配置 Profile"让用户盲目重配——留 warning + 堆栈供诊断。
            logger.warning(
                "ProfileRegistry lookup failed for capability %r; routing "
                "fails closed to UNCONFIGURED",
                capability,
                exc_info=True,
            )
            return None

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        candidate = str(mode or "").strip().lower()
        if candidate in _known_modes():
            return candidate
        # Unknown / stale mode falls back to chat so it can never trigger a
        # paid generation action (plan §A1.5 §1).
        return DEFAULT_ASSISTANT_MODE

    @staticmethod
    def _missing_reason(
        mode: str,
        missing: list[str],
        probes_by_cap: dict[str, CapabilityResolution],
    ) -> str:
        reasons = [
            probes_by_cap[cap].reason for cap in missing if probes_by_cap[cap].reason
        ]
        if not reasons:
            return f"模式 {mode!r} 缺少必需能力：{', '.join(missing)}"
        return "；".join(reasons)


# ---------------------------------------------------------------------- #
# small helpers kept module-private
# ---------------------------------------------------------------------- #


def _capability_to_mode(capability: str) -> str:
    """Reverse-map a single-capability mode id for diagnostics."""

    return {
        CapabilityKind.LANGUAGE: "chat",
        CapabilityKind.IMAGE_GENERATION: "image",
        CapabilityKind.VIDEO_GENERATION: "video",
        CapabilityKind.MUSIC_GENERATION: "music",
        CapabilityKind.SPEECH_TO_TEXT: "asr",
        CapabilityKind.TEXT_TO_SPEECH: "tts",
        CapabilityKind.VISION_UNDERSTANDING: "vision",
        CapabilityKind.EMBEDDING: "embedding",
        CapabilityKind.TEACHING_ANIMATION: "motion_graphic",
    }.get(capability, "chat")


__all__ = [
    "AssistantCapabilityRouter",
    "endpoint_style_for_profile",
]
