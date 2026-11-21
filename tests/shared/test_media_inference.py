from __future__ import annotations

import base64
import io
import json
import threading
import wave
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from src.shared.ai_models.generation.media_inference import (
    MediaInferenceClient,
    MediaInferenceConfigurationError,
    MediaInferenceError,
    TranscriptSpan,
    is_local_media_endpoint,
    validate_media_endpoint,
)
from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, DataPolicy


@pytest.fixture
def server() -> Iterator[tuple[str, list[tuple[str, dict[str, str], bytes]], dict[str, Any]]]:
    received: list[tuple[str, dict[str, str], bytes]] = []
    responses: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.do_POST()

        def do_POST(self) -> None:
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.path, {key.lower(): val for key, val in self.headers.items()}, data))
            payload = responses.get(self.path, (404, {}, {"error": "unexpected route"}))
            status, headers, body = payload if isinstance(payload, tuple) else (200, {}, payload)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", received, responses
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def _profile(endpoint: str, kind: str = "openai", model: str = "test-model") -> AiProviderProfile:
    return AiProviderProfile("test", "Test service", kind, endpoint, model, capabilities=("vision", "asr"))


def _photo(path: Path) -> None:
    Image.new("RGB", (20, 10), "blue").save(path)


def _audio(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0" * 64000)


@pytest.mark.parametrize("route", ["/prefix/v1/chat/completions", "/prefix", "/prefix/api/tags"])
def test_ollama_vision_and_model_discovery_use_configured_endpoint(server: tuple, tmp_path: Path, route: str) -> None:
    base, calls, responses = server
    responses["/prefix/v1/chat/completions"] = {"choices": [{"message": {"content": "蓝色画面"}}]}
    responses["/prefix/api/tags"] = {"models": [{"name": "z:7b"}, {"name": "a:7b"}, {"name": "z:7b"}, None]}
    path = tmp_path / "personal-name.png"
    _photo(path)

    client = MediaInferenceClient(_profile(f"{base}{route}", "ollama", "local:7b"))

    assert client.describe_image(path) == "蓝色画面"
    assert client.list_models() == ["a:7b", "z:7b"]
    request = json.loads(calls[0][2])
    assert request["model"] == "local:7b"
    data_url = request["messages"][0]["content"][1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(",")[1]))) as image:
        assert image.size == (20, 10)
    assert path.name.encode() not in calls[0][2]


@pytest.mark.parametrize(("answer", "expected"), [
    (
        '{"caption":" 蓝色画面 ","tags":[" 静物 ",null,"蓝色","静物",""]}',
        ("蓝色画面", ("静物", "蓝色")),
    ),
    (
        '```json\n{"caption":"蓝色画面","tags":["静物"]}\n```',
        ("蓝色画面", ("静物",)),
    ),
    ("蓝色画面", ("蓝色画面", ())),
])
def test_vision_returns_description_and_tags_in_one_request(
    server: tuple, tmp_path: Path, answer: str, expected: tuple[str, tuple[str, ...]],
) -> None:
    base, calls, responses = server
    responses["/v1/chat/completions"] = {"choices": [{"message": {"content": answer}}]}
    path = tmp_path / "photo.png"
    _photo(path)

    assert MediaInferenceClient(_profile(base)).analyze_image(path) == expected
    assert len(calls) == 1
    parts = json.loads(calls[0][2])["messages"][0]["content"]
    assert "caption" in parts[0]["text"] and "tags" in parts[0]["text"]


def test_truncated_vision_result_is_retryable(server: tuple, tmp_path: Path) -> None:
    base, _, responses = server
    responses["/v1/chat/completions"] = {
        "choices": [{"message": {"content": '{"caption":"蓝色画面","tags":['}}],
    }
    path = tmp_path / "photo.png"
    _photo(path)

    with pytest.raises(MediaInferenceError, match="格式不完整"):
        MediaInferenceClient(_profile(base)).analyze_image(path)


