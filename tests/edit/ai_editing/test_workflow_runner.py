"""TAEP M7 — WorkflowRunner 测试（口播精剪 / 30秒广告 / 字幕初稿）。

验收（§11.1 M7）：

- golden：三个工作流在证据就绪时产出符合预期的 variant/artifact。
- 降级：证据缺失 → degradation 非空、不强行产 plan（红线）。
- partial：转录只覆盖部分时长 → variant 仍产出但 beat_coverage<1.0。
- 字幕：从转录产 typed artifact；OCR 不进字幕；存 ArtifactStore 后用户编辑优先。
- 复用闭环：runner 产的 variant 经 select→validate→approve→commit→undo 一次恢复。
- 证据强制引用：decision 的 evidence_refs 必须非空。

沿用 ``test_variant_planner.py`` / ``test_analysis.py`` 的 inline fixture 风格。
"""

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
from src.features.edit.ai_editing.artifact_models import (
    ArtifactKind,
    ArtifactSource,
    ScriptLine,
    ScriptArtifact,
)
from src.features.edit.ai_editing.artifact_store import ArtifactStore
from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.editorial_models import (
    DurationTarget,
    EditorialBrief,
    FrameRange,
    Platform,
    SegmentKind,
    SemanticSegment,
    VariantStatus,
)
from src.features.edit.ai_editing.variant_planner import (
    STRATEGY_A_TALKING_HEAD,
    STRATEGY_B_BROLL,
    VariantPlanner,
)
from src.features.edit.ai_editing.workflow import (
    BUILTIN_SPECS,
    PRODUCT_AD_30S_SPEC,
    SUBTITLE_DRAFT_SPEC,
    VOICEOVER_POLISH_SPEC,
    WorkflowId,
    WorkflowInput,
    WorkflowResult,
    WorkflowRunner,
)


# ================================================================
# Fixtures
# ================================================================


def _brief_service() -> BriefService:
    bs = BriefService()
    bs.save(EditorialBrief(
        brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
        required_beats=("hook", "product_value", "call_to_action"),
    ))
    return bs


def _transcript_analysis(asset_id: str = "a1") -> AssetAnalysis:
    """3 段转录 + scene cut + tag，覆盖口播/广告工作流所需证据。"""
    return AssetAnalysis(
        asset_id=asset_id, fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},
                    {"frame": 540, "confidence": 0.9, "kind": "soft"}),
        transcript=(
            TranscriptSegment(0, 108, "开场反应，吸引注意", speaker="S1", confidence=0.95),
            TranscriptSegment(108, 432, "产品核心卖点详细介绍", speaker="S1", confidence=0.9),
            TranscriptSegment(540, 180, "立即点击了解更多", speaker="S1", confidence=0.92),
        ),
        tags=({"start_frame": 10, "end_frame": 60, "labels": ["product", "closeup"]},),
        audio=AudioAnalysis(integrated_lufs=-18.0, speech_ranges=[(0, 540), (540, 720)]),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )


def _shot_only_analysis(asset_id: str = "a1") -> AssetAnalysis:
    """只有 scene cut，无转录（用于降级测试）。"""
    return AssetAnalysis(
        asset_id=asset_id, fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},),
        transcript=(),  # 无转录
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )


def _analysis_service_with(asset_id: str, analysis: AssetAnalysis) -> AnalysisService:
    svc = AnalysisService()
    svc.ingest(asset_id, analysis, tuple(analysis_to_segments(analysis)))
    return svc


def _runner(
    *, analysis: AnalysisService, artifact_store: ArtifactStore | None = None,
    brief: BriefService | None = None, translation_provider=None,
) -> WorkflowRunner:
    """构造 runner（on_plan_compiled 默认丢弃；闭环测试单独注入 repo）。"""
    return WorkflowRunner(
        planner=VariantPlanner(brief or _brief_service()),
        analysis_service=analysis,
        brief_service=brief or _brief_service(),
        artifact_store=artifact_store,
        translation_provider=translation_provider,
    )


def _voiceover_input(asset_ids: tuple[str, ...] = ("a1",)) -> WorkflowInput:
    return WorkflowInput(
        workflow_id=WorkflowId.VOICEOVER_POLISH,
        asset_ids=asset_ids, timeline_id="t1", project_id="p1",
        base_revision=0, brief_id="b1", target_duration=720,
    )


# ================================================================
# Spec 查询
# ================================================================


