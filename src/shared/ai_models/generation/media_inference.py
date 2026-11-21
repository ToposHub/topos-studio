"""Bounded HTTP requests for model discovery and media analysis."""

from __future__ import annotations

import base64
import http.client
import io
import ipaddress
import json
import math
import os
import ssl
import stat
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

from PIL import Image, ImageOps

from src.shared.ai_models.generation.provider_profiles import AiProviderProfile


_CHAT_KINDS = frozenset({"openai", "openai_compat", "local_http", "deepseek", "ollama"})
_RESPONSES_KINDS = frozenset({"openai_responses", "volcengine_responses"})
_ANTHROPIC_KINDS = frozenset({"anthropic", "claude"})
_OPENAI_ASR_KINDS = _CHAT_KINDS - {"ollama", "deepseek"} | {"openai_responses"}
_API_ROUTES = ("chat/completions", "audio/transcriptions", "responses", "messages", "models", "embeddings")
_VOLC_FLASH = "api/v3/auc/bigmodel/recognize/flash"
_VOLC_RESOURCE = "volc.bigasr.auc_turbo"
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_ASR_FORMATS = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".mp4": "audio/mp4", ".webm": "audio/webm",
}
_MAX_AUDIO_BYTES = 25_000_000
_MAX_IMAGE_BYTES = 40_000_000
_MAX_IMAGE_PIXELS = 60_000_000
_MAX_RESPONSE_BYTES = 4_000_000
_CAPTION_PROMPT = (
    "用简明中文描述这张素材的可见内容，包含主要物体、人物、动作、环境和适合检索的细节。"
    "只描述能从画面中观察到的内容。"
)
TAGGED_CAPTION_PROMPT = (
    "用简明中文记录素材中可见的主体、物体、动作和场景。"
    '只输出一个 JSON 对象，格式为 {"caption":"一到两句描述","tags":["检索标签"]}。'
    "提供三到八个简短、具体、不重复的标签。不要推测身份或不可见的信息。"
)


class MediaInferenceError(RuntimeError):
    """An inference failure with a message safe to show in the application."""


class MediaInferenceConfigurationError(MediaInferenceError):
    """The endpoint, capability, protocol, or upload policy needs configuration."""


def parse_image_analysis(answer: str) -> tuple[str, tuple[str, ...]]:
    """Normalize a visual description from an HTTP model or MCP tool."""
    answer = answer.strip()
    if not answer:
        raise MediaInferenceError("视觉服务未返回画面描述。")
    structured = answer.startswith(("{", "[", "```"))
    lines = answer.splitlines()
    if len(lines) > 2 and lines[0].strip().lower() in {"```json", "```"} and lines[-1].strip() == "```":
        answer = "\n".join(lines[1:-1]).strip()
    try:
        result = json.loads(answer)
    except json.JSONDecodeError as exc:
        if structured:
            raise MediaInferenceError("视觉服务返回的描述和标签格式不完整。") from exc
        # Some compatible endpoints answer with a plain caption.
        return answer, ()
    if not isinstance(result, dict):
        raise MediaInferenceError("视觉服务未返回有效的描述对象。")
    caption = result.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        raise MediaInferenceError("视觉服务未返回有效的画面描述。")
    tag_values = result.get("tags")
    if not isinstance(tag_values, list):
        return caption.strip(), ()
    tags = (tag.strip() for tag in tag_values if isinstance(tag, str))
    unique_tags = dict.fromkeys(tag for tag in tags if 0 < len(tag) <= 32 and tag.isprintable())
    return caption.strip(), tuple(unique_tags)[:8]


@dataclass(frozen=True)
class TranscriptSpan:
    start_seconds: float
    end_seconds: float
    text: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> None:
        return None


