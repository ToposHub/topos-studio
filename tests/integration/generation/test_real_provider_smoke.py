"""Opt-in smoke test for a provider-neutral real generation endpoint.

The default suite skips this test. Enabling it requires an isolated test account and
explicit environment variables; secrets are read once into the credential abstraction and
never printed or serialized.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.real_provider]

from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.generation_provider import ProviderRegistry
from src.features.edit.ai_editing.http_generation_provider import HttpGenerationProvider
from src.features.edit.ai_editing.jobs import JobRegistry, JobStatus
from src.features.edit.ai_editing.media_generation_service import GenerationService
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
    ProviderLimits,
)


@pytest.mark.skipif(
    os.getenv("TOPOS_RUN_REAL_GENERATION") != "1",
    reason="real generation requires TOPOS_RUN_REAL_GENERATION=1",
)
def test_real_provider_generation_smoke() -> None:
    endpoint = os.getenv("TOPOS_REAL_GENERATION_ENDPOINT", "")
    secret = os.getenv("TOPOS_REAL_GENERATION_SECRET", "")
    if not endpoint or not secret:
        pytest.skip(
            "set TOPOS_REAL_GENERATION_ENDPOINT and TOPOS_REAL_GENERATION_SECRET "
            "in an isolated test environment",
        )
    provider_id = os.getenv("TOPOS_REAL_GENERATION_PROVIDER_ID", "real-generation")
    model_id = os.getenv("TOPOS_REAL_GENERATION_MODEL", "")
    credential_ref = "topos:real-generation-smoke"
    credentials = FakeCredentialStore()
    credentials.set_secret(credential_ref, secret)
    profile = AiProviderProfile(
        profile_id=provider_id, display_name="isolated real generation smoke",
        provider_kind="generation_http", endpoint=endpoint, model_id=model_id,
        credential_ref=credential_ref, modalities=("video",),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=False),
        limits=ProviderLimits(request_timeout_seconds=60.0, max_retries=0),
    )
    provider = HttpGenerationProvider(profile, credentials)
    registry = ProviderRegistry()
    registry.register(provider, preferred=True)
    request = GenerationRequest(
        request_id="real-generation-smoke", modality=GenerationModality.VIDEO,
        prompt="short abstract product motion, no identifiable people",
        consent=GenerationConsent(
            confirmed=True, provider_id=provider_id,
            cost_acknowledged=True, upload_acknowledged=True,
            copyright_acknowledged=True, target_location="generation_bin",
        ),
    )
    job, diagnostics = GenerationService(
        registry, jobs=JobRegistry(),
    ).request_generation(request)
    assert diagnostics == []
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.result_manifest and job.result_manifest["candidate_ids"]
