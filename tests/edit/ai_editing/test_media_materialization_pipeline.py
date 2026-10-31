"""B4 生成媒体物化 checksum/probe/QA 测试。"""

from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generated_media_materializer import GeneratedMediaMaterializer
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationModality,
    GenerationProvenance,
)
from src.features.edit.ai_editing.media_materialization_pipeline import (
    GeneratedMediaMaterializationPipeline,
    GeneratedMediaProbeService,
    GeneratedMediaValidationPolicy,
)


def test_materializer_reports_checksum_and_pipeline_hides_path(tmp_path: Path) -> None:
    source = tmp_path / "generated.mp4"
    source.write_bytes(b"fake-media")
    asset = GeneratedAsset(
        asset_id="gen-1", request_id="r1", modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="seedance", model_id="m1"),
        preview_handle=str(source),
    )
    result = GeneratedMediaMaterializer(cache_dir=tmp_path / "cache").materialize(asset)
    assert result.succeeded and result.sha256
    assert result.mime_type == "video/mp4"
    media, _ = GeneratedMediaMaterializationPipeline(
        GeneratedMediaMaterializer(cache_dir=tmp_path / "cache"),
        probe_service=GeneratedMediaProbeService(
            lambda _path: {"mime_type": "video/mp4", "decoder": "fixture"},
        ),
        policy=GeneratedMediaValidationPolicy(),
        thumbnail_builder=lambda path, asset_id: f"{asset_id}.jpg",
    ).materialize(asset)
    assert media is not None and media.importable
    assert media.to_dict()["has_thumbnail"] is True
    assert "path" not in media.to_dict()


def test_pipeline_blocks_when_probe_is_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "generated.mp4"
    source.write_bytes(b"not-a-real-video")
    asset = GeneratedAsset(
        asset_id="gen-probe", request_id="r1", modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="seedance", model_id="m1"),
        preview_handle=str(source),
    )

    def fail_probe(_path: str):
        raise RuntimeError("ffprobe unavailable")

    media, result = GeneratedMediaMaterializationPipeline(
        GeneratedMediaMaterializer(cache_dir=tmp_path / "cache"),
        probe_service=GeneratedMediaProbeService(fail_probe),
    ).materialize(asset)
    assert result.succeeded
    assert media is not None
    assert not media.importable
    assert media.qa_issues == ("media_probe_failed",)


def test_materializer_blocks_provider_checksum_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "generated.mp4"
    source.write_bytes(b"generated-payload")
    asset = GeneratedAsset(
        asset_id="gen-checksum", request_id="r1", modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="seedance", model_id="m1"),
        preview_handle=str(source), expected_sha256="0" * 64,
    )
    result = GeneratedMediaMaterializer(cache_dir=tmp_path / "cache").materialize(asset)
    assert not result.succeeded
    assert result.sha256 == hashlib.sha256(b"generated-payload").hexdigest()
    assert "checksum" in result.message


def test_materializer_accepts_matching_provider_checksum(tmp_path: Path) -> None:
    source = tmp_path / "generated.mp4"
    source.write_bytes(b"generated-payload")
    digest = hashlib.sha256(b"generated-payload").hexdigest()
    asset = GeneratedAsset(
        asset_id="gen-checksum-ok", request_id="r1", modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="seedance", model_id="m1"),
        preview_handle=str(source), expected_sha256=f"sha256:{digest}",
    )
    result = GeneratedMediaMaterializer(cache_dir=tmp_path / "cache").materialize(asset)
    assert result.succeeded and result.sha256 == digest
