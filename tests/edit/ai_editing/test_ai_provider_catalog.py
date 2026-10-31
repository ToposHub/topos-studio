"""Unified AI provider source/catalog tests."""

from __future__ import annotations

from src.features.edit.ai_editing.ai_provider_catalog import (
    SOURCE_LOCAL,
    SOURCE_PRESET,
    category_for_profile,
    get_preset,
    profile_supports_input,
    presets_for_category,
)
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile


def test_profile_round_trip_keeps_source_preset_and_capabilities():
    profile = AiProviderProfile(
        profile_id="ark-image",
        display_name="Ark Image",
        provider_kind="volcengine_seedream",
        endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
        model_id="doubao-seedream-5.0-lite",
        source_kind=SOURCE_PRESET,
        preset_id="volcengine-seedream-image",
        capabilities=("vision", "image"),
        modalities=("image",),
    )

    restored = AiProviderProfile.from_dict(profile.to_dict())

    assert restored.source_kind == SOURCE_PRESET
    assert restored.preset_id == "volcengine-seedream-image"
    assert restored.capabilities == ("vision", "image")
    assert category_for_profile(restored) == "vision"


def test_legacy_profile_is_inferred_into_language_tab():
    profile = AiProviderProfile.from_dict({
        "profile_id": "legacy",
        "display_name": "Legacy",
        "provider_kind": "openai",
        "modalities": ["text", "tool_call"],
    })

    assert profile.capabilities == ("language",)
    assert category_for_profile(profile) == "language"


def test_catalog_has_local_speech_and_cloud_presets():
    local = get_preset("local-qwen-asr")
    assert local is not None
    assert local.source_kind == SOURCE_LOCAL
    assert local.capabilities == ("asr",)

    speech = presets_for_category("speech")
    assert any(item.preset_id == "volcengine-plan-tts" for item in speech)
    assert any(item.preset_id == "local-qwen-tts" for item in speech)


def test_catalog_has_glm_coding_preset() -> None:
    glm = get_preset("zhipu-glm-coding")
    assert glm is not None
    assert glm.provider_kind == "openai"
    assert glm.endpoint.endswith("/api/coding/paas/v4")
    assert glm.model_id == "glm-5.2"


def test_seed_lite_legacy_profile_is_recognized_as_multimodal() -> None:
    profile = AiProviderProfile(
        profile_id="ark-lite",
        display_name="Doubao Seed Lite",
        provider_kind="volcengine_responses",
        endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
        model_id="doubao-seed-2.0-lite",
        modalities=("text", "tool_call"),
        capabilities=("language", "vision_understanding"),
    )
    assert all(profile_supports_input(profile, kind) for kind in (
        "image", "video", "audio", "file",
    ))


def test_plain_text_profile_is_not_multimodal() -> None:
    profile = AiProviderProfile(
        profile_id="deepseek",
        display_name="DeepSeek",
        provider_kind="deepseek",
        endpoint="https://api.deepseek.com",
        model_id="deepseek-chat",
        modalities=("text", "tool_call"),
        capabilities=("language",),
    )
    assert not profile_supports_input(profile, "image")
    assert not profile_supports_input(profile, "video")
    assert not profile_supports_input(profile, "audio")
    assert not profile_supports_input(profile, "file")
