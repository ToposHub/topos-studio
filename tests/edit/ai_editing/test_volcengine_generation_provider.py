"""Focused contract tests for the Volcengine Agent Plan media adapters."""

from __future__ import annotations

import json
import base64
from pathlib import Path

from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile, FakeCredentialStore
from src.features.edit.ai_editing import volcengine_generation_provider as module


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return self._payload


def _consent(provider_id: str) -> GenerationConsent:
    return GenerationConsent(
        confirmed=True,
        provider_id=provider_id,
        cost_acknowledged=True,
        upload_acknowledged=True,
        copyright_acknowledged=True,
        target_location="generation_bin",
    )


def test_seedream_adapter_maps_2k_image_response():
    store = FakeCredentialStore()
    store.set_secret("topos:image", "secret")
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _Response(json.dumps({
            "model": "doubao-seedream-5.0-lite",
            "data": [{"url": "https://example.invalid/image.jpeg"}],
            "usage": {"generated_images": 1},
        }).encode())

    profile = AiProviderProfile(
        profile_id="volcengine-image",
        display_name="Seedream",
        provider_kind="volcengine_seedream",
        endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
        model_id="doubao-seedream-5.0-lite",
        credential_ref="topos:image",
        modalities=("image",),
    )
    provider = module.VolcengineSeedreamImageProvider(profile, store, opener=opener)
    request = GenerationRequest(
        request_id="image-test",
        modality=GenerationModality.IMAGE,
        prompt="a blue lamp",
        consent=_consent(profile.profile_id),
    )

    result = provider.generate(request)

    assert result.succeeded
    assert result.actual_cost_units == 1.0
    assert result.candidates[0].preview_handle.endswith("image.jpeg")
    assert seen["url"].endswith("/images/generations")
    assert seen["body"]["size"] == "2048x2048"


def test_tts_adapter_exposes_account_deduction_gate_without_leaking_body():
    store = FakeCredentialStore()
    store.set_secret("topos:voice", "secret")

    def opener(request, timeout):
        return _Response(json.dumps({
            "code": 45000030,
            "message": "AgentPlanDeductNotEnabled",
        }).encode())

    profile = AiProviderProfile(
        profile_id="volcengine-voice",
        display_name="Agent Plan TTS",
        provider_kind="volcengine_plan_tts",
        endpoint="https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional",
        model_id="doubao-seed-tts-2.0",
        credential_ref="topos:voice",
        modalities=("voice",),
    )
    provider = module.VolcenginePlanTtsProvider(profile, store, opener=opener)
    request = GenerationRequest(
        request_id="voice-test",
        modality=GenerationModality.VOICE,
        prompt="你好，测试语音。",
        consent=_consent(profile.profile_id),
    )

    result = provider.generate(request)

    assert not result.succeeded
    assert result.error_code == "GEN_PROVIDER_PLAN_DEDUCTION_DISABLED"
    assert "secret" not in result.error_message


def test_tts_adapter_caches_successful_audio(tmp_path: Path, monkeypatch):
    store = FakeCredentialStore()
    store.set_secret("topos:voice", "secret")
    monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path)

    def opener(request, timeout):
        return _Response(b"ID3-test-audio")

    profile = AiProviderProfile(
        profile_id="volcengine-voice",
        display_name="Agent Plan TTS",
        provider_kind="volcengine_plan_tts",
        endpoint="https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional",
        model_id="doubao-seed-tts-2.0",
        credential_ref="topos:voice",
        modalities=("voice",),
    )
    provider = module.VolcenginePlanTtsProvider(profile, store, opener=opener)
    request = GenerationRequest(
        request_id="voice-success",
        modality=GenerationModality.VOICE,
        prompt="你好。",
        consent=_consent(profile.profile_id),
    )

    result = provider.generate(request)

    assert result.succeeded
    cached = Path(result.candidates[0].preview_handle)
    assert cached.is_file()
    assert cached.read_bytes() == b"ID3-test-audio"


def test_tts_adapter_decodes_newline_delimited_audio_chunks():
    store = FakeCredentialStore()
    store.set_secret("topos:voice", "secret")
    chunks = [
        {"code": 0, "data": base64.b64encode(b"ID3-").decode()},
        {"code": 0, "data": base64.b64encode(b"audio").decode()},
    ]

    def opener(request, timeout):
        return _Response(("\n".join(json.dumps(item) for item in chunks)).encode())

    profile = AiProviderProfile(
        profile_id="volcengine-voice",
        display_name="Agent Plan TTS",
        provider_kind="volcengine_plan_tts",
        endpoint="https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional",
        model_id="doubao-seed-tts-2.0",
        credential_ref="topos:voice",
        modalities=("voice",),
    )
    provider = module.VolcenginePlanTtsProvider(profile, store, opener=opener)
    request = GenerationRequest(
        request_id="voice-chunks",
        modality=GenerationModality.VOICE,
        prompt="你好。",
        consent=_consent(profile.profile_id),
    )

    result = provider.generate(request)

    assert result.succeeded
    assert Path(result.candidates[0].preview_handle).read_bytes() == b"ID3-audio"
