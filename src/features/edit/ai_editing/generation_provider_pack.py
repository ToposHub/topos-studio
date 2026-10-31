"""Provider Pack 生命周期与首个 Seedance-compatible adapter 合同（M11 B4）。

Provider Pack 把 capability discovery、create/poll/resume/cancel 和输出归一化收在
provider 边界。UI、GenerationBin 和 timeline 不依赖厂商字段。默认 contract mock 是
明确的 ``demo``，真实 Seedance pack 需要宿主注入已配置 transport；本模块不保存密钥。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from src.features.edit.ai_editing.generation_models import (
    ContentSafetyResult,
    GeneratedAsset,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
    LicenseStatus,
)
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider,
)


class ProviderJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            ProviderJobState.SUCCEEDED,
            ProviderJobState.FAILED,
            ProviderJobState.CANCELLED,
        }


@dataclass(frozen=True)
class ProviderPackManifest:
    pack_id: str
    version: str
    provider_id: str
    modalities: tuple[GenerationModality, ...]
    capabilities: tuple[str, ...] = ()
    credential_ref: str = ""
    requires_network: bool = True
    is_demo: bool = False
    license: str = ""

    def __post_init__(self) -> None:
        if not self.pack_id or not self.version or not self.provider_id:
            raise ValueError("provider pack identity is required")
        if not self.modalities:
            raise ValueError("provider pack must declare a modality")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "provider_id": self.provider_id,
            "modalities": [m.value for m in self.modalities],
            "capabilities": list(self.capabilities),
            "credential_ref": self.credential_ref,
            "requires_network": self.requires_network,
            "is_demo": self.is_demo,
            "license": self.license,
        }


@dataclass(frozen=True)
class ProviderJob:
    job_id: str
    request_id: str
    provider_id: str
    state: ProviderJobState
    progress: float = 0.0
    checkpoint: dict[str, Any] = field(default_factory=dict)
    result: GenerationResult | None = None
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "state": self.state.value,
            "progress": self.progress,
            "checkpoint": dict(self.checkpoint),
            "result": self.result.to_dict() if self.result else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class GenerationProviderPack(Protocol):
    manifest: ProviderPackManifest

    def discover_capabilities(self) -> dict[str, Any]: ...
    def create(self, request: GenerationRequest) -> ProviderJob: ...
    def poll(self, job_id: str) -> ProviderJob: ...
    def resume(self, checkpoint: Mapping[str, Any]) -> ProviderJob: ...
    def cancel(self, job_id: str) -> ProviderJob: ...


class ProviderTransport(Protocol):
    """Seedance/HTTP host 注入的最小 transport；transport 负责 credential。"""

    def create(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def poll(self, job_id: str) -> Mapping[str, Any]: ...
    def cancel(self, job_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _ResumableRequest:
    """Minimal redacted request metadata needed after a process restart."""

    request_id: str
    modality: GenerationModality
    input_prompt_hash: str
    max_candidates: int = 1
    reference_asset_ids: tuple[str, ...] = ()
    recipe_id: str = ""
    recipe_schema_version: str = ""
    reference_bindings: tuple[dict[str, Any], ...] = ()

    @property
    def prompt_hash(self) -> str:
        return self.input_prompt_hash

    @property
    def effective_reference_asset_ids(self) -> tuple[str, ...]:
        return self.reference_asset_ids

    @property
    def effective_reference_bindings(self) -> tuple[Any, ...]:
        return tuple(_CheckpointReferenceBinding(item) for item in self.reference_bindings)

    @property
    def recipe(self) -> Any:
        if not self.recipe_id:
            return None
        return _CheckpointRecipe(self.recipe_id, self.recipe_schema_version)

    @property
    def budget(self) -> Any:
        return _CheckpointBudget(self.max_candidates)

    @classmethod
    def from_request(cls, request: GenerationRequest) -> "_ResumableRequest":
        recipe = request.recipe
        return cls(
            request_id=request.request_id,
            modality=request.modality,
            input_prompt_hash=request.prompt_hash,
            max_candidates=request.budget.max_candidates,
            reference_asset_ids=request.effective_reference_asset_ids,
            recipe_id=recipe.recipe_id if recipe else "",
            recipe_schema_version=recipe.schema_version if recipe else "",
            reference_bindings=tuple(
                binding.to_dict() for binding in request.effective_reference_bindings
            ),
        )

    @classmethod
    def from_checkpoint(cls, checkpoint: Mapping[str, Any]) -> "_ResumableRequest":
        raw = checkpoint.get("request")
        if not isinstance(raw, Mapping):
            raise ValueError("Seedance checkpoint is missing resumable request metadata")
        try:
            modality = GenerationModality(str(raw["modality"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("Seedance checkpoint has an invalid modality") from exc
        request_id = str(raw.get("request_id", ""))
        prompt_hash = str(raw.get("input_prompt_hash", ""))
        if not request_id or not prompt_hash:
            raise ValueError("Seedance checkpoint has incomplete request metadata")
        bindings = tuple(
            dict(item) for item in raw.get("reference_bindings", ()) or ()
            if isinstance(item, Mapping)
        )
        return cls(
            request_id=request_id,
            modality=modality,
            input_prompt_hash=prompt_hash,
            max_candidates=max(1, int(raw.get("max_candidates", 1) or 1)),
            reference_asset_ids=tuple(
                str(item) for item in raw.get("reference_asset_ids", ()) or ()
            ),
            recipe_id=str(raw.get("recipe_id", "")),
            recipe_schema_version=str(raw.get("recipe_schema_version", "")),
            reference_bindings=bindings,
        )

    def to_checkpoint_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "modality": self.modality.value,
            "input_prompt_hash": self.input_prompt_hash,
            "max_candidates": self.max_candidates,
            "reference_asset_ids": list(self.reference_asset_ids),
            "recipe_id": self.recipe_id,
            "recipe_schema_version": self.recipe_schema_version,
            "reference_bindings": [dict(item) for item in self.reference_bindings],
        }


@dataclass(frozen=True)
class _CheckpointReferenceBinding:
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class _CheckpointRecipe:
    recipe_id: str
    schema_version: str


@dataclass(frozen=True)
class _CheckpointBudget:
    max_candidates: int


class ContractMockProviderPack:
    """离线 contract mock：create → poll → succeeded/cancel/resume。"""

    def __init__(self, *, provider_id: str = "pack-contract-mock") -> None:
        self.manifest = ProviderPackManifest(
            pack_id=f"{provider_id}.pack", version="1.0.0", provider_id=provider_id,
            modalities=tuple(GenerationModality), capabilities=(
                "capability_discovery", "create", "poll", "resume", "cancel",
            ), requires_network=False, is_demo=True, license="internal-test",
        )
        self._provider = DeterministicGenerationProvider(provider_id=provider_id)
        self._jobs: dict[str, ProviderJob] = {}
        self._requests: dict[str, GenerationRequest] = {}
        self._polls: dict[str, int] = {}

    def discover_capabilities(self) -> dict[str, Any]:
        return {
            "source": "demo",
            "pack": self.manifest.to_dict(),
            "modalities": [m.value for m in self.manifest.modalities],
        }

    def create(self, request: GenerationRequest) -> ProviderJob:
        if request.modality not in self.manifest.modalities:
            return self._failed(request, "UNSUPPORTED_MODALITY", "pack does not support modality")
        job_id = f"{self.manifest.provider_id}:{request.request_id}"
        job = ProviderJob(
            job_id=job_id, request_id=request.request_id,
            provider_id=self.manifest.provider_id, state=ProviderJobState.QUEUED,
            checkpoint={"pack_id": self.manifest.pack_id, "request_id": request.request_id,
                        "remote_job_id": job_id},
        )
        self._jobs[job_id] = job
        self._requests[job_id] = request
        self._polls[job_id] = 0
        return job

    def poll(self, job_id: str) -> ProviderJob:
        job = self._require(job_id)
        if job.state.terminal:
            return job
        count = self._polls.get(job_id, 0) + 1
        self._polls[job_id] = count
        request = self._requests[job_id]
        if count == 1:
            updated = ProviderJob(
                job_id=job.job_id, request_id=job.request_id,
                provider_id=job.provider_id, state=ProviderJobState.RUNNING,
                progress=0.5, checkpoint=job.checkpoint,
            )
        else:
            result = self._provider.generate(request)
            updated = ProviderJob(
                job_id=job.job_id, request_id=job.request_id,
                provider_id=job.provider_id,
                state=ProviderJobState.SUCCEEDED if result.succeeded else ProviderJobState.FAILED,
                progress=1.0, checkpoint=job.checkpoint, result=result,
                error_code=result.error_code, error_message=result.error_message,
            )
        self._jobs[job_id] = updated
        return updated

    def resume(self, checkpoint: Mapping[str, Any]) -> ProviderJob:
        pack_id = str(checkpoint.get("pack_id", ""))
        job_id = str(checkpoint.get("remote_job_id", ""))
        if pack_id != self.manifest.pack_id or not job_id:
            raise ValueError("provider checkpoint does not match pack")
        return self._require(job_id)

    def cancel(self, job_id: str) -> ProviderJob:
        job = self._require(job_id)
        if job.state.terminal:
            return job
        updated = ProviderJob(
            job_id=job.job_id, request_id=job.request_id,
            provider_id=job.provider_id, state=ProviderJobState.CANCELLED,
            progress=job.progress, checkpoint=job.checkpoint,
            error_code="GEN_PROVIDER_CANCELLED", error_message="cancelled by user",
        )
        self._jobs[job_id] = updated
        return updated

    def _require(self, job_id: str) -> ProviderJob:
        if job_id not in self._jobs:
            raise KeyError(f"provider job not found: {job_id}")
        return self._jobs[job_id]

    def _failed(self, request: GenerationRequest, code: str, message: str) -> ProviderJob:
        return ProviderJob(
            job_id=f"{self.manifest.provider_id}:failed:{request.request_id}",
            request_id=request.request_id, provider_id=self.manifest.provider_id,
            state=ProviderJobState.FAILED, error_code=code, error_message=message,
        )


class SeedanceVideoProviderPack:
    """Seedance-compatible video Pack。

    这里只定义 provider-neutral lifecycle 和 transport mapping；真实 endpoint、鉴权和
    上传策略由宿主注入 ``ProviderTransport``，因此 import 这个类不会联网。
    """

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        model_id: str = "seedance-video",
        pack_version: str = "1.0.0",
    ) -> None:
        self.transport = transport
        self.model_id = model_id
        self.manifest = ProviderPackManifest(
            pack_id="seedance.video", version=pack_version, provider_id="seedance",
            modalities=(GenerationModality.VIDEO,),
            capabilities=("image_to_video", "first_last_frame", "poll", "resume", "cancel"),
            requires_network=True, is_demo=False, license="provider-terms",
        )
        self._requests: dict[str, GenerationRequest | _ResumableRequest] = {}
        self._jobs: dict[str, ProviderJob] = {}
        # AUD-13607：worker 线程 poll 与 UI 线程 cancel 可能并发访问两个字典，
        # 参照 goal_round_driver 的 RLock 模式保护读写（transport 网络调用留在
        # 锁外，避免持锁做阻塞 IO）。RLock 因 resume() 复用 poll()。
        self._state_lock = threading.RLock()

    def discover_capabilities(self) -> dict[str, Any]:
        return {"source": "provider_transport", "pack": self.manifest.to_dict()}

    def create(self, request: GenerationRequest) -> ProviderJob:
        if request.modality != GenerationModality.VIDEO:
            return self._failed(request, "UNSUPPORTED_MODALITY", "Seedance pack supports video only")
        payload = self.transport.create(request.to_dict(retain_prompts=True))
        job_id = str(payload.get("job_id", payload.get("id", "")))
        if not job_id:
            return self._failed(request, "GEN_PROVIDER_SCHEMA_INVALID", "create response has no job id")
        with self._state_lock:
            self._requests[job_id] = request
            job = self._from_payload(job_id, request, payload)
            self._jobs[job_id] = job
        return job

    def poll(self, job_id: str) -> ProviderJob:
        with self._state_lock:
            request = self._requests[job_id]
        # transport 调用（网络 IO）在锁外执行；仅字典读写持锁。
        job = self._from_payload(job_id, request, self.transport.poll(job_id))
        with self._state_lock:
            self._jobs[job_id] = job
        return job

    def resume(self, checkpoint: Mapping[str, Any]) -> ProviderJob:
        job_id = str(checkpoint.get("remote_job_id", ""))
        if not job_id or str(checkpoint.get("pack_id", "")) != self.manifest.pack_id:
            raise ValueError("provider checkpoint does not match Seedance pack")
        # A resumed job can be owned by a freshly started application. Restore
        # its redacted identity before polling rather than relying on the old
        # process-local request dictionary.
        with self._state_lock:
            self._requests[job_id] = _ResumableRequest.from_checkpoint(checkpoint)
        return self.poll(job_id)

    def cancel(self, job_id: str) -> ProviderJob:
        with self._state_lock:
            request = self._requests[job_id]
        job = self._from_payload(job_id, request, self.transport.cancel(job_id))
        with self._state_lock:
            self._jobs[job_id] = job
        return job

    def _from_payload(
        self,
        job_id: str,
        request: GenerationRequest | _ResumableRequest,
        payload: Mapping[str, Any],
    ) -> ProviderJob:
        status = str(payload.get("status", "running")).lower().replace("-", "_")
        state = {
            "queued": ProviderJobState.QUEUED,
            "pending": ProviderJobState.QUEUED,
            "running": ProviderJobState.RUNNING,
            "processing": ProviderJobState.RUNNING,
            "succeeded": ProviderJobState.SUCCEEDED,
            "completed": ProviderJobState.SUCCEEDED,
            "failed": ProviderJobState.FAILED,
            "error": ProviderJobState.FAILED,
            "cancelled": ProviderJobState.CANCELLED,
            "canceled": ProviderJobState.CANCELLED,
        }.get(status, ProviderJobState.RUNNING)
        result = _normalize_result(self.manifest.provider_id, self.model_id, request, payload)
        if result is not None and result.succeeded:
            state = ProviderJobState.SUCCEEDED
        checkpoint = {
            "pack_id": self.manifest.pack_id, "request_id": request.request_id,
            "remote_job_id": job_id,
            "request": (
                _ResumableRequest.from_request(request).to_checkpoint_dict()
                if isinstance(request, GenerationRequest)
                else request.to_checkpoint_dict()
            ),
        }
        return ProviderJob(
            job_id=job_id, request_id=request.request_id, provider_id=self.manifest.provider_id,
            state=state, progress=1.0 if state.terminal else 0.5,
            checkpoint=checkpoint, result=result,
            error_code="GEN_PROVIDER_FAILED" if state == ProviderJobState.FAILED else "",
            error_message=str(payload.get("error", "")),
        )

    def _failed(self, request: GenerationRequest, code: str, message: str) -> ProviderJob:
        return ProviderJob(
            job_id=f"seedance:failed:{request.request_id}", request_id=request.request_id,
            provider_id=self.manifest.provider_id, state=ProviderJobState.FAILED,
            error_code=code, error_message=message,
        )


class ProviderPackGenerationAdapter:
    """将 Pack lifecycle 适配到现有 ``GenerationProvider`` 同步接口。"""

    def __init__(
        self,
        pack: GenerationProviderPack,
        *,
        max_polls: int = 120,
        poll_interval_seconds: float = 0.25,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.pack = pack
        self.provider_id = pack.manifest.provider_id
        self.is_demo = pack.manifest.is_demo
        self._max_polls = max(1, max_polls)
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._sleep_fn = sleep_fn

    @property
    def supported_modalities(self) -> tuple[GenerationModality, ...]:
        return self.pack.manifest.modalities

    def generate(
        self, request: GenerationRequest,
        *, progress: Callable[[float, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> GenerationResult:
        job = self.pack.create(request)
        for index in range(self._max_polls):
            if progress is not None:
                progress(job.progress, f"{self.provider_id}: {job.state.value}")
            if job.state.terminal:
                return job.result or GenerationResult(
                    request_id=request.request_id,
                    error_code=job.error_code or "GEN_PROVIDER_FAILED",
                    error_message=job.error_message,
                )
            if is_cancelled is not None and is_cancelled():
                cancelled = self.pack.cancel(job.job_id)
                return GenerationResult(
                    request_id=request.request_id,
                    error_code=cancelled.error_code or "GEN_PROVIDER_CANCELLED",
                    error_message=cancelled.error_message or "provider pack cancellation requested",
                )
            if self._poll_interval_seconds:
                self._sleep_fn(self._poll_interval_seconds)
            job = self.pack.poll(job.job_id)
            # The final permitted poll may itself complete/fail the remote
            # task.  Return that terminal state before the timeout branch;
            # otherwise ``max_polls=1`` could cancel a successfully completed
            # generation and report a false timeout.
            if job.state.terminal:
                if progress is not None:
                    progress(job.progress, f"{self.provider_id}: {job.state.value}")
                return job.result or GenerationResult(
                    request_id=request.request_id,
                    error_code=job.error_code or "GEN_PROVIDER_FAILED",
                    error_message=job.error_message,
                )
        # A local timeout must cancel the remote task as well, otherwise a
        # costly provider job can continue after Topos has dropped it.
        try:
            cancelled = self.pack.cancel(job.job_id)
            cancel_detail = cancelled.error_message or cancelled.state.value
        except Exception as exc:  # noqa: BLE001 -- timeout remains a hard failure
            cancel_detail = f"remote cancellation failed: {type(exc).__name__}"
        return GenerationResult(
            request_id=request.request_id,
            error_code="GEN_PROVIDER_TIMEOUT",
            error_message=(
                f"provider pack exceeded {self._max_polls} polls; "
                f"remote cancellation: {cancel_detail}"
            ),
        )


def _normalize_result(
    provider_id: str,
    model_id: str,
    request: GenerationRequest,
    payload: Mapping[str, Any],
) -> GenerationResult | None:
    outputs = payload.get("outputs", payload.get("candidates", ()))
    if not isinstance(outputs, list) or not outputs:
        return None
    candidates: list[GeneratedAsset] = []
    for index, item in enumerate(outputs[: max(1, request.budget.max_candidates)]):
        if not isinstance(item, Mapping):
            return GenerationResult(request_id=request.request_id, error_code="GEN_PROVIDER_SCHEMA_INVALID")
        handle = str(item.get("url", item.get("preview_url", "")))
        if not handle:
            return GenerationResult(request_id=request.request_id, error_code="GEN_PROVIDER_SCHEMA_INVALID")
        # 外部 payload 的数值字段必须安全转换（铁律：外部数据须验证）：
        # 非数值内容返回类型化 GEN_PROVIDER_SCHEMA_INVALID，而不是让
        # float() 的 ValueError 穿透 poll/generate。
        try:
            cost_units = float(item.get("cost_units", 0.0) or 0.0)
        except (TypeError, ValueError):
            return GenerationResult(
                request_id=request.request_id,
                error_code="GEN_PROVIDER_SCHEMA_INVALID",
                error_message=f"output {index + 1} cost_units is not numeric",
            )
        raw_duration = item.get("duration_seconds")
        if raw_duration is None:
            duration_seconds: float | None = None
        else:
            try:
                duration_seconds = float(raw_duration)
            except (TypeError, ValueError):
                return GenerationResult(
                    request_id=request.request_id,
                    error_code="GEN_PROVIDER_SCHEMA_INVALID",
                    error_message=f"output {index + 1} duration_seconds is not numeric",
                )
        candidates.append(GeneratedAsset(
            asset_id=str(item.get("asset_id", f"gen-{request.request_id}-c{index + 1}")),
            request_id=request.request_id, modality=request.modality,
            provenance=GenerationProvenance(
                provider_id=provider_id, model_id=str(item.get("model_id", model_id)),
                model_version=str(item.get("model_version", "provider-pack-v1")),
                input_prompt_hash=request.prompt_hash,
                reference_asset_ids=request.effective_reference_asset_ids,
                recipe_id=request.recipe.recipe_id if request.recipe else "",
                recipe_schema_version=request.recipe.schema_version if request.recipe else "",
                reference_bindings=tuple(
                    binding.to_dict() for binding in request.effective_reference_bindings
                ),
            ),
            license=LicenseStatus.UNKNOWN,
            safety=ContentSafetyResult(passed=True),
            cost_units=cost_units,
            preview_handle=handle,
            expected_sha256=str(item.get("sha256", item.get("checksum", "")) or ""),
            duration_seconds=duration_seconds,
        ))
    try:
        actual_cost_units = float(payload.get("actual_cost_units", 0.0) or 0.0)
    except (TypeError, ValueError):
        return GenerationResult(
            request_id=request.request_id,
            error_code="GEN_PROVIDER_SCHEMA_INVALID",
            error_message="actual_cost_units is not numeric",
        )
    return GenerationResult(
        request_id=request.request_id, candidates=tuple(candidates),
        actual_cost_units=actual_cost_units,
    )


__all__ = [
    "ProviderJobState", "ProviderPackManifest", "ProviderJob",
    "GenerationProviderPack", "ProviderTransport", "ContractMockProviderPack",
    "SeedanceVideoProviderPack", "ProviderPackGenerationAdapter",
]