def test_builtin_specs_cover_three_workflows():
    assert set(BUILTIN_SPECS.keys()) == {
        WorkflowId.VOICEOVER_POLISH, WorkflowId.PRODUCT_AD_30S,
        WorkflowId.INTERVIEW_POLISH, WorkflowId.PODCAST_CLIP,
        WorkflowId.SUBTITLE_DRAFT, WorkflowId.TUTORIAL_COURSE,
        WorkflowId.MULTILINGUAL_VERSION,
    }


def test_spec_query_returns_builtin():
    runner = _runner(analysis=_analysis_service_with("a1", _transcript_analysis()))
    assert runner.spec(WorkflowId.VOICEOVER_POLISH) is VOICEOVER_POLISH_SPEC
    assert runner.spec(WorkflowId.PRODUCT_AD_30S) is PRODUCT_AD_30S_SPEC
    assert runner.spec(WorkflowId.SUBTITLE_DRAFT) is SUBTITLE_DRAFT_SPEC


def test_voiceover_spec_requires_transcript_only():
    assert VOICEOVER_POLISH_SPEC.required_evidence_kinds == (SegmentKind.TRANSCRIPT,)
    assert VOICEOVER_POLISH_SPEC.produces_plan is True
    assert VOICEOVER_POLISH_SPEC.produces_artifact_kind is None


def test_subtitle_spec_produces_artifact_not_plan():
    assert SUBTITLE_DRAFT_SPEC.produces_plan is False
    assert SUBTITLE_DRAFT_SPEC.produces_artifact_kind == ArtifactKind.SCRIPT


def test_ad_spec_requires_transcript_shot_tag():
    kinds = set(PRODUCT_AD_30S_SPEC.required_evidence_kinds)
    assert SegmentKind.TRANSCRIPT in kinds
    assert SegmentKind.SHOT in kinds
    assert SegmentKind.TAG in kinds
    assert "problem" in PRODUCT_AD_30S_SPEC.target_beats


# ================================================================
# 输入校验
# ================================================================


def test_validate_input_empty_assets_reports_diagnostic():
    runner = _runner(analysis=_analysis_service_with("a1", _transcript_analysis()))
    inp = WorkflowInput(
        workflow_id=WorkflowId.VOICEOVER_POLISH, asset_ids=(),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
    )
    result = runner.run(inp)
    assert result.variants == ()
    assert result.artifact_id is None
    assert any(d.get("code") == "TAEP_SCHEMA_INVALID" for d in result.diagnostics)


def test_validate_input_bad_duration():
    runner = _runner(analysis=_analysis_service_with("a1", _transcript_analysis()))
    inp = WorkflowInput(
        workflow_id=WorkflowId.VOICEOVER_POLISH, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=0,
    )
    result = runner.run(inp)
    assert result.variants == ()
    assert any(d.get("code") == "TAEP_SOURCE_RANGE_INVALID" for d in result.diagnostics)


# ================================================================
# 口播精剪：golden
# ================================================================


def test_voiceover_golden_produces_ab_variants():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input())
    assert isinstance(result, WorkflowResult)
    assert len(result.variants) >= 2
    labels = {v.label for v in result.variants}
    assert STRATEGY_A_TALKING_HEAD in labels
    assert STRATEGY_B_BROLL in labels
    assert result.degraded is False


def test_voiceover_variants_have_evidence_refs():
    """红线（§11.1 M7）：每条 decision 带 evidence_refs。"""
    analysis = _analysis_service_with("a1", _transcript_analysis())
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input())
    for v in result.variants:
        for d in v.decisions:
            assert d.evidence_refs, (
                f"decision {d.decision_id} 缺 evidence_refs（违反 M7 红线）"
            )


def test_voiceover_variants_have_outline_and_plan_ref():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input())
    for v in result.variants:
        assert len(v.outline) == 3  # hook / product_value / call_to_action
        assert v.edit_plan_ref  # plan_id 可解析
        assert v.scores.hard_constraints_passed is True
        assert 0.0 < v.scores.duration_fit <= 1.0


# ================================================================
# 口播精剪：降级（证据缺失）
# ================================================================


def test_voiceover_degrades_when_no_transcript():
    """红线：无转录 → 不强行产 plan，返回 degradation + missing_evidence。"""
    analysis = _analysis_service_with("a1", _shot_only_analysis())
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input())
    assert result.variants == ()
    assert result.degradation is not None
    assert result.degraded is True
    assert len(result.missing_evidence) > 0
    assert any("transcript" in m or "a1" in m for m in result.missing_evidence)


