"""analysis_skills 单元测试（plan §10.4 P2.1）。

覆盖：

- resolve_analysis_scope：selection / bin / all 三类范围解析；analyze_all 必须
  二次确认 + 显示数量/预计；pool_analysis_enabled=False 拒绝 analyze_all。
- build_analysis_request：构造 AnalysisRequest；analyze_all 设 max_assets 守门；
  空 scope 返回 None。
- collect_transcript_trace：完整路由追踪（Qwen/Demucs/WhisperX 全可见），
  满足 plan §10.4 验收；优先用持久化 route_steps。
- collect_evidence_refs：版本化引用；OCR 标 untrusted；不传原始文本。

红线（plan §10.4 / §0.3 第 6 条 / §1.3）：

- 默认只分析当前选择；analyze_all 显式确认。
- evidence 是版本化引用；OCR 不可信。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from dataclasses import replace

from src.features.ai_harness.analysis_skills import (
    AnalysisScopeResolution,
    build_analysis_request,
    collect_evidence_refs,
    collect_transcript_trace,
    resolve_analysis_scope,
)
from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    OcrDetection,
    TranscriptSegment,
)
from src.features.edit.ai_editing.analysis_request import (
    AnalysisDataPolicy,
    AnalysisRecipe,
)


# ================================================================
# resolve_analysis_scope
# ================================================================


class TestResolveSelection:
    def test_selection_returns_chosen(self):
        s = resolve_analysis_scope(
            kind="selection", selected_asset_ids=("a1", "a2", "a3"),
        )
        assert s.kind == "selection"
        assert s.asset_ids == ("a1", "a2", "a3")
        assert s.count == 3
        assert s.requires_confirmation is False  # 默认不需二次确认

    def test_selection_empty_warns(self):
        s = resolve_analysis_scope(kind="selection", selected_asset_ids=())
        assert s.asset_ids == ()
        assert "未选择" in s.confirm_prompt


class TestResolveBin:
    def test_bin_returns_bin_assets(self):
        s = resolve_analysis_scope(
            kind="bin", bin_asset_ids=("b1", "b2"),
        )
        assert s.asset_ids == ("b1", "b2")
        assert s.requires_confirmation is True  # bin 需确认

    def test_bin_empty(self):
        s = resolve_analysis_scope(kind="bin")
        assert s.asset_ids == ()


class TestResolveAll:
    def test_all_requires_confirmation(self):
        """plan §0.3 第 6 条：analyze_all 必须二次确认。"""
        s = resolve_analysis_scope(
            kind="all", all_asset_ids=[f"a{i}" for i in range(10)],
        )
        assert s.requires_confirmation is True
        assert s.count == 10
        # 提示含数量 + 预计耗时
        assert "10" in s.confirm_prompt
        assert "分钟" in s.confirm_prompt or "耗时" in s.confirm_prompt

    def test_all_blocked_by_pool_switch(self):
        """pool_analysis_enabled=False → analyze_all 拒绝。"""
        s = resolve_analysis_scope(
            kind="all", all_asset_ids=["a1"], pool_analysis_enabled=False,
        )
        assert s.asset_ids == ()
        assert "未开启" in s.confirm_prompt or "不允许" in s.confirm_prompt

    def test_all_empty_pool(self):
        s = resolve_analysis_scope(kind="all", all_asset_ids=[])
        assert s.asset_ids == ()
        assert "空" in s.confirm_prompt


# ================================================================
# build_analysis_request
# ================================================================


class TestBuildRequest:
    def test_selection_request(self):
        scope = resolve_analysis_scope(
            kind="selection", selected_asset_ids=("a1",),
        )
        req = build_analysis_request(
            project_id="p1", scope=scope, requested_by="skill:analyze_selection",
        )
        assert req is not None
        assert req.asset_ids == ("a1",)
        assert req.requested_by == "skill:analyze_selection"
        assert req.is_confirmed is False  # PROPOSED

    def test_all_sets_budget_guard(self):
        """analyze_all 设 max_assets 守门（防全库爆）。"""
        scope = resolve_analysis_scope(
            kind="all", all_asset_ids=["a1", "a2"],
        )
        req = build_analysis_request(project_id="p1", scope=scope)
        assert req is not None
        assert req.budget.max_assets is not None
        assert req.budget.max_assets == 200

    def test_empty_scope_returns_none(self):
        scope = AnalysisScopeResolution(
            kind="selection", asset_ids=(), count=0, confirm_prompt="x",
        )
        assert build_analysis_request(project_id="p1", scope=scope) is None

    def test_cloud_recipe_requires_cloud_policy(self):
        scope = resolve_analysis_scope(
            kind="selection", selected_asset_ids=("a1",),
        )
        # CLOUD_ENHANCED + LOCAL_ONLY → AnalysisRequest 拒绝（构造期）
        with pytest.raises(Exception):
            build_analysis_request(
                project_id="p1", scope=scope,
                recipe_id=AnalysisRecipe.CLOUD_ENHANCED,
                data_policy=AnalysisDataPolicy.LOCAL_ONLY,
            )


# ================================================================
# collect_transcript_trace —— plan §10.4 验收核心
# ================================================================


def _analysis_with_router(asset_id, selection, quality=0.5, words=False):
    """构造带 router_selection 的 fake AssetAnalysis。"""
    seg = TranscriptSegment(
        start_frame=0, end_frame=100, text="hello", speaker="S1",
        confidence=quality,
        words=(("w", 0, 10, "h", 0.9),) if words else (),
    )
    ana = AssetAnalysis(
        asset_id=asset_id, fingerprint=AssetFingerprint(),
        transcript=(seg,),
    )
    return replace(ana, provenance=replace(
        ana.provenance, extra={"router_selection": selection},
    ))


class TestTranscriptTrace:
    def test_qwen_original_high_quality(self):
        ana = _analysis_with_router("a1", "qwen3_asr_original", quality=0.9, words=True)
        trace = collect_transcript_trace("a1", ana)
        assert trace.final_selection == "qwen3_asr_original"
        assert trace.used_demucs is False
        assert trace.used_whisperx_fallback is False
        # Qwen 步骤 selected
        assert any(s.provider == "qwen3_asr" and s.selected for s in trace.steps)

    def test_demucs_path(self):
        """背景音乐重的素材 → 进 Demucs（plan §10.4 验收样例）。"""
        ana = _analysis_with_router("a1", "qwen3_asr_demucs_v4", quality=0.8)
        trace = collect_transcript_trace("a1", ana)
        assert trace.used_demucs is True
        assert trace.used_whisperx_fallback is False
        assert any(s.provider == "demucs_v4" and s.selected for s in trace.steps)

    def test_whisperx_fallback(self):
        ana = _analysis_with_router("a1", "whisperx_fallback", quality=0.7)
        trace = collect_transcript_trace("a1", ana)
        assert trace.used_whisperx_fallback is True
        assert any(s.provider == "whisperx" and s.selected for s in trace.steps)

    def test_low_confidence_without_demucs_selection(self):
        """qwen3_asr_original_low_confidence → 推断 Demucs 被尝试但未选中。"""
        ana = _analysis_with_router("a1", "qwen3_asr_original_low_confidence",
                                    quality=0.4)
        trace = collect_transcript_trace("a1", ana)
        assert trace.used_demucs is True  # auto 路径会试 Demucs
        assert trace.final_selection == "qwen3_asr_original_low_confidence"

    def test_uses_persisted_route_steps_when_present(self):
        """未来 provider 直接持久化 route_steps 时优先用。"""
        seg = TranscriptSegment(0, 100, "x", "S1", 0.5)
        ana = AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(),
                            transcript=(seg,))
        extra = {
            "router_selection": "qwen3_asr_demucs_v4",
            "route_steps": [
                {"provider": "qwen3_asr", "quality": 0.3, "attempted": True,
                 "selected": False, "note": "低质量"},
                {"provider": "demucs_v4", "quality": 0.85, "attempted": True,
                 "selected": True, "note": "提升"},
            ],
        }
        ana = replace(ana, provenance=replace(ana.provenance, extra=extra))
        trace = collect_transcript_trace("a1", ana)
        assert len(trace.steps) == 2
        assert trace.steps[1].note == "提升"

    def test_to_public_dict_shape(self):
        ana = _analysis_with_router("a1", "qwen3_asr_original", quality=0.9)
        trace = collect_transcript_trace("a1", ana)
        d = trace.to_public_dict()
        assert d["final_selection"] == "qwen3_asr_original"
        assert "used_demucs" in d
        assert "steps" in d
        assert isinstance(d["steps"], list)

    def test_empty_transcript_zero_quality(self):
        ana = AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint())
        trace = collect_transcript_trace("a1", ana)
        assert trace.final_quality == 0.0

    def test_word_alignment_detected(self):
        ana = _analysis_with_router("a1", "qwen3_asr_original", quality=0.9, words=True)
        trace = collect_transcript_trace("a1", ana)
        assert trace.has_word_alignment is True


# ================================================================
# collect_evidence_refs
# ================================================================


class TestCollectEvidenceRefs:
    def test_returns_versioned_refs_not_raw_text(self):
        """红线：返回引用（asset_id+kind+版本），不传模型原始文本。"""
        seg = TranscriptSegment(0, 100, "secret text", "S1", 0.9)
        ana = replace(
            AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(),
                          transcript=(seg,)),
            provenance=AnalysisProvenance(
                extra={"router_selection": "qwen3_asr_original"}),
        )
        svc = type("S", (), {"get_analysis": lambda self, aid: ana})()
        refs = collect_evidence_refs(("a1",), svc)
        assert len(refs) == 1
        ref = refs[0]
        assert ref.kind == "transcript"
        assert ref.asset_id == "a1"
        assert ref.provider  # 有 provider（来自 router_selection）
        # 原始文本不在 ref 字段里（EvidenceRef 无 text 字段）
        assert not hasattr(ref, "text")

    def test_ocr_marked_untrusted(self):
        """OCR 文本不可信（plan §1.3）。"""
        ocr = (OcrDetection(frame=0, text="不可信文字", confidence=0.9),)
        ana = AssetAnalysis(
            asset_id="a1", fingerprint=AssetFingerprint(), ocr=ocr,
        )
        svc = type("S", (), {"get_analysis": lambda self, aid: ana})()
        refs = collect_evidence_refs(("a1",), svc)
        ocr_ref = next(r for r in refs if r.kind == "ocr")
        assert ocr_ref.trust == "untrusted"

    def test_missing_analysis_skipped(self):
        svc = type("S", (), {"get_analysis": lambda self, aid: None})()
        assert collect_evidence_refs(("a1",), svc) == []

    def test_transcript_ref_has_router_trace(self):
        seg = TranscriptSegment(0, 100, "x", "S1", 0.9)
        ana = replace(
            AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(),
                          transcript=(seg,)),
            provenance=AnalysisProvenance(extra={"router_selection": "qwen3_asr_original"}),
        )
        svc = type("S", (), {"get_analysis": lambda self, aid: ana})()
        refs = collect_evidence_refs(("a1",), svc)
        assert refs[0].router_trace is not None
        assert refs[0].router_trace.final_selection == "qwen3_asr_original"
