"""LLM client：统一封装 OpenAI / Ollama / Claude（Anthropic）三种协议。

设计目标（TAEP §11.4 / §7 transport-neutral）：

- 一个统一的 ``LLMClient`` 接口（``complete`` / ``test_connection``），由
  ``make_client(profile, credential_store)`` 根据 ``provider_kind`` 工厂分发。
- OpenAI 与 Ollama 共用 ``OpenAICompatClient``（Ollama 原生提供
  ``/v1/chat/completions`` 兼容端点）；Claude 用独立的 ``AnthropicClient``
  （``/v1/messages`` + ``x-api-key`` + ``anthropic-version`` 头）。
- 用标准库 ``urllib.request``（与项目现有 HTTP 风格一致，无需为 client 加
  httpx/openai SDK 依赖）；调用方负责把 ``complete`` 放到后台线程（见
  ``ai_edit_controller._PlanWorker``）。

请求/响应格式见各 client 文档字符串。所有错误统一抛 ``LLMError``（带可读原因）。
"""

from __future__ import annotations

import json
import logging
import re
import http.client
import os
import socket
import time
import uuid
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    CredentialStore,
)
from src.features.edit.ai_editing.llm_cost import CostLedger, make_cost_record
from src.features.edit.ai_editing.privacy_guard import redact_text

logger = logging.getLogger(__name__)