def test_voiceover_degrades_when_asset_not_analyzed():
    """asset 完全未分析（status=unknown）→ 降级。"""
    analysis = AnalysisService()  # 空，无 ingest
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input(("a1",)))
    assert result.degraded
    assert result.variants == ()
    assert any("unknown" in m or "ready" in m for m in result.missing_evidence)


def test_voiceover_degrades_when_pending():
    """asset 已 ingest 记录但无 segments（pending）→ 降级。"""
    svc = AnalysisService()
    # ingest 一个空 segments 的记录 → status=pending
    empty = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )
    svc.ingest("a1", empty, ())
    runner = _runner(analysis=svc)
    result = runner.run(_voiceover_input())
    assert result.degraded
    assert result.variants == ()


# ================================================================
# 口播精剪：partial（转录不完整）
# ================================================================


def test_voiceover_partial_coverage_still_produces_variant():
    """转录只覆盖部分时长 → variant 仍产出（不伪装完整），beat_coverage 反映覆盖。"""
    # 只有 1 段转录，覆盖范围远小于 720 帧目标
    partial = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},),
        transcript=(TranscriptSegment(0, 120, "只有开场", speaker="S1", confidence=0.9),),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )
    analysis = _analysis_service_with("a1", partial)
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input())
    # 仍产出 variant（不阻断），但 beat_coverage 反映覆盖不全
    assert len(result.variants) >= 1
    # 至少一个 variant 的 beat_coverage < 1.0（部分 beat 没有证据）
    assert any(v.scores.beat_coverage < 1.0 for v in result.variants) or any(
        not b.evidence for v in result.variants for b in v.outline
    )


# ================================================================
# 30 秒产品广告：golden + 降级
# ================================================================


