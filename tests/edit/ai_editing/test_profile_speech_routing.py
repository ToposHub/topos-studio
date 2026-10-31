"""Tests for profile-selected ASR/TTS runtime adapters."""

from __future__ import annotations

import json

from src.features.edit.ai_editing.analysis_models import AssetFingerprint
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
    ProfileRegistry,
)
from src.features.edit.ai_editing.profile_tts_provider import ProfileAwareTtsProvider
from src.features.edit.ai_editing.remote_transcript_provider import RemoteTranscriptProvider
from src.features.edit.ai_editing.tts_provider import SpeechCue, SynthesizedSpeech


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


def test_volcengine_remote_asr_maps_utterance_timecodes(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake-audio")
    store = FakeCredentialStore()
    profile = AiProviderProfile(
        profile_id="ark-asr",
        display_name="Ark ASR",
        provider_kind="volcengine_plan_asr",
        source_kind="preset",
        endpoint="https://openspeech.example/asr",
        model_id="volc.seedasr.sauc.duration",
        credential_ref="secret:ark-asr",
        modalities=("asr", "media_upload"),
        capabilities=("asr",),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
    )
    store.set_secret(profile.credential_ref, "test-key")
    captured = {}

    def opener(request, timeout):
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        return _Response(json.dumps({
            "result": {
                "utterances": [{
                    "start_time": 0,
                    "end_time": 1500,
                    "text": "你好世界",
                    "additions": {"speaker": "7"},
                }],
            },
        }).encode())

    provider = RemoteTranscriptProvider(
        profile,
        store,
        asset_path_provider=lambda _asset_id: str(audio),
        opener=opener,
    )
    analysis, _segments = provider.analyze(
        "asset-1", AssetFingerprint(size=audio.stat().st_size), fps="24/1",
    )

    assert analysis.transcript[0].text == "你好世界"
    assert analysis.transcript[0].speaker == "S1"
    assert analysis.transcript[0].end_frame == 36
    assert captured["headers"]["X-api-key"] == "test-key"
    assert captured["headers"]["X-api-resource-id"] == "volc.seedasr.sauc.duration"


def test_tts_router_prefers_active_local_capability_profile():
    profile = AiProviderProfile(
        profile_id="local-tts",
        display_name="Local TTS",
        provider_kind="local_qwen_tts",
        source_kind="local",
        model_id="qwen3_tts_customvoice",
        capabilities=("tts",),
        modalities=("voice",),
    )
    registry = ProfileRegistry()
    registry.add(profile)
    registry.set_active(profile.profile_id)
    store = FakeCredentialStore()

    class _Local:
        def is_model_ready(self, _settings):
            return True

        def synthesize(self, cues, *, fps, settings, output_path=None):
            return SynthesizedSpeech(
                path="local.wav", sample_rate=48000, duration_seconds=0.5,
                first_frame=int(cues[0].start_frame), model="Qwen3-TTS",
            )

    provider = ProfileAwareTtsProvider(_Local(), registry, store)
    result = provider.synthesize((SpeechCue("测试", 12),), fps=24.0)

    assert result.model == "Qwen3-TTS"
    assert provider.active_profile is profile


def test_registry_keeps_capability_active_profiles_independent():
    registry = ProfileRegistry()
    language = AiProviderProfile(
        profile_id="language", display_name="Language", capabilities=("language",),
    )
    speech = AiProviderProfile(
        profile_id="speech", display_name="Speech", capabilities=("tts",),
    )
    registry.add(language)
    registry.add(speech)
    registry.set_active("language")
    registry.set_active("speech")

    assert registry.active_profile_for_capability("language") is language
    assert registry.active_profile_for_capability("tts") is speech
