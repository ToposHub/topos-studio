"""AssistantCapabilityRouter tests (plan §A1.5 / §2.8).

Validates the contract that separates "mode is selectable" from "mode has a
real provider":

- Empty / partial registries fail closed with an actionable reason.
- Media generation without a real provider never reports success; a demo stub
  is explicitly flagged ``DEMO_ONLY``.
- ``vision_understanding`` is never satisfied by an image-generation provider
  (the Seedream / VLM split, plan §2.8 §2).
- Multi-capability modes (Motion Graphic) are blocking as soon as one required
  capability is missing.
- Capability-specific active profiles are isolated (switching the language
  model does not change TTS / image routing).
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.assistant_capability_router import (
    AssistantCapabilityRouter,
)
from src.features.edit.ai_editing.generation_models import GenerationModality
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider,
    ProviderRegistry,
)
from src.features.edit.ai_editing.provider_capabilities import (
    CapabilityAvailability,
    CapabilityKind,
)
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    ProfileRegistry,
)


# ---------------------------------------------------------------------- #
# fixtures / helpers
# ---------------------------------------------------------------------- #


def _language_profile(profile_id: str = "lang1") -> AiProviderProfile:
    return AiProviderProfile(
        profile_id=profile_id,
        display_name="GLM",
        provider_kind="openai",
        capabilities=("language",),
    )


def _profile_with_capability(
    profile_id: str, capability: str, kind: str = "openai"
) -> AiProviderProfile:
    return AiProviderProfile(
        profile_id=profile_id,
        display_name=profile_id,
        provider_kind=kind,
        capabilities=(capability,),
    )


class _FakeRealProvider:
    """A non-demo generation provider used only for routing tests."""

    def __init__(self, provider_id: str, modalities, *, is_demo: bool = False) -> None:
        self.provider_id = provider_id
        self.is_demo = is_demo
        self._modalities = tuple(modalities)

    @property
    def supported_modalities(self):
        return self._modalities

    def generate(self, *args, **kwargs):  # pragma: no cover - never called in routing tests
        raise RuntimeError("routing tests never generate")


@pytest.fixture
def profiles_with_language() -> ProfileRegistry:
    reg = ProfileRegistry()
    reg.add(_language_profile())
    reg.set_active_for_capability("language", "lang1")
    return reg


# ---------------------------------------------------------------------- #
# Empty registries -> everything fails closed
# ---------------------------------------------------------------------- #


class TestFailClosed:
    def test_no_registries_blocks_everything(self):
        router = AssistantCapabilityRouter(None)
        for mode in ("chat", "image", "video", "music", "vision", "asr", "tts"):
            res = router.resolve(mode)
            assert res.is_blocking, mode
            assert res.reason, f"missing reason for {mode}"

    def test_missing_language_profile_blocks_chat(self):
        router = AssistantCapabilityRouter(ProfileRegistry())
        res = router.resolve("chat")
        assert res.availability == CapabilityAvailability.UNCONFIGURED
        assert res.is_blocking

    def test_unknown_mode_normalises_to_chat(self):
        router = AssistantCapabilityRouter(None)
        # Unknown mode must never resolve to a paid generation path.
        res = router.resolve("totally-bogus-future-mode")
        assert res.mode == "chat"
        assert res.capability == CapabilityKind.LANGUAGE


# ---------------------------------------------------------------------- #
# Language + Motion Graphic routing
# ---------------------------------------------------------------------- #


class TestLanguageRouting:
    def test_chat_available_with_language_profile(self, profiles_with_language):
        router = AssistantCapabilityRouter(profiles_with_language)
        res = router.resolve("chat")
        assert res.is_runnable
        assert res.profile_id == "lang1"
        assert res.is_blocking is False

    def test_motion_graphic_needs_adapter(self, profiles_with_language):
        # Without the teaching-animation adapter, Motion Graphic is blocked.
        router = AssistantCapabilityRouter(
            profiles_with_language, teaching_animation_available=False
        )
        res = router.resolve("motion_graphic")
        assert res.is_blocking
        assert CapabilityKind.TEACHING_ANIMATION in res.missing_capabilities

    def test_motion_graphic_available_with_language_and_adapter(
        self, profiles_with_language
    ):
        router = AssistantCapabilityRouter(
            profiles_with_language, teaching_animation_available=True
        )
        res = router.resolve("motion_graphic")
        assert res.is_runnable
        assert res.adapter == "TeachingAnimationAgentAdapter"

    def test_motion_graphic_blocked_without_language(self):
        # adapter present but no language profile -> blocked, lists language.
        router = AssistantCapabilityRouter(
            ProfileRegistry(), teaching_animation_available=True
        )
        res = router.resolve("motion_graphic")
        assert res.is_blocking
        assert CapabilityKind.LANGUAGE in res.missing_capabilities


# ---------------------------------------------------------------------- #
# Media generation routing (image / video / music)
# ---------------------------------------------------------------------- #


class TestGenerationRouting:
    def test_generation_blocked_without_registry(self, profiles_with_language):
        router = AssistantCapabilityRouter(profiles_with_language)
        for mode in ("image", "video", "music"):
            res = router.resolve(mode)
            assert res.availability == CapabilityAvailability.NOT_INSTALLED, mode

    def test_demo_provider_flagged_not_success(self, profiles_with_language):
        gen = ProviderRegistry()
        gen.register(DeterministicGenerationProvider(), preferred=True)
        router = AssistantCapabilityRouter(
            profiles_with_language, generation_registry=gen
        )
        res = router.resolve("image")
        # Demo must be reported as DEMO_ONLY, never AVAILABLE (plan §2.8 §3).
        assert res.availability == CapabilityAvailability.DEMO_ONLY
        assert res.demo is True
        assert res.is_runnable  # runnable but explicitly demo-marked

    def test_real_provider_available(self, profiles_with_language):
        gen = ProviderRegistry()
        gen.register(
            _FakeRealProvider(
                "seedance-real", (GenerationModality.VIDEO,), is_demo=False
            ),
            preferred=True,
        )
        router = AssistantCapabilityRouter(
            profiles_with_language, generation_registry=gen
        )
        res = router.resolve("video")
        assert res.availability == CapabilityAvailability.AVAILABLE
        assert res.demo is False
        assert res.provider_id == "seedance-real"

    def test_other_modalities_still_demo_when_only_one_real(self, profiles_with_language):
        gen = ProviderRegistry()
        gen.register(DeterministicGenerationProvider(), preferred=True)
        gen.register(
            _FakeRealProvider(
                "seedance-real", (GenerationModality.VIDEO,), is_demo=False
            ),
            preferred=True,
        )
        router = AssistantCapabilityRouter(
            profiles_with_language, generation_registry=gen
        )
        assert router.resolve("video").availability == CapabilityAvailability.AVAILABLE
        assert router.resolve("music").availability == CapabilityAvailability.DEMO_ONLY
        assert router.resolve("image").availability == CapabilityAvailability.DEMO_ONLY


# ---------------------------------------------------------------------- #
# Vision split (the key §2.8 §2 guarantee)
# ---------------------------------------------------------------------- #


class TestVisionSplit:
    def test_image_generation_does_not_satisfy_vision(self, profiles_with_language):
        # A real image-generation provider exists ...
        gen = ProviderRegistry()
        gen.register(
            _FakeRealProvider(
                "seedream-real", (GenerationModality.IMAGE,), is_demo=False
            ),
            preferred=True,
        )
        router = AssistantCapabilityRouter(
            profiles_with_language, generation_registry=gen
        )
        image_res = router.resolve("image")
        assert image_res.is_runnable
        # ... but vision understanding is still blocked (no VLM).
        vision_res = router.resolve("vision")
        assert vision_res.is_blocking
        assert "VLM" in vision_res.reason or "视觉" in vision_res.reason

    def test_vision_needs_dedicated_profile(self):
        reg = ProfileRegistry()
        # A profile declares the new vision_understanding tag.
        reg.add(
            _profile_with_capability(
                "vlm1", CapabilityKind.VISION_UNDERSTANDING
            )
        )
        reg.set_active_for_capability(
            CapabilityKind.VISION_UNDERSTANDING, "vlm1"
        )
        router = AssistantCapabilityRouter(reg)
        res = router.resolve("vision")
        assert res.is_runnable
        assert res.profile_id == "vlm1"


# ---------------------------------------------------------------------- #
# Profile-driven capabilities (asr / tts / embedding)
# ---------------------------------------------------------------------- #


class TestProfileCapabilityRouting:
    @pytest.mark.parametrize(
        "legacy_tag,capability,mode",
        [
            ("tts", CapabilityKind.TEXT_TO_SPEECH, "tts"),
            ("asr", CapabilityKind.SPEECH_TO_TEXT, "asr"),
            ("embedding", CapabilityKind.EMBEDDING, "embedding"),
        ],
    )
    def test_available_when_profile_configured(self, legacy_tag, capability, mode):
        reg = ProfileRegistry()
        reg.add(_profile_with_capability("p1", legacy_tag))
        reg.set_active_for_capability(legacy_tag, "p1")
        router = AssistantCapabilityRouter(reg)
        res = router.resolve(mode)
        assert res.is_runnable
        assert res.profile_id == "p1"

    def test_blocked_without_profile(self):
        router = AssistantCapabilityRouter(ProfileRegistry())
        for mode in ("tts", "asr", "embedding"):
            assert router.resolve(mode).is_blocking, mode


# ---------------------------------------------------------------------- #
# Capability isolation
# ---------------------------------------------------------------------- #


class TestCapabilityIsolation:
    def test_language_profile_does_not_satisfy_tts(self):
        # Switching the active language model must not silently change TTS.
        reg = ProfileRegistry()
        reg.add(_language_profile())
        reg.set_active_for_capability("language", "lang1")
        router = AssistantCapabilityRouter(reg)
        assert router.resolve("chat").is_runnable
        assert router.resolve("tts").is_blocking

    def test_independent_profiles_for_each_capability(self):
        reg = ProfileRegistry()
        reg.add(_profile_with_capability("lang", "language"))
        reg.add(_profile_with_capability("tts1", "tts", kind="volcengine_plan_tts"))
        reg.set_active_for_capability("language", "lang")
        reg.set_active_for_capability("tts", "tts1")
        router = AssistantCapabilityRouter(reg)
        chat = router.resolve("chat")
        tts = router.resolve("tts")
        assert chat.profile_id == "lang"
        assert tts.profile_id == "tts1"
        assert chat.profile_id != tts.profile_id


# ---------------------------------------------------------------------- #
# resolve_all
# ---------------------------------------------------------------------- #


class TestResolveAll:
    def test_resolves_every_registered_ui_mode(self, profiles_with_language):
        router = AssistantCapabilityRouter(
            profiles_with_language, teaching_animation_available=True
        )
        all_res = router.resolve_all()
        # All five selector modes are present.
        assert set(all_res.keys()) == {"chat", "motion_graphic", "video", "music", "image"}
        for mode, res in all_res.items():
            assert res.mode == mode
        assert all_res["chat"].is_runnable
        assert all_res["motion_graphic"].is_runnable
        # No generation registry -> media modes blocked.
        assert all_res["video"].is_blocking