class MediaInferenceClient:
    """Run one explicitly selected AI service profile from a worker thread."""

    @staticmethod
    def supports(provider_kind: str, capability: str) -> bool:
        kind = provider_kind.strip().lower()
        if capability in {"language", "vision", "vision_understanding"}:
            return kind in _CHAT_KINDS | _RESPONSES_KINDS | _ANTHROPIC_KINDS
        if capability == "asr":
            return kind in _OPENAI_ASR_KINDS | {"volcengine_plan_asr"}
        if capability == "embedding":
            return kind in _CHAT_KINDS | {"openai_responses", "openai_embedding"}
        if capability == "models":
            return kind in _CHAT_KINDS | _RESPONSES_KINDS | _ANTHROPIC_KINDS | {"openai_embedding"}
        return False

    def __init__(self, profile: AiProviderProfile, api_key: str | None = None) -> None:
        self._profile = profile
        self._kind = profile.provider_kind.strip().lower()
        self._model = profile.model_id.strip()
        self._api_key = (api_key or "").strip()
        self._endpoint, self.is_loopback = validate_media_endpoint(profile.endpoint, self._api_key)
        timeout = profile.limits.request_timeout_seconds
        self._timeout = min(120.0, max(0.1, timeout)) if math.isfinite(timeout) else 30.0
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=_tls_context()),
        )

    def describe_image(self, path: str | Path) -> str:
        return self._describe_image(path, _CAPTION_PROMPT)

    def analyze_image(self, path: str | Path) -> tuple[str, tuple[str, ...]]:
        """Return a searchable caption and independent tags from one request."""
        return parse_image_analysis(self._describe_image(path, TAGGED_CAPTION_PROMPT))

    def _describe_image(self, path: str | Path, prompt: str) -> str:
        self._vision_ready()
        self._check_upload({"vision", "vision_understanding", "image", "video"})
        raw = _read_media(path, _MAX_IMAGE_BYTES)
        return self._describe(_jpeg(raw), prompt)

    def transcribe(self, path: str | Path) -> tuple[TranscriptSpan, ...]:
        self._asr_ready()
        self._check_upload({"asr"})
        suffix = Path(path).suffix.lower()
        if suffix not in _ASR_FORMATS:
            raise MediaInferenceError("音频格式不受支持。请先转为 WAV。")
        if self._kind == "volcengine_plan_asr" and suffix not in {".wav", ".mp3", ".ogg"}:
            raise MediaInferenceError("此语音转文字服务需要 WAV、MP3 或 OGG 格式。")
        return self._transcribe(_read_media(path, _MAX_AUDIO_BYTES), suffix)

    def test(self, capability: str) -> str:
        """Exercise the configured protocol with generated media or a short prompt."""
        if capability in {"vision", "vision_understanding"}:
            self._vision_ready()
            image = io.BytesIO()
            Image.new("RGB", (64, 64), "red").save(image, format="JPEG")
            self._describe(image.getvalue(), "What color is this square?")
            return "视觉模型测试成功"
        if capability == "asr":
            self._asr_ready()
            self._transcribe(_probe_wav(), ".wav")
            return "语音转文字接口测试成功"
        if capability == "language":
            self._vision_ready()
            self._describe(None, "Reply with OK.")
            return "语言模型测试成功"
        if capability == "embedding" and self.supports(self._kind, capability):
            self._require_model()
            payload = self._request("embeddings", {"model": self._model, "input": "Topos"})
            rows = payload.get("data")
            vector = rows[0].get("embedding") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
            if not isinstance(vector, list) or not vector:
                raise MediaInferenceError("服务未返回有效向量。")
            return "向量化模型测试成功"
        raise MediaInferenceConfigurationError("当前协议不支持测试所选能力。")

    def list_models(self) -> list[str]:
        if self._kind == "ollama":
            path = self._base_path().removesuffix("/v1")
            rows = self._request("", path=f"{path}/api/tags").get("models")
            field = "name"
        elif self.supports(self._kind, "models"):
            rows = self._request("models").get("data")
            field = "id"
        else:
            raise MediaInferenceConfigurationError("当前协议没有可用的模型列表接口。")
        if not isinstance(rows, list):
            raise MediaInferenceError("服务未返回有效的模型列表。")
        models: list[str] = []
        for row in rows:
            value = row.get(field) if isinstance(row, dict) else None
            if isinstance(value, str) and value.strip() and len(value) <= 512 and value.isprintable():
                models.append(value.strip())
        return sorted(set(models), key=str.casefold)

    def _vision_ready(self) -> None:
        if not self.supports(self._kind, "vision"):
            raise MediaInferenceConfigurationError("当前协议不支持图片理解或对话。")
        self._require_model()

    def _asr_ready(self) -> None:
        if not self.supports(self._kind, "asr"):
            raise MediaInferenceConfigurationError("当前协议不支持语音转文字。")
        self._require_model()

    def _require_model(self) -> None:
        if not self._model or len(self._model) > 512 or not self._model.isprintable():
            raise MediaInferenceConfigurationError("请设置有效的模型 ID。")

    def _check_upload(self, capability: set[str]) -> None:
        profile = self._profile
        if not capability.intersection((*profile.capabilities, *profile.modalities)):
            raise MediaInferenceConfigurationError("所选服务未声明此素材分析能力。")
        if not self.is_loopback and not profile.allows_media_upload:
            raise MediaInferenceConfigurationError("请在所选服务的设置中允许上传素材。")

    def _describe(self, image: bytes | None, prompt: str) -> str:
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        encoded = base64.b64encode(image).decode("ascii") if image else ""
        data_url = f"data:image/jpeg;base64,{encoded}"
        if self._kind in _ANTHROPIC_KINDS:
            if encoded:
                parts.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": encoded,
                }})
            payload = self._request("messages", {
                "model": self._model, "max_tokens": 300,
                "messages": [{"role": "user", "content": parts}],
            })
            answer = _text_blocks(payload.get("content"), "text")
        elif self._kind in _RESPONSES_KINDS:
            parts[0]["type"] = "input_text"
            if encoded:
                parts.append({"type": "input_image", "image_url": data_url})
            payload = self._request("responses", {
                "model": self._model, "store": False, "max_output_tokens": 512,
                "input": [{"role": "user", "content": parts}],
            })
            messages = payload.get("output")
            answer = "\n".join(
                _text_blocks(item.get("content"), "output_text")
                for item in messages if isinstance(item, dict) and item.get("type") == "message"
            ).strip() if isinstance(messages, list) else ""
        else:
            if encoded:
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
            payload = self._request("chat/completions", {
                "model": self._model, "stream": False,
                "messages": [{"role": "user", "content": parts if encoded else prompt}],
            })
            choices = payload.get("choices")
            message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
            answer = _text_blocks(message.get("content"), "text") if isinstance(message, dict) else ""
        if not answer:
            raise MediaInferenceError("模型未返回有效文本。")
        return answer[:8000]

    def _transcribe(self, audio: bytes, suffix: str) -> tuple[TranscriptSpan, ...]:
        audio_duration = _wav_seconds(audio, suffix)
        if self._kind == "volcengine_plan_asr":
            resource = self._model
            if resource in {"volc.seedasr.sauc.duration", "volc.bigasr.sauc.duration"}:
                resource = _VOLC_RESOURCE
            if not resource.isascii():
                raise MediaInferenceConfigurationError("语音服务资源 ID 无效。")
            path = self._endpoint.path.rstrip("/") or f"/{_VOLC_FLASH}"
            payload = self._request("", {
                "user": {"uid": "topos"},
                "audio": {"data": base64.b64encode(audio).decode("ascii"), "format": suffix[1:]},
                "request": {"model_name": "bigmodel", "show_utterances": True},
            }, path=path, extra_headers={
                "X-Api-Resource-Id": resource, "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Api-Sequence": "-1",
            })
            result = payload.get("result")
            if not isinstance(result, dict):
                raise MediaInferenceError("服务未返回有效转录结果。")
            info = payload.get("audio_info")
            duration = _seconds(info.get("duration"), 1000.0) if isinstance(info, dict) else None
            return _transcript_spans(result, audio_duration or duration, milliseconds=True)

        model = self._model.rsplit("/", 1)[-1].lower()
        fields = {"model": self._model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
        if model.startswith("gpt-4o-transcribe-diarize"):
            fields = {"model": self._model, "response_format": "diarized_json", "chunking_strategy": "auto"}
        elif model.startswith(("gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe")):
            fields = {"model": self._model, "response_format": "json"}
        boundary = f"topos-{uuid.uuid4().hex}"
        payload = self._request("audio/transcriptions", _multipart(fields, audio, suffix, boundary), extra_headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        return _transcript_spans(payload, audio_duration or _seconds(payload.get("duration")))

    def _base_path(self) -> str:
        base = self._endpoint.path.rstrip("/")
        for suffix in _API_ROUTES:
            if base.endswith(f"/{suffix}"):
                base = base.removesuffix(f"/{suffix}")
                break
        if self._kind == "ollama":
            base = base.removesuffix("/api/tags").removesuffix("/api")
        return base

    def _request(
        self, route: str, body: dict[str, Any] | bytes | None = None, *, path: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if path is None:
            base = self._base_path()
            if self._kind == "ollama" and not base.endswith("/v1"):
                base += "/v1"
            path = f"{base or '/v1'}/{route}"
        headers = {"Accept": "application/json"}
        if self._kind in _ANTHROPIC_KINDS:
            headers["Anthropic-Version"] = "2023-06-01"
            if self._api_key:
                headers["X-Api-Key"] = self._api_key
        elif self._api_key:
            if self._kind == "volcengine_plan_asr":
                headers["X-Api-Key"] = self._api_key
            else:
                headers["Authorization"] = f"Bearer {self._api_key}"
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=True).encode("ascii")
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            self._endpoint._replace(path=path).geturl(), body, headers,
            method="POST" if body is not None else "GET",
        )
        deadline = time.monotonic() + self._timeout
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if self._kind == "volcengine_plan_asr":
                    status = response.headers.get("X-Api-Status-Code")
                    if status == "20000003":
                        return {"result": {"text": ""}}
                    if status not in (None, "20000000"):
                        raise MediaInferenceError("语音转文字服务拒绝了请求，请检查资源 ID 和权限。")
                raw = bytearray()
                while len(raw) <= _MAX_RESPONSE_BYTES:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    part = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - len(raw)))
                    if not part:
                        break
                    raw.extend(part)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise MediaInferenceError(f"模型服务请求失败（HTTP {exc.code}）。") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise MediaInferenceError("无法连接模型服务，或请求已超时。") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise MediaInferenceError("模型服务响应超出大小限制。")
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise MediaInferenceError("模型服务返回了无效的 JSON。") from None
        if not isinstance(data, dict) or data.get("error"):
            raise MediaInferenceError("模型服务返回了错误或无法识别的响应。")
        return data