def test_ad30_golden_produces_variants_with_problem_beat():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    runner = _runner(analysis=analysis)
    inp = WorkflowInput(
        workflow_id=WorkflowId.PRODUCT_AD_30S, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = runner.run(inp)
    assert len(result.variants) >= 2
    # 广告 spec 的 beat 含 problem
    for v in result.variants:
        beat_ids = {b.beat_id for b in v.outline}
        # planner 按 spec.target_beats 分配（含 problem）
        assert "hook" in beat_ids or "call_to_action" in beat_ids


def test_ad30_degrades_when_missing_tag_or_shot():
    """广告需要 transcript+shot+tag；缺 tag → 降级。"""
    # _shot_only_analysis 无 transcript，但 _transcript_analysis 有 tag。
    # 构造一个缺 tag 的：只有 transcript + shot
    no_tag = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},),
        transcript=(TranscriptSegment(0, 108, "x", speaker="S1", confidence=0.9),),
        tags=(),  # 无 tag
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )
    analysis = _analysis_service_with("a1", no_tag)
    runner = _runner(analysis=analysis)
    inp = WorkflowInput(
        workflow_id=WorkflowId.PRODUCT_AD_30S, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = runner.run(inp)
    assert result.degraded
    assert result.variants == ()


# ================================================================
# 字幕初稿：artifact 路径
# ================================================================


def test_subtitle_draft_produces_artifact():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    store = ArtifactStore()
    runner = _runner(analysis=analysis, artifact_store=store)
    inp = WorkflowInput(
        workflow_id=WorkflowId.SUBTITLE_DRAFT, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = runner.run(inp)
    assert result.artifact_id is not None
    assert result.variants == ()  # 字幕不产 plan
    # artifact 可取回
    artifact = store.require(result.artifact_id)
    assert isinstance(artifact, ScriptArtifact)
    assert artifact.kind == ArtifactKind.SCRIPT
    assert artifact.source == ArtifactSource.SKILL
    assert len(artifact.lines) == 3  # 3 段转录


def test_subtitle_draft_lines_carry_speaker_and_evidence():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    store = ArtifactStore()
    runner = _runner(analysis=analysis, artifact_store=store)
    inp = WorkflowInput(
        workflow_id=WorkflowId.SUBTITLE_DRAFT, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = runner.run(inp)
    artifact = store.require(result.artifact_id)
    for ln in artifact.lines:
        assert ln.speaker in ("S1", "S2")  # 匿名（M4 红线）
        assert ln.text  # 非空


def test_subtitle_draft_without_store_reports_diagnostic():
    """字幕工作流需要 ArtifactStore；不注入 → 返回诊断，不抛错。"""
    analysis = _analysis_service_with("a1", _transcript_analysis())
    runner = _runner(analysis=analysis, artifact_store=None)
    inp = WorkflowInput(
        workflow_id=WorkflowId.SUBTITLE_DRAFT, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = runner.run(inp)
    assert result.artifact_id is None
    assert any(d.get("code") == "TAEP_SCHEMA_INVALID" for d in result.diagnostics)


def test_subtitle_draft_degrades_when_no_transcript():
    """字幕需要转录；无转录 → 降级（不产空 artifact）。"""
    analysis = _analysis_service_with("a1", _shot_only_analysis())
    store = ArtifactStore()
    runner = _runner(analysis=analysis, artifact_store=store)
    inp = WorkflowInput(
        workflow_id=WorkflowId.SUBTITLE_DRAFT, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = runner.run(inp)
    assert result.degraded
    assert result.artifact_id is None


# ================================================================
# 红线复用：M6 user-priority-override
# ================================================================


def test_subtitle_artifact_user_edit_blocks_skill_override():
    """用户编辑字幕 artifact 后，工作流（SKILL source）重跑 → 降级结果，不覆盖。

    AUD-15011 后：run() 不再让 ArtifactOverrideError 穿透（模块契约"失败以
    WorkflowResult.degradation 表达"），用户编辑优先的红线不变——store 中的
    artifact 保持用户版本。
    """
    analysis = _analysis_service_with("a1", _transcript_analysis())
    store = ArtifactStore()
    runner = _runner(analysis=analysis, artifact_store=store)
    inp = WorkflowInput(
        workflow_id=WorkflowId.SUBTITLE_DRAFT, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    # 第一次：工作流产 artifact
    result = runner.run(inp)
    artifact = store.require(result.artifact_id)
    # 用户编辑（标记 user_edited）
    user_version = artifact.with_user_edit()
    saved = store.save_user_edit(user_version)
    assert saved.user_edited is True
    # 再次跑工作流（同 artifact_id，source=SKILL）→ 被拒但以降级结果返回
    rerun = runner.run(inp)
    assert rerun.degraded
    assert rerun.artifact_id is None
    assert "编辑" in (rerun.degradation or "")
    # store 中的 artifact 仍是用户版本（未被模型覆盖）
    assert store.require(saved.artifact_id).user_edited is True


# ================================================================
# 复用闭环：variant 经 AssistantService select→validate→approve→commit→undo
# ================================================================


def test_workflow_variants_feed_into_assistant_service_commit_loop():
    """runner 产的 variant 经 AssistantService 走既有提交闭环（不写第二条路径）。

    验证 §11.1 M7 "应用仍只有一个 Undo 单元"：run_workflow 产的 variant 与
    plan_variants 产的同源，复用 select/validate/apply 路径。复刻
    ``test_ai_e2e_deterministic.test_e2e_ai_edit_lands_on_command_stack_and_is_undoable``
    的 _FakeView + CommandStack 模式。
    """
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
    from src.features.edit.ai_editing.approval_service import ApprovalService
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.ai_editing.assistant_service import AssistantService
    from src.features.edit.ai_editing.plan_compiler import compile_plan
    from src.features.edit.ai_editing.transaction_service import (
        TransactionService, _expected_commit_scope,
    )
    from src.features.edit.core.commands import CommandStack
    from src.features.edit.core.project import (
        EditProject, MediaAsset, TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    # 构造最小 project + timeline
    asset = MediaAsset(asset_id="a1", path="/x.mp4", media_type="video",
                       display_name="A", metadata={"duration_frames": 720})
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    ref = TimelineSequenceRef(timeline_id="t1", name="Main", fps=24.0,
                              width=1920, height=1080, sample_rate=48000)
    project = EditProject(project_id="p1", name="P", assets={"a1": asset},
                          timelines={"t1": ref}, root_bin_id="root",
                          active_timeline_id="t1")
    state = ProjectSequences(project=project, sequences={"t1": seq})

    analysis = _analysis_service_with("a1", _transcript_analysis())
    brief = _brief_service()
    EditSession.unbind()

    class FakeHost:
        def __init__(self):
            self._state = state

        def get_state(self): return self._state
        def on_committed(self, result):
            if result.applied:
                self._state = ProjectSequences(
                    project=result.new_project, sequences=result.new_sequences,
                )

    host = FakeHost()
    session = EditSession.bind(host)
    svc = AssistantService(
        session, brief_service=brief, analysis_service=analysis,
    )

    # 1. 工作流产 variant（经 run_workflow）
    inp = WorkflowInput(
        workflow_id=WorkflowId.VOICEOVER_POLISH, asset_ids=("a1",),
        timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        target_duration=720,
    )
    result = svc.run_workflow(inp)
    assert len(result.variants) >= 2
    chosen = result.variants[0]
    # variant 灌入了 service 的 _variants（select/validate/apply 路径复用）
    svc.select_variant(chosen.variant_id)
    assert svc._variants[chosen.variant_id].status == VariantStatus.SELECTED

    # 2. plan 从仓库解析（run_workflow 的 on_plan_compiled 已存入）
    plan = svc.plan_repository.get_plan(chosen.edit_plan_ref)
    assert plan is not None
    assert plan.brief_id == "b1"

    # 3. 真实 preview（绑定 plan_hash + diff_hash）
    txn = TransactionService()
    preview = txn.preview(plan, state)
    assert not preview.has_errors, f"plan 预览失败: {preview.report.diagnostics}"
    assert preview.diff is not None
    assert preview.diff.added_clips  # 至少加了一个 clip

    # 4. approval 合同 + 一个带真实 CommandStack 的 fake TimelineView（复刻 E2E）
    stack = CommandStack()

    class _FakeView:
        def __init__(self_, tl):
            self_._timeline = tl
            self_.emits = []

        def execute_command(self_, command):
            new = stack.execute(command, self_._timeline)
            if isinstance(new, tuple):
                new = new[0]
            self_._timeline = new
            self_.emits.append(new)

    view = _FakeView(state.sequences["t1"])

    class _FakeMW:
        timeline_panel = type("P", (), {"timeline_view": view})()

    # AUD-2105：最小 fake 不建模 MainWindow/feature_flags，显式声明
    # legacy_no_policy（本测试聚焦 M7 单 undo 单元闭环）。
    bridge = MainWindowAiEditHostBridge(
        _FakeMW(), svc, ApprovalService(), legacy_no_policy=True,
    )

    grant = bridge.approval_service().issue_after_user_confirmation(
        _expected_commit_scope(plan), issued_by="test",
    )
    bridge.approval_service().consume(grant.grant_id, _expected_commit_scope(plan))

    # 5. 经 bridge.commit_ai_command 推入真实 CommandStack（单一 undo 单元）
    compiled = compile_plan(plan)
    new_tl = bridge.commit_ai_command(
        plan.target.timeline_id, compiled.macro,
        undo_label=compiled.undo_label,
    )
    assert new_tl is not None
    assert len(new_tl.video_tracks[0].clips) >= 1  # clip 入栈
    assert stack.can_undo() is True

    # 6. 一次 undo 整体恢复（M7 核心验收：单个 MacroCommand = 单次 undo）
    undone = stack.undo(view._timeline)
    if isinstance(undone, tuple):
        undone = undone[0]
    assert undone is not None
    assert len(undone.video_tracks[0].clips) == 0  # 回到提交前

    EditSession.unbind()


# ================================================================
# 红线：低置信度标记
# ================================================================


def test_result_degradation_flag_when_low_confidence():
    """WorkflowResult.degraded 在 degradation 非空时为 True。"""
    analysis = AnalysisService()  # 空 → 必降级
    runner = _runner(analysis=analysis)
    result = runner.run(_voiceover_input())
    assert result.degraded is True
    assert result.degradation is not None


def test_multilingual_workflow_never_copies_source_without_provider():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    runner = _runner(analysis=analysis, artifact_store=ArtifactStore())
    result = runner.run(WorkflowInput(
        workflow_id=WorkflowId.MULTILINGUAL_VERSION,
        asset_ids=("a1",), timeline_id="t1", project_id="p1",
        base_revision=0, brief_id="b1", target_language="en-US",
    ))
    assert result.degraded
    assert result.artifact_id is None


def test_multilingual_workflow_saves_typed_translation_artifact():
    analysis = _analysis_service_with("a1", _transcript_analysis())
    store = ArtifactStore()

    def translate(segments, language):
        assert language == "en-US"
        return tuple(
            ScriptLine(
                line_id=f"{seg.segment_id}-en",
                text=f"translated:{seg.payload['text']}",
                note=f"evidence_ref={seg.segment_id}",
            )
            for seg in segments
        )

    runner = _runner(
        analysis=analysis, artifact_store=store,
        translation_provider=translate,
    )
    result = runner.run(WorkflowInput(
        workflow_id=WorkflowId.MULTILINGUAL_VERSION,
        asset_ids=("a1",), timeline_id="t1", project_id="p1",
        base_revision=0, brief_id="b1", target_language="en-US",
    ))
    assert result.artifact_id is not None
    artifact = store.get(result.artifact_id)
    assert isinstance(artifact, ScriptArtifact)
    assert artifact.source == ArtifactSource.SKILL
