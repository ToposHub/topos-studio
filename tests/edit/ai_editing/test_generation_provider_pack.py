"""B4 Provider Pack lifecycle contract tests (offline + transport mock)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.generation_provider_pack import (
    ContractMockProviderPack,
    ProviderPackGenerationAdapter,
    ProviderJobState,
    SeedanceVideoProviderPack,
)


def _request() -> GenerationRequest:
    return GenerationRequest(
        request_id="r-pack", modality=GenerationModality.VIDEO,
        prompt="产品特写", consent=GenerationConsent(
            confirmed=True, provider_id="pack-contract-mock",
            cost_acknowledged=True, upload_acknowledged=True,
            copyright_acknowledged=True,
        ),
    )


def test_contract_mock_create_poll_cancel_resume() -> None:
    pack = ContractMockProviderPack()
    created = pack.create(_request())
    assert created.state is ProviderJobState.QUEUED
    assert created.checkpoint["pack_id"] == pack.manifest.pack_id
    resumed = pack.resume(created.checkpoint)
    assert resumed.job_id == created.job_id
    assert pack.poll(created.job_id).state is ProviderJobState.RUNNING
    done = pack.poll(created.job_id)
    assert done.state is ProviderJobState.SUCCEEDED
    assert done.result is not None and done.result.succeeded


def test_pack_adapter_preserves_existing_generation_provider_contract() -> None:
    adapter = ProviderPackGenerationAdapter(
        ContractMockProviderPack(), poll_interval_seconds=0.0,
    )
    result = adapter.generate(_request())
    assert result.succeeded
    assert result.candidates[0].provenance.provider_id == "pack-contract-mock"
    assert adapter.is_demo is True


def test_pack_adapter_propagates_cooperative_cancel_to_provider_pack() -> None:
    pack = ContractMockProviderPack()
    adapter = ProviderPackGenerationAdapter(pack, poll_interval_seconds=0.0)

    result = adapter.generate(_request(), is_cancelled=lambda: True)

    assert result.error_code == "GEN_PROVIDER_CANCELLED"
    assert result.error_message == "cancelled by user"


class _Transport:
    def __init__(self) -> None:
        self.states = [
            {"status": "running"},
            {"status": "succeeded", "outputs": [{"url": "https://provider/out.mp4"}]},
        ]

    def create(self, payload):
        assert payload["modality"] == "video"
        return {"id": "remote-1", "status": "queued"}

    def poll(self, job_id):
        assert job_id == "remote-1"
        return self.states.pop(0)

    def cancel(self, job_id):
        return {"status": "cancelled"}


def test_seedance_pack_is_transport_injected_and_not_demo() -> None:
    pack = SeedanceVideoProviderPack(_Transport())
    created = pack.create(_request())
    assert created.state is ProviderJobState.QUEUED
    running = pack.poll(created.job_id)
    assert running.state is ProviderJobState.RUNNING
    done = pack.poll(created.job_id)
    assert done.state is ProviderJobState.SUCCEEDED
    assert done.result is not None and done.result.candidates[0].preview_handle.endswith(".mp4")
    assert pack.manifest.is_demo is False


def test_seedance_pack_resume_restores_redacted_request_after_restart() -> None:
    first = SeedanceVideoProviderPack(_Transport())
    created = first.create(_request())
    checkpoint = created.checkpoint
    assert "产品特写" not in str(checkpoint)

    restarted = SeedanceVideoProviderPack(_Transport())
    resumed = restarted.resume(checkpoint)
    assert resumed.job_id == "remote-1"
    assert resumed.request_id == "r-pack"
    completed = restarted.poll(resumed.job_id)
    assert completed.result is not None and completed.result.succeeded


def test_pack_timeout_cancels_remote_job() -> None:
    class StuckPack(ContractMockProviderPack):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled: list[str] = []

        def poll(self, job_id):
            job = self._require(job_id)
            return type(job)(
                job_id=job.job_id, request_id=job.request_id,
                provider_id=job.provider_id, state=ProviderJobState.RUNNING,
                progress=0.5, checkpoint=job.checkpoint,
            )

        def cancel(self, job_id):
            self.cancelled.append(job_id)
            return super().cancel(job_id)

    pack = StuckPack()
    result = ProviderPackGenerationAdapter(
        pack, max_polls=1, poll_interval_seconds=0.0,
    ).generate(_request())
    assert result.error_code == "GEN_PROVIDER_TIMEOUT"
    assert pack.cancelled == ["pack-contract-mock:r-pack"]


def test_final_allowed_poll_returns_completed_result_without_cancelling() -> None:
    transport = _Transport()
    transport.states = [{
        "status": "completed",
        "outputs": [{"url": "https://provider.example/output.mp4"}],
    }]
    pack = SeedanceVideoProviderPack(transport)
    result = ProviderPackGenerationAdapter(
        pack, max_polls=1, poll_interval_seconds=0.0,
    ).generate(_request())
    assert result.succeeded is True
