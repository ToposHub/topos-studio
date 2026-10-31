"""P4 evidence cache persistence and fingerprint invalidation tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    AudioAnalysis,
    TranscriptSegment,
    analysis_to_segments,
)
from src.features.edit.ai_editing.analysis_service import AnalysisService


def test_analysis_cache_round_trips_stable_evidence_ids() -> None:
    service = AnalysisService()
    analysis = AssetAnalysis(
        asset_id="asset-1",
        fingerprint=AssetFingerprint(size=12, mtime_ns=34),
        transcript=(TranscriptSegment(0, 48, "产品卖点", speaker="S1"),),
        audio=AudioAnalysis(integrated_lufs=-18.0, bpm=120.0),
        provenance=AnalysisProvenance(provider="asr-test", model="v1"),
    )
    service.ingest("asset-1", analysis, tuple(analysis_to_segments(analysis)))

    payload = service.export_all()
    restored = AnalysisService()
    assert restored.restore_all(payload) == 1
    assert restored.get_analysis("asset-1").fingerprint == analysis.fingerprint
    assert restored.get_segments("asset-1")[0].segment_id == service.get_segments("asset-1")[0].segment_id
    assert restored.get_analysis("asset-1").audio.bpm == 120.0


def test_stale_fingerprints_are_removed_as_cache_misses() -> None:
    service = AnalysisService()
    analysis = AssetAnalysis(
        asset_id="asset-1",
        fingerprint=AssetFingerprint(size=1, mtime_ns=1),
        transcript=(TranscriptSegment(0, 10, "old"),),
    )
    service.ingest("asset-1", analysis, tuple(analysis_to_segments(analysis)))

    invalidated = service.invalidate_stale({
        "asset-1": AssetFingerprint(size=2, mtime_ns=2),
    })
    assert invalidated == ("asset-1",)
    assert service.get_analysis("asset-1") is None
