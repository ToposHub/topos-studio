"""P4/P8/P9 evidence and commercial-export gate tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.commercial_gate import check_generated_media_for_export
from src.features.edit.ai_editing.evidence_policy import validate_evidence_selection
from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationModality,
    GenerationProvenance,
    LicenseStatus,
)


class _Importer:
    def import_asset(self, asset):
        return f"media-{asset.asset_id}", []


def _commercial_asset() -> GeneratedAsset:
    return GeneratedAsset(
        asset_id="generated-1",
        request_id="request-1",
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(
            provider_id="provider-1",
            model_id="model-1",
            model_version="2026.08",
            input_prompt_hash="prompt-hash",
            license_evidence_ref="license:provider-1-2026",
            license_region="global",
            license_checked_at="2026-08-10T00:00:00Z",
        ),
        license=LicenseStatus.KNOWN_LICENSED,
    )


def test_evidence_selection_requires_known_scoped_refs() -> None:
    result = validate_evidence_selection(
        ("seg-1", "seg-2"),
        {"seg-1": "asset-1", "seg-2": "asset-2"},
        required_count=2,
        allowed_asset_ids=("asset-1",),
    )
    assert not result.valid
    assert result.unknown_refs == ()
    assert result.unscoped_refs == ("seg-2",)

    valid = validate_evidence_selection(
        ("seg-1",), {"seg-1": "asset-1"}, allowed_asset_ids=("asset-1",),
    )
    assert valid.valid
    object_value = validate_evidence_selection(
        ("seg-1",), {"seg-1": SimpleNamespace(asset_id="asset-1")},
        allowed_asset_ids=("asset-1",),
    )
    assert object_value.valid


def test_commercial_gate_fails_closed_only_when_requested() -> None:
    assert check_generated_media_for_export(None).allowed
    required_without_store = check_generated_media_for_export(
        None, require_commercial_ready=True,
    )
    assert not required_without_store.allowed

    class _Bin:
        def __init__(self, report):
            self._report = report

        def provenance_report(self):
            return self._report

    unknown = check_generated_media_for_export(
        _Bin({"any_license_unknown": True, "entries": []}),
        require_commercial_ready=True,
    )
    assert not unknown.allowed
    assert "许可未知" in unknown.blocking[0]

    cleared = check_generated_media_for_export(
        _Bin({
            "any_license_unknown": False,
            "entries": [{"asset_id": "asset-1", "commercial_ready": True}],
        }),
        require_commercial_ready=True,
    )
    assert cleared.allowed
    assert cleared.blocking == ()


def test_commercial_gate_requires_real_import_audit_and_license_region() -> None:
    generation_bin = GenerationBin()
    generation_bin.add_candidates((_commercial_asset(),))
    generation_bin.import_to_media_pool(
        "generated-1", importer=_Importer(), approved_by="user-1",
    )

    allowed = check_generated_media_for_export(
        generation_bin,
        require_commercial_ready=True,
        required_license_region="global",
    )
    assert allowed.allowed

    wrong_region = check_generated_media_for_export(
        generation_bin,
        require_commercial_ready=True,
        required_license_region="CN",
    )
    assert not wrong_region.allowed
    assert "许可证区域" in wrong_region.blocking[0]


def test_commercial_gate_blocks_real_bin_with_missing_audit_evidence() -> None:
    generation_bin = GenerationBin()
    generation_bin.add_candidates(GeneratedAsset(
        asset_id="generated-2",
        request_id="request-2",
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="provider-1", model_id="model-1"),
        license=LicenseStatus.KNOWN_LICENSED,
    ) for _ in range(1))
    generation_bin.import_to_media_pool("generated-2", importer=_Importer())

    result = check_generated_media_for_export(
        generation_bin, require_commercial_ready=True,
    )

    assert not result.allowed
    assert any("导入批准记录" in item for item in result.blocking)
    assert any("许可证据引用" in item for item in result.blocking)
