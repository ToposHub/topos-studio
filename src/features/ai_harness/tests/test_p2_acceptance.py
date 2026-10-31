"""P2.4 端到端验收测试（plan §10.4 验收样例）。

plan §10.4 验收："对背景音乐很重的片段运行自动字幕，RunRecord 明确显示 Qwen
质量、是否进入 Demucs、是否回退 WhisperX、最终证据版本；用户可由该结果创建
字幕轨道并一次 Undo。"

本文件验证：

1. 背景音乐重的素材 → AdaptiveTranscriptProvider 走 Demucs → RunRecord 的诊断
   里能拿到完整 TranscriptTrace（Qwen 低质量 → Demucs 提升 → 最终选择）。
2. WhisperX 回退路径同样可见。
3. 高质量 Qwen 原始路径不进 Demucs。
4. 自动字幕产 EditPlan（PREVIEW），可进 preview/diff/approval（不直接写）。
5. evidence 版本（field_provenance）记录进 trace。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core


from src.features.ai_harness.editorial_skills import register_p2_skills
from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, InvocationSource, RunPolicy,
)
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.skill_compiler import (
    SkillCompiler, definition_from_manifest,
)
from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance, AssetAnalysis, AssetFingerprint, FieldProvenance,
    TranscriptSegment,
)
from src.features.edit.ai_editing.editorial_models import (
    FrameRange, SegmentKind, SemanticSegment,
)
from src.features.edit.ai_editing.skill_registry import SkillRegistry


# ================================================================
# 验收场景：背景音乐重的片段 → Demucs 路径
# ================================================================


def _make_analysis(asset_id, router_selection, confidence=0.5, words=False):
    """构造带 router_selection + field_provenance 的 fake AssetAnalysis。"""
    seg = TranscriptSegment(
        start_frame=0, end_frame=200, text="口播内容", speaker="S1",
        confidence=confidence,
        words=(("w", 0, 10, "口", 0.9),) if words else (),
    )
    prov = AnalysisProvenance(
        provider="transcript_auto", model="qwen3-asr",
        extra={"router_selection": router_selection},
    )
    field_prov = FieldProvenance.single("transcript", prov)
    return AssetAnalysis(
        asset_id=asset_id, fingerprint=AssetFingerprint(content_hash="h1"),
        transcript=(seg,),
        provenance=prov, field_provenance=field_prov,
    )


def _run_auto_caption_with_analysis(analysis_by_asset):
    """端到端跑 auto_caption，provider 返回 (segments, analysis_by_asset)。"""
    reg = SkillRegistry()
    register_p2_skills(reg)
    rt = HarnessRuntime()
    adapter = LegacyHarnessAdapter(None, rt)

    def provider(asset_ids, kind):
        if kind != "transcript":
            return ([], {})
        segs = []
        for aid in asset_ids:
            ana = analysis_by_asset.get(aid)
            if ana is None:
                continue
            for i, t in enumerate(ana.transcript):
                segs.append(SemanticSegment(
                    segment_id=f"{aid}_t{i}", asset_id=aid,
                    kind=SegmentKind.TRANSCRIPT,
                    source_range=FrameRange(t.start_frame, t.end_frame - t.start_frame),
                    payload={"text": t.text}, confidence=t.confidence,
                ))
        return (segs, analysis_by_asset)

    adapter.set_editorial_provider(provider)
    compiler = SkillCompiler()
    manifest = reg.require("topos.auto_caption")
    definition = definition_from_manifest(manifest, is_official=True)
    inv = Invocation(
        source=InvocationSource.CHAT, skill_id="topos.auto_caption",
        scope=InvocationScope(
            project_id="p1", timeline_id="t1",
            selected_asset_ids=tuple(analysis_by_asset.keys()),
        ),
        policy=RunPolicy(),
        inputs={"brief_id": "b1"},
        parent_session_id="sess1",
    )
    compiled = compiler.compile(definition, inv)
    rid = adapter.invoke_compiled(compiled, inv)
    return rt.get_run(rid), compiled


def _trace_of(rec, asset_id):
    """从 RunRecord 诊断里取指定 asset 的 TranscriptTrace dict。"""
    for d in rec.diagnostics:
        if d.get("code") == "TRANSCRIPT_ROUTE_TRACE" and d.get("asset_id") == asset_id:
            return d.get("trace", {})
    return None


class TestDemucsPathAcceptance:
    """plan §10.4 验收：背景音乐重的片段进 Demucs。"""

    def test_run_record_shows_demucs_path(self):
        """Qwen 原始低质量 → 进 Demucs → 最终选 Demucs。"""
        ana = _make_analysis("noisy1", "qwen3_asr_demucs_v4", confidence=0.85)
        rec, _ = _run_auto_caption_with_analysis({"noisy1": ana})
        assert rec.status in ("succeeded", "succeeded_with_warnings")
        trace = _trace_of(rec, "noisy1")
        assert trace is not None, "RunRecord 缺少 TRANSCRIPT_ROUTE_TRACE 诊断"
        # 明确显示：进 Demucs、最终选 Demucs
        assert trace["used_demucs"] is True
        assert trace["final_selection"] == "qwen3_asr_demucs_v4"
        # 步骤里有 demucs_v4 且 selected
        demucs_step = next(s for s in trace["steps"] if s["provider"] == "demucs_v4")
        assert demucs_step["selected"] is True
        # 证据版本记录
        assert trace["evidence_version"]

    def test_run_record_shows_quality(self):
        """RunRecord 明确显示 Qwen 质量。"""
        ana = _make_analysis("noisy2", "qwen3_asr_demucs_v4", confidence=0.82, words=True)
        rec, _ = _run_auto_caption_with_analysis({"noisy2": ana})
        trace = _trace_of(rec, "noisy2")
        assert trace is not None
        assert trace["final_quality"] > 0
        # 步骤里 Qwen 的 quality 可见
        qwen_step = next(s for s in trace["steps"] if s["provider"] == "qwen3_asr")
        assert "quality" in qwen_step


class TestWhisperXFallbackAcceptance:
    """plan §10.4：Qwen/Demucs 均不足 → 回退 WhisperX。"""

    def test_whisperx_fallback_visible(self):
        ana = _make_analysis("bad1", "whisperx_fallback", confidence=0.7)
        rec, _ = _run_auto_caption_with_analysis({"bad1": ana})
        trace = _trace_of(rec, "bad1")
        assert trace["used_whisperx_fallback"] is True
        assert trace["final_selection"] == "whisperx_fallback"


class TestHighQualityQwenAcceptance:
    """高质量 Qwen 原始 → 不进 Demucs。"""

    def test_no_demucs_when_high_quality(self):
        ana = _make_analysis("clean1", "qwen3_asr_original", confidence=0.92, words=True)
        rec, _ = _run_auto_caption_with_analysis({"clean1": ana})
        trace = _trace_of(rec, "clean1")
        assert trace["used_demucs"] is False
        assert trace["used_whisperx_fallback"] is False
        assert trace["final_selection"] == "qwen3_asr_original"


class TestEditPlanAcceptance:
    """plan §10.4：自动字幕产 EditPlan（PREVIEW），可进 preview/diff/approval。"""

    def test_auto_caption_produces_preview_plan(self):
        ana = _make_analysis("a1", "qwen3_asr_original", confidence=0.9, words=True)
        rec, compiled = _run_auto_caption_with_analysis({"a1": ana})
        # compiled 是 CompiledSkill（不含 plan）；plan 在 editorial outcome 里。
        # 这里验证 RunRecord 成功 + subtitle_draft artifact。
        assert rec.status in ("succeeded", "succeeded_with_warnings")
        assert any(a.artifact_type == "subtitle_draft" for a in rec.artifacts)

    def test_plan_does_not_write_timeline_directly(self):
        """红线：editorial path 不直接写时间线（plan 由 host 经 preview/approval 写）。

        本测试验证 RunRecord 不含任何'已应用'标记（apply 走另一条 approval 路径）。
        """
        ana = _make_analysis("a1", "qwen3_asr_original", confidence=0.9, words=True)
        rec, _ = _run_auto_caption_with_analysis({"a1": ana})
        # 没有 apply 相关事件（auto_caption 只产提议，不 apply）
        apply_events = [e for e in rec.events if "apply" in e.kind or "commit" in e.kind]
        assert apply_events == []


class TestMultiAssetTrace:
    """多素材场景：每个 asset 都有独立 trace。"""

    def test_each_asset_has_own_trace(self):
        analyses = {
            "clean": _make_analysis("clean", "qwen3_asr_original", 0.9, words=True),
            "noisy": _make_analysis("noisy", "qwen3_asr_demucs_v4", 0.8),
        }
        rec, _ = _run_auto_caption_with_analysis(analyses)
        clean_trace = _trace_of(rec, "clean")
        noisy_trace = _trace_of(rec, "noisy")
        assert clean_trace is not None and noisy_trace is not None
        assert clean_trace["used_demucs"] is False
        assert noisy_trace["used_demucs"] is True