def validate_media_endpoint(endpoint: str, api_key: str = "") -> tuple[SplitResult, bool]:
    """Validate the configured recipient and identify same-host destinations."""
    raw = endpoint.strip()
    if not raw.isascii() or not raw.isprintable() or "\\" in raw or " " in raw:
        raise MediaInferenceConfigurationError("请填写有效的模型服务 URL。")
    if api_key and (not api_key.isascii() or not api_key.isprintable()):
        raise MediaInferenceConfigurationError("API Key 的格式无效。")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise MediaInferenceConfigurationError("请填写有效的模型服务 URL。") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or port == 0
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise MediaInferenceConfigurationError("请填写不含凭据、查询参数或片段的 HTTP(S) 服务 URL。")
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += ":" + parsed.netloc.rpartition(":")[2]
    if parsed.netloc.lower() != authority.lower() or any(character in host for character in "/@%"):
        raise MediaInferenceConfigurationError("请填写有效的模型服务 URL。")
    loopback, private_ip = _endpoint_network(parsed)
    if parsed.scheme == "http" and not loopback and (not private_ip or api_key):
        raise MediaInferenceConfigurationError("公共服务器或需要 API Key 的局域网服务必须使用 HTTPS。")
    return parsed, loopback


def is_local_media_endpoint(parsed: SplitResult) -> bool:
    """Report whether a validated URL uses loopback or a numeric LAN address."""
    return any(_endpoint_network(parsed))


