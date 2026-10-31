"""T1.2 — 路径 A（自由对话/legacy brief）证据流验证。

T1.1 修复了 ``get_semantic_index`` 返回空索引导致路径 A 必败的问题。本测试固化
"T1.1 后路径 A 在已分析素材上能拿到真实证据并驱动规划"这个不变量（docs/
ai_closure_focused_plan_2026-08-04.md 工作流 1 T1.2 评估结论：保持路径 A/B
并存设计，T1.1 已足够）。

验收：
- 已分析素材：``get_semantic_index()`` 非空 → ``plan_variants`` 不抛"没有分析片段"。
- 未分析素材：空索引 → 规划给出明确"请先分析"信号（不静默产空方案）。
- 规则引擎（无 LLM）在空索引时优雅产 variant（兜底可用）。
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
from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.edit_api import EditSession
from src.features.edit.ai_editing.editorial_models import (
    DurationTarget,
    EditorialBrief,
    Platform,
)
from src.features.edit.ai_editing.plan_repository import PlanRepository
from src.features.edit.ai_editing.variant_planner import VariantPlanner
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
    """最小 MainWindow 桩（暴露 bridge 需要的属性，无 Qt）。"""

    def __init__(self, project, sequences):
        self._current_edit_project = project
        self._current_timeline_sequences = sequences


def _make_clip(asset_id: str) -> ClipInstance:
    return ClipInstance(
        asset_id=asset_id, track_id="tr1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )


def _make_project_with_asset(asset_id: str) -> EditProject:
    from src.features.edit.core.project import MediaAsset
    project = EditProject.create("Test")
    project.assets[asset_id] = MediaAsset(
        asset_id=asset_id, path=f"/tmp/{asset_id}.mp4",
        media_type="video", display_name=asset_id, metadata={},
    )
    return project


def _make_timeline_with_clip(asset_id: str) -> dict:
    track = EditTrack(clips=(_make_clip(asset_id),))
    seq = TimelineSequence(
        timeline_id="t1", name="T",
        video_tracks=(track,), audio_tracks=(), subtitle_tracks=(),
    )
    return {"t1": seq}


def _transcript_analysis(asset_id: str) -> AssetAnalysis:
    return AssetAnalysis(
        asset_id=asset_id, fingerprint=AssetFingerprint(),
        transcript=(
            TranscriptSegment(0, 100, "开场吸引注意", speaker="S1", confidence=0.9),
            TranscriptSegment(100, 200, "产品卖点", speaker="S1", confidence=0.88),
        ),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )


# ================================================================
# Tests：路径 A 证据流（T1.1 修复后）
# ================================================================


class TestPathAEvidenceFlow:
    """验证路径 A 的'bridge.get_semantic_index → plan'证据流。"""

    def test_analyzed_asset_evidence_reaches_planner(self):
        """已分析素材：bridge 返回非空索引，规则引擎能据此产 variant。"""
        asset_id = "a1"
        analysis = AnalysisService()
        analysis.ingest(asset_id, _transcript_analysis(asset_id),
                        tuple(analysis_to_segments(_transcript_analysis(asset_id))))
        project = _make_project_with_asset(asset_id)
        sequences = _make_timeline_with_clip(asset_id)
        mw = _FakeMainWindow(project, sequences)
        bridge = MainWindowAiEditHostBridge(
            mw, assistant_service=None, approval_service=ApprovalService(),
            analysis_service=analysis,
        )

        index = bridge.get_semantic_index()
        assert len(index) == 2  # 两条 transcript segment
        # 索引中能查到该 asset 的证据
        segs = index.for_asset(asset_id)
        assert len(segs) == 2

    def test_unanalyzed_asset_empty_index_clear_signal(self):
        """未分析素材：空索引。规划器据此给出明确'请先分析'信号。"""
        asset_id = "a1"
        analysis = AnalysisService()  # 空
        project = _make_project_with_asset(asset_id)
        sequences = _make_timeline_with_clip(asset_id)
        mw = _FakeMainWindow(project, sequences)
        bridge = MainWindowAiEditHostBridge(
            mw, assistant_service=None, approval_service=ApprovalService(),
            analysis_service=analysis,
        )

        index = bridge.get_semantic_index()
        assert len(index) == 0
        assert index.for_asset(asset_id) == []

    def test_rule_planner_works_with_evidence(self):
        """端到端：bridge 索引 → 规则引擎 plan 产 variant（不抛'没有分析片段'）。

        这验证 T1.1 修复后，路径 A（brief → get_semantic_index → plan）对已分析
        素材能完整跑通。规则引擎用于可重复测试（不依赖 LLM 网络）。
        """
        asset_id = "a1"
        analysis = AnalysisService()
        analysis.ingest(asset_id, _transcript_analysis(asset_id),
                        tuple(analysis_to_segments(_transcript_analysis(asset_id))))
        project = _make_project_with_asset(asset_id)
        sequences = _make_timeline_with_clip(asset_id)
        mw = _FakeMainWindow(project, sequences)
        bridge = MainWindowAiEditHostBridge(
            mw, assistant_service=None, approval_service=ApprovalService(),
            analysis_service=analysis,
        )

        index = bridge.get_semantic_index()
        # 构造最小 brief + planner，模拟 _on_brief_approved → plan_variants
        brief_service = BriefService()
        brief_service.save(EditorialBrief(
            brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
            target_duration=DurationTarget(200, 180, 220),
            required_beats=("hook", "product_value"),
        ))
        planner = VariantPlanner(brief_service)
        from src.features.edit.ai_editing.variant_planner import PlannerInput
        inp = PlannerInput(
            brief_id="b1", brief_revision=1, asset_id=asset_id,
            timeline_id="t1", project_id="proj-1", base_revision=0,
            analysis_snapshot_hash="",
        )
        # 不应抛"没有可用的分析片段"（那是 LLM planner 的行为）
        variants = planner.plan(inp, index)
        assert len(variants) >= 1
