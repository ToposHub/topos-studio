"""Legacy adapters 单元测试（plan §14 step 3 / §10.2 P0.2）。

覆盖：

- ``invocation_to_workflow_input`` / ``invocation_to_skill_input`` 字段映射。
- ``LegacyHarnessAdapter.invoke``：workflow 路径产同一 RunRecord（plan §14 step 3
  验收：同一"自动字幕"从 chat 和 workflow 发起获得同结构 RunRecord）。
- skill 路径（官方 Skill roles=() 走 workflow backend；多角色走 TaskGraph）。
- 取消 token 透传（plan §10.2 P0.6）：run.cancel -> run_skill 收到 cancel_requested。
- 异常被归入 RunRecord（不向上冒泡破坏 runtime）。
- degradation / missing_evidence -> SUCCEEDED_WITH_WARNINGS。
- 无产出非降级 -> FAILED。

红线对照（plan §10.2 P0.6 / §1.4）：

- 取消是真实 token 传播。
- 同一 RunRecord 结构（chat 与 workflow 同源）。
- 不允许两边各自创建 JobRegistry（adapter 走 runtime 单点）。
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
from src.features.ai_harness.legacy_adapters import (
    LegacyHarnessAdapter,
    invocation_to_skill_input,
    invocation_to_workflow_input,
)
from src.features.edit.ai_editing.workflow import WorkflowId


# ================================================================
# 映射函数
# ================================================================


class TestMapping:
    def test_workflow_input_mapping(self):
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="topos.subtitle_draft",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1", "a2"),
            ),
            inputs={"brief_id": "b1", "target_duration": 360, "base_revision": 3},
        )
        wf = invocation_to_workflow_input(inv)
        assert wf.workflow_id == WorkflowId.SUBTITLE_DRAFT
        assert wf.asset_ids == ("a1", "a2")
        assert wf.timeline_id == "t1"
        assert wf.project_id == "p1"
        assert wf.brief_id == "b1"
        assert wf.target_duration == 360
        assert wf.base_revision == 3

    def test_workflow_input_unknown_skill_raises(self):
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="totally.unknown",
            scope=InvocationScope(project_id="p1"),
        )
        with pytest.raises(ValueError):
            invocation_to_workflow_input(inv)

    def test_workflow_input_strips_prefix(self):
        # "topos.voiceover_polish" 与 "voiceover_polish" 都应映射成功
        for sid in ("topos.voiceover_polish", "voiceover_polish"):
            inv = Invocation(
                source=InvocationSource.WORKFLOW, skill_id=sid,
                scope=InvocationScope(project_id="p1"),
            )
            assert invocation_to_workflow_input(inv).workflow_id == WorkflowId.VOICEOVER_POLISH

    def test_skill_input_mapping(self):
        inv = Invocation(
            source=InvocationSource.CHAT, skill_id="topos.voiceover_polish",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            inputs={"brief_id": "b1", "evidence_refs": ("e1", "e2"),
                    "brief_summary": "30 秒竖版"},
        )
        sk = invocation_to_skill_input(inv)
        assert sk.skill_id == "topos.voiceover_polish"
        assert sk.asset_ids == ("a1",)
        assert sk.evidence_refs == ("e1", "e2")
        assert sk.brief_summary == "30 秒竖版"


# ================================================================
# LegacyHarnessAdapter.invoke
# ================================================================


def _build_service_with_analysis(segs):
    """构造一个带分析证据的 AssistantService（复用既有测试模式）。"""
    from src.features.edit.ai_editing.brief_service import BriefService
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.ai_editing.assistant_service import AssistantService
    from src.features.edit.ai_editing.editorial_models import (
        DurationTarget, EditorialBrief, Platform,
    )
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
    # AUD-22602：bind 之后的任一构造步骤抛错时，模块级单例 EditSession 必须
    # 显式解绑再抛出，否则全局绑定泄漏进同进程后续测试（隐式跨测试耦合）。
    # 这里只做清理后原样 re-raise，不吞错。
    try:
        bs = BriefService()
        bs.save(EditorialBrief(
            brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
            target_duration=DurationTarget(720, 648, 792),
            required_beats=("hook", "product_value", "call_to_action"),
        ))
        svc = AssistantService(
            session, brief_service=bs,
            analysis_service=_FakeAnalysis(segs),
        )
    except BaseException:
        EditSession.unbind()
        raise
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


@pytest.fixture
def service_no_evidence():
    svc, state = _build_service_with_analysis(())
    yield svc, state
    from src.features.edit.ai_editing.edit_api import EditSession
    EditSession.unbind()


def _invocation(skill_id="topos.voiceover_polish", source=InvocationSource.WORKFLOW):
    return Invocation(
        source=source, skill_id=skill_id,
        scope=InvocationScope(
            project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
        ),
        policy=RunPolicy(permission=PermissionLevel.PROPOSE),
        inputs={"brief_id": "b1", "target_duration": 720, "base_revision": 0},
        parent_session_id="sess1",
    )


class TestInvokeWorkflowPath:
    def test_workflow_succeeds_with_variants(self, service_with_evidence):
        svc, _ = service_with_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke(_invocation())
        rec = rt.get_run(rid)
        assert rec.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
        # 有产出
        assert rec.source == InvocationSource.WORKFLOW
        assert rec.skill_id == "topos.voiceover_polish"

    def test_chat_and_workflow_same_run_record_structure(self, service_with_evidence):
        """plan §14 step 3 验收：chat 与 workflow 同结构 RunRecord。"""
        svc, _ = service_with_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        # workflow source
        rid_wf = adapter.invoke(_invocation(source=InvocationSource.WORKFLOW))
        # chat source（同一个 skill，可路由到 workflow 路径）
        rid_chat = adapter.invoke(_invocation(source=InvocationSource.CHAT))
        rec_wf = rt.get_run(rid_wf)
        rec_chat = rt.get_run(rid_chat)
        # 同结构：字段齐全、status 取值范围一致
        assert set(rec_wf.to_public_dict().keys()) == set(rec_chat.to_public_dict().keys())
        assert rec_wf.skill_id == rec_chat.skill_id
        assert rec_wf.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
        assert rec_chat.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)

    def test_missing_evidence_is_warnings_not_failure(self, service_no_evidence):
        svc, _ = service_no_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke(_invocation())
        rec = rt.get_run(rid)
        # 无证据 → degradation/missing_evidence → 不算 FAILED
        assert rec.status != RunStatus.FAILED
        # 至少有 missing_evidence 诊断或 with_warnings
        assert (rec.status == RunStatus.SUCCEEDED_WITH_WARNINGS
                or any(d.get("code") == "MISSING_EVIDENCE" for d in rec.diagnostics))


class TestInvokeSkillPath:
    def test_skill_unknown_routes_to_skill_and_records(self, service_with_evidence):
        svc, _ = service_with_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="topos.does_not_exist",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            inputs={"brief_id": "b1", "target_duration": 720, "base_revision": 0},
            parent_session_id="sess1",
        )
        rid = adapter.invoke(inv)
        rec = rt.get_run(rid)
        # 未注册 skill → SkillRuntime 返回诊断 → 不算崩溃
        assert rec.status in (
            RunStatus.FAILED, RunStatus.SUCCEEDED_WITH_WARNINGS,
        )
        # 诊断里有记录
        assert len(rec.diagnostics) >= 1 or rec.error_code


class TestCancelPropagation:
    def test_cancel_token_forwarded_to_run_skill(self, service_with_evidence):
        """plan §10.2 P0.6：取消是真实 token 传播。

        走公开 ``adapter.invoke`` 的 skill 路径，验证：
        1. adapter 把 ``runtime.is_cancelled`` 作为可调用的 ``cancel_requested``
           传入 ``run_skill``；
        2. ``run.cancel`` 后同一 token 立刻翻转为 True（真实传播，非一次性快照）；
        3. ``cancelled_tasks`` → run 终态 CANCELLED。
        """
        svc, _ = service_with_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        seen = {}

        class _CancelledSkillResult:
            """run_skill 返回值替身：声明任务已因取消终止。"""

            diagnostics: tuple = ()
            artifacts: tuple = ()
            variants: tuple = ()
            degraded: bool = False
            failed_tasks: tuple = ()
            artifact_id = None
            cancelled_tasks = ("creative_director",)

        def fake_run_skill(sk_input, *, cancel_requested=None):
            seen["cancel_callable"] = callable(cancel_requested)
            seen["before_cancel"] = bool(cancel_requested())
            # 模拟长任务执行期间用户取消：run 已由 invoke() start，
            # 本测试进程内只有这一个 run，从 runtime 取其 id 后 cancel。
            active_rid = rt.list_runs()[-1].run_id
            rt.cancel(active_rid)
            seen["after_cancel"] = bool(cancel_requested())
            return _CancelledSkillResult()

        svc.run_skill = fake_run_skill  # type: ignore[method-assign]
        # unknown skill → 不映射 WorkflowId → 走 skill 路径（run_skill）
        inv = Invocation(
            source=InvocationSource.DEVELOPER, skill_id="topos.long_running",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            inputs={"brief_id": "b1"},
            parent_session_id="sess1",
        )
        rid = adapter.invoke(inv)
        # run_skill 被调用且收到了可调用的 cancel token
        assert seen["cancel_callable"] is True
        assert seen["before_cancel"] is False
        # cancel 后同一 token 立刻翻转为 True（透传真实生效）
        assert seen["after_cancel"] is True
        # cancelled_tasks + is_cancelled → 终态 CANCELLED
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.CANCELLED
        assert rt.is_cancelled(rid)


class TestExceptionHandling:
    def test_workflow_exception_recorded_not_raised(self, service_with_evidence):
        svc, _ = service_with_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        # 构造一个会让 run_workflow 内部抛错的 invocation（坏 base_revision 类型已
        # 被 int() 兜住；这里用无法映射的 workflow 但强制走 workflow 路径）。
        # 改用 monkey：把 service.run_workflow 替换成抛错。
        calls = []
        def boom(inp):
            calls.append(inp)
            raise RuntimeError("simulated provider crash")
        svc.run_workflow = boom  # type: ignore[method-assign]
        inv = _invocation(skill_id="topos.subtitle_draft")
        rid = adapter.invoke(inv)
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "WORKFLOW_EXCEPTION"
        assert "simulated provider crash" in rec.error_message
        # 诊断也记录
        assert any(d.get("code") == "WORKFLOW_EXCEPTION" for d in rec.diagnostics)


class TestNoDuplicateJobRegistry:
    def test_adapter_uses_single_runtime(self, service_with_evidence):
        """plan §10.2 P0.2：不允许两边各自创建 JobRegistry。

        adapter 只持有传入的 runtime；多次 invoke 复用同一 runtime 的 _runs。
        """
        svc, _ = service_with_evidence
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid1 = adapter.invoke(_invocation())
        rid2 = adapter.invoke(_invocation())
        # 同一 runtime 的 list_runs 能看到两次
        runs = rt.list_runs()
        ids = {r.run_id for r in runs}
        assert rid1 in ids and rid2 in ids