def test_openai_asr_uploads_anonymous_wav_and_returns_provider_segments(server: tuple, tmp_path: Path) -> None:
    base, calls, responses = server
    responses["/v1/audio/transcriptions"] = {
        "text": "street closing soon", "duration": 2, "segments": [
            {"start": 0.25, "end": 1.75, "text": "street closing soon"},
        ],
    }
    path = tmp_path / 'confidential" recording.wav'
    _audio(path)

    client = MediaInferenceClient(_profile(base, model="whisper-1"), "secret")

    assert client.transcribe(path) == (TranscriptSpan(0.25, 1.75, "street closing soon"),)
    _, headers, body = calls[0]
    assert headers["authorization"] == "Bearer secret"
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'filename="audio.wav"' in body
    assert b"whisper-1" in body and b"verbose_json" in body and b"timestamp_granularities[]" in body
    assert b"confidential" not in body


def test_text_only_asr_uses_measured_duration_and_reports_invalid_provider_segments(server: tuple, tmp_path: Path) -> None:
    base, calls, responses = server
    route = "/v1/audio/transcriptions"
    responses[route] = {"text": "voice"}
    path = tmp_path / "audio.wav"
    _audio(path)
    client = MediaInferenceClient(_profile(base, model="gpt-transcribe"))

    assert client.transcribe(path) == (TranscriptSpan(0.0, 2.0, "voice"),)
    assert b"verbose_json" not in calls[0][2]
    responses[route] = {"segments": [{"start": "Infinity", "end": 0.5, "text": "broken"}]}
    with pytest.raises(MediaInferenceError, match="时间码无效"):
        client.transcribe(path)


def test_volcengine_flash_uses_json_and_millisecond_spans(server: tuple, tmp_path: Path) -> None:
    base, calls, responses = server
    route = "/api/v3/auc/bigmodel/recognize/flash"
    responses[route] = (200, {"X-Api-Status-Code": "20000000"}, {
        "audio_info": {"duration": 2000},
        "result": {"utterances": [{"start_time": 150, "end_time": 900, "text": "你好"}]},
    })
    path = tmp_path / "audio.wav"
    _audio(path)
    client = MediaInferenceClient(_profile(base, "volcengine_plan_asr", "volc.seedasr.sauc.duration"), "volc-key")

    assert client.transcribe(path) == (TranscriptSpan(0.15, 0.9, "你好"),)
    _, headers, body = calls[0]
    assert headers["x-api-key"] == "volc-key"
    assert headers["x-api-resource-id"] == "volc.bigasr.auc_turbo"
    audio = json.loads(body)["audio"]
    assert audio["format"] == "wav"
    assert base64.b64decode(audio["data"]) == path.read_bytes()
    responses[route] = (200, {"X-Api-Status-Code": "20000003"}, b"")
    assert client.transcribe(path) == ()
    assert client.test("asr")


def test_actual_upload_requires_capability_and_off_host_policy(tmp_path: Path) -> None:
    client = MediaInferenceClient(_profile("http://192.168.1.20:8080/v1", "local_http"))
    assert not client.is_loopback
    with pytest.raises(MediaInferenceConfigurationError, match="允许上传素材"):
        client.describe_image(tmp_path / "missing.png")
    with pytest.raises(MediaInferenceConfigurationError, match="允许上传素材"):
        client.transcribe(tmp_path / "missing.wav")

    policy_only = replace(_profile("https://service.invalid/v1"), data_policy=DataPolicy(allow_media_upload=True))
    with pytest.raises(MediaInferenceConfigurationError, match="允许上传素材"):
        MediaInferenceClient(policy_only).describe_image(tmp_path / "missing.png")
    allowed = replace(policy_only, modalities=("media_upload",))
    with pytest.raises(MediaInferenceError, match="无法读取"):
        MediaInferenceClient(allowed).describe_image(tmp_path / "missing.png")
    no_capability = replace(allowed, capabilities=())
    with pytest.raises(MediaInferenceConfigurationError, match="未声明"):
        MediaInferenceClient(no_capability).describe_image(tmp_path / "missing.png")


