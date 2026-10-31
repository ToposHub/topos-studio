"""Provider capability contracts and mode → capability routing (plan §4.1 / §A1.5).

This module makes the difference between *a mode is selectable in the UI* and
*a mode has a real, verified provider* explicit and machine-checkable.

Three concerns are kept separate, as required by plan §2.8:

- **Capability** — what the system needs to fulfill a mode (``language``,
  ``image_generation``, ``vision_understanding``, ...).  ``vision`` is split
  into understanding vs. generation so a Seedream image profile can never be
  mistaken for a VLM.
- **ProviderContract** — the wire-level shape a provider speaks (endpoint
  style, stream / usage / abort support, input/output artifact types).  A
  generic ``/v1/generations`` URL is not assumed compatible until the contract
  validates.
- **CapabilityResolution** — the immutable result of resolving a mode to a
  single profile + provider + contract + availability.  Unavailable modes
  carry a human-readable reason and fail closed (plan §2.8 路由原则).

Endpoint normalisation lives here so the transport layer is the *only* place
that repairs ``/v1``-style mistakes (plan §A0 step 9); the user's original
config is never silently overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.features.edit.ai_editing.agent_models import (
    HTML_GENERATION_PROTOCOL_VERSION,
)


# ---------------------------------------------------------------------- #
# Capability kinds (plan §2.8 / §A1.5 step 4)
# ---------------------------------------------------------------------- #


class CapabilityKind:
    """What a mode needs from a provider.

    ``vision`` is intentionally split: ``vision_understanding`` (VLM / OCR /
    image analysis) is a different capability from ``image_generation``.
    """

    LANGUAGE = "language"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"
    MUSIC_GENERATION = "music_generation"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    VISION_UNDERSTANDING = "vision_understanding"
    EMBEDDING = "embedding"
    #: Special capability satisfied by the Teaching Animation adapter, not by a
    #: single remote provider (HTML artifact + browser verify + confirm card).
    TEACHING_ANIMATION = "teaching_animation"

    KNOWN: frozenset[str] = frozenset(
        {
            LANGUAGE, IMAGE_GENERATION, VIDEO_GENERATION,
            MUSIC_GENERATION, SPEECH_TO_TEXT, TEXT_TO_SPEECH,
            VISION_UNDERSTANDING, EMBEDDING, TEACHING_ANIMATION,
        }
    )

    #: Media generation capabilities that must have a real provider and may
    #: never fall back to a deterministic stub in production (plan §2.8 §3).
    MEDIA_GENERATION: frozenset[str] = frozenset(
        {IMAGE_GENERATION, VIDEO_GENERATION, MUSIC_GENERATION}
    )


#: Mode → required capabilities routing table (plan §A1.5 step 4).
#:
#: A mode may need more than one capability (Motion Graphic needs language to
#: draft HTML *and* the teaching-animation adapter to stage/verify/confirm).
MODE_CAPABILITY_REQUIREMENTS: dict[str, frozenset[str]] = {
    "chat": frozenset({CapabilityKind.LANGUAGE}),
    "motion_graphic": frozenset(
        {CapabilityKind.LANGUAGE, CapabilityKind.TEACHING_ANIMATION}
    ),
    "image": frozenset({CapabilityKind.IMAGE_GENERATION}),
    "video": frozenset({CapabilityKind.VIDEO_GENERATION}),
    "music": frozenset({CapabilityKind.MUSIC_GENERATION}),
    "asr": frozenset({CapabilityKind.SPEECH_TO_TEXT}),
    "tts": frozenset({CapabilityKind.TEXT_TO_SPEECH}),
    "vision": frozenset({CapabilityKind.VISION_UNDERSTANDING}),
    "embedding": frozenset({CapabilityKind.EMBEDDING}),
}


def capabilities_for_mode(mode: str) -> frozenset[str]:
    """Return the capabilities a mode requires.

    Disk-backed Assistant plugins are authoritative for their own mode ids.
    Unknown / stale mode ids still resolve to the single ``language``
    capability so they never accidentally trigger a paid media generation
    (plan §2.8 §1).
    """

    candidate = str(mode or "").strip().lower()
    try:
        from src.features.edit.ai_editing.assistant_plugins import (
            get_assistant_plugin_registry,
        )

        plugin_capabilities = get_assistant_plugin_registry().capabilities_for(candidate)
        if plugin_capabilities is not None:
            return plugin_capabilities
    except (ImportError, AttributeError):
        # Keep capability resolution import-safe for minimal tooling that
        # imports this module without the Assistant plugin package.
        pass
    return MODE_CAPABILITY_REQUIREMENTS.get(
        candidate,
        frozenset({CapabilityKind.LANGUAGE}),
    )


# ---------------------------------------------------------------------- #
# Availability (plan §2.8 / §A1.5 step 5)
# ---------------------------------------------------------------------- #


class CapabilityAvailability:
    """Why a capability can or cannot run right now (plan §A1.5 step 5)."""

    AVAILABLE = "available"
    UNCONFIGURED = "unconfigured"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    NOT_INSTALLED = "not_installed"
    POLICY_DISABLED = "policy_disabled"
    CONNECTION_FAILED = "connection_failed"
    DEMO_ONLY = "demo_only"

    KNOWN: frozenset[str] = frozenset(
        {
            AVAILABLE, UNCONFIGURED, PROTOCOL_INCOMPATIBLE,
            NOT_INSTALLED, POLICY_DISABLED, CONNECTION_FAILED, DEMO_ONLY,
        }
    )

    #: States that must block execution entirely (fail closed).
    BLOCKING: frozenset[str] = frozenset(
        {
            UNCONFIGURED, PROTOCOL_INCOMPATIBLE, NOT_INSTALLED,
            POLICY_DISABLED, CONNECTION_FAILED,
        }
    )

    @staticmethod
    def is_runnable(availability: str) -> bool:
        return availability in (
            CapabilityAvailability.AVAILABLE,
            CapabilityAvailability.DEMO_ONLY,
        )

    @staticmethod
    def is_blocking(availability: str) -> bool:
        return availability in CapabilityAvailability.BLOCKING


# ---------------------------------------------------------------------- #
# Endpoint style + contract (plan §4.1 / §A0 step 9)
# ---------------------------------------------------------------------- #


class EndpointStyle:
    """The wire shape a provider speaks (plan §4.1 ProviderContract)."""

    OPENAI_CHAT = "openai_chat"                # /v1/chat/completions
    OPENAI_RESPONSES = "openai_responses"      # /v1/responses (Responses API)
    ANTHROPIC_MESSAGES = "anthropic_messages"  # /v1/messages
    HTTP_GENERATION = "http_generation"        # async /v1/generations job API
    VOLCENGINE_SEEDREAM = "volcengine_seedream"
    VOLCENGINE_TTS = "volcengine_tts"
    VOLCENGINE_ASR = "volcengine_asr"
    LOCAL_MODEL = "local_model"
    EMBEDDING = "embedding"
    OTHER = "other"

    KNOWN: frozenset[str] = frozenset(
        {
            OPENAI_CHAT, OPENAI_RESPONSES, ANTHROPIC_MESSAGES,
            HTTP_GENERATION, VOLCENGINE_SEEDREAM, VOLCENGINE_TTS,
            VOLCENGINE_ASR, LOCAL_MODEL, EMBEDDING, OTHER,
        }
    )


@dataclass(frozen=True)
class ProviderContract:
    """The declared wire contract of one provider for one capability.

    A provider is only considered usable for a capability when its contract
    matches what the capability needs (input type, output artifact type,
    stream / abort support).  This stops a chat endpoint from being used as a
    media generator or a Seedream image profile from being routed to a vision
    request (plan §2.8 §4 / §5).
    """

    endpoint_style: str = EndpointStyle.OTHER
    capability: str = CapabilityKind.LANGUAGE
    supports_stream: bool = False
    supports_json_mode: bool = False
    supports_tool_calling: bool = False
    supports_usage: bool = False
    supports_abort: bool = False
    input_artifact: str = "text"
    output_artifact: str = "text"
    contract_version: str = HTML_GENERATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        style = str(self.endpoint_style or "").strip().lower()
        if style not in EndpointStyle.KNOWN:
            style = EndpointStyle.OTHER
        cap = str(self.capability or "").strip().lower()
        if cap not in CapabilityKind.KNOWN:
            cap = CapabilityKind.LANGUAGE
        object.__setattr__(self, "endpoint_style", style)
        object.__setattr__(self, "capability", cap)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_style": self.endpoint_style,
            "capability": self.capability,
            "supports_stream": self.supports_stream,
            "supports_json_mode": self.supports_json_mode,
            "supports_tool_calling": self.supports_tool_calling,
            "supports_usage": self.supports_usage,
            "supports_abort": self.supports_abort,
            "input_artifact": self.input_artifact,
            "output_artifact": self.output_artifact,
            "contract_version": self.contract_version,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ProviderContract":
        return ProviderContract(
            endpoint_style=str(d.get("endpoint_style", EndpointStyle.OTHER)),
            capability=str(d.get("capability", CapabilityKind.LANGUAGE)),
            supports_stream=bool(d.get("supports_stream", False)),
            supports_json_mode=bool(d.get("supports_json_mode", False)),
            supports_tool_calling=bool(d.get("supports_tool_calling", False)),
            supports_usage=bool(d.get("supports_usage", False)),
            supports_abort=bool(d.get("supports_abort", False)),
            input_artifact=str(d.get("input_artifact", "text")),
            output_artifact=str(d.get("output_artifact", "text")),
            contract_version=str(
                d.get("contract_version", HTML_GENERATION_PROTOCOL_VERSION)
            ),
        )


# ---------------------------------------------------------------------- #
# CapabilityResolution (plan §4.1)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapabilityResolution:
    """The immutable answer to "can this mode run, and how?".

    Produced by the capability router before any model request.  When
    ``availability`` is blocking, ``reason`` explains the actionable cause
    (unconfigured / protocol incompatible / ...) and the caller must fail
    closed instead of substituting another modality (plan §2.8 路由原则).
    """

    mode: str = "chat"
    capability: str = CapabilityKind.LANGUAGE
    profile_id: str = ""
    provider_id: str = ""
    adapter: str = ""
    endpoint_style: str = EndpointStyle.OTHER
    contract_version: str = HTML_GENERATION_PROTOCOL_VERSION
    availability: str = CapabilityAvailability.UNCONFIGURED
    reason: str = ""
    demo: bool = False
    missing_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        availability = str(self.availability or "").strip().lower()
        if availability not in CapabilityAvailability.KNOWN:
            # Unknown availability is treated as blocking so a stale value can
            # never accidentally enable a generation path.
            availability = CapabilityAvailability.UNCONFIGURED
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "mode", str(self.mode or "chat").strip().lower())
        cap = str(self.capability or "").strip().lower()
        if cap not in CapabilityKind.KNOWN:
            cap = CapabilityKind.LANGUAGE
        object.__setattr__(self, "capability", cap)
        # A demo resolution is always marked, even if availability says runnable.
        if self.demo and availability == CapabilityAvailability.AVAILABLE:
            object.__setattr__(self, "availability", CapabilityAvailability.DEMO_ONLY)

    @property
    def is_runnable(self) -> bool:
        return CapabilityAvailability.is_runnable(self.availability) and not (
            self.demo and CapabilityAvailability.is_blocking(self.availability)
        )

    @property
    def is_blocking(self) -> bool:
        return CapabilityAvailability.is_blocking(self.availability)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "capability": self.capability,
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "adapter": self.adapter,
            "endpoint_style": self.endpoint_style,
            "contract_version": self.contract_version,
            "availability": self.availability,
            "reason": self.reason,
            "demo": self.demo,
            "missing_capabilities": list(self.missing_capabilities),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CapabilityResolution":
        return CapabilityResolution(
            mode=str(d.get("mode", "chat")),
            capability=str(d.get("capability", CapabilityKind.LANGUAGE)),
            profile_id=str(d.get("profile_id", "")),
            provider_id=str(d.get("provider_id", "")),
            adapter=str(d.get("adapter", "")),
            endpoint_style=str(d.get("endpoint_style", EndpointStyle.OTHER)),
            contract_version=str(
                d.get("contract_version", HTML_GENERATION_PROTOCOL_VERSION)
            ),
            availability=str(
                d.get("availability", CapabilityAvailability.UNCONFIGURED)
            ),
            reason=str(d.get("reason", "")),
            demo=bool(d.get("demo", False)),
            missing_capabilities=tuple(d.get("missing_capabilities", [])),
        )


# ---------------------------------------------------------------------- #
# Convenience: an unavailable resolution (fail closed)
# ---------------------------------------------------------------------- #


def unavailable_resolution(
    mode: str,
    *,
    capability: str = CapabilityKind.LANGUAGE,
    availability: str = CapabilityAvailability.UNCONFIGURED,
    reason: str = "",
) -> CapabilityResolution:
    """Build a blocking resolution with sane defaults (fail closed)."""

    return CapabilityResolution(
        mode=mode,
        capability=capability,
        availability=availability,
        reason=reason,
    )


# ---------------------------------------------------------------------- #
# Endpoint normalisation (plan §A0 step 9 / §A0 step 10)
# ---------------------------------------------------------------------- #


def normalize_endpoint(endpoint: str) -> str:
    """Return a normalised copy of *endpoint* without mutating the original.

    Applies the transport-layer ``/v1`` repair (plan §A0 step 9) so the rest
    of the stack receives one canonical form.  The caller is expected to keep
    the user's raw config intact and only use the normalised value at send
    time (plan §A0 step 10 迁移预览).
    """

    raw = (endpoint or "").strip()
    if not raw:
        return ""
    # Ensure a scheme so urlsplit does not treat the host as a path.
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return endpoint.strip()
    if not parts.scheme or not parts.netloc:
        return endpoint.strip()
    path = parts.path or "/"
    # Collapse a doubled /v1/v1 segment that appears when a base URL already
    # contained /v1 and the client appended it again.
    while "/v1/v1" in path:
        path = path.replace("/v1/v1", "/v1")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def detect_endpoint_issue(endpoint: str) -> str | None:
    """Return a human-readable issue with *endpoint*, or ``None`` if clean.

    Used to surface a *migration preview* (plan §A0 step 10) rather than
    silently rewriting the user's config.  Only obvious, safe-to-report
    problems are flagged here; HTTP errors are translated by the transport.
    """

    raw = (endpoint or "").strip()
    if not raw:
        return "未填写 endpoint"
    if "://" not in raw and not raw.startswith("http://") and not raw.startswith("https://"):
        return "endpoint 缺少 scheme（http/https）"
    if "/v1/v1" in raw:
        return "endpoint 中出现重复 /v1，将在发送时规范化"
    return None


__all__ = [
    "CapabilityKind",
    "MODE_CAPABILITY_REQUIREMENTS",
    "capabilities_for_mode",
    "CapabilityAvailability",
    "EndpointStyle",
    "ProviderContract",
    "CapabilityResolution",
    "unavailable_resolution",
    "normalize_endpoint",
    "detect_endpoint_issue",
]
