"""Volcengine Ark Agent Plan media-generation adapters.

This module keeps provider-specific wire formats out of the UI and maps the
working Agent Plan image/TTS endpoints into the provider-neutral generation
contract.  Video is deliberately not implemented here: the current Medium
plan does not accept new Seedance 1.5 Pro tasks.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import tempfile
import ssl
from collections.abc import Mapping
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.shared.ai_models.generation.credential_store import CredentialStore
from src.shared.ai_models.generation.generation_models import (
    ContentSafetyResult,
    GeneratedAsset,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
    LicenseStatus,
)
from src.shared.ai_models.generation.model_pack_manager import default_cache_dir
from src.shared.ai_models.generation.provider_profiles import AiProviderProfile


logger = logging.getLogger(__name__)

_IMAGE_PATH = "/images/generations"
_DEFAULT_TTS_RESOURCE_ID = "seed-tts-2.0"
_DEFAULT_SPEAKER = "zh_female_vv_uranus_bigtts"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Agent Plan 拒绝旧的 1024 默认；不足该像素数时等比放大（保持宽高比）。
_MIN_IMAGE_PIXELS = 3_686_400  # 1920×1920
_BASE_EDGE = 2048

# AUD-31710：generated_media 是可重建的生成产物缓存（§0：可重建缓存不入
# 工程文件），不允许无限累积。写入后按总字节 / 文件数双上限做"最旧优先"
# 淘汰；可用环境变量放宽（0 视为关闭该维度上限）。
GENERATED_MEDIA_MAX_BYTES = 512 * 1024 * 1024
GENERATED_MEDIA_MAX_FILES = 500


def prune_generated_media_cache(
    root: Path,
    *,
    max_bytes: int = GENERATED_MEDIA_MAX_BYTES,
    max_files: int = GENERATED_MEDIA_MAX_FILES,
) -> int:
    """Prune oldest files from the generated-media cache over budget (AUD-31710).

    只删除已落位的产物文件（跳过 ``.`` 开头的并发写临时文件）。删除失败
    （Windows 上被占用等）只留痕不中断——清理失败不得影响生成主流程。

    Returns:
        实际删除的文件数。
    """
    try:
        entries = [p for p in root.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return 0
    if not entries:
        return 0
    stats: list[tuple[float, Path, int]] = []
    total_bytes = 0
    for path in entries:
        try:
            st = path.stat()
        except OSError:
            continue
        stats.append((st.st_mtime, path, st.st_size))
        total_bytes += st.st_size
    if not stats:
        return 0
    if (max_files <= 0 or len(stats) <= max_files) and (
        max_bytes <= 0 or total_bytes <= max_bytes
    ):
        return 0
    stats.sort(key=lambda item: (item[0], item[1].name))  # 最旧优先
    removed = 0
    for _mtime, path, size in stats:
        if (
            (max_files <= 0 or len(stats) - removed <= max_files)
            and (max_bytes <= 0 or total_bytes <= max_bytes)
        ):
            break
        try:
            path.unlink()
        except OSError:
            logger.debug("generated_media prune: failed to unlink %s", path, exc_info=True)
            continue
        total_bytes -= size
        removed += 1
    if removed:
        logger.info(
            "generated_media cache pruned %d file(s) from %s", removed, root,
        )
    return removed


def _parse_aspect_ratio(ratio: str) -> tuple[int, int] | None:
    """Parse an ``"a:b"`` aspect string; ``None`` when absent/invalid."""
    text = (ratio or "").strip()
    if not text:
        return None
    parts = text.split(":", 1) if ":" in text else text.split("x", 1)
    if len(parts) != 2:
        return None
    try:
        num, den = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if num <= 0 or den <= 0:
        return None
    return num, den


def _upsize_to_min_pixels(width: int, height: int) -> tuple[int, int]:
    """Scale (width, height) up proportionally to the minimum pixel count."""
    if width <= 0 or height <= 0 or width * height >= _MIN_IMAGE_PIXELS:
        return width, height
    scale = math.sqrt(_MIN_IMAGE_PIXELS / float(width * height))
    width = max(1, int(round(width * scale)))
    height = max(1, int(round(height * scale)))
    # 偶数对齐（生成端点普遍要求偶数尺寸），并兜底保证达到最小像素。
    width += width % 2
    height += height % 2
    while width * height < _MIN_IMAGE_PIXELS:
        width += 2
        height += 2
    return width, height


class _VolcengineProviderError(Exception):
    """Sanitized provider error; response bodies are never surfaced."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _VolcengineProviderBase:
    """Shared credential, HTTP and provenance helpers."""

    is_demo = False

    def __init__(
        self,
        profile: AiProviderProfile,
        credential_store: CredentialStore,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._profile = profile
        self._credential_store = credential_store
        self._opener = opener or urllib.request.urlopen

    @property
    def provider_id(self) -> str:
        return self._profile.profile_id

    @property
    def supported_modalities(self) -> tuple[GenerationModality, ...]:
        supported: list[GenerationModality] = []
        for raw in self._profile.modalities:
            try:
                modality = GenerationModality(raw)
            except ValueError:
                continue
            if modality not in supported:
                supported.append(modality)
        return tuple(supported)

    def _secret(self, request: GenerationRequest) -> str:
        secret = self._credential_store.get_secret(self._profile.credential_ref)
        if not secret:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_CREDENTIAL_MISSING", "credential is not configured",
            )
        if request.modality not in self.supported_modalities:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_UNSUPPORTED_MODALITY",
                f"provider does not support {request.modality.value}",
            )
        return secret

    def _request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(dict(body), ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method=method,
        )
        try:
            response = self._open(request)
            with response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc.code) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_NETWORK_ERROR", "provider network request failed",
            ) from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_RESPONSE_TOO_LARGE", "provider response exceeds limit",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_SCHEMA_INVALID", "provider response is not JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise _VolcengineProviderError(
                "GEN_PROVIDER_SCHEMA_INVALID", "provider response must be an object",
            )
        return payload

    def _request_bytes(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> bytes:
        request = urllib.request.Request(
            url,
            data=json.dumps(dict(body), ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method=method,
        )
        try:
            response = self._open(request)
            with response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc.code) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_NETWORK_ERROR", "provider network request failed",
            ) from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_RESPONSE_TOO_LARGE", "provider response exceeds limit",
            )
        return raw

    def _open(self, request: urllib.request.Request) -> Any:
        """Open HTTPS with the bundled CA bundle when available.

        Some macOS Python installations do not link the system trust store.
        We keep normal certificate verification enabled and only provide
        certifi's CA file when the production opener is the stdlib opener;
        injected test openers retain their original two-argument contract.

        AUD-31700: only the CA-context construction is guarded here.  The
        opener call itself must stay outside the ``except`` — ``HTTPError`` /
        ``URLError`` / ``TimeoutError`` are all ``OSError`` subclasses, and
        catching them here used to unconditionally resend the same billed
        POST after a real failure.
        """
        timeout = max(0.1, float(self._profile.limits.request_timeout_seconds))
        if self._opener is not urllib.request.urlopen:
            return self._opener(request, timeout=timeout)
        context = None
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except (ImportError, OSError, TypeError):
            context = None
        if context is not None:
            return self._opener(request, timeout=timeout, context=context)
        return self._opener(request, timeout=timeout)

    @staticmethod
    def _http_error(status: int) -> _VolcengineProviderError:
        if status in (401, 403):
            return _VolcengineProviderError(
                "GEN_PROVIDER_AUTH_FAILED", "provider authentication failed",
            )
        if status == 429:
            return _VolcengineProviderError(
                "GEN_PROVIDER_RATE_LIMITED", "provider rate limit reached",
            )
        return _VolcengineProviderError(
            "GEN_PROVIDER_HTTP_ERROR", f"provider HTTP status {status}",
        )

    def _url(self, suffix: str) -> str:
        endpoint = self._profile.endpoint.rstrip("/")
        if endpoint.endswith(suffix):
            return endpoint
        return f"{endpoint}{suffix}"

    def _validated_url(self, suffix: str = "") -> str:
        """Build the request URL and validate the endpoint (AUD-31701).

        ``AiProviderProfile.endpoint`` defaults to ``""``; an empty or
        relative endpoint would make ``urllib.request.Request`` raise a raw
        ``ValueError`` outside every ``try`` block, escaping the provider
        error contract.  Map that to ``GEN_PROVIDER_CONFIG_INVALID`` instead.
        """
        url = self._url(suffix)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_CONFIG_INVALID",
                "provider endpoint must be an absolute http(s) URL",
            )
        return url

    def _provenance(
        self,
        request: GenerationRequest,
        *,
        model_version: str,
    ) -> GenerationProvenance:
        # AUD-31705: these adapters never upload reference media (requests
        # carrying references are rejected in ``generate`` with
        # GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED).  Provenance must record the
        # inputs actually sent, so reference fields stay empty — recording
        # never-uploaded assets would falsify the audit chain.
        return GenerationProvenance(
            provider_id=self.provider_id,
            model_id=self._profile.model_id,
            model_version=model_version,
            requested_at=datetime.now(timezone.utc).isoformat(),
            input_prompt_hash=request.prompt_hash,
            reference_asset_ids=(),
            parent_skill_id=request.requested_by,
            recipe_id=request.recipe.recipe_id if request.recipe else "",
            recipe_schema_version=request.recipe.schema_version if request.recipe else "",
            reference_bindings=(),
        )

    def _reject_reference_media(self, request: GenerationRequest) -> None:
        """Reject reference-carrying requests instead of dropping them (AUD-31705)."""
        if request.effective_reference_asset_ids or request.effective_reference_bindings:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED",
                "provider does not support reference media upload",
            )

    @staticmethod
    def _error(request: GenerationRequest, error: _VolcengineProviderError) -> GenerationResult:
        return GenerationResult(
            request_id=request.request_id,
            error_code=error.code,
            error_message=error.message,
        )