# 默认请求超时（秒）。规划请求可能较长，但避免无限挂起。
_DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class LLMRequestPolicy:
    """Transport policy shared by all HTTP-compatible model clients."""

    timeout_seconds: float = _DEFAULT_TIMEOUT
    max_retries: int = 2
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0
    retry_statuses: tuple[int, ...] = (408, 409, 425, 429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("backoff values cannot be negative")


@dataclass(frozen=True)
class LLMUsage:
    request_id: str
    provider_kind: str
    model_id: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_units: float | None = None
    estimated: bool = False


class LLMError(RuntimeError):
    """Stable, user-readable LLM error with retry and audit metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "LLM_REQUEST_FAILED",
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


# ---------------------------------------------------------------------- #
# 统一接口
# ---------------------------------------------------------------------- #


class LLMClient(Protocol):
    """统一的 LLM 调用接口。"""

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """发送对话，返回助手回复的纯文本。

        Args:
            messages: OpenAI 风格的 ``[{role, content}]``（system/user/assistant）。
            json_mode: 提示后端尽量返回合法 JSON（用于结构化解析）。
            temperature: 采样温度。
            max_tokens: 最多生成多少 token（None 用后端默认）。
        """
        ...

    def test_connection(self) -> None:
        """发一个最小请求验证 endpoint/key/model 可用；失败抛 ``LLMError``。"""
        ...

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], None] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """流式返回文本；断流必须抛出带 code 的 ``LLMError``。"""
        ...


# ---------------------------------------------------------------------- #
# 工具：HTTP 请求
# ---------------------------------------------------------------------- #


def _is_max_tokens_rejection(exc: Exception) -> bool:
    """True when the provider rejected the request because of ``max_tokens``.

    Generative HTML asks for the model's full output budget, but providers cap
    ``max_tokens`` differently (8k/16k/64k/128k...).  A request above a cap is
    answered with HTTP 400 naming ``max_tokens``; that is a tunable value, not
    a hard failure, so callers may retry once on the provider's real cap.
    """
    text = str(exc).lower()
    if "max_tokens" not in text and "max output tokens" not in text:
        return False
    return getattr(exc, "status_code", None) not in (401, 403)


def _max_tokens_cap_from_rejection(message: str, requested: int) -> int:
    """Best-effort provider cap parsed from a max_tokens rejection message.

    Providers phrase caps differently ("max_tokens is too large: 65536 > 8192",
    "max_tokens must be <= 8192", ...).  Take the number that follows the
    requested value; fall back to half the request.  Retrying on the parsed cap
    keeps a generous output budget instead of dropping to a tiny server default
    (e.g. 4k) that would silently truncate a rich webpage mid-document.
    """
    match = re.search(rf"\b{requested}\b\D{{0,12}}(\d{{3,}})", message)
    if match:
        cap = int(match.group(1))
        if 1024 <= cap < requested:
            return cap
    return max(1024, requested // 2)


def _post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    policy: LLMRequestPolicy | None = None,
) -> dict[str, Any]:
    """POST JSON with bounded retries and stable error codes."""
    policy = policy or LLMRequestPolicy()
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(policy.max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=policy.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"响应不是合法 JSON：{raw[:200]}", code="LLM_INVALID_RESPONSE"
                ) from exc
            if not isinstance(value, dict):
                raise LLMError("响应 JSON 顶层不是对象", code="LLM_INVALID_RESPONSE")
            return value
        except urllib.error.HTTPError as exc:
            detail = redact_text(exc.read().decode("utf-8", errors="replace")[:500])
            retryable = exc.code in policy.retry_statuses
            if retryable and attempt < policy.max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                _sleep_before_retry(policy, attempt, retry_after)
                continue
            if exc.code == 401:
                raise LLMError(
                    f"鉴权失败（HTTP 401），请检查 API key。{detail}",
                    code="LLM_AUTH_FAILED", status_code=exc.code,
                ) from exc
            if exc.code == 403:
                raise LLMError(
                    f"访问被拒绝（HTTP 403）。{detail}",
                    code="LLM_PERMISSION_DENIED", status_code=exc.code,
                ) from exc
            if exc.code == 404:
                raise LLMError(
                    f"端点不存在（HTTP 404）：{url}。请检查 URL/模型名。{detail}",
                    code="LLM_ENDPOINT_NOT_FOUND", status_code=exc.code,
                ) from exc
            raise LLMError(
                f"HTTP {exc.code}: {detail or exc.reason}",
                code="LLM_RATE_LIMITED" if exc.code == 429 else "LLM_HTTP_ERROR",
                retryable=retryable, status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            if attempt < policy.max_retries:
                _sleep_before_retry(policy, attempt, None)
                continue
            reason = getattr(exc, "reason", None) or str(exc)
            raise LLMError(
                f"无法连接到 {url}：{reason}", code="LLM_NETWORK_ERROR", retryable=True
            ) from exc

    raise LLMError("请求重试次数耗尽", code="LLM_RETRY_EXHAUSTED", retryable=True)


def _post_multipart_file(
    url: str,
    headers: dict[str, str],
    file_path: str,
    *,
    filename: str,
    mime: str,
    purpose: str = "user_data",
    policy: LLMRequestPolicy | None = None,
) -> dict[str, Any]:
    """以流式 multipart 方式上传本地文件，避免把视频整体读进内存。"""
    policy = policy or LLMRequestPolicy()
    try:
        size = os.path.getsize(file_path)
    except OSError as exc:
        raise LLMError(
            f"无法读取附件：{filename}", code="LLM_FILE_NOT_FOUND",
        ) from exc
    boundary = f"----ToposColor{uuid.uuid4().hex}"
    safe_filename = (filename or os.path.basename(file_path) or "attachment").replace(
        "\\", "_",
    ).replace('"', "'")
    prefix = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"purpose\"\r\n\r\n"
        f"{purpose}\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{safe_filename}\"\r\n"
        f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    parsed = urlsplit(url)
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query

    for attempt in range(policy.max_retries + 1):
        conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        try:
            conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(parsed.netloc, timeout=policy.timeout_seconds)
            conn.putrequest("POST", request_path)
            conn.putheader("Authorization", headers.get("Authorization", ""))
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(len(prefix) + size + len(suffix)))
            conn.endheaders()
            conn.send(prefix)
            with open(file_path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
            conn.send(suffix)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"文件上传响应不是合法 JSON：{raw[:200]}",
                        code="LLM_INVALID_RESPONSE",
                    ) from exc
                if not isinstance(value, dict):
                    raise LLMError("文件上传响应格式无效", code="LLM_INVALID_RESPONSE")
                return value
            retryable = response.status in policy.retry_statuses
            if retryable and attempt < policy.max_retries:
                _sleep_before_retry(policy, attempt, response.getheader("Retry-After"))
                continue
            detail = redact_text(raw[:500])
            raise LLMError(
                f"文件上传失败（HTTP {response.status}）：{detail or response.reason}",
                code="LLM_RATE_LIMITED" if response.status == 429 else "LLM_FILE_UPLOAD_FAILED",
                retryable=retryable,
                status_code=response.status,
            )
        except LLMError:
            raise
        except (OSError, socket.timeout, TimeoutError) as exc:
            if attempt < policy.max_retries:
                _sleep_before_retry(policy, attempt, None)
                continue
            raise LLMError(
                f"上传附件失败：{filename}：{exc}",
                code="LLM_NETWORK_ERROR",
                retryable=True,
            ) from exc
        finally:
            if conn is not None:
                conn.close()
    raise LLMError("文件上传重试次数耗尽", code="LLM_RETRY_EXHAUSTED", retryable=True)


def _get_json(
    url: str,
    headers: dict[str, str],
    *,
    policy: LLMRequestPolicy | None = None,
) -> dict[str, Any]:
    """GET JSON，用于轮询 File API 的处理状态。"""
    policy = policy or LLMRequestPolicy()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=policy.timeout_seconds) as resp:
            value = json.loads(resp.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise LLMError("文件状态响应格式无效", code="LLM_INVALID_RESPONSE")
        return value
    except urllib.error.HTTPError as exc:
        detail = redact_text(exc.read().decode("utf-8", errors="replace")[:500])
        raise LLMError(
            f"查询文件状态失败（HTTP {exc.code}）：{detail}",
            code="LLM_FILE_UPLOAD_FAILED", status_code=exc.code,
        ) from exc
    except (urllib.error.URLError, OSError, socket.timeout, TimeoutError) as exc:
        raise LLMError(
            f"无法查询文件状态：{exc}", code="LLM_NETWORK_ERROR", retryable=True,
        ) from exc


def _sleep_before_retry(policy: LLMRequestPolicy, attempt: int, retry_after: str | None) -> None:
    """Bounded sleep; Retry-After is honored only when it is numeric."""
    delay = policy.backoff_seconds * (2 ** attempt)
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    time.sleep(min(delay, policy.max_backoff_seconds))


def _usage_from_response(
    data: dict[str, Any], profile: AiProviderProfile,
) -> LLMUsage:
    raw = data.get("usage")
    usage = raw if isinstance(raw, dict) else {}
    prompt = _optional_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _optional_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _optional_int(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    cost = usage.get("cost_units", usage.get("cost"))
    try:
        cost_value = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_value = None
    return LLMUsage(
        request_id=str(data.get("id") or uuid.uuid4()),
        provider_kind=profile.provider_kind,
        model_id=profile.model_id,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_units=cost_value,
        estimated=cost_value is None,
    )


def _extract_choice_content(data: dict[str, Any]) -> tuple[Any, str]:
    """Return ``(content, finish_reason)`` from a chat-completion response.

    ``content`` preserves the raw provider value (typically ``str`` or
    ``None``); ``finish_reason`` defaults to ``""`` when absent.
    """
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message["content"]
        finish_reason = str(choice.get("finish_reason", "") or "")
        return content, finish_reason
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"无法解析 OpenAI 响应：{data}", code="LLM_INVALID_RESPONSE"
        ) from exc


def _stream_sse_text(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    policy: LLMRequestPolicy,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Consume OpenAI/Anthropic-compatible SSE without blocking the UI thread."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    for attempt in range(policy.max_retries + 1):
        received = False
        try:
            pieces: list[str] = []
            usage: dict[str, Any] = {}
            finish_reason = ""
            with urllib.request.urlopen(request, timeout=policy.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        break
                    try:
                        payload = json.loads(payload_text)
                    except json.JSONDecodeError:
                        raise LLMError(
                            "流式响应包含非法 JSON", code="LLM_INVALID_RESPONSE"
                        )
                    if not isinstance(payload, dict):
                        continue
                    if isinstance(payload.get("usage"), dict):
                        usage = dict(payload["usage"])
                    content = ""
                    choices = payload.get("choices")
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        first_choice = choices[0]
                        fr = first_choice.get("finish_reason")
                        if fr:
                            finish_reason = str(fr)
                        message = first_choice.get("delta", first_choice.get("message", {}))
                        if isinstance(message, dict):
                            content = str(message.get("content", "") or "")
                    if not content and isinstance(payload.get("delta"), dict):
                        content = str(payload["delta"].get("text", "") or "")
                    if content:
                        received = True
                        pieces.append(content)
                        if on_delta is not None:
                            on_delta(content)
            return "".join(pieces), usage, finish_reason
        except urllib.error.HTTPError as exc:
            retryable = exc.code in policy.retry_statuses
            detail = redact_text(exc.read().decode("utf-8", errors="replace")[:500])
            if retryable and not received and attempt < policy.max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                _sleep_before_retry(policy, attempt, retry_after)
                continue
            raise LLMError(
                f"流式请求 HTTP {exc.code}: {detail or exc.reason}",
                code="LLM_RATE_LIMITED" if exc.code == 429 else "LLM_HTTP_ERROR",
                retryable=retryable, status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            if not received and attempt < policy.max_retries:
                _sleep_before_retry(policy, attempt, None)
                continue
            raise LLMError(
                f"流式请求断开：{getattr(exc, 'reason', None) or str(exc)}",
                code="LLM_STREAM_INTERRUPTED", retryable=True,
            ) from exc
    raise LLMError("流式请求重试次数耗尽", code="LLM_RETRY_EXHAUSTED", retryable=True)


def _stream_responses_text(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    policy: LLMRequestPolicy,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Consume Responses API SSE text deltas.

    Responses uses typed events rather than Chat Completions' ``choices``
    envelope.  In particular, visible text arrives as
    ``response.output_text.delta`` with the chunk in ``delta``; usage is
    normally attached to ``response.completed``.  Keeping this parser
    transport-local lets the UI receive the same on-delta contract regardless
    of whether the active profile uses Chat Completions or Responses.
    """
    data = json.dumps({**body, "stream": True}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            **headers,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    for attempt in range(policy.max_retries + 1):
        received = False
        try:
            pieces: list[str] = []
            usage: dict[str, Any] = {}
            with urllib.request.urlopen(request, timeout=policy.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        break
                    try:
                        payload = json.loads(payload_text)
                    except json.JSONDecodeError as exc:
                        raise LLMError(
                            "Responses 流式响应包含非法 JSON",
                            code="LLM_INVALID_RESPONSE",
                        ) from exc
                    if not isinstance(payload, dict):
                        continue
                    event_type = str(payload.get("type", "") or "")
                    content = ""
                    if event_type == "response.output_text.delta":
                        content = str(payload.get("delta", "") or "")
                    elif isinstance(payload.get("delta"), str):
                        # Some compatible gateways omit the event type while
                        # retaining the Responses ``delta`` shape.
                        content = str(payload.get("delta") or "")
                    response_payload = payload.get("response")
                    if isinstance(response_payload, dict):
                        response_usage = response_payload.get("usage")
                        if isinstance(response_usage, dict):
                            usage = dict(response_usage)
                    event_usage = payload.get("usage")
                    if isinstance(event_usage, dict):
                        usage = dict(event_usage)
                    if content:
                        received = True
                        pieces.append(content)
                        if on_delta is not None:
                            on_delta(content)
            return "".join(pieces), usage
        except urllib.error.HTTPError as exc:
            retryable = exc.code in policy.retry_statuses
            detail = redact_text(exc.read().decode("utf-8", errors="replace")[:500])
            if retryable and not received and attempt < policy.max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                _sleep_before_retry(policy, attempt, retry_after)
                continue
            raise LLMError(
                f"Responses 流式请求 HTTP {exc.code}: {detail or exc.reason}",
                code="LLM_RATE_LIMITED" if exc.code == 429 else "LLM_HTTP_ERROR",
                retryable=retryable,
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            if not received and attempt < policy.max_retries:
                _sleep_before_retry(policy, attempt, None)
                continue
            raise LLMError(
                f"Responses 流式请求断开：{getattr(exc, 'reason', None) or str(exc)}",
                code="LLM_STREAM_INTERRUPTED",
                retryable=True,
            ) from exc
    raise LLMError(
        "Responses 流式请求重试次数耗尽",
        code="LLM_RETRY_EXHAUSTED",
        retryable=True,
    )


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _resolve_api_endpoint(endpoint: str, route: str, *, default_version: str = "v1") -> str:
    """Resolve a base or complete provider URL without duplicating routes.

    Preferences accepts both ``https://host/v1`` and a complete endpoint such
    as ``https://host/v1/chat/completions``.  Versioned bases such as GLM
    Coding's ``.../v4`` receive only the route component.
    """
    raw = str(endpoint or "").strip()
    if not raw:
        return raw
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    route = route.strip("/")
    route_lower = route.lower()
    if lowered.endswith("/" + route_lower) or (
        route_lower == "chat/completions" and lowered.endswith("/completions")
    ):
        resolved_path = path or "/" + route
    else:
        segments = [segment for segment in path.split("/") if segment]
        last = segments[-1].lower() if segments else ""
        if not path:
            resolved_path = f"/{default_version}/{route}"
        elif last.startswith("v") and last[1:].replace(".", "").isdigit():
            resolved_path = f"{path}/{route}"
        else:
            resolved_path = f"{path}/{route}"
    return urlunsplit((parsed.scheme, parsed.netloc, resolved_path, parsed.query, parsed.fragment))


def _openai_chat_endpoint(endpoint: str) -> str:
    return _resolve_api_endpoint(endpoint, "chat/completions")


def _anthropic_messages_endpoint(endpoint: str) -> str:
    return _resolve_api_endpoint(endpoint, "messages")


def _responses_endpoint(endpoint: str) -> str:
    return _resolve_api_endpoint(endpoint, "responses")


def _files_endpoint(endpoint: str) -> str:
    """Resolve the provider base URL to its File API collection endpoint."""
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/responses"):
        path = path[: -len("/responses")]
    base = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
    return _resolve_api_endpoint(base, "files")


def _file_endpoint(endpoint: str, file_id: str) -> str:
    return _files_endpoint(endpoint).rstrip("/") + "/" + str(file_id).strip()


def _local_document_text(file_path: str, filename: str) -> str | None:
    """安全地提取常见文本/Office 文档内容，作为 File API 的兼容回退。

    DOCX/XLSX/PPTX 都是 ZIP+XML 容器；这里只读取文本节点，不执行宏、不解析
    外部链接。PDF、视频、音频仍走方舟 File API 的原生多模态输入。
    """
    suffix = os.path.splitext(filename or file_path)[1].lower()
    text_suffixes = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".xml", ".html"}
    office_suffixes = {".docx", ".xlsx", ".pptx"}
    if suffix not in text_suffixes | office_suffixes:
        return None
    max_chars = 120_000
    try:
        if suffix in text_suffixes:
            with open(file_path, "rb") as handle:
                raw = handle.read(max_chars * 4)
            return raw.decode("utf-8", errors="replace")[:max_chars]
        with zipfile.ZipFile(file_path) as archive:
            if suffix == ".docx":
                names = ["word/document.xml"]
            elif suffix == ".pptx":
                names = sorted(
                    name for name in archive.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                )
            else:
                names = [
                    name for name in archive.namelist()
                    if name.startswith("xl/worksheets/") and name.endswith(".xml")
                ]
                shared: list[str] = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    shared = ["".join(node.itertext()) for node in root]
            chunks: list[str] = []
            for name in names:
                root = ET.fromstring(archive.read(name))
                if suffix == ".xlsx":
                    # 保留表格单元格边界；sharedStrings 的引用值也一并展开。
                    for cell in root.iter():
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        value = next(
                            (child for child in cell if child.tag.rsplit("}", 1)[-1] == "v"),
                            None,
                        )
                        if value is None or value.text is None:
                            continue
                        cell_value = value.text
                        if cell.attrib.get("t") == "s":
                            try:
                                cell_value = shared[int(cell_value)]
                            except (ValueError, IndexError):
                                pass
                        chunks.append(cell_value)
                else:
                    chunks.append(" ".join(root.itertext()))
                if sum(len(item) for item in chunks) >= max_chars:
                    break
            return "\n".join(chunks)[:max_chars]
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return None


# ---------------------------------------------------------------------- #
# OpenAI 兼容（同时服务 OpenAI 与 Ollama）
# ---------------------------------------------------------------------- #


class OpenAICompatClient:
    """OpenAI 兼容协议 client（``/v1/chat/completions``）。

    同时服务：
    - ``provider_kind == "openai"``：endpoint 如 ``https://api.openai.com``，
      鉴权 ``Authorization: Bearer {api_key}``。
    - ``provider_kind == "ollama"``：endpoint 如 ``http://localhost:11434``，
      无需 key（Ollama 默认不鉴权，传 ``Bearer`` 会被忽略）；走其内置的
      ``/v1/chat/completions`` 兼容端点。
    """

    def __init__(
        self,
        profile: AiProviderProfile,
        api_key: str,
        *,
        policy: LLMRequestPolicy | None = None,
        cost_ledger: CostLedger | None = None,
        cost_budget: float | None = None,
    ) -> None:
        self._profile = profile
        self._api_key = api_key
        self._policy = policy or LLMRequestPolicy()
        self._cost_ledger = cost_ledger
        self._cost_budget = cost_budget
        self._last_usage: LLMUsage | None = None
        if not profile.endpoint:
            raise LLMError("endpoint（URL）未配置")
        if not profile.model_id:
            raise LLMError("model_id（模型 ID）未配置")

    def _headers(self) -> dict[str, str]:
        # OpenAI 与 Ollama 都接受 Bearer 头；Ollama 忽略它
        return {"Authorization": f"Bearer {self._api_key}"}

    def _request_thinking_options(self) -> dict[str, Any]:
        """Return provider-specific reasoning options for structured requests.

        Some reasoning models spend the *entire* bounded completion budget in
        their thinking channel, leaving ``message.content`` empty even though
        ``finish_reason`` is ``length``.  This breaks every structured-output
        caller (HTML generation, JSON-mode, tool calls).  Verified empirically:
        glm-5.2 burns 5978/6000 tokens on reasoning for an HTML design prompt
        and returns zero visible content.

        Disabling thinking for these models is strictly beneficial: the chat
        quality stays good (often more concise), latency drops ~5×, and token
        waste drops ~10×.  Kept to models where the problem is confirmed so
        unrelated OpenAI-compatible providers retain their defaults.
        """
        model = self._profile.model_id.lower()
        if model.startswith("deepseek-v4-flash"):
            return {"type": "disabled"}
        # glm-5.x reasoning models: reasoning eats the whole max_tokens quota
        # on HTML/JSON generation, producing empty content (finish=length).
        if model.startswith("glm-5") or model.startswith("glm-4.6"):
            return {"type": "disabled"}
        return {}

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        self._check_budget()
        thinking = self._request_thinking_options()
        # Step down to the provider's real cap (parsed from the rejection) or
        # half the request; the budget strictly decreases so this terminates.
        budget = max_tokens
        while True:
            try:
                content, finish_reason = self._complete_request(
                    messages, thinking=thinking, json_mode=json_mode,
                    temperature=temperature, max_tokens=budget,
                )
                break
            except LLMError as exc:
                if budget is None or not _is_max_tokens_rejection(exc):
                    raise
                next_budget = _max_tokens_cap_from_rejection(str(exc), budget)
                if next_budget >= budget:
                    raise
                budget = next_budget
        # Reasoning models may burn the entire max_tokens quota on their
        # thinking channel, returning empty content with finish_reason=
        # "length".  When the prefix list did not already disable thinking,
        # retry once with it disabled so callers always receive visible
        # content regardless of model name (defensive against new models).
        if (
            not content
            and finish_reason == "length"
            and thinking != {"type": "disabled"}
        ):
            content, _ = self._complete_request(
                messages, thinking={"type": "disabled"}, json_mode=json_mode,
                temperature=temperature, max_tokens=max_tokens,
            )
        # 协议保证返回 str：provider 返回 null/非文本 content（例如
        # finish_reason="content_filter" 或工具偏置）时必须抛类型化错误，
        # 而不是让 None 穿透给按 str 使用返回值的调用方。
        if not isinstance(content, str):
            raise LLMError(
                f"provider returned no text content (finish_reason={finish_reason!r})",
                code="LLM_INVALID_RESPONSE",
            )
        return content

    def _complete_request(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: dict[str, Any],
        json_mode: bool,
        temperature: float,
        max_tokens: int | None,
    ) -> tuple[Any, str]:
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            # OpenAI / Ollama 都支持 response_format=json_object
            body["response_format"] = {"type": "json_object"}
        if thinking:
            body["thinking"] = thinking
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        url = _openai_chat_endpoint(self._profile.endpoint)
        data = _post_json(url, self._headers(), body, policy=self._policy)
        self._last_usage = _usage_from_response(data, self._profile)
        self._record_usage("completion")
        return _extract_choice_content(data)

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], None] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        self._check_budget()
        thinking = self._request_thinking_options()
        # Same budget ladder as ``complete``: step down to the provider's real
        # cap instead of failing the generation.
        budget = max_tokens
        while True:
            try:
                text, usage, finish_reason = _stream_sse_text(
                    _openai_chat_endpoint(self._profile.endpoint),
                    self._headers(),
                    self._build_stream_body(
                        messages, thinking=thinking,
                        temperature=temperature, max_tokens=budget,
                    ),
                    policy=self._policy, on_delta=on_delta,
                )
                break
            except LLMError as exc:
                if budget is None or not _is_max_tokens_rejection(exc):
                    raise
                next_budget = _max_tokens_cap_from_rejection(str(exc), budget)
                if next_budget >= budget:
                    raise
                budget = next_budget
        self._last_usage = _usage_from_response(
            {"usage": usage, "id": uuid.uuid4().hex}, self._profile,
        )
        self._record_usage("stream_completion")
        # Same defensive retry as ``complete``: when a reasoning model burns
        # the budget on its thinking channel the stream yields no content
        # deltas and ``finish_reason`` is ``"length"``.  Retry once with
        # thinking disabled.  ``on_delta`` was never called during the empty
        # first attempt, so the retry streams cleanly from scratch.
        if (
            not text
            and finish_reason == "length"
            and thinking != {"type": "disabled"}
        ):
            text, usage, _ = _stream_sse_text(
                _openai_chat_endpoint(self._profile.endpoint),
                self._headers(),
                self._build_stream_body(
                    messages, thinking={"type": "disabled"},
                    temperature=temperature, max_tokens=max_tokens,
                ),
                policy=self._policy, on_delta=on_delta,
            )
            self._last_usage = _usage_from_response(
                {"usage": usage, "id": uuid.uuid4().hex}, self._profile,
            )
            self._record_usage("stream_completion")
        return text

    def _build_stream_body(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if thinking:
            body["thinking"] = thinking
        return body

    @property
    def last_usage(self) -> LLMUsage | None:
        return self._last_usage

    def _check_budget(self) -> None:
        if self._cost_ledger is not None and not self._cost_ledger.can_spend(self._cost_budget):
            raise LLMError("已达到 LLM 成本预算，调用已阻断", code="LLM_BUDGET_EXCEEDED")

    def _record_usage(self, request_type: str) -> None:
        if self._cost_ledger is None or self._last_usage is None:
            return
        self._cost_ledger.record(make_cost_record(
            request_id=self._last_usage.request_id,
            provider_kind=self._last_usage.provider_kind,
            model_id=self._last_usage.model_id,
            request_type=request_type,
            total_tokens=self._last_usage.total_tokens,
            cost_units=self._last_usage.cost_units,
            estimated=self._last_usage.estimated,
        ))

    def test_connection(self) -> None:
        self.complete(
            [{"role": "user", "content": "ping"}],
            max_tokens=8,
            temperature=0.0,
        )

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.6,
        max_tokens: int | None = None,
    ) -> Any:
        """Send a chat completion with native tool-calling (OpenAI tools API).

        Returns a :class:`ToolCallResponse`-compatible object with ``.text``,
        ``.tool_calls`` (tuple of objects with ``.tool_name`` / ``.call_id`` /
        ``.arguments``) and ``.finish`` (bool).  When the model burns its
        budget on reasoning (empty content, ``finish_reason="length"``) the
        same defensive retry-with-thinking-disabled from :meth:`complete`
        applies.
        """
        self._check_budget()
        thinking = self._request_thinking_options()
        response = self._tools_request(
            messages, tools, thinking=thinking,
            temperature=temperature, max_tokens=max_tokens,
        )
        # Defensive retry for reasoning models (see complete()).
        # Only retry when BOTH text AND tool_calls are empty — otherwise the
        # model produced a usable response (tool_calls are valid even with
        # empty text and finish_reason="length").
        if (
            not response.text
            and not response.tool_calls
            and response.stop_reason == "length"
            and thinking != {"type": "disabled"}
        ):
            response = self._tools_request(
                messages, tools, thinking={"type": "disabled"},
                temperature=temperature, max_tokens=max_tokens,
            )
        return response

    def _tools_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        thinking: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
    ) -> Any:
        """Single tools-API request; returns a parsed ToolCallResponse."""
        from src.features.edit.ai_editing.tool_calling_client import (
            ToolCallRequest,
            ToolCallResponse,
        )

        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": "auto",
        }
        if thinking:
            body["thinking"] = thinking
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        url = _openai_chat_endpoint(self._profile.endpoint)
        data = _post_json(url, self._headers(), body, policy=self._policy)
        self._last_usage = _usage_from_response(data, self._profile)
        self._record_usage("tool_completion")

        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = str(message.get("content", "") or "")
            finish_reason = str(choice.get("finish_reason", "") or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"无法解析 OpenAI tools 响应：{data}", code="LLM_INVALID_RESPONSE"
            ) from exc

        raw_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCallRequest] = []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            call_id = str(tc.get("id") or "")
            tool_name = str(func.get("name") or "")
            raw_args = func.get("arguments")
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    arguments = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                arguments = raw_args
            else:
                arguments = {}
            if tool_name:
                tool_calls.append(ToolCallRequest(
                    call_id=call_id, tool_name=tool_name, arguments=arguments,
                ))

        finish = not tool_calls  # No tool calls → converged
        return ToolCallResponse(
            text=content,
            tool_calls=tuple(tool_calls),
            stop_reason=finish_reason or ("tool_use" if tool_calls else "stop"),
            finish=finish,
        )


