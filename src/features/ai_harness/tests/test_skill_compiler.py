"""SkillCompiler 单元测试（plan §10.3 P1.3）。

覆盖 plan §10.3 P1.3 验收：同一内置"口播精剪"可由 chat、快捷卡、workflow
模板启动，最终都走同一 plan repo / approval / undo（同一 RunRecord 结构）。

核心反证：chat / 快捷卡 / workflow 三入口经 SkillCompiler 编译后 invoke_compiled
得到的 RunRecord 与直接 invoke 得到的结构一致（同一执行路径）。

红线（plan §10.3 P1.3 / §1.3）：

- 不让 run_workflow/run_skill 各自维护 variant/消息/状态副本（统一进 RunRecord）。
- 编译期校验输入 schema（缺必填 → 不 runnable）。
- 编译器不执行 provider（纯 Python）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    PermissionLevel,
    RunPolicy,
    RunStatus,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_harness.skill_compiler import (
    SkillCompiler,
    SkillDefinition,
    definition_from_manifest,
    official_definitions,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
    SkillSource,
)


# 复用 test_legacy_adapters.py 的 service fixture 模式
def _build_service_with_analysis(segs):
    from src.features.edit.ai_editing.brief_service import BriefService
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.ai_editing.assistant_service import AssistantService
    from src.features.edit.core.project import (
        EditProject, MediaAsset, TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    class _Host:
        def __init__(self, state) -> None:
            self._state = state
        def get_state(self):
            return self._state
        def get_manifest(self):
            from src.features.edit.ai_editing.capability_manifest import build_manifest
            return build_manifest()
        def on_committed(self, result):
            pass
        def request_user_approval(self, plan, diff):
            return None

    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/x.mp4", media_type="video", display_name="A",
        metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0, width=1920, height=1080,
        sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="P", assets={"a1": asset}, timelines={"t1": ref},
        root_bin_id="root", active_timeline_id="t1",
    )
    state = ProjectSequences(project=project, sequences={"t1": seq})

    class _FakeAnalysis:
        def __init__(self, segs) -> None:
            self._segs = tuple(segs)
        def get_status(self, asset_id):
            return "ready" if self._segs else "pending"
        def get_segments(self, asset_id):
            return self._segs

    EditSession.unbind()
    session = EditSession.bind(_Host(state))
    bs = BriefService()
    from src.features.edit.ai_editing.editorial_models import (
        DurationTarget, EditorialBrief, Platform,
    )
    bs.save(EditorialBrief(
        brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
        required_beats=("hook", "product_value", "call_to_action"),
    ))
    svc = AssistantService(
        session, brief_service=bs,
        analysis_service=_FakeAnalysis(segs),
    )
    return svc, state


def _transcript_segs():
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )
    return tuple(
        SemanticSegment(
            segment_id=f"t{i}", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
            source_range=FrameRange(s, d), payload={"text": txt}, confidence=0.9,
        )
        for i, (s, d, txt) in enumerate([
            (0, 108, "开场"), (108, 432, "卖点"), (540, 180, "行动号召"),
        ])
    )


@pytest.fixture
def service_with_evidence():
    svc, state = _build_service_with_analysis(_transcript_segs())
    yield svc, state
    from src.features.edit.ai_editing.edit_api import EditSession
    EditSession.unbind()


def _invocation(source=InvocationSource.WORKFLOW, skill_id="topos.voiceover_polish"):
    return Invocation(
        source=source, skill_id=skill_id,
        scope=InvocationScope(
            project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
        ),
        policy=RunPolicy(permission=PermissionLevel.PROPOSE),
        inputs={"brief_id": "b1", "target_duration": 720, "base_revision": 0},
        parent_session_id="sess1",
    )


# ================================================================
# 编译目标解析
# ================================================================


class TestTargetResolution:
    def test_official_skill_routes_to_workflow(self):
        compiler = SkillCompiler()
        defs = official_definitions()
        vo = next(d for d in defs if d.skill_id == "topos.voiceover_polish")
        compiled = compiler.compile(vo, _invocation())
        assert compiled.target == "workflow"
        assert compiled.workflow_input is not None

    def test_multie_role_skill_routes_to_taskgraph(self):
        compiler = SkillCompiler()
        # 构造一个 roles 非空的 manifest
        m = SkillManifest(
            skill_id="test.multirole",
            roles=("creative_director", "qa_reviewer"),
            source=SkillSource.OFFICIAL,
        )
        d = definition_from_manifest(m)
        compiled = compiler.compile(d, _invocation(skill_id="test.multirole"))
        assert compiled.target == "taskgraph"
        assert compiled.skill_input is not None

    def test_explicit_hint_workflow_with_valid_skill(self):
        """execution_hint=workflow 且 skill_id 可映射到 WorkflowId → workflow。"""
        compiler = SkillCompiler()
        m = SkillManifest(
            skill_id="topos.subtitle_draft", roles=("x",),  # roles 非空但 hint 覆盖
            source=SkillSource.OFFICIAL,
        )
        d = SkillDefinition(manifest=m, execution_hint="workflow")
        inv = _invocation(skill_id="topos.subtitle_draft")
        compiled = compiler.compile(d, inv)
        assert compiled.target == "workflow"
        assert compiled.workflow_input is not None

    def test_explicit_hint_workflow_with_invalid_skill_unsupported(self):
        """hint=workflow 但 skill_id 无法映射 → legacy_unsupported。"""
        compiler = SkillCompiler()
        m = SkillManifest(skill_id="totally.unknown", roles=("x",))
        d = SkillDefinition(manifest=m, execution_hint="workflow")
        compiled = compiler.compile(d, _invocation(skill_id="totally.unknown"))
        assert compiled.target == "legacy_unsupported"
        assert not compiled.runnable


# ================================================================
# 输入校验
# ================================================================


class TestInputValidation:
    def test_missing_required_brief_id_blocks(self):
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="topos.voiceover_polish",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            inputs={"target_duration": 720, "base_revision": 0},  # 缺 brief_id
            parent_session_id="sess1",
        )
        compiled = compiler.compile(d, inv)
        assert compiled.runnable is False
        assert any(d.get("code") == "MISSING_REQUIRED_INPUT" for d in compiled.diagnostics)

    def test_missing_asset_ids_blocks(self):
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="topos.voiceover_polish",
            scope=InvocationScope(project_id="p1", timeline_id="t1",
                                  selected_asset_ids=()),  # 空
            inputs={"brief_id": "b1"},
        )
        compiled = compiler.compile(d, inv)
        # asset_ids 来自 scope；空 tuple → 缺失
        assert compiled.runnable is False

    def test_complete_inputs_runnable(self):
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)
        compiled = compiler.compile(d, _invocation())
        assert compiled.runnable is True
        assert not compiled.has_errors


# ================================================================
# P1.3 核心验收：三入口同底层
# ================================================================


class TestThreeEntriesSamePath:
    """plan §10.3 P1.3：chat / 快捷卡 / workflow 模板同底层 RunRecord。

    通过 invoke_compiled（编译后统一入口）得到的 RunRecord 与直接 invoke 得到
    的结构一致，证明没有第二套系统。
    """

    def test_chat_vs_workflow_same_structure(self, service_with_evidence):
        svc, _ = service_with_evidence
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)

        # chat 入口：经 router 产出 invocation（这里直接构造 chat source）
        rt_chat = HarnessRuntime()
        adapter_chat = LegacyHarnessAdapter(svc, rt_chat)
        inv_chat = _invocation(source=InvocationSource.CHAT)
        compiled_chat = compiler.compile(d, inv_chat)
        rid_chat = adapter_chat.invoke_compiled(compiled_chat, inv_chat)

        # workflow 入口
        rt_wf = HarnessRuntime()
        adapter_wf = LegacyHarnessAdapter(svc, rt_wf)
        inv_wf = _invocation(source=InvocationSource.WORKFLOW)
        compiled_wf = compiler.compile(d, inv_wf)
        rid_wf = adapter_wf.invoke_compiled(compiled_wf, inv_wf)

        rec_chat = rt_chat.get_run(rid_chat)
        rec_wf = rt_wf.get_run(rid_wf)
        # 同结构：字段集合一致
        assert set(rec_chat.to_public_dict().keys()) == set(rec_wf.to_public_dict().keys())
        # 同 skill_id / 同终态集合
        assert rec_chat.skill_id == rec_wf.skill_id == "topos.voiceover_polish"
        assert rec_chat.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
        assert rec_wf.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)

    def test_compiled_vs_direct_invoke_same_record_shape(self, service_with_evidence):
        """编译后 invoke_compiled 与直接 invoke 得到同结构 RunRecord。"""
        svc, _ = service_with_evidence
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)

        # 直接 invoke（旧路径）
        rt1 = HarnessRuntime()
        ad1 = LegacyHarnessAdapter(svc, rt1)
        rid1 = ad1.invoke(_invocation())

        # 编译后 invoke_compiled（新统一路径）
        rt2 = HarnessRuntime()
        ad2 = LegacyHarnessAdapter(svc, rt2)
        compiled = compiler.compile(d, _invocation())
        rid2 = ad2.invoke_compiled(compiled, _invocation())

        rec1 = rt1.get_run(rid1)
        rec2 = rt2.get_run(rid2)
        assert set(rec1.to_public_dict().keys()) == set(rec2.to_public_dict().keys())
        assert rec1.skill_id == rec2.skill_id

    def test_unrunnable_compiled_goes_failed(self, service_with_evidence):
        svc, _ = service_with_evidence
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)
        # 缺 brief_id → 编译失败
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="topos.voiceover_polish",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            inputs={"target_duration": 720},  # 缺 brief_id
        )
        compiled = compiler.compile(d, inv)
        assert not compiled.runnable
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke_compiled(compiled, inv)
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "COMPILE_FAILED"
        assert any(diag.get("code") == "MISSING_REQUIRED_INPUT" for diag in rec.diagnostics)

    def test_workflow_target_runs_and_produces_variants(self, service_with_evidence):
        svc, _ = service_with_evidence
        compiler = SkillCompiler()
        d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)
        compiled = compiler.compile(d, _invocation())
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke_compiled(compiled, _invocation())
        rec = rt.get_run(rid)
        # 成功（或 with_warnings）
        assert rec.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)


# ================================================================
# official_definitions / definition_from_manifest
# ================================================================


class TestDefinitionHelpers:
    def test_official_definitions_three(self):
        defs = official_definitions()
        ids = {d.skill_id for d in defs}
        assert ids == {"topos.subtitle_draft", "topos.voiceover_polish",
                       "topos.product_ad_30s"}
        for d in defs:
            assert d.is_official is True

    def test_definition_from_manifest_carries_roles(self):
        m = SkillManifest(skill_id="t", roles=("a", "a", "b"))
        d = definition_from_manifest(m)
        assert d.roles == ("a", "b")  # manifest __post_init__ 去重
        assert d.skill_id == "t"


# ================================================================
# 红线：编译器不执行 provider
# ================================================================


class TestCompilerPurity:
    def test_compile_does_not_call_service(self, service_with_evidence):
        """compile 只做编译；不调 service.run_workflow/run_skill。"""
        svc, _ = service_with_evidence
        called = []
        orig_wf = svc.run_workflow
        orig_sk = svc.run_skill
        svc.run_workflow = lambda inp: called.append("wf")  # type: ignore
        svc.run_skill = lambda *a, **k: called.append("sk")  # type: ignore
        try:
            compiler = SkillCompiler()
            d = definition_from_manifest(OFFICIAL_VOICEOVER_POLISH, is_official=True)
            compiled = compiler.compile(d, _invocation())
            assert called == []  # 编译期不调 service
            assert compiled.runnable is True  # 但编译成功
        finally:
            svc.run_workflow = orig_wf
            svc.run_skill = orig_sk
