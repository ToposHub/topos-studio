"""Opt-in-free Provider Pack integration contract.

This test deliberately uses the explicit offline contract mock. Real provider smoke tests
must be separately marked ``real_provider`` and require isolated credentials.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = [pytest.mark.edit_core, pytest.mark.integration]

from src.features.edit.ai_editing.generated_media_materializer import GeneratedMediaMaterializer
from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.generation_provider_pack import (
    ContractMockProviderPack,
    ProviderJobState,
)


class _Importer:
    def __init__(self) -> None:
        self.assets = []

    def import_asset(self, asset):
        self.assets.append(asset)
        return f"media-{asset.asset_id}", []


def _request() -> GenerationRequest:
    return GenerationRequest(
        request_id="integration-pack-1", modality=GenerationModality.VIDEO,
        prompt="脱敏测试镜头", consent=GenerationConsent(
            confirmed=True, provider_id="pack-contract-mock",
            cost_acknowledged=True, upload_acknowledged=True,
            copyright_acknowledged=True, target_location="generation_bin",
        ),
    )


def test_pack_create_poll_cancel_resume_materialize_import(tmp_path: Path) -> None:
    pack = ContractMockProviderPack()
    request = _request()

    created = pack.create(request)
    assert created.state is ProviderJobState.QUEUED
    cancelled = pack.cancel(created.job_id)
    assert cancelled.state is ProviderJobState.CANCELLED
    assert cancelled.result is None

    resumed_job = pack.create(request)
    resumed = pack.resume(resumed_job.checkpoint)
    assert resumed.state is ProviderJobState.QUEUED
    assert pack.poll(resumed.job_id).state is ProviderJobState.RUNNING
    completed = pack.poll(resumed.job_id)
    assert completed.state is ProviderJobState.SUCCEEDED
    assert completed.result is not None and completed.result.succeeded

    candidate = completed.result.candidates[0]
    source = tmp_path / "candidate.mp4"
    source.write_bytes(b"offline-provider-payload")
    materialized = replace(candidate, preview_handle=str(source))
    result = GeneratedMediaMaterializer(cache_dir=tmp_path / "cache").materialize(materialized)
    assert result.succeeded and result.sha256 and result.size_bytes > 0

    bin = GenerationBin()
    bin.add_candidates((replace(materialized, preview_handle=result.path),))
    bin.mark_previewed(materialized.asset_id)
    importer = _Importer()
    media_id, diagnostics = bin.import_to_media_pool(
        materialized.asset_id, importer=importer, approved_by="integration-user",
    )
    assert diagnostics == []
    assert media_id == f"media-{materialized.asset_id}"
    report = bin.provenance_report()
    assert report["imported_count"] == 1
    assert report["entries"][0]["approved_by"] == "integration-user"
    assert report["entries"][0]["provenance"]["provider_id"] == "pack-contract-mock"
