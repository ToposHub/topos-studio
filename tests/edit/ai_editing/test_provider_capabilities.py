"""Provider capability contract + mode routing tests (plan §4.1 / §A1.5 / §A0 §9).

Validates the contract that lets the UI distinguish "a mode is selectable"
from "a mode has a real provider".  Key guarantees:

- ``vision`` is split: image generation is never the same capability as vision
  understanding (plan §2.8 §2).
- Unknown / stale modes resolve to the ``language`` capability so they can
  never accidentally trigger a paid media generation (plan §2.8 §1).
- ``CapabilityResolution`` fails closed when a provider is missing.
- Endpoint ``/v1`` repair happens only here, and the user config is never
  mutated (plan §A0 step 9/10).
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.provider_capabilities import (
    CapabilityAvailability,
    CapabilityKind,
    CapabilityResolution,
    EndpointStyle,
    ProviderContract,
    capabilities_for_mode,
    detect_endpoint_issue,
    normalize_endpoint,
    unavailable_resolution,
)


# ---------------------------------------------------------------------- #
# Mode → capability routing
# ---------------------------------------------------------------------- #


class TestModeCapabilityRouting:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("chat", {CapabilityKind.LANGUAGE}),
            ("image", {CapabilityKind.IMAGE_GENERATION}),
            ("video", {CapabilityKind.VIDEO_GENERATION}),
            ("music", {CapabilityKind.MUSIC_GENERATION}),
            ("asr", {CapabilityKind.SPEECH_TO_TEXT}),
            ("tts", {CapabilityKind.TEXT_TO_SPEECH}),
            ("vision", {CapabilityKind.VISION_UNDERSTANDING}),
            ("embedding", {CapabilityKind.EMBEDDING}),
        ],
    )
    def test_known_mode_routes(self, mode, expected):
        assert capabilities_for_mode(mode) == frozenset(expected)

    def test_motion_graphic_needs_language_and_adapter(self):
        caps = capabilities_for_mode("motion_graphic")
        assert CapabilityKind.LANGUAGE in caps
        assert CapabilityKind.TEACHING_ANIMATION in caps

    @pytest.mark.parametrize("mode", ["", "bogus", "FUTURE_MODE", None])
    def test_unknown_mode_never_triggers_media(self, mode):
        # Must fall back to language, never to a paid generation capability.
        caps = capabilities_for_mode(mode)
        assert caps == frozenset({CapabilityKind.LANGUAGE})
        assert not caps & CapabilityKind.MEDIA_GENERATION

    def test_vision_is_split_from_image_generation(self):
        # Plan §2.8 §2: the two capabilities must be distinct tokens.
        assert CapabilityKind.VISION_UNDERSTANDING != CapabilityKind.IMAGE_GENERATION
        assert capabilities_for_mode("vision") != capabilities_for_mode("image")

    def test_media_generation_capabilities_grouped(self):
        assert CapabilityKind.MEDIA_GENERATION == frozenset(
            {
                CapabilityKind.IMAGE_GENERATION,
                CapabilityKind.VIDEO_GENERATION,
                CapabilityKind.MUSIC_GENERATION,
            }
        )


# ---------------------------------------------------------------------- #
# CapabilityAvailability
# ---------------------------------------------------------------------- #


class TestCapabilityAvailability:
    @pytest.mark.parametrize(
        "avail,runnable",
        [
            (CapabilityAvailability.AVAILABLE, True),
            (CapabilityAvailability.DEMO_ONLY, True),
            (CapabilityAvailability.UNCONFIGURED, False),
            (CapabilityAvailability.PROTOCOL_INCOMPATIBLE, False),
            (CapabilityAvailability.NOT_INSTALLED, False),
            (CapabilityAvailability.POLICY_DISABLED, False),
            (CapabilityAvailability.CONNECTION_FAILED, False),
        ],
    )
    def test_runnable_classification(self, avail, runnable):
        assert CapabilityAvailability.is_runnable(avail) is runnable

    def test_blocking_states_set(self):
        blocking = CapabilityAvailability.BLOCKING
        assert CapabilityAvailability.AVAILABLE not in blocking
        assert CapabilityAvailability.UNCONFIGURED in blocking
        assert CapabilityAvailability.PROTOCOL_INCOMPATIBLE in blocking


# ---------------------------------------------------------------------- #
# ProviderContract
# ---------------------------------------------------------------------- #


class TestProviderContract:
    def test_unknown_endpoint_style_normalised(self):
        contract = ProviderContract(endpoint_style="weird")
        assert contract.endpoint_style == EndpointStyle.OTHER

    def test_unknown_capability_normalised_to_language(self):
        contract = ProviderContract(capability="nope")
        assert contract.capability == CapabilityKind.LANGUAGE

    def test_roundtrip(self):
        contract = ProviderContract(
            endpoint_style=EndpointStyle.OPENAI_CHAT,
            capability=CapabilityKind.LANGUAGE,
            supports_stream=True,
            supports_tool_calling=True,
            supports_usage=True,
            supports_abort=True,
        )
        restored = ProviderContract.from_dict(contract.to_dict())
        assert restored.endpoint_style == EndpointStyle.OPENAI_CHAT
        assert restored.supports_stream is True
        assert restored.supports_abort is True

    def test_image_generation_contract_distinct_from_chat(self):
        chat = ProviderContract(
            endpoint_style=EndpointStyle.OPENAI_CHAT,
            capability=CapabilityKind.LANGUAGE,
        )
        image = ProviderContract(
            endpoint_style=EndpointStyle.HTTP_GENERATION,
            capability=CapabilityKind.IMAGE_GENERATION,
            output_artifact="image",
        )
        assert chat.capability != image.capability
        assert chat.output_artifact != image.output_artifact


# ---------------------------------------------------------------------- #
# CapabilityResolution
# ---------------------------------------------------------------------- #


class TestCapabilityResolution:
    def test_unavailable_resolution_blocks(self):
        res = unavailable_resolution(
            "video",
            capability=CapabilityKind.VIDEO_GENERATION,
            availability=CapabilityAvailability.UNCONFIGURED,
            reason="未配置视频生成 Provider",
        )
        assert res.is_blocking
        assert not res.is_runnable
        assert res.reason

    def test_demo_resolution_marked_even_when_runnable(self):
        res = CapabilityResolution(
            mode="video",
            capability=CapabilityKind.VIDEO_GENERATION,
            availability=CapabilityAvailability.AVAILABLE,
            demo=True,
        )
        # A demo resolution is forced to DEMO_ONLY, never plain AVAILABLE.
        assert res.availability == CapabilityAvailability.DEMO_ONLY
        assert res.demo is True

    def test_unknown_availability_fails_closed(self):
        res = CapabilityResolution(
            mode="image",
            availability="whatever",
        )
        assert res.availability == CapabilityAvailability.UNCONFIGURED
        assert res.is_blocking

    def test_roundtrip(self):
        res = CapabilityResolution(
            mode="chat",
            capability=CapabilityKind.LANGUAGE,
            profile_id="p1",
            provider_id="openai",
            adapter="OpenAICompatClient",
            endpoint_style=EndpointStyle.OPENAI_CHAT,
            availability=CapabilityAvailability.AVAILABLE,
        )
        restored = CapabilityResolution.from_dict(res.to_dict())
        assert restored.profile_id == "p1"
        assert restored.endpoint_style == EndpointStyle.OPENAI_CHAT
        assert restored.is_runnable


# ---------------------------------------------------------------------- #
# Endpoint normalisation (plan §A0 step 9 / 10)
# ---------------------------------------------------------------------- #


class TestEndpointNormalisation:
    def test_empty_returns_empty(self):
        assert normalize_endpoint("") == ""

    def test_doubled_v1_collapsed(self):
        assert normalize_endpoint(
            "https://api.x.com/v1/v1/chat/completions"
        ) == "https://api.x.com/v1/chat/completions"

    def test_missing_scheme_added(self):
        result = normalize_endpoint("api.x.com/v1")
        assert result.startswith("https://")
        assert "api.x.com" in result

    def test_original_value_not_implicitly_required_to_mutate(self):
        original = "https://api.x.com/v1/v1/chat"
        normalize_endpoint(original)
        # The function is pure; callers keep the raw config (plan §A0 step 10).
        assert original == "https://api.x.com/v1/v1/chat"

    def test_detect_doubled_v1_issue(self):
        assert detect_endpoint_issue("https://api.x.com/v1/v1") is not None

    def test_detect_missing_scheme(self):
        assert detect_endpoint_issue("api.x.com/v1") is not None

    def test_detect_empty(self):
        assert detect_endpoint_issue("") is not None

    def test_clean_endpoint_has_no_issue(self):
        assert detect_endpoint_issue("https://api.openai.com/v1") is None
