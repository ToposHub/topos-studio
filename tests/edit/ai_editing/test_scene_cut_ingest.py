"""T1.3 — SceneCut 结果接入 AnalysisService 测试。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 1 T1.3）：

- ``AnalysisService.ingest_scene_cut_analysis``：把 ``SceneCutAnalysis`` 转成 SHOT
  segment 灌入仓库，返回 segment 数。
- rejected candidate 被跳过；accepted/pending 保留。
- 灌入后 ``get_status == "ready"``、``get_segments`` 返回 SHOT segment。
- 空 candidates → 返回 0，不写入。
- 转换复用既有 ``SceneCutProvider.from_scene_cut_analysis``（已测试，不重复覆盖）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.core.scene_cut.models import (
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
)


def _make_candidate(frame: int, *, confidence: float = 0.9,
                    decision: SceneCutDecision = SceneCutDecision.PENDING,
                    kind: SceneCutKind = SceneCutKind.HARD_CUT) -> SceneCutCandidate:
    return SceneCutCandidate(
        source_frame=frame, pts=frame, seconds=float(frame) / 24.0,
        score=0.9, confidence=confidence, kind=kind, decision=decision,
    )


def _make_analysis(asset_id: str = "a1",
                   candidates: tuple[SceneCutCandidate, ...] = ()) -> SceneCutAnalysis:
    return SceneCutAnalysis(
        analysis_id="an1", asset_id=asset_id, source_path=f"/tmp/{asset_id}.mp4",
        candidates=candidates,
    )


class TestIngestSceneCutAnalysis:
    def test_ingests_accepted_candidates_as_shot_segments(self):
        svc = AnalysisService()
        analysis = _make_analysis("a1", candidates=(
            _make_candidate(0),
            _make_candidate(100),
            _make_candidate(200),
        ))
        n = svc.ingest_scene_cut_analysis("a1", analysis)
        assert n == 3
        assert svc.get_status("a1") == "ready"
        segs = svc.get_segments("a1")
        assert len(segs) == 3
        assert all(s.kind == SegmentKind.SHOT for s in segs)
        assert all(s.asset_id == "a1" for s in segs)

    def test_rejected_candidates_skipped(self):
        svc = AnalysisService()
        analysis = _make_analysis("a1", candidates=(
            _make_candidate(0, decision=SceneCutDecision.ACCEPTED),
            _make_candidate(50, decision=SceneCutDecision.REJECTED),
            _make_candidate(100, decision=SceneCutDecision.PENDING),
        ))
        n = svc.ingest_scene_cut_analysis("a1", analysis)
        assert n == 2  # rejected 跳过
        segs = svc.get_segments("a1")
        frames = sorted(s.source_range.start for s in segs)
        assert frames == [0, 100]

    def test_empty_candidates_returns_zero_no_write(self):
        svc = AnalysisService()
        analysis = _make_analysis("a1", candidates=())
        n = svc.ingest_scene_cut_analysis("a1", analysis)
        assert n == 0
        assert svc.get_status("a1") == "unknown"
        assert svc.get_segments("a1") == ()

    def test_ingest_merges_with_existing_transcript(self):
        """场景检测结果与既有转录证据合并（不覆盖其它 kind）。"""
        from src.features.edit.ai_editing.analysis_models import (
            AnalysisProvenance,
            AssetAnalysis,
            AssetFingerprint,
            TranscriptSegment,
            analysis_to_segments,
        )
        svc = AnalysisService()
        # 先灌入转录
        transcript = AssetAnalysis(
            asset_id="a1", fingerprint=AssetFingerprint(),
            transcript=(TranscriptSegment(0, 50, "hi", speaker="S1", confidence=0.9),),
            provenance=AnalysisProvenance(provider="test", model="m1"),
        )
        svc.ingest("a1", transcript, tuple(analysis_to_segments(transcript)))

        # 再灌入场景
        scene = _make_analysis("a1", candidates=(_make_candidate(0),))
        svc.ingest_scene_cut_analysis("a1", scene)

        segs = svc.get_segments("a1")
        kinds = {s.kind for s in segs}
        assert SegmentKind.TRANSCRIPT in kinds
        assert SegmentKind.SHOT in kinds

    def test_ingest_makes_asset_known(self):
        """灌入后 known_asset_ids 包含该 asset。"""
        svc = AnalysisService()
        analysis = _make_analysis("shot-asset", candidates=(_make_candidate(0),))
        svc.ingest_scene_cut_analysis("shot-asset", analysis)
        assert "shot-asset" in svc.known_asset_ids()
