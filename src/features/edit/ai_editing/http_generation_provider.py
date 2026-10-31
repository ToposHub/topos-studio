"""Configurable HTTP generation adapter (P2).

This module deliberately contains no vendor SDK.  A media provider is described by
an :class:`AiProviderProfile` and a small set of HTTP routes, then exposed through
the existing ``GenerationProvider`` contract.  The wire contract is intentionally
boring and mockable:

* ``POST /v1/generations`` accepts a provider-neutral request and an idempotency key;
* ``GET /v1/generations/{job_id}`` returns queued/running/succeeded/failed state;
* ``POST /v1/generations/{job_id}/cancel`` is available to hosts that can cancel a
  running job;
* successful responses contain ``outputs`` (or ``candidates``), each with a media
  URL and optional provenance, license, safety and cost fields.

The adapter is suitable for a real provider once the user supplies an endpoint and
credential.  It never writes secrets to logs or generated provenance.  Provider
specific response differences should be handled by a thin subclass/route adapter,
not in the UI or ``GenerationService``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from src.features.edit.ai_editing.generation_models import (
    ContentSafetyResult,
    GeneratedAsset,
    GenerationModality,
    GenerationRequest,
    GenerationResult,
    GenerationProvenance,
    LicenseStatus,
)
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    CredentialStore,
)


DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_POLL_SECONDS = 15 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class HttpGenerationRoutes:
    """Relative HTTP paths for one provider adapter."""

    submit: str = "/v1/generations"
    poll: str = "/v1/generations/{job_id}"
    cancel: str = "/v1/generations/{job_id}/cancel"


@dataclass(frozen=True)
class RemoteGenerationCheckpoint:
    """Minimal provider checkpoint safe to persist in ``JobHandle``."""

    request_id: str
    provider_id: str
    remote_job_id: str
    state: str = "running"
    resumable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "remote_job_id": self.remote_job_id,
            "state": self.state,
            "resumable": self.resumable,
        }


class HttpGenerationProvider:
    """Provider-neutral remote generation adapter.

    ``generate`` submits once and polls until a terminal response.  The existing
    ``GenerationService`` should run it in a worker for production UI use.  The
    optional ``cancel`` method lets a host cancel a provider job when it owns the
    remote job id.
    """

    provider_id: str

    def __init__(
        self,
        profile: AiProviderProfile,
        credential_store: CredentialStore,
        *,
        routes: HttpGenerationRoutes | None = None,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        max_poll_seconds: float = DEFAULT_MAX_POLL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._profile = profile
        self._credential_store = credential_store
        self._routes = routes or HttpGenerationRoutes()
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._max_poll_seconds = float(max_poll_seconds)
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._max_response_bytes = int(max_response_bytes)
        self.provider_id = profile.profile_id
        self._last_remote_job_id = ""
        # AUD-13602：共享 worker 并发 generate 时，实例级的"最近 job id"会互相
        # 覆盖。逐请求的映射（request_id → remote_job_id）是无歧义的取消通道；
        # ``last_remote_job_id`` 仅为兼容保留（见其 docstring）。
        self._remote_jobs: dict[str, str] = {}
        self._remote_jobs_lock = threading.Lock()
        if self._max_poll_seconds <= 0:
            raise ValueError("max_poll_seconds must be positive")
        if self._max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")

    @property
    def supported_modalities(self) -> tuple[GenerationModality, ...]:
        """Modalities declared by the profile (unknown values are ignored)."""
        supported: list[GenerationModality] = []
        for raw in self._profile.modalities:
            try:
                modality = GenerationModality(raw)
            except ValueError:
                continue
            if modality not in supported:
                supported.append(modality)
        return tuple(supported)

    @property
    def last_remote_job_id(self) -> str:
        """Most recently submitted remote job (deprecated).

        With a shared worker pool this instance-level value is ambiguous:
        concurrent ``generate`` calls overwrite each other, so cancelling via
        this id may target the wrong remote task.  Prefer the per-request
        :meth:`remote_job_id` lookup or the ``checkpoint`` callback, which
        carries the exact ``remote_job_id`` for one request.
        """
        with self._remote_jobs_lock:
            return self._last_remote_job_id

    def remote_job_id(self, request_id: str) -> str:
        """Return the remote job id submitted for ``request_id`` ("" if unknown)."""
        with self._remote_jobs_lock:
            return self._remote_jobs.get(request_id, "")

    def _remember_remote_job(self, request_id: str, remote_job_id: str) -> None:
        with self._remote_jobs_lock:
            self._remote_jobs[request_id] = remote_job_id
            self._last_remote_job_id = remote_job_id
            # 会话内请求数无界：只保留最近的映射，避免另一种无限增长。
            while len(self._remote_jobs) > 128:
                self._remote_jobs.pop(next(iter(self._remote_jobs)))

    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: Callable[[float, str], None] | None = None,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        resume_remote_job_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> GenerationResult:
        if request.modality not in self.supported_modalities:
            return self._error(
                request,
                "GEN_PROVIDER_UNSUPPORTED_MODALITY",
                f"provider does not support {request.modality.value}",
            )
        parsed_endpoint = urllib.parse.urlparse(self._profile.endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
            return self._error(request, "GEN_PROVIDER_CONFIG_INVALID", "endpoint is not configured")
        if request.effective_reference_asset_ids and not self._profile.allows_media_upload:
            return self._error(
                request,
                "GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED",
                "reference assets require an explicit media-upload data policy",
            )
        secret = self._credential_store.get_secret(self._profile.credential_ref)
        if not secret:
            return self._error(
                request,
                "GEN_PROVIDER_CREDENTIAL_MISSING",
                "credential is not configured",
            )

        headers = self._headers(secret, request.request_id)
        if resume_remote_job_id:
            remote_job_id = str(resume_remote_job_id)
            self._remember_remote_job(request.request_id, remote_job_id)
            self._emit_checkpoint(checkpoint, request, remote_job_id, "resumed")
            return self._poll_remote_job(
                request, headers, remote_job_id, progress=progress,
                checkpoint=checkpoint, is_cancelled=is_cancelled,
            )

        body = self._request_body(request)
        if progress is not None:
            progress(0.02, "正在提交远程生成任务")
        try:
            submitted = self._request_json(
                "POST", self._url(self._routes.submit), headers, body,
            )
        except _HttpAdapterError as exc:
            return self._error(request, exc.code, exc.message)

        immediate = self._result_from_payload(request, submitted)
        if immediate is not None:
            if progress is not None:
                progress(1.0, "远程生成任务已完成")
            return immediate

        remote_job_id = self._string(submitted, "job_id") or self._string(submitted, "id")
        if not remote_job_id:
            return self._error(
                request,
                "GEN_PROVIDER_SCHEMA_INVALID",
                "submit response has neither outputs nor job_id",
            )
        self._remember_remote_job(request.request_id, remote_job_id)
        self._emit_checkpoint(checkpoint, request, remote_job_id, "running")
        return self._poll_remote_job(
            request, headers, remote_job_id, progress=progress,
            checkpoint=checkpoint, is_cancelled=is_cancelled,
        )

    def _poll_remote_job(
        self,
        request: GenerationRequest,
        headers: Mapping[str, str],
        remote_job_id: str,
        *,
        progress: Callable[[float, str], None] | None = None,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> GenerationResult:
        started = self._monotonic()
        while True:
            elapsed = self._monotonic() - started
            if elapsed >= self._max_poll_seconds:
                self._emit_checkpoint(checkpoint, request, remote_job_id, "timeout")
                return self._error(request, "GEN_PROVIDER_TIMEOUT", "remote generation polling timed out")
            if is_cancelled is not None and is_cancelled():
                cancelled = self.cancel(remote_job_id)
                self._emit_checkpoint(
                    checkpoint, request, remote_job_id,
                    "cancelled" if cancelled else "cancel_failed",
                )
                return self._error(
                    request,
                    "GEN_PROVIDER_CANCELLED" if cancelled else "GEN_PROVIDER_CANCEL_FAILED",
                    "remote generation cancellation requested"
                    if cancelled else "remote generation cancellation failed",
                )
            if self._poll_interval_seconds:
                self._sleep(self._poll_interval_seconds)
            if progress is not None:
                progress(min(0.95, 0.05 + 0.9 * elapsed / self._max_poll_seconds), "等待远程生成任务")
            try:
                payload = self._request_json(
                    "GET",
                    self._url(self._routes.poll, job_id=remote_job_id),
                    headers,
                    None,
                )
            except _HttpAdapterError as exc:
                self._emit_checkpoint(checkpoint, request, remote_job_id, "failed")
                return self._error(request, exc.code, exc.message)
            result, terminal = self._poll_result(request, payload)
            if result is not None:
                if progress is not None and result.succeeded:
                    progress(1.0, "远程生成任务已完成")
                self._emit_checkpoint(
                    checkpoint, request, remote_job_id,
                    "succeeded" if result.succeeded else "failed",
                )
                return result
            if terminal:
                self._emit_checkpoint(checkpoint, request, remote_job_id, "failed")
                return self._error(
                    request,
                    "GEN_PROVIDER_FAILED",
                    "remote provider returned a terminal response without outputs",
                )

    def _emit_checkpoint(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
        request: GenerationRequest,
        remote_job_id: str,
        state: str,
    ) -> None:
        if callback is None:
            return
        callback(RemoteGenerationCheckpoint(
            request_id=request.request_id,
            provider_id=self.provider_id,
            remote_job_id=remote_job_id,
            state=state,
        ).to_dict())

    def cancel(self, remote_job_id: str) -> bool:
        """Ask the remote provider to cancel a submitted job."""
        parsed_endpoint = urllib.parse.urlparse(self._profile.endpoint)
        if (
            not remote_job_id
            or parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.netloc
        ):
            return False
        secret = self._credential_store.get_secret(self._profile.credential_ref)
        if not secret:
            return False
        try:
            self._request_json(
                "POST",
                self._url(self._routes.cancel, job_id=remote_job_id),
                self._headers(secret, f"cancel-{remote_job_id}"),
                {"job_id": remote_job_id},
            )
        except _HttpAdapterError:
            return False
        return True

    def _request_body(self, request: GenerationRequest) -> dict[str, Any]:
        constraints: dict[str, Any] = {
            "max_candidates": max(1, request.budget.max_candidates),
        }
        if request.budget.max_cost_units is not None:
            constraints["max_cost_units"] = request.budget.max_cost_units
        if request.budget.max_duration_seconds is not None:
            constraints["max_duration_seconds"] = request.budget.max_duration_seconds
        return {
            "request_id": request.request_id,
            "modality": request.modality.value,
            "model": self._profile.model_id,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "duration_seconds": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "style": request.style,
            "reference_asset_ids": list(request.effective_reference_asset_ids),
            "reference_bindings": [
                binding.to_dict() for binding in request.effective_reference_bindings
            ],
            "recipe": request.recipe.to_dict() if request.recipe else None,
            "constraints": constraints,
        }

    def _headers(self, secret: str, idempotency_key: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "Topos-Color-Generation-Adapter/1.0",
        }
        if self._profile.provider_kind.lower() in {"anthropic", "x_api_key"}:
            headers["x-api-key"] = secret
        else:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _url(self, path: str, *, job_id: str | None = None) -> str:
        endpoint = self._profile.endpoint.rstrip("/")
        rendered = path.format(job_id=urllib.parse.quote(job_id or "", safe=""))
        if not rendered.startswith("/"):
            rendered = f"/{rendered}"
        return f"{endpoint}{rendered}"

    def _request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=dict(headers),
            method=method,
        )
        retries = max(0, int(self._profile.limits.max_retries))
        timeout = max(0.1, float(self._profile.limits.request_timeout_seconds))
        for attempt in range(retries + 1):
            try:
                response = self._opener(request, timeout=timeout)
                with response:
                    raw_length = response.headers.get("Content-Length")
                    # 响应头是外部数据：非数字 Content-Length 视为未知长度，
                    # 由下面的实读字节上限兜底，绝不让 int() 的 ValueError 穿透。
                    try:
                        content_length = int(raw_length) if raw_length else None
                    except ValueError:
                        content_length = None
                    if content_length is not None and content_length > self._max_response_bytes:
                        raise _HttpAdapterError("GEN_PROVIDER_RESPONSE_TOO_LARGE", "provider response exceeds limit")
                    raw = response.read(self._max_response_bytes + 1)
                    if len(raw) > self._max_response_bytes:
                        raise _HttpAdapterError("GEN_PROVIDER_RESPONSE_TOO_LARGE", "provider response exceeds limit")
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise _HttpAdapterError("GEN_PROVIDER_SCHEMA_INVALID", "provider response is not JSON") from exc
                    if not isinstance(payload, dict):
                        raise _HttpAdapterError("GEN_PROVIDER_SCHEMA_INVALID", "provider response must be an object")
                    return payload
            except _HttpAdapterError:
                raise
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 408 or exc.code == 429 or exc.code >= 500
                if retryable and attempt < retries:
                    self._sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                if exc.code in (401, 403):
                    raise _HttpAdapterError("GEN_PROVIDER_AUTH_FAILED", "provider authentication failed") from exc
                if exc.code == 429:
                    raise _HttpAdapterError("GEN_PROVIDER_RATE_LIMITED", "provider rate limit reached") from exc
                raise _HttpAdapterError("GEN_PROVIDER_HTTP_ERROR", f"provider HTTP status {exc.code}") from exc
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                if attempt < retries:
                    self._sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                raise _HttpAdapterError("GEN_PROVIDER_NETWORK_ERROR", "provider network request failed") from exc
        raise _HttpAdapterError("GEN_PROVIDER_NETWORK_ERROR", "provider network request failed")

    def _result_from_payload(
        self, request: GenerationRequest, payload: Mapping[str, Any],
    ) -> GenerationResult | None:
        status = self._status(payload)
        if status in {"cancelled", "canceled"}:
            return self._error(request, "GEN_PROVIDER_CANCELLED", "remote generation was cancelled")
        if status in {"failed", "error", "rejected", "moderated"}:
            code = "GEN_PROVIDER_UNSAFE" if status in {"rejected", "moderated"} else "GEN_PROVIDER_FAILED"
            return self._error(request, code, "remote provider rejected or failed the generation")
        if not self._outputs(payload) and status not in {"succeeded", "completed", "success"}:
            return None
        return self._make_result(request, payload)

    def _poll_result(
        self, request: GenerationRequest, payload: Mapping[str, Any],
    ) -> tuple[GenerationResult | None, bool]:
        status = self._status(payload)
        if status in {"queued", "pending", "running", "processing", "in_progress"}:
            return None, False
        if status in {"cancelled", "canceled"}:
            return self._error(request, "GEN_PROVIDER_CANCELLED", "remote generation was cancelled"), True
        if status in {"failed", "error", "rejected", "moderated"}:
            code = "GEN_PROVIDER_UNSAFE" if status in {"rejected", "moderated"} else "GEN_PROVIDER_FAILED"
            return self._error(request, code, "remote provider rejected or failed the generation"), True
        if status in {"succeeded", "completed", "success"} or self._outputs(payload):
            return self._make_result(request, payload), True
        return None, False

    def _make_result(self, request: GenerationRequest, payload: Mapping[str, Any]) -> GenerationResult:
        outputs = self._outputs(payload)
        if not outputs:
            return self._error(request, "GEN_PROVIDER_SCHEMA_INVALID", "successful response has no outputs")
        candidates: list[GeneratedAsset] = []
        for index, raw in enumerate(outputs[: max(1, request.budget.max_candidates)]):
            if not isinstance(raw, Mapping):
                return self._error(request, "GEN_PROVIDER_SCHEMA_INVALID", "output is not an object")
            handle = self._string(raw, "preview_url") or self._string(raw, "download_url") or self._string(raw, "url")
            if not handle:
                return self._error(request, "GEN_PROVIDER_SCHEMA_INVALID", "output has no media URL")
            asset_id = self._string(raw, "asset_id") or f"gen-{request.request_id}-c{index + 1}"
            safety = self._safety(raw.get("safety", payload.get("safety")))
            license_status = self._license(raw.get("license", payload.get("license")))
            provenance = GenerationProvenance(
                provider_id=self.provider_id,
                model_id=self._string(raw, "model_id") or self._string(payload, "model") or self._profile.model_id,
                model_version=self._string(raw, "model_version") or self._string(payload, "provider_version"),
                requested_at=self._string(raw, "created_at") or _now_iso(),
                input_prompt_hash=request.prompt_hash,
                seed=self._int(raw, "seed"),
                reference_asset_ids=request.effective_reference_asset_ids,
                parent_skill_id=request.requested_by,
                recipe_id=request.recipe.recipe_id if request.recipe else "",
                recipe_schema_version=request.recipe.schema_version if request.recipe else "",
                reference_bindings=tuple(
                    binding.to_dict() for binding in request.effective_reference_bindings
                ),
            )
            candidates.append(GeneratedAsset(
                asset_id=asset_id,
                request_id=request.request_id,
                modality=request.modality,
                provenance=provenance,
                license=license_status,
                safety=safety,
                cost_units=self._float(raw, "cost_units") or 0.0,
                preview_handle=handle,
                expected_sha256=(
                    self._string(raw, "sha256") or self._string(raw, "checksum")
                ),
                duration_seconds=self._float(raw, "duration_seconds"),
                created_at=self._string(raw, "created_at") or _now_iso(),
            ))
        actual_cost = self._float(payload, "actual_cost_units")
        if actual_cost is None:
            actual_cost = sum(candidate.cost_units for candidate in candidates)
        return GenerationResult(
            request_id=request.request_id,
            candidates=tuple(candidates),
            actual_cost_units=actual_cost or 0.0,
        )

    @staticmethod
    def _outputs(payload: Mapping[str, Any]) -> list[Any]:
        for key in ("outputs", "candidates", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _status(payload: Mapping[str, Any]) -> str:
        return str(payload.get("status", "")).strip().lower().replace("-", "_")

    @staticmethod
    def _string(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _float(payload: Mapping[str, Any], key: str) -> float | None:
        value = payload.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(payload: Mapping[str, Any], key: str) -> int | None:
        value = payload.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _license(value: Any) -> LicenseStatus:
        try:
            return LicenseStatus(str(value or LicenseStatus.UNKNOWN.value).lower())
        except ValueError:
            return LicenseStatus.UNKNOWN

    @staticmethod
    def _safety(value: Any) -> ContentSafetyResult:
        if not isinstance(value, Mapping):
            return ContentSafetyResult()
        categories = value.get("categories", ())
        if isinstance(categories, str):
            categories = (categories,)
        if not isinstance(categories, (list, tuple)):
            categories = ()
        return ContentSafetyResult(
            passed=bool(value.get("passed", True)),
            categories=tuple(str(item) for item in categories),
            provider_safety_version=str(value.get("provider_safety_version", "")),
            detail=str(value.get("detail", "")),
        )

    @staticmethod
    def _error(request: GenerationRequest, code: str, message: str) -> GenerationResult:
        return GenerationResult(
            request_id=request.request_id,
            error_code=code,
            error_message=message,
        )


@dataclass(frozen=True)
class _HttpAdapterError(Exception):
    code: str
    message: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_MAX_POLL_SECONDS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "HttpGenerationProvider",
    "HttpGenerationRoutes",
    "RemoteGenerationCheckpoint",
]