class OpenAIResponsesClient:
    """OpenAI Responses API client for providers exposing ``/responses``.

    Volcengine Agent Plan's OpenAI-compatible base URL exposes Responses at
    ``{endpoint}/responses`` rather than the legacy ``/v1/chat/completions``
    path.  Ordinary chat uses the Responses SSE stream so the UI can render
    text as it arrives; structured artifact callers can still use ``complete``
    when they need an atomic document.
    """

    def __init__(
        self,
        profile: AiProviderProfile,
        api_key: str,
        *,
        policy: LLMRequestPolicy | None = None,
        cost_ledger: CostLedger | None = None,
        cost_budget: float | None = None,
    ) -> None:
        self._profile = profile
        self._api_key = api_key
        self._policy = policy or LLMRequestPolicy()
        self._cost_ledger = cost_ledger
        self._cost_budget = cost_budget
        self._last_usage: LLMUsage | None = None
        self._uploaded_file_ids: dict[tuple[str, int, int], str] = {}
        if not profile.endpoint:
            raise LLMError("endpoint（URL）未配置")
        if not profile.model_id:
            raise LLMError("model_id（模型 ID）未配置")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        del temperature  # Responses models choose sampling internally.
        self._check_budget()
        input_items = []
        for message in messages:
            input_items.append({
                "role": str(message.get("role", "user")),
                "content": self._prepare_responses_content(message.get("content", "")),
            })
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "input": input_items,
            "reasoning": {"effort": "low"},
            "store": False,
        }
        # 协议约定 max_tokens=None 用后端默认；省略字段而不是强制 1024，
        # 否则长输出会被静默截断（AUD-13709）。
        if max_tokens is not None:
            body["max_output_tokens"] = max_tokens
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        data = _post_json(
            _responses_endpoint(self._profile.endpoint),
            self._headers(), body, policy=self._policy,
        )
        self._last_usage = _usage_from_response(data, self._profile)
        self._record_usage("completion")
        text = self._extract_text(data)
        if not text:
            raise LLMError(
                "无法解析 Responses 响应中的输出文本：任务可能因输出上限未完成",
                code="LLM_INVALID_RESPONSE",
            )
        return text

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], None] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        del temperature  # Responses models choose sampling internally.
        self._check_budget()
        input_items = []
        for message in messages:
            input_items.append({
                "role": str(message.get("role", "user")),
                "content": self._prepare_responses_content(message.get("content", "")),
            })
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "input": input_items,
            "reasoning": {"effort": "low"},
            "store": False,
        }
        # 同 complete：None 时省略 max_output_tokens，交由后端模型级默认。
        if max_tokens is not None:
            body["max_output_tokens"] = max_tokens
        try:
            text, usage = _stream_responses_text(
                _responses_endpoint(self._profile.endpoint),
                self._headers(),
                body,
                policy=self._policy,
                on_delta=on_delta,
            )
        except LLMError as exc:
            # A compatible gateway may expose /responses but not SSE. Keep
            # that provider usable, while real SSE-capable endpoints stream
            # normally. The fallback cannot reduce latency, but preserves the
            # old stable completion behavior for those gateways.
            if exc.status_code not in (400, 404, 405, 422):
                raise
            text = self.complete(messages, max_tokens=max_tokens)
            if on_delta is not None:
                on_delta(text)
            return text
        if not text:
            raise LLMError(
                "Responses 流式响应没有输出文本",
                code="LLM_INVALID_RESPONSE",
            )
        self._last_usage = _usage_from_response(
            {"usage": usage, "id": uuid.uuid4().hex}, self._profile,
        )
        self._record_usage("stream_completion")
        return text

    @property
    def last_usage(self) -> LLMUsage | None:
        return self._last_usage

    def _check_budget(self) -> None:
        if self._cost_ledger is not None and not self._cost_ledger.can_spend(self._cost_budget):
            raise LLMError("已达到 LLM 成本预算，调用已阻断", code="LLM_BUDGET_EXCEEDED")

    def _record_usage(self, request_type: str) -> None:
        if self._cost_ledger is None or self._last_usage is None:
            return
        self._cost_ledger.record(make_cost_record(
            request_id=self._last_usage.request_id,
            provider_kind=self._last_usage.provider_kind,
            model_id=self._last_usage.model_id,
            request_type=request_type,
            total_tokens=self._last_usage.total_tokens,
            cost_units=self._last_usage.cost_units,
            estimated=self._last_usage.estimated,
        ))

    def _upload_file(self, file_path: str, *, filename: str, mime: str) -> str:
        """上传附件并等待 File API 完成预处理，返回可复用的 file_id。"""
        try:
            stat = os.stat(file_path)
        except OSError as exc:
            raise LLMError(
                f"附件不存在或无法读取：{filename}", code="LLM_FILE_NOT_FOUND",
            ) from exc
        # 方舟文档/视频 File API 的常用单文件上限；更早失败比读入/上传一半再失败更友好。
        max_bytes = 512 * 1024 * 1024
        if stat.st_size > max_bytes:
            raise LLMError(
                f"附件过大：{filename}（{stat.st_size / 1024 / 1024:.1f} MB），"
                "请压缩到 512 MB 以内。",
                code="LLM_FILE_TOO_LARGE",
            )
        cache_key = (os.path.abspath(file_path), int(stat.st_mtime_ns), int(stat.st_size))
        cached = self._uploaded_file_ids.get(cache_key)
        if cached:
            return cached
        response = _post_multipart_file(
            _files_endpoint(self._profile.endpoint),
            self._headers(),
            file_path,
            filename=filename or os.path.basename(file_path),
            mime=mime or "application/octet-stream",
            purpose="user_data",
            policy=self._policy,
        )
        file_id = str(response.get("id") or response.get("file_id") or "")
        if not file_id:
            raise LLMError(
                f"文件上传成功但未返回 file_id：{filename}",
                code="LLM_INVALID_RESPONSE",
            )
        status = str(response.get("status") or "").lower()
        if status in {"processing", "queued", "pending"}:
            deadline = time.monotonic() + min(60.0, max(10.0, self._policy.timeout_seconds))
            while time.monotonic() < deadline:
                time.sleep(1.0)
                state = _get_json(
                    _file_endpoint(self._profile.endpoint, file_id),
                    self._headers(), policy=self._policy,
                )
                status = str(state.get("status") or "").lower()
                if status in {"processed", "ready", "completed", "succeeded", "active"}:
                    break
                if status in {"failed", "error", "cancelled"}:
                    detail = str(state.get("error", "") or state.get("message", ""))
                    raise LLMError(
                        f"文件预处理失败：{filename}。{detail}",
                        code="LLM_FILE_PROCESSING_FAILED",
                    )
        self._uploaded_file_ids[cache_key] = file_id
        return file_id

    def _prepare_responses_content(self, content: Any) -> list[dict[str, Any]]:
        """将中间文件 part 上传并替换为 Responses API 的 file_id 引用。"""
        blocks = self._to_responses_content(content)
        prepared: list[dict[str, Any]] = []
        for block in blocks:
            file_path = str(block.get("file_path") or "")
            if not file_path:
                prepared.append(block)
                continue
            if str(block.get("type")) == "input_file":
                local_text = _local_document_text(
                    file_path,
                    str(block.get("filename") or os.path.basename(file_path)),
                )
                if local_text is not None:
                    prepared.append({
                        "type": "input_text",
                        "text": (
                            f"[附件：{block.get('filename') or os.path.basename(file_path)}]\n"
                            + local_text
                        ),
                    })
                    continue
            file_id = self._upload_file(
                file_path,
                filename=str(block.get("filename") or os.path.basename(file_path)),
                mime=str(block.get("mime") or "application/octet-stream"),
            )
            # filename/mime/media_kind 是项目内部元数据，不能原样发送给
            # Responses；供应商块只保留 type + file_id。
            out = {
                "type": str(block.get("type") or "input_file"),
                "file_id": file_id,
            }
            prepared.append(out)
        return prepared

    @staticmethod
    def _to_responses_content(content: Any) -> list[dict[str, Any]]:
        """把 OpenAI chat 风格 content（str 或 content-parts）转成 Responses API 块。

        多模态：``{type:text}`` → ``{type:input_text}``；
        ``{type:image_url, image_url:{url}}`` → ``{type:input_image, image_url}``。
        ``{type:file, file_path:...}`` → ``input_file/input_video/input_audio``；
        本地路径仍是中间态，实例 transport 会先上传再填充 ``file_id``。
        纯文本字符串 → 单个 ``input_text`` 块（与旧行为一致）。
        """
        if isinstance(content, list):
            blocks: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    blocks.append({"type": "input_text", "text": str(part.get("text", ""))})
                elif ptype == "image_url":
                    url = part.get("image_url", "")
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    if url:
                        blocks.append({"type": "input_image", "image_url": str(url)})
                elif ptype == "file":
                    media_kind = str(part.get("media_kind") or "file").lower()
                    input_type = {
                        "video": "input_video",
                        "audio": "input_audio",
                    }.get(media_kind, "input_file")
                    block: dict[str, Any] = {
                        "type": input_type,
                        "filename": str(part.get("filename") or "attachment"),
                        "mime": str(part.get("mime") or "application/octet-stream"),
                    }
                    if part.get("file_id"):
                        block["file_id"] = str(part["file_id"])
                    elif part.get("file_path"):
                        block["file_path"] = str(part["file_path"])
                    blocks.append(block)
            return blocks or [{"type": "input_text", "text": ""}]
        return [{"type": "input_text", "text": str(content or "")}]

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        output = data.get("output")
        if not isinstance(output, list):
            return ""
        pieces: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"output_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        pieces.append(text)
        return "".join(pieces)

    def test_connection(self) -> None:
        self.complete(
            [{"role": "user", "content": "ping"}],
            max_tokens=32,
            temperature=0.0,
        )