def _endpoint_network(parsed: SplitResult) -> tuple[bool, bool]:
    address = (parsed.hostname or "").lower().removesuffix(".")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    loopback = address == "localhost" or (ip is not None and ip.is_loopback)
    private_ip = ip is not None and any(ip in network for network in _PRIVATE_NETWORKS)
    return loopback, private_ip


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        return ssl.create_default_context()


def _read_media(path: str | Path, limit: int) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MediaInferenceError("素材不是普通文件。")
            if info.st_size > limit:
                raise MediaInferenceError("素材文件超出模型请求的大小限制。")
            raw = stream.read(limit + 1)
    except (OSError, ValueError):
        raise MediaInferenceError("无法读取素材文件。") from None
    if not raw or len(raw) > limit:
        raise MediaInferenceError("素材文件为空或超出模型请求的大小限制。")
    return raw


def _jpeg(raw: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > _MAX_IMAGE_PIXELS:
                raise MediaInferenceError("图片分辨率超出模型请求的限制。")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1600, 1600))
            rgb = image.convert("RGB")
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=88)
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise MediaInferenceError("无法解码此图片。") from None


def _text_blocks(content: object, block_type: str) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"].strip() for item in content if isinstance(item, dict)
        and item.get("type") == block_type and isinstance(item.get("text"), str)
    ).strip()


