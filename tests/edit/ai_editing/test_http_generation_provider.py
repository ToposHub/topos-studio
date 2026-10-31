"""P2 mock-fixture tests for the provider-neutral HTTP generation adapter."""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.http_generation_provider import HttpGenerationProvider
from src.features.edit.ai_editing.jobs import JobRegistry, JobStatus
from src.features.edit.ai_editing.media_generation_service import GenerationService
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore as ProfileFakeCredentialStore,
    ProviderLimits,
)
from src.features.edit.ai_editing.generation_provider import ProviderRegistry


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self._raw))}

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _ScriptedOpener:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, request: Any, *, timeout: float) -> _Response:
        self.calls.append((request.method, request.full_url, dict(request.header_items())))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _Response(response)


def _profile(*, modalities: tuple[str, ...] = ("video",), allow_upload: bool = False) -> AiProviderProfile:
    return AiProviderProfile(
        profile_id="remote-video",
        display_name="Remote Video",
        provider_kind="generation_http",
        endpoint="https://provider.example",
        model_id="video-v1",
        credential_ref="topos:remote-video",
        modalities=modalities,
        data_policy=DataPolicy(allow_media_upload=allow_upload),
        limits=ProviderLimits(request_timeout_seconds=1.0, max_retries=1),
    )


def _request(**kwargs: Any) -> GenerationRequest:
    values = dict(
        request_id="req-1",
        modality=GenerationModality.VIDEO,
        prompt="产品发布会特写",
        consent=GenerationConsent(
            confirmed=True,
            provider_id="remote-video",
            cost_acknowledged=True,
            upload_acknowledged=True,
            copyright_acknowledged=True,
        ),
    )
    values.update(kwargs)
    return GenerationRequest(**values)


def _provider(opener: _ScriptedOpener, *, allow_upload: bool = False) -> HttpGenerationProvider:
    credentials = ProfileFakeCredentialStore()
    credentials.set_secret("topos:remote-video", "test-secret")
    return HttpGenerationProvider(
        _profile(allow_upload=allow_upload),
        credentials,
        opener=opener,
        sleep=lambda _seconds: None,
        poll_interval_seconds=0,
    )


def test_http_provider_submits_polls_and_maps_provenance() -> None:
    opener = _ScriptedOpener(
        {"id": "remote-1", "status": "queued"},
        {"id": "remote-1", "status": "running", "progress": 0.4},
        {
            "id": "remote-1",
            "status": "succeeded",
            "model": "video-v1",
            "provider_version": "2026.08",
            "actual_cost_units": 1.25,
            "outputs": [{
                "asset_id": "remote-asset-1",
                "download_url": "https://provider.example/media/remote-asset-1.mp4",
                "license": "known_licensed",
                "safety": {"passed": True, "provider_safety_version": "safe-2"},
                "seed": 42,
            }],
        },
    )
    provider = _provider(opener)
    progress: list[float] = []
    result = provider.generate(_request(), progress=lambda fraction, _message: progress.append(fraction))

    assert result.succeeded
    assert result.actual_cost_units == 1.25
    assert result.candidates[0].provenance.provider_id == "remote-video"
    assert result.candidates[0].provenance.model_version == "2026.08"
    assert result.candidates[0].license.value == "known_licensed"
    assert result.candidates[0].preview_handle.endswith(".mp4")
    assert progress[-1] == 1.0
    assert provider.last_remote_job_id == "remote-1"
    assert opener.calls[0][0] == "POST"
    assert opener.calls[0][2]["Authorization"] == "Bearer test-secret"
    assert opener.calls[0][2]["Idempotency-key"] == "req-1"


def test_http_provider_can_resume_polling_from_a_persisted_checkpoint() -> None:
    opener = _ScriptedOpener({
        "id": "remote-1",
        "status": "succeeded",
        "outputs": [{"url": "https://provider.example/media/resumed.mp4"}],
    })
    provider = _provider(opener)
    checkpoints: list[dict[str, Any]] = []
    result = provider.generate(
        _request(),
        checkpoint=checkpoints.append,
        resume_remote_job_id="remote-1",
    )
    assert result.succeeded
    assert opener.calls[0][0] == "GET"
    assert checkpoints[0]["state"] == "resumed"
    assert checkpoints[-1]["state"] == "succeeded"


