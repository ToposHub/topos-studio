"""Tests for profile-selected ASR/TTS runtime adapters."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

from src.features.edit.ai_editing.analysis_models import AssetFingerprint
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
    ProfileRegistry,
)
from src.features.edit.ai_editing.profile_tts_provider import ProfileAwareTtsProvider
from src.features.edit.ai_editing.remote_transcript_provider import RemoteTranscriptProvider
from src.features.edit.ai_editing.transcript_router import ProfileAwareTranscriptProvider
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


def test_volcengine_flash_uses_json_audio_and_upgrades_legacy_stream_resource(tmp_path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"test audio")
    profile = AiProviderProfile(
        profile_id="flash", display_name="Flash ASR", provider_kind="volcengine_plan_asr",
        endpoint="https://speech.example/api/v3/auc/bigmodel/recognize/flash",
        model_id="volc.seedasr.sauc.duration", credential_ref="secret:flash",
        capabilities=("asr",), modalities=("asr", "media_upload"),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
    )
    store = FakeCredentialStore()
    store.set_secret(profile.credential_ref, "test-key")
    requests = []

    def opener(request, *, timeout: float) -> _Response:
        requests.append(request)
        response = _Response(b'{"result":{"utterances":[{"start_time":0,"end_time":1000,"text":"speech"}]}}')
        response.headers = {"X-Api-Status-Code": "20000000" if len(requests) == 1 else "20000003"}
        return response

    provider = RemoteTranscriptProvider(profile, store, asset_path_provider=lambda _id: str(audio), opener=opener)
    fingerprint = AssetFingerprint(size=audio.stat().st_size)
    analysis, _ = provider.analyze("asset", fingerprint, fps="24/1")
    assert analysis.transcript[0].end_frame == 24
    request = requests[0]
    assert request.get_header("X-api-resource-id") == "volc.bigasr.auc_turbo"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body["audio"] == {"data": base64.b64encode(b"test audio").decode("ascii"), "format": "wav"}
    assert body["request"]["show_utterances"] is True
    silence, segments = provider.analyze("asset", fingerprint, fps="24/1")
    assert not silence.transcript and not segments


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


def test_cleared_speech_bindings_use_local_instead_of_another_remote_profile() -> None:
    remote = AiProviderProfile(
        profile_id="speech-remote", display_name="Remote speech", capabilities=("asr", "tts"),
        source_kind="preset", endpoint="https://speech.example/v1",
    )
    registry = ProfileRegistry()
    registry.add(remote)
    store = FakeCredentialStore()
    local_asr = SimpleNamespace(validate_options=lambda _options: "local ASR")
    local_tts = SimpleNamespace(is_model_ready=lambda _settings: True)
    asr = ProfileAwareTranscriptProvider(local_asr, registry, store)
    tts = ProfileAwareTtsProvider(local_tts, registry, store)
    registry.set_active_for_capability("asr", remote.profile_id)
    registry.set_active_for_capability("tts", remote.profile_id)
    assert asr.active_profile is tts.active_profile is remote
    registry.set_active_for_capability("asr", "")
    registry.set_active_for_capability("tts", "")
    assert asr.active_profile is tts.active_profile is None
    assert asr.validate_options() == "local ASR"
    assert tts.is_model_ready()
    legacy_registry = SimpleNamespace(list=lambda: [remote])
    assert ProfileAwareTranscriptProvider(local_asr, legacy_registry, store).active_profile is remote
    assert ProfileAwareTtsProvider(local_tts, legacy_registry, store).active_profile is remote


def test_openai_transcription_uses_configured_no_key_lan_endpoint_with_upload_permission(tmp_path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fixture audio")
    profile = AiProviderProfile(
        profile_id="lan-asr", display_name="LAN ASR", provider_kind="local_http",
        source_kind="local", endpoint="http://192.168.1.40:8000/v1", model_id="whisper",
        credential_ref="secret:lan-asr", modalities=("asr", "media_upload"), capabilities=("asr",),
        data_policy=DataPolicy(execution="local", allow_media_upload=True),
    )
    store = FakeCredentialStore()
    captured = []

    def opener(request, *, timeout: float) -> _Response:
        captured.append(request)
        return _Response(b'{"segments":[{"start":0.0,"end":1.0,"text":"local speech"}]}')

    def provider_for(selected: AiProviderProfile) -> RemoteTranscriptProvider:
        return RemoteTranscriptProvider(selected, store, asset_path_provider=lambda _asset: str(audio), opener=opener)

    registry = ProfileRegistry()
    registry.add(profile)
    registry.set_active_for_capability("asr", profile.profile_id)
    routing = ProfileAwareTranscriptProvider(object(), registry, store)
    assert routing._use_remote(routing.active_profile)
    provider = provider_for(profile)
    assert provider.is_available()
    analysis, _ = provider.analyze("asset", AssetFingerprint(size=audio.stat().st_size), fps="24/1")
    assert analysis.transcript[0].text == "local speech"
    assert captured[0].full_url == "http://192.168.1.40:8000/v1/audio/transcriptions"
    assert "Authorization" not in captured[0].headers
    denied = replace(profile, data_policy=DataPolicy(execution="local", allow_media_upload=False))
    assert "未允许素材上传" in provider_for(denied).validate_options()
    cloud = replace(profile, endpoint="https://speech.example/v1")
    assert "API Key" in provider_for(cloud).validate_options()
    store.set_secret(profile.credential_ref, "test-key")
    assert "HTTPS" in provider.validate_options()
    assert provider_for(replace(profile, endpoint="https://192.168.1.40:8000/v1")).is_available()
    assert len(captured) == 1
