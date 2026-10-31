"""T1.1 — MainWindowAiEditHostBridge.get_semantic_index 闭合测试。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 1）：

- ``get_semantic_index`` 从共享 AnalysisService 拉真实 segment 建索引（路径 A 不再
  因空索引必败）。
- asset_ids 来源：优先 timeline clips 引用；timeline 无 clip 时回退 project.assets。
- 无 AnalysisService / 无工程 / 无已分析素材 → 空索引（planner 据此报"请先分析"）。
- 委托共享 helper ``build_index_from_analysis``，与 WorkflowRunner._build_index 同源。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    TranscriptSegment,
    analysis_to_segments,
)
from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.approval_service import ApprovalService
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)


# ================================================================
# Fakes
# ================================================================


class _FakeMainWindow:
    """最小 MainWindow 桩：只暴露 bridge 需要的属性（无 Qt）。"""

    def __init__(self, project, sequences):
        self._current_edit_project = project
        self._current_timeline_sequences = sequences
        # bridge.commit 等用不到的字段保持缺失（getattr 容错）


def _make_clip(asset_id: str, *, track_id: str = "tr1", start: int = 0,
               duration: int = 100) -> ClipInstance:
    """造一个合法 ClipInstance（满足 __post_init__ 校验）。"""
    return ClipInstance(
        asset_id=asset_id, track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


def _make_timeline(asset_id: str, timeline_id: str = "t1") -> TimelineSequence:
    """造一个含单个 clip（引用 asset_id）的最小 timeline。"""
    clip = _make_clip(asset_id)
    track = EditTrack(clips=(clip,))
    return TimelineSequence(
        timeline_id=timeline_id, name="T",
        video_tracks=(track,), audio_tracks=(), subtitle_tracks=(),
    )


def _make_project(asset_ids: tuple[str, ...]) -> EditProject:
    project = EditProject.create("Test")
    # 直接填充 assets dict（MediaAsset 最小构造）
    from src.features.edit.core.project import MediaAsset
    for aid in asset_ids:
        project.assets[aid] = MediaAsset(
            asset_id=aid, path=f"/tmp/{aid}.mp4",
            media_type="video", display_name=f"asset-{aid}", metadata={},
        )
    return project


def _transcript_analysis(asset_id: str) -> AssetAnalysis:
    return AssetAnalysis(
        asset_id=asset_id, fingerprint=AssetFingerprint(),
        transcript=(
            TranscriptSegment(0, 50, "开场", speaker="S1", confidence=0.9),
            TranscriptSegment(50, 100, "卖点", speaker="S1", confidence=0.88),
        ),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )


def _analysis_service_with(asset_id: str, analysis: AssetAnalysis) -> AnalysisService:
    svc = AnalysisService()
    svc.ingest(asset_id, analysis, tuple(analysis_to_segments(analysis)))
    return svc


# ================================================================
# Tests
# ================================================================


def _make_bridge(*, analysis_service, project, sequences):
    mw = _FakeMainWindow(project, sequences)
    return MainWindowAiEditHostBridge(
        mw, assistant_service=None, approval_service=ApprovalService(),
        analysis_service=analysis_service,
    )


class TestGetSemanticIndex:
    """get_semantic_index：从真实分析证据建索引。"""

    def test_returns_segments_for_timeline_clip_assets(self):
        """timeline clip 引用的 asset 的 segment 进入索引。"""
        analysis = _analysis_service_with("a1", _transcript_analysis("a1"))
        project = _make_project(("a1",))
        seqs = {"t1": _make_timeline("a1")}
        bridge = _make_bridge(
            analysis_service=analysis, project=project, sequences=seqs,
        )

        index = bridge.get_semantic_index()

        assert len(index) == 2  # 两条 transcript segment
        segs = index.for_asset("a1")
        assert len(segs) == 2
        assert all(s.asset_id == "a1" for s in segs)

    def test_falls_back_to_project_assets_when_no_clips(self):
        """timeline 无 clip 时，回退到 project.assets（含尚未放上时间线的素材）。"""
        analysis = _analysis_service_with("orphan", _transcript_analysis("orphan"))
        project = _make_project(("orphan",))
        # 空 timeline（无 clip）
        seqs = {"t1": TimelineSequence(
            timeline_id="t1", name="T",
            video_tracks=(), audio_tracks=(), subtitle_tracks=(),
        )}
        bridge = _make_bridge(
            analysis_service=analysis, project=project, sequences=seqs,
        )

        index = bridge.get_semantic_index()

        assert len(index) == 2
        assert len(index.for_asset("orphan")) == 2

    def test_empty_when_no_analysis_service(self):
        """无 AnalysisService（未装配）→ 空索引。"""
        project = _make_project(("a1",))
        seqs = {"t1": _make_timeline("a1")}
        bridge = _make_bridge(
            analysis_service=None, project=project, sequences=seqs,
        )

        assert len(bridge.get_semantic_index()) == 0

    def test_empty_when_no_project(self):
        """无工程 → 空索引。"""
        analysis = _analysis_service_with("a1", _transcript_analysis("a1"))
        mw = _FakeMainWindow(None, None)
        bridge = MainWindowAiEditHostBridge(
            mw, assistant_service=None, approval_service=ApprovalService(),
            analysis_service=analysis,
        )

        assert len(bridge.get_semantic_index()) == 0

    def test_empty_when_asset_not_analyzed(self):
        """asset 在工程里但未被分析 → 空索引（planner 据此报'请先分析'）。"""
        analysis = AnalysisService()  # 空 service
        project = _make_project(("a1",))
        seqs = {"t1": _make_timeline("a1")}
        bridge = _make_bridge(
            analysis_service=analysis, project=project, sequences=seqs,
        )

        assert len(bridge.get_semantic_index()) == 0

    def test_multiple_assets_all_collected(self):
        """多 asset 的证据都被收集（timeline 多 clip）。"""
        analysis = AnalysisService()
        for aid in ("a1", "a2"):
            analysis.ingest(aid, _transcript_analysis(aid),
                            tuple(analysis_to_segments(_transcript_analysis(aid))))
        project = _make_project(("a1", "a2"))
        clip1 = _make_clip("a1", start=0, duration=50)
        clip2 = _make_clip("a2", start=50, duration=50)
        track = EditTrack(clips=(clip1, clip2))
        seqs = {"t1": TimelineSequence(
            timeline_id="t1", name="T",
            video_tracks=(track,), audio_tracks=(), subtitle_tracks=(),
        )}
        bridge = _make_bridge(
            analysis_service=analysis, project=project, sequences=seqs,
        )

        index = bridge.get_semantic_index()

        assert len(index.for_asset("a1")) == 2
        assert len(index.for_asset("a2")) == 2
        assert len(index) == 4