def _multipart(fields: dict[str, str], audio: bytes, suffix: str, boundary: str) -> bytes:
    chunks = [
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n').encode("utf-8")
        for field, value in fields.items()
    ]
    return b"".join([
        *chunks,
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio{suffix}"\r\n'
         f"Content-Type: {_ASR_FORMATS[suffix]}\r\n\r\n").encode("ascii"),
        audio, f"\r\n--{boundary}--\r\n".encode("ascii"),
    ])


def _probe_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0" * 32000)
    return output.getvalue()


def _wav_seconds(audio: bytes, suffix: str) -> float | None:
    if suffix != ".wav":
        return None
    try:
        with wave.open(io.BytesIO(audio), "rb") as stream:
            return stream.getnframes() / stream.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def _seconds(value: object, scale: float = 1.0) -> float | None:
    if not isinstance(value, (float, int, str)) or isinstance(value, bool):
        return None
    try:
        number = float(value) / scale
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def _transcript_spans(
    payload: dict[str, Any], duration: float | None, *, milliseconds: bool = False,
) -> tuple[TranscriptSpan, ...]:
    rows = payload.get("utterances" if milliseconds else "segments")
    result: list[TranscriptSpan] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"].strip():
            continue
        start = _seconds(row.get("start_time"), 1000.0) if milliseconds else _seconds(row.get("start"))
        end = _seconds(row.get("end_time"), 1000.0) if milliseconds else _seconds(row.get("end"))
        if start is None or end is None:
            raise MediaInferenceError("语音转文字服务返回的片段时间码无效。")
        if duration is not None:
            end = min(end, duration)
        if start >= end:
            raise MediaInferenceError("语音转文字服务返回的片段时间范围无效。")
        result.append(TranscriptSpan(start, end, row["text"].strip()))
    if result:
        return tuple(sorted(result, key=lambda span: (span.start_seconds, span.end_seconds)))
    text = payload.get("text")
    if isinstance(text, str):
        if not text.strip():
            return ()
        if duration is None or duration <= 0.0:
            raise MediaInferenceError("语音转文字服务未返回时间码，无法确定音频时长。")
        return (TranscriptSpan(0.0, duration, text.strip()),)
    if isinstance(rows, list) and not rows:
        return ()
    raise MediaInferenceError("语音转文字服务未返回有效转录结果。")