class VolcengineSeedreamImageProvider(_VolcengineProviderBase):
    """Seedream 5.0 lite adapter for the Agent Plan image endpoint."""

    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: Callable[[float, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> GenerationResult:
        try:
            # AUD-31702: check cancellation before any credential/keychain IO.
            if is_cancelled is not None and is_cancelled():
                return self._error(
                    request,
                    _VolcengineProviderError("GEN_PROVIDER_CANCELLED", "generation cancelled"),
                )
            secret = self._secret(request)
            self._reject_reference_media(request)
            url = self._validated_url(_IMAGE_PATH)
            if progress is not None:
                progress(0.05, "正在提交 Seedream 图片生成任务")
            payload = self._request_json(
                "POST",
                url,
                {
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                self._body(request),
            )
            # AUD-31702: the blocking HTTP call cannot be interrupted; honour
            # a cancellation that arrived while it was in flight before the
            # result is cached or surfaced as success.
            if is_cancelled is not None and is_cancelled():
                return self._error(
                    request,
                    _VolcengineProviderError("GEN_PROVIDER_CANCELLED", "generation cancelled"),
                )
            candidates = self._candidates(request, payload)
            if not candidates:
                return self._error(
                    request,
                    _VolcengineProviderError(
                        "GEN_PROVIDER_SCHEMA_INVALID", "image response has no media output",
                    ),
                )
            if progress is not None:
                progress(1.0, "Seedream 图片生成完成")
            usage = payload.get("usage")
            actual_cost = 0.0
            if isinstance(usage, Mapping):
                try:
                    actual_cost = float(usage.get("generated_images", 0) or 0)
                except (TypeError, ValueError):
                    actual_cost = 0.0
            return GenerationResult(
                request_id=request.request_id,
                candidates=tuple(candidates),
                actual_cost_units=actual_cost,
            )
        except _VolcengineProviderError as exc:
            return self._error(request, exc)
        except Exception:
            # AUD-31701: unexpected non-contract failures (e.g. malformed
            # constraint values) must stay inside the error-result contract.
            return self._error(
                request,
                _VolcengineProviderError("GEN_PROVIDER_FAILED", "unexpected provider failure"),
            )

    def _body(self, request: GenerationRequest) -> dict[str, Any]:
        constraints = dict(request.recipe.output_constraints) if request.recipe else {}
        width, height = self._resolve_size(request)
        return {
            "model": self._profile.model_id,
            "prompt": request.prompt,
            "size": f"{width}x{height}",
            "sequential_image_generation": "disabled",
            "response_format": "url",
            "watermark": bool(constraints.get("watermark", True)),
        }

    def _resolve_size(self, request: GenerationRequest) -> tuple[int, int]:
        """Resolve the requested image size (AUD-31703).

        Priority: explicit recipe dimensions → recipe aspect_ratio →
        request aspect_ratio → 2K square fallback.  Sizes below the Agent
        Plan minimum pixel count are scaled up proportionally so the user's
        chosen aspect ratio survives (the old code forced 2048×2048).
        """
        recipe = request.recipe
        if recipe is not None and recipe.width and recipe.height:
            return _upsize_to_min_pixels(int(recipe.width), int(recipe.height))
        ratio = ""
        if recipe is not None and recipe.aspect_ratio:
            ratio = recipe.aspect_ratio
        elif request.aspect_ratio:
            ratio = request.aspect_ratio
        parsed = _parse_aspect_ratio(ratio)
        if parsed is None:
            return _BASE_EDGE, _BASE_EDGE
        num, den = parsed
        # 长边锚定基准边长，保持请求的宽高比。
        if num >= den:
            width, height = _BASE_EDGE, max(1, round(_BASE_EDGE * den / num))
        else:
            width, height = max(1, round(_BASE_EDGE * num / den)), _BASE_EDGE
        return _upsize_to_min_pixels(width, height)

    def _candidates(
        self, request: GenerationRequest, payload: Mapping[str, Any],
    ) -> list[GeneratedAsset]:
        raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            return []
        candidates: list[GeneratedAsset] = []
        for index, raw in enumerate(raw_items[: max(1, request.budget.max_candidates)]):
            if not isinstance(raw, Mapping):
                continue
            handle = str(raw.get("url") or "").strip()
            if not handle and raw.get("b64_json"):
                try:
                    handle = self._cache_b64(
                        request.request_id,
                        str(raw["b64_json"]),
                        suffix=".png",
                        # AUD-31704: include the candidate index so multiple
                        # b64 candidates no longer overwrite one file.
                        candidate=index + 1,
                    )
                except (ValueError, OSError):
                    continue
            if not handle:
                continue
            candidates.append(GeneratedAsset(
                asset_id=f"gen-{request.request_id}-c{index + 1}",
                request_id=request.request_id,
                modality=GenerationModality.IMAGE,
                provenance=self._provenance(
                    request,
                    model_version=str(payload.get("model") or self._profile.model_id),
                ),
                license=LicenseStatus.UNKNOWN,
                safety=ContentSafetyResult(),
                preview_handle=handle,
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
        return candidates

    @staticmethod
    def _cache_b64(
        request_id: str,
        encoded: str,
        *,
        suffix: str,
        candidate: int = 1,
    ) -> str:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid image payload") from exc
        root = default_cache_dir() / "generated_media" / "volcengine"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{request_id}-c{candidate}{suffix}"
        fd, temporary_name = tempfile.mkstemp(prefix=f".{request_id}.", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        # AUD-31710：每次写入后做容量收敛，缓存不再无限累积。
        prune_generated_media_cache(root)
        return str(target)


class VolcenginePlanTtsProvider(_VolcengineProviderBase):
    """Agent Plan doubao-seed-tts-2.0 HTTP adapter."""

    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: Callable[[float, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> GenerationResult:
        try:
            # AUD-31702: check cancellation before any credential/keychain IO.
            if is_cancelled is not None and is_cancelled():
                return self._error(
                    request,
                    _VolcengineProviderError("GEN_PROVIDER_CANCELLED", "generation cancelled"),
                )
            secret = self._secret(request)
            self._reject_reference_media(request)
            url = self._validated_url()
            if progress is not None:
                progress(0.05, "正在提交 Agent Plan 语音生成任务")
            raw = self._request_bytes(
                "POST",
                url,
                {
                    "X-Api-Key": secret,
                    "X-Api-Resource-Id": _DEFAULT_TTS_RESOURCE_ID,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg, application/json",
                },
                self._body(request),
            )
            # AUD-31702: honour a cancellation that arrived while the
            # blocking HTTP call was in flight — before caching audio bytes.
            if is_cancelled is not None and is_cancelled():
                return self._error(
                    request,
                    _VolcengineProviderError("GEN_PROVIDER_CANCELLED", "generation cancelled"),
                )
            if self._looks_like_json(raw):
                parsed_audio, error = self._parse_json_audio(raw)
                if error is not None:
                    return self._error(request, error)
                raw = parsed_audio
            if not raw:
                return self._error(
                    request,
                    _VolcengineProviderError("GEN_PROVIDER_SCHEMA_INVALID", "voice response is empty"),
                )
            handle = self._cache_audio(request.request_id, raw, request)
            candidate = GeneratedAsset(
                asset_id=f"gen-{request.request_id}-c1",
                request_id=request.request_id,
                modality=GenerationModality.VOICE,
                provenance=self._provenance(
                    request,
                    model_version=self._profile.model_id or _DEFAULT_TTS_RESOURCE_ID,
                ),
                license=LicenseStatus.UNKNOWN,
                safety=ContentSafetyResult(),
                preview_handle=handle,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            if progress is not None:
                progress(1.0, "Agent Plan 语音生成完成")
            return GenerationResult(request_id=request.request_id, candidates=(candidate,))
        except _VolcengineProviderError as exc:
            return self._error(request, exc)
        except Exception:
            # AUD-31701: unexpected non-contract failures must stay inside
            # the error-result contract.
            return self._error(
                request,
                _VolcengineProviderError("GEN_PROVIDER_FAILED", "unexpected provider failure"),
            )

    def _body(self, request: GenerationRequest) -> dict[str, Any]:
        constraints = dict(request.recipe.output_constraints) if request.recipe else {}
        output_format = str(
            (request.recipe.output_format if request.recipe else "")
            or constraints.get("format")
            or "mp3"
        ).lower()
        sample_rate = int(constraints.get("sample_rate", 24000))
        return {
            "req_params": {
                "text": request.prompt,
                "speaker": str(constraints.get("speaker", _DEFAULT_SPEAKER)),
                "audio_params": {
                    "format": output_format,
                    "sample_rate": sample_rate,
                },
            },
        }

    @staticmethod
    def _looks_like_json(raw: bytes) -> bool:
        stripped = raw.lstrip()
        return stripped.startswith(b"{") or stripped.startswith(b"[")

    @staticmethod
    def _parse_json_audio(
        raw: bytes,
    ) -> tuple[bytes, _VolcengineProviderError | None]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return b"", _VolcengineProviderError(
                "GEN_PROVIDER_SCHEMA_INVALID", "voice response is not valid audio",
            )
        payloads: list[Mapping[str, Any]] = []
        try:
            one = json.loads(text)
            if isinstance(one, Mapping):
                payloads.append(one)
        except json.JSONDecodeError:
            # The HTTP endpoint may return one JSON envelope per audio chunk.
            # Decode each chunk independently so per-chunk base64 padding stays
            # valid before concatenating the decoded audio bytes.
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    return b"", _VolcengineProviderError(
                        "GEN_PROVIDER_SCHEMA_INVALID", "voice response is not valid JSON audio",
                    )
                if not isinstance(item, Mapping):
                    return b"", _VolcengineProviderError(
                        "GEN_PROVIDER_SCHEMA_INVALID", "voice response is not valid JSON audio",
                    )
                payloads.append(item)
        if not payloads:
            return b"", _VolcengineProviderError(
                "GEN_PROVIDER_SCHEMA_INVALID", "voice response is not valid audio",
            )
        audio_chunks: list[bytes] = []
        for payload in payloads:
            code = str(payload.get("code", ""))
            if code and code not in {"0", "20000000"}:
                if code == "45000030":
                    return b"", _VolcengineProviderError(
                        "GEN_PROVIDER_PLAN_DEDUCTION_DISABLED",
                        "Agent Plan deduction is not enabled for this account or seat",
                    )
                return b"", _VolcengineProviderError(
                    "GEN_PROVIDER_FAILED", "voice provider returned an error",
                )
            encoded = payload.get("data")
            if encoded is None or encoded == "":
                # A successful stream may finish with an empty terminal
                # envelope after the final audio chunk.
                continue
            if not isinstance(encoded, str):
                return b"", _VolcengineProviderError(
                    "GEN_PROVIDER_SCHEMA_INVALID", "voice provider returned no audio data",
                )
            try:
                audio_chunks.append(base64.b64decode(encoded, validate=True))
            except (ValueError, binascii.Error):
                return b"", _VolcengineProviderError(
                    "GEN_PROVIDER_SCHEMA_INVALID", "voice provider returned invalid audio data",
                )
        if not audio_chunks:
            return b"", _VolcengineProviderError(
                "GEN_PROVIDER_SCHEMA_INVALID", "voice provider returned no audio data",
            )
        return b"".join(audio_chunks), None

    @staticmethod
    def _cache_audio(
        request_id: str, data: bytes, request: GenerationRequest,
    ) -> str:
        output_format = str(
            request.recipe.output_format if request.recipe else "mp3"
        ).lower()
        suffix = output_format if output_format in {"mp3", "wav", "pcm"} else "mp3"
        root = default_cache_dir() / "generated_media" / "volcengine"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{request_id}.{suffix}"
        fd, temporary_name = tempfile.mkstemp(prefix=f".{request_id}.", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        # AUD-31710：每次写入后做容量收敛，缓存不再无限累积。
        prune_generated_media_cache(root)
        return str(target)


def make_volcengine_generation_provider(
    profile: AiProviderProfile,
    credential_store: CredentialStore,
) -> VolcengineSeedreamImageProvider | VolcenginePlanTtsProvider | None:
    """Build a supported Agent Plan media adapter from a user profile.

    AUD-31713: the public factory no longer leaks the private base class in
    its signature; it returns the union of the two public adapters instead.
    """

    kind = profile.provider_kind.lower()
    if kind in {"volcengine_seedream", "volcengine_ark_image"}:
        return VolcengineSeedreamImageProvider(profile, credential_store)
    if kind in {"volcengine_plan_tts", "volcengine_ark_voice"}:
        return VolcenginePlanTtsProvider(profile, credential_store)
    return None


__all__ = [
    "GENERATED_MEDIA_MAX_BYTES",
    "GENERATED_MEDIA_MAX_FILES",
    "VolcenginePlanTtsProvider",
    "VolcengineSeedreamImageProvider",
    "make_volcengine_generation_provider",
    "prune_generated_media_cache",
]