@pytest.mark.parametrize("endpoint,key", [
    ("http://example.org", ""), ("http://192.168.1.30", "secret"),
    ("https://u:pass@example.org", ""), ("https://example.org/?secret=bad", ""),
    ("file:///tmp/media", ""), ("http://127.0.0.1:99999", ""),
    ("https://example.org", "secret\r\nInjected: yes"), ("http://[::1]extra", ""),
])
def test_rejects_invalid_or_insecure_config_without_echoing_credentials(endpoint: str, key: str) -> None:
    with pytest.raises(MediaInferenceConfigurationError) as caught:
        MediaInferenceClient(_profile(endpoint), key)
    assert endpoint not in str(caught.value)
    assert not key or key not in str(caught.value)


def test_classifies_loopback_lan_and_public_addresses() -> None:
    loopback, directly_local = validate_media_endpoint("http://[::1]:11434")
    assert directly_local and is_local_media_endpoint(loopback)
    for url in ("http://10.0.0.30:8080", "https://[fd00::1]:9000"):
        address, directly_local = validate_media_endpoint(url)
        assert not directly_local and is_local_media_endpoint(address)
    public, directly_local = validate_media_endpoint("https://model.example.org")
    assert not directly_local and not is_local_media_endpoint(public)


def test_probes_only_send_generated_media_and_allow_silence(server: tuple) -> None:
    base, calls, responses = server
    responses["/v1/chat/completions"] = {"choices": [{"message": {"content": "Red"}}]}
    responses["/v1/audio/transcriptions"] = {"text": "", "segments": []}
    responses["/v1/embeddings"] = {"data": [{"embedding": [0.25, -0.4]}]}
    client = MediaInferenceClient(_profile(base))

    assert client.test("vision")
    assert client.test("asr")
    assert client.test("embedding")
    request = json.loads(calls[0][2])
    image_url = request["messages"][0]["content"][1]["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(image_url.split(",")[1]))) as image:
        assert image.size == (64, 64)
        assert image.getpixel((0, 0))[0] > 240
    audio = calls[1][2]
    start = audio.index(b"RIFF")
    with wave.open(io.BytesIO(audio[start:]), "rb") as media:
        assert media.getframerate() == media.getnframes() == 16000


def test_anthropic_and_responses_use_their_own_endpoints_and_payloads(server: tuple) -> None:
    base, calls, responses = server
    responses["/v1/messages"] = {"content": [{"type": "text", "text": "Red"}]}
    responses["/api/plan/v3/responses"] = {"output": [{
        "type": "message", "content": [{"type": "output_text", "text": "Red"}],
    }]}

    assert MediaInferenceClient(_profile(base, "anthropic"), "anthropic-key").test("vision")
    assert MediaInferenceClient(_profile(f"{base}/api/plan/v3", "volcengine_responses"), "ark-key").test("vision")
    assert calls[0][1]["x-api-key"] == "anthropic-key"
    assert json.loads(calls[0][2])["messages"][0]["content"][1]["source"]["type"] == "base64"
    assert calls[1][1]["authorization"] == "Bearer ark-key"
    assert json.loads(calls[1][2])["input"][0]["content"][1]["type"] == "input_image"


@pytest.mark.parametrize("status", [302, 307, 401, 429])
def test_never_exposes_provider_error_or_forwards_requests(server: tuple, status: int) -> None:
    base, calls, responses = server
    responses["/v1/chat/completions"] = (
        status, {"Location": f"{base}/unexpected"}, {"detail": "credential source path and sensitive response"},
    )
    client = MediaInferenceClient(_profile(base), "test-secret")

    with pytest.raises(MediaInferenceError, match=f"HTTP {status}") as caught:
        client.test("vision")
    assert "sensitive" not in str(caught.value) and "test-secret" not in str(caught.value)
    assert len(calls) == 1


def test_server_response_size_and_format_are_bounded(server: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _, responses = server
    route = "/v1/models"
    monkeypatch.setattr("src.shared.ai_models.generation.media_inference._MAX_RESPONSE_BYTES", 256)
    responses[route] = (200, {}, b"{" + b" " * 256)
    client = MediaInferenceClient(_profile(base))

    with pytest.raises(MediaInferenceError, match="超出大小限制"):
        client.list_models()
    responses[route] = (200, {}, b"{confidential garbage")
    with pytest.raises(MediaInferenceError, match="无效的 JSON") as caught:
        client.list_models()
    assert "confidential" not in str(caught.value)