# ---------------------------------------------------------------------- #
# Anthropic（Claude）
# ---------------------------------------------------------------------- #


class AnthropicClient:
    """Anthropic Messages API client（Claude）。

    endpoint 如 ``https://api.anthropic.com``，端点 ``/v1/messages``。
    鉴权头 ``x-api-key: {api_key}`` + ``anthropic-version: 2023-06-01``。
    请求体与 OpenAI 不同：顶层 ``messages`` 不含 system（system 单独字段）。
    """

    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        profile: AiProviderProfile,
        api_key: str,
        *,
        policy: LLMRequestPolicy | None = None,
        cost_ledger: CostLedger | None = None,
        cost_budget: float | None = None,
    ) -> None:
        self._profile = profile
        self._api_key = api_key
        self._policy = policy or LLMRequestPolicy()
        self._cost_ledger = cost_ledger
        self._cost_budget = cost_budget
        self._last_usage: LLMUsage | None = None
        if not profile.endpoint:
            raise LLMError("endpoint（URL）未配置")
        if not profile.model_id:
            raise LLMError("model_id（模型 ID）未配置")
        if not api_key:
            raise LLMError("Claude 需要 API key")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
        }

    def _split_system(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict]]:
        """把 system 消息抽到顶层（Anthropic 要求 system 不在 messages 里）。"""
        system_parts: list[str] = []
        convo: list[dict] = []
        for msg in messages:
            if msg.get("role") == "system":
                if msg.get("content"):
                    system_parts.append(str(msg["content"]))
            else:
                convo.append({"role": msg["role"], "content": msg.get("content", "")})
        return ("\n\n".join(system_parts), convo)

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        system, convo = self._split_system(messages)
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "messages": convo,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else 1024,
        }
        if system:
            body["system"] = system

        url = _anthropic_messages_endpoint(self._profile.endpoint)
        self._check_budget()
        data = _post_json(url, self._headers(), body, policy=self._policy)
        self._last_usage = _usage_from_response(data, self._profile)
        self._record_usage("completion")
        try:
            # Anthropic 响应：{"content": [{"type": "text", "text": "..."}]}
            blocks = data["content"]
            texts = [b["text"] for b in blocks if b.get("type") == "text"]
            return "".join(texts)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"无法解析 Anthropic 响应：{data}", code="LLM_INVALID_RESPONSE"
            ) from exc

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        on_delta: Callable[[str], None] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        system, convo = self._split_system(messages)
        body: dict[str, Any] = {
            "model": self._profile.model_id,
            "messages": convo,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else 1024,
            "stream": True,
        }
        if system:
            body["system"] = system
        self._check_budget()
        text, usage, _finish_reason = _stream_sse_text(
            _anthropic_messages_endpoint(self._profile.endpoint),
            self._headers(), body, policy=self._policy, on_delta=on_delta,
        )
        self._last_usage = _usage_from_response(
            {"usage": usage, "id": uuid.uuid4().hex}, self._profile,
        )
        self._record_usage("stream_completion")
        return text

    @property
    def last_usage(self) -> LLMUsage | None:
        return self._last_usage

    def _check_budget(self) -> None:
        if self._cost_ledger is not None and not self._cost_ledger.can_spend(self._cost_budget):
            raise LLMError("已达到 LLM 成本预算，调用已阻断", code="LLM_BUDGET_EXCEEDED")

    def _record_usage(self, request_type: str) -> None:
        if self._cost_ledger is None or self._last_usage is None:
            return
        self._cost_ledger.record(make_cost_record(
            request_id=self._last_usage.request_id,
            provider_kind=self._last_usage.provider_kind,
            model_id=self._last_usage.model_id,
            request_type=request_type,
            total_tokens=self._last_usage.total_tokens,
            cost_units=self._last_usage.cost_units,
            estimated=self._last_usage.estimated,
        ))

    def test_connection(self) -> None:
        self.complete(
            [{"role": "user", "content": "ping"}],
            max_tokens=8,
            temperature=0.0,
        )


