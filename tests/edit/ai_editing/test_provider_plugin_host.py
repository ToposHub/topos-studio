"""P3 provider/analysis plugin lifecycle bridge tests."""

from __future__ import annotations

from typing import Any

from src.features.ai_harness.plugin_runtime import PluginExecutionPolicy, PluginState
from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
)
from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.provider_plugin import (
    AnalysisPluginHost,
    ProviderDescriptor,
)


class _AnalysisProvider:
    provider_id = "transcript.plugin"
    supported_kinds = (SegmentKind.TRANSCRIPT,)

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[Any, ...]]:
        return (
            AssetAnalysis(
                asset_id=asset_id,
                fingerprint=fingerprint,
                fps=fps,
                provenance=AnalysisProvenance(provider=self.provider_id, model="test"),
            ),
            (),
        )


def test_analysis_provider_is_hidden_until_plugin_activation() -> None:
    service = AnalysisService()
    host = AnalysisPluginHost(service)
    provider = _AnalysisProvider()
    descriptor = ProviderDescriptor(
        provider_id=provider.provider_id,
        display_name="Local transcript plugin",
        capabilities=("transcript", "transcript"),
    )

    assert host.register(
        provider,
        descriptor,
        policy=PluginExecutionPolicy(max_concurrent_calls=2),
    )
    assert provider.provider_id not in service.available_providers()
    assert host.list_descriptors(active_only=True) == ()

    assert host.activate(provider.provider_id)
    assert provider.provider_id in service.available_providers()
    assert host.list_descriptors(active_only=True) == (descriptor,)
    record = host.plugin_registry.get(host.plugin_id_for(provider.provider_id))
    assert record is not None
    assert record.state == PluginState.ACTIVE
    assert record.policy.max_concurrent_calls == 2

    assert host.deactivate(provider.provider_id)
    assert provider.provider_id not in service.available_providers()
    assert host.list_descriptors(active_only=True) == ()


def test_provider_descriptor_marks_cloud_data_consent_and_duplicate_is_rejected() -> None:
    descriptor = ProviderDescriptor(
        provider_id="remote.ocr",
        display_name="Remote OCR",
        provider_kind="analysis",
        data_policy="cloud",
    )
    assert descriptor.requires_consent
    assert descriptor.to_dict()["data_policy"] == "cloud"

    service = AnalysisService()
    host = AnalysisPluginHost(service)
    built_in_collision = _AnalysisProvider()
    built_in_collision.provider_id = "scene_cut_local"
    assert not host.register(built_in_collision)

    provider = _AnalysisProvider()
    assert host.register(provider)
    assert not host.register(provider)


def test_provider_descriptor_rejects_unknown_kind_or_policy() -> None:
    try:
        ProviderDescriptor(
            provider_id="bad.kind",
            display_name="Bad",
            provider_kind="arbitrary",
        )
    except ValueError as exc:
        assert "provider kind" in str(exc)
    else:
        raise AssertionError("unknown provider kind must fail closed")

    try:
        ProviderDescriptor(
            provider_id="bad.policy",
            display_name="Bad",
            data_policy="arbitrary",
        )
    except ValueError as exc:
        assert "data policy" in str(exc)
    else:
        raise AssertionError("unknown data policy must fail closed")