def test_http_provider_propagates_cooperative_cancel_to_remote_job() -> None:
    opener = _ScriptedOpener(
        {"id": "remote-1", "status": "queued"},
        {"status": "cancelled"},
    )
    provider = _provider(opener)
    checkpoints: list[dict[str, Any]] = []

    result = provider.generate(
        _request(),
        checkpoint=checkpoints.append,
        is_cancelled=lambda: True,
    )

    assert result.error_code == "GEN_PROVIDER_CANCELLED"
    assert [call[0] for call in opener.calls] == ["POST", "POST"]
    assert opener.calls[1][1].endswith("/v1/generations/remote-1/cancel")
    assert checkpoints[-1]["state"] == "cancelled"


def test_http_provider_retries_transient_network_failure_without_exposing_secret() -> None:
    opener = _ScriptedOpener(
        urllib.error.URLError("secret=test-secret"),
        {
            "status": "succeeded",
            "outputs": [{"url": "https://provider.example/media/out.png"}],
        },
    )
    provider = _provider(opener)
    result = provider.generate(_request(modality=GenerationModality.VIDEO))
    assert result.succeeded
    assert len(opener.calls) == 2
    assert "test-secret" not in result.error_message


def test_http_provider_requires_credential_and_upload_policy() -> None:
    credentials = ProfileFakeCredentialStore()
    provider = HttpGenerationProvider(
        _profile(), credentials, opener=_ScriptedOpener(), poll_interval_seconds=0,
    )
    missing = provider.generate(_request())
    assert missing.error_code == "GEN_PROVIDER_CREDENTIAL_MISSING"

    credentials.set_secret("topos:remote-video", "test-secret")
    blocked = provider.generate(_request(reference_asset_ids=("asset-1",)))
    assert blocked.error_code == "GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED"


def test_generation_service_blocks_unsafe_and_over_budget_results() -> None:
    unsafe_opener = _ScriptedOpener({
        "status": "succeeded",
        "outputs": [{
            "url": "https://provider.example/media/out.mp4",
            "safety": {"passed": False, "categories": ["copyright"]},
        }],
    })
    unsafe_provider = _provider(unsafe_opener)
    unsafe_registry = ProviderRegistry()
    unsafe_registry.register(unsafe_provider, preferred=True)
    unsafe_service = GenerationService(
        unsafe_registry, bin=GenerationBin(), jobs=JobRegistry(),
    )
    unsafe_job, unsafe_diags = unsafe_service.request_generation(_request())
    assert unsafe_job is not None
    assert unsafe_job.status == JobStatus.FAILED
    assert unsafe_job.error_code == "CONTENT_SAFETY_BLOCKED"
    assert unsafe_service.generation_bin.count == 0
    assert unsafe_diags[0].code.value == "TAEP_QUALITY_GATE_FAILED"

    budget_opener = _ScriptedOpener({
        "status": "succeeded",
        "actual_cost_units": 3.0,
        "outputs": [{"url": "https://provider.example/media/out.mp4"}],
    })
    budget_provider = _provider(budget_opener)
    budget_registry = ProviderRegistry()
    budget_registry.register(budget_provider, preferred=True)
    budget_service = GenerationService(
        budget_registry, bin=GenerationBin(), jobs=JobRegistry(),
    )
    budget_job, budget_diags = budget_service.request_generation(
        _request(budget=GenerationBudget(max_cost_units=2.0)),
    )
    assert budget_job is not None
    assert budget_job.status == JobStatus.FAILED
    assert budget_job.error_code == "GENERATION_BUDGET_EXCEEDED"
    assert budget_service.generation_bin.count == 0
    assert budget_diags[0].code.value == "TAEP_QUOTA_EXCEEDED"