# ---------------------------------------------------------------------- #
# 工厂
# ---------------------------------------------------------------------- #


def make_client(
    profile: AiProviderProfile,
    credential_store: CredentialStore,
    *,
    policy: LLMRequestPolicy | None = None,
    cost_ledger: CostLedger | None = None,
    cost_budget: float | None = None,
) -> LLMClient:
    """根据 ``provider_kind`` 构造对应的 LLM client。

    Args:
        profile: provider 配置（endpoint / model_id / credential_ref）。
        credential_store: 取 API key 的凭据存储。

    Raises:
        LLMError: provider_kind 未知 / endpoint 缺失 / API key 取不到。
    """
    kind = (profile.provider_kind or "").lower()
    # 取 API key（Ollama 默认无 key，允许空）
    api_key = ""
    if profile.credential_ref:
        secret = credential_store.get_secret(profile.credential_ref)
        api_key = secret if secret else ""

    policy = policy or LLMRequestPolicy(
        timeout_seconds=profile.limits.request_timeout_seconds,
        max_retries=profile.limits.max_retries,
    )
    if kind in ("openai", "ollama", "deepseek"):
        # Ollama 允许无 key；OpenAI 需要但 client 不强制（让后端 401 来报错）
        return OpenAICompatClient(
            profile, api_key, policy=policy, cost_ledger=cost_ledger,
            cost_budget=cost_budget if cost_budget is not None else profile.limits.max_cost_units_per_request,
        )
    if kind in ("openai_responses", "volcengine_responses"):
        return OpenAIResponsesClient(
            profile, api_key, policy=policy, cost_ledger=cost_ledger,
            cost_budget=cost_budget if cost_budget is not None else profile.limits.max_cost_units_per_request,
        )
    if kind in ("anthropic", "claude"):
        return AnthropicClient(
            profile, api_key, policy=policy, cost_ledger=cost_ledger,
            cost_budget=cost_budget if cost_budget is not None else profile.limits.max_cost_units_per_request,
        )
    # 兼容旧值 local_http / openai_compat：按 OpenAI 兼容处理
    if kind in ("local_http", "openai_compat", ""):
        logger.warning(
            "provider_kind=%r 已废弃，按 OpenAI 兼容协议处理", profile.provider_kind
        )
        return OpenAICompatClient(
            profile, api_key, policy=policy, cost_ledger=cost_ledger,
            cost_budget=cost_budget if cost_budget is not None else profile.limits.max_cost_units_per_request,
        )
    raise LLMError(
        f"不支持的 provider_kind: {profile.provider_kind!r}"
        "（可选：openai / ollama / anthropic）"
    )
