"""§11.8 矩阵补齐：ai_edit_controller（UI signal → service 映射）。

M1.1：FakeHost 实现类型化 ``AiEditHostBridge`` 协议（取代旧的 host:Any +
鸭子类型），controller 通过协议方法访问宿主能力。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.ai_edit_controller import AiEditController
from src.features.edit.ai_editing.assistant_events import (
    AssistantRequest,
    AssistantState,
)
from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.edit_api import EditSession
from src.features.edit.ai_editing.editorial_models import EditorialBrief


class _FakeHost:
    """实现 AiEditHostBridge 协议的测试宿主（类型化，M1.1）。"""

    def __init__(self, state, index=None):
        self._state = state
        self._index = index
        self.committed = []
        self._approval_service = None
        self.approval_grant = None  # 若设为非 None，request_plan_approval 返回它

    # ---- 只读世界状态 ---- #
    def get_project_sequences(self):
        return self._state

    def get_capability_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        return build_manifest()

    def get_semantic_index(self):
        return self._index

    # ---- 授权签发（M0.1）---- #
    def request_plan_approval(self, plan, preview):
        return self.approval_grant

    def approval_service(self):
        return self._approval_service

    # ---- plan 检索（M2.1）---- #
    def get_plan_by_id(self, plan_id):
        return None

    # ---- 提交（M2.2：进入真实 CommandStack）---- #
    def commit_ai_command(self, timeline_id, macro, *, undo_label):
        """测试桩：直接 apply macro 到 state 的 sequence，模拟真实 execute_command。"""
        from src.features.edit.core.timeline_v2 import TimelineSequence
        seq = self._state.sequences.get(timeline_id)
        if seq is None:
            return None
        try:
            new_seq = macro.apply(seq)
            if isinstance(new_seq, tuple):
                new_seq = new_seq[0]
            # 更新 state（模拟 _on_edit_timeline_changed 的两层 sync）
            from src.features.edit.ai_editing.edit_project_service import ProjectSequences
            new_seqs = dict(self._state.sequences)
            new_seqs[timeline_id] = new_seq
            self._state = ProjectSequences(project=self._state.project, sequences=new_seqs)
            self.committed.append(new_seq)
            return new_seq
        except Exception:
            return None

    def get_active_timeline_id(self):
        return self._state.project.active_timeline_id if self._state else None

    # ---- 提交反馈 ---- #
    def refresh_after_ai_commit(self, result):
        self.committed.append(result)

    # 兼容旧 EditSessionHost（session.bind 仍需要这些）
    def get_state(self):
        return self._state

    def on_committed(self, result):
        self.committed.append(result)
        from src.features.edit.ai_editing.edit_project_service import ProjectSequences
        if getattr(result, "applied", False):
            self._state = ProjectSequences(
                project=result.new_project, sequences=result.new_sequences
            )

    def request_user_approval(self, plan, diff):
        return self.approval_grant


class _FakePanel:
    """最小面板桩，模拟 AiEditAssistantPanel 的 signal。"""

    def __init__(self):
        from PyQt6.QtCore import pyqtSignal
        # 用真实 signal 以验证 controller connect
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        self._real = AiEditAssistantPanel()
        # 记录 controller 回填的预览 brief（M1.3）
        self.pending_brief = None

    @property
    def request_submitted(self):
        return self._real.request_submitted

    @property
    def variant_selected(self):
        return self._real.variant_selected

    @property
    def apply_requested(self):
        return self._real.apply_requested

    @property
    def cancel_requested(self):
        return self._real.cancel_requested

    @property
    def brief_patch_requested(self):
        return self._real.brief_patch_requested

    @property
    def context_inspector_requested(self):
        # M5：上下文检查器信号（controller 连接）
        return self._real.context_inspector_requested

    @property
    def context_block_excluded(self):
        return self._real.context_block_excluded

    @property
    def context_block_restored(self):
        return self._real.context_block_restored

    def set_context_bundle(self, bundle):
        """M5：记录 controller 回填的 ContextBundle。"""
        self.last_bundle = bundle

    def set_pending_brief(self, brief):
        """M1.3：记录 controller 回填的预览 brief。"""
        self.pending_brief = brief

    def refresh_state(self, snapshot):
        self._real.refresh_state(snapshot)


def _build(qapp):
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational
    seq = TimelineSequence(timeline_id="t1", name="Main", fps=Rational(24, 1),
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),))
    asset = MediaAsset(asset_id="a1", path="/x.mp4", media_type="video", display_name="A", metadata={"duration_frames": 5000})
    ref = TimelineSequenceRef(timeline_id="t1", name="Main", fps=24.0, width=1920, height=1080, sample_rate=48000)
    project = EditProject(project_id="p1", name="P", assets={"a1": asset}, timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1")
    state = ProjectSequences(project=project, sequences={"t1": seq})
    host = _FakeHost(state)
    EditSession.unbind()
    session = EditSession.bind(host)
    bs = BriefService()
    bs.save(EditorialBrief(brief_id="b1", revision=1))
    service = AssistantService(session, brief_service=bs)
    panel = _FakePanel()
    controller = AiEditController(panel, service, bridge=host)
    return controller, service, panel, host


def test_controller_routes_state_to_panel(qapp):
    controller, service, panel, host = _build(qapp)
    states = []
    controller.state_changed.connect(lambda s: states.append(s.state))
    controller.start(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        profile_id="p1", profile_connected=True,
    )
    assert service.state == AssistantState.IDLE
    assert states  # 收到状态广播


def test_controller_request_gates_on_brief_approval(qapp):
    """M1.3：提交需求后停在 AWAITING_BRIEF_APPROVAL，未规划；批准 Brief 后才规划。"""
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )
    from src.features.edit.ai_editing.semantic_index import SemanticIndex
    controller, service, panel, host = _build(qapp)
    idx = SemanticIndex()
    idx.add(SemanticSegment(segment_id="t0", asset_id="a1", source_range=FrameRange(0, 100),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "hi"}, confidence=0.9))
    host._index = idx

    controller.start(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        profile_id="p1", profile_connected=True,
    )
    # 模拟用户提交需求
    panel.request_submitted.emit(AssistantRequest(
        request_id="r1", text="30 秒竖版", profile_id="p1",
    ))
    # 门禁：停在 AWAITING_BRIEF_APPROVAL，未规划（variants 为空）
    assert service.state == AssistantState.AWAITING_BRIEF_APPROVAL
    assert len(service._variants) == 0
    # controller 已把预览 brief 回填进面板
    assert panel.pending_brief is not None

    # 用户在编辑器点"批准 Brief" → 触发规划
    approved = panel.pending_brief.with_revision_bumped()
    panel.brief_patch_requested.emit(approved)
    # 批准后才规划
    assert len(service._variants) >= 2
    assert service.state != AssistantState.AWAITING_BRIEF_APPROVAL


def test_controller_cancel(qapp):
    controller, service, panel, host = _build(qapp)
    controller.start(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        profile_id="p1", profile_connected=True,
    )
    panel.cancel_requested.emit()
    assert service.state == AssistantState.CANCELLED


def test_controller_cancel_marks_chat_worker_cooperatively(qapp):
    """停止聊天时必须同时通知本地 worker，不能只翻转 service 状态。"""
    controller, service, panel, host = _build(qapp)

    class _Worker:
        def __init__(self):
            self.cancel_requested = False
            self.quit_called = False

        def isRunning(self):
            return True

        def quit(self):
            self.quit_called = True

    worker = _Worker()
    controller._chat_worker = worker
    controller._on_cancel()

    assert worker.cancel_requested is True
    assert worker.quit_called is True
    assert service.state == AssistantState.CANCELLED


def test_motion_graphic_postprocess_runs_off_gui_thread(qapp):
    """Browser post-processing must not execute in the controller's thread."""
    import threading
    import time

    controller, service, panel, host = _build(qapp)
    main_ident = threading.get_ident()
    worker_idents = []
    callback_idents = []
    completed = []

    def processor(request_id, response):
        worker_idents.append(threading.get_ident())
        time.sleep(0.05)
        return {"request_id": request_id, "response": response}

    def on_finished(request_id, payload):
        callback_idents.append(threading.get_ident())
        completed.append((request_id, payload))

    assert controller.start_motion_graphic_postprocess(
        "mg-async", "<html></html>", processor, on_finished,
        lambda request_id, error: completed.append((request_id, error)),
    ) is True

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not completed:
        qapp.processEvents()
        time.sleep(0.01)

    assert completed == [("mg-async", {"request_id": "mg-async", "response": "<html></html>"})]
    assert worker_idents and worker_idents[0] != main_ident
    assert callback_idents == [main_ident]


def test_retry_replays_last_request(qapp):
    """错误页重试应复用最近请求文本并生成新的 request id。"""
    controller, service, panel, host = _build(qapp)
    controller._last_request_text = "帮我生成一个片头"
    replayed = []
    controller._on_request_submitted = replayed.append

    controller._on_retry_requested()

    assert len(replayed) == 1
    assert replayed[0].text == "帮我生成一个片头"
    assert replayed[0].request_id


def test_controller_records_autonomous_outcomes_in_goal_supervisor(qapp):
    from src.features.edit.ai_editing.goal_supervisor import GoalSpec, GoalStatus

    controller, service, panel, host = _build(qapp)
    controller._autonomous_goals["run-goal"] = GoalSpec(
        goal_id="run-goal", project_id="p1", timeline_id="t1",
        objective_ref="skill:product-ad",
    )

    controller._record_autonomous_goal_cycle(
        "run-goal", code="STALE_PLAN", applied=False, reason="stale",
    )
    first = controller._autonomous_goal_checkpoints.load("run-goal")
    assert first is not None and first.status == GoalStatus.RUNNING

    controller._record_autonomous_goal_cycle(
        "run-goal", code="AUTO_APPLIED", applied=True, reason="done",
    )
    second = controller._autonomous_goal_checkpoints.load("run-goal")
    assert second is not None and second.status == GoalStatus.SUCCEEDED


def test_replanned_run_keeps_parent_goal_budget_and_checkpoint(qapp):
    from src.features.edit.ai_editing.goal_supervisor import GoalSpec, GoalStatus

    controller, service, panel, host = _build(qapp)
    controller._autonomous_goals["goal-parent"] = GoalSpec(
        goal_id="goal-parent", project_id="p1", timeline_id="t1",
        objective_ref="skill:product-ad",
    )
    controller._autonomous_goal_ids["run-replan"] = "goal-parent"

    controller._record_autonomous_goal_cycle(
        "run-replan", code="STALE_PLAN", applied=False, reason="stale",
    )
    checkpoint = controller._autonomous_goal_checkpoints.load("goal-parent")
    assert checkpoint is not None
    assert checkpoint.cycle == 1
    assert checkpoint.status == GoalStatus.RUNNING
    assert controller._autonomous_goal_checkpoints.load("run-replan") is None

    controller._record_autonomous_goal_cycle(
        "run-replan", code="AUTO_APPLIED", applied=True, reason="done",
    )
    checkpoint = controller._autonomous_goal_checkpoints.load("goal-parent")
    assert checkpoint is not None and checkpoint.status == GoalStatus.SUCCEEDED


def test_controller_set_profile(qapp):
    controller, service, panel, host = _build(qapp)
    controller.start(AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"))
    assert service.state == AssistantState.NO_PROFILE
    controller.set_profile("cloud-1", True)
    assert service.state == AssistantState.IDLE


def test_auto_caption_agent_reports_missing_audio_scope(qapp):
    """自然语言自动字幕没有选中音频时，run 应失败并给出可诊断错误码。"""
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime

    controller, service, panel, host = _build(qapp)
    controller.runtime = HarnessRuntime()
    host.get_selected_audio_clip = lambda: None
    inv = Invocation(
        source=InvocationSource.CHAT,
        skill_id="topos.auto_caption",
        scope=InvocationScope(project_id="p1", timeline_id="t1"),
        policy=RunPolicy(),
    )
    assert controller._start_auto_caption_agent(inv) is True
    run = controller.runtime.get_run(controller._active_chat_run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "AUDIO_CLIP_NOT_SELECTED"


def test_auto_caption_agent_runs_asr_then_commits(qapp):
    """Agent worker 完成后应推进 runtime 到 succeeded 并调用字幕写入。"""
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime

    controller, service, panel, host = _build(qapp)
    controller.runtime = HarnessRuntime()
    host.get_selected_audio_clip = lambda: ("c1", "a1", "asset-1")
    started = []

    def start_transcription(asset_id, options, *, on_progress, on_completed, on_failed):
        started.append((asset_id, options))
        on_progress(0.5, "ASR")
        on_completed(object())
        return True, ""

    host.start_ai_transcription = start_transcription
    committed = []
    host.commit_ai_transcript_subtitles = (
        lambda clip_id, track_id, asset_id, analysis: (
            committed.append((clip_id, track_id, asset_id)), (True, "")
        )[1]
    )
    inv = Invocation(
        source=InvocationSource.CHAT,
        skill_id="topos.auto_caption",
        scope=InvocationScope(
            project_id="p1", timeline_id="t1", selected_asset_ids=("asset-1",),
        ),
        policy=RunPolicy(),
    )
    assert controller._start_auto_caption_agent(inv) is True
    run = controller.runtime.get_run(controller._active_chat_run_id)
    assert run is not None
    assert run.status == "succeeded"
    assert started and started[0][0] == "asset-1"
    assert committed == [("c1", "a1", "asset-1")]


def test_retry_preserves_original_scope_and_inputs(qapp, monkeypatch):
    """失败重试不能改用用户当前选择覆盖原 run 的作用域。"""
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime

    controller, service, panel, host = _build(qapp)
    controller.runtime = HarnessRuntime()
    controller.router = object()  # retry handler only needs a non-null router
    inv = Invocation(
        source=InvocationSource.CHAT,
        skill_id="topos.product_ad_30s",
        scope=InvocationScope(
            project_id="p1", timeline_id="t1", selected_asset_ids=("asset-old",),
        ),
        policy=RunPolicy(),
        inputs={"target_duration": "30s", "platform": "vertical"},
        parent_session_id="session-1",
    )
    run_id = controller.runtime.start(inv)
    controller.runtime.mark_running(run_id)
    controller.runtime.finish(
        run_id, success=False, error_code="ASR_FAILED", error_message="temporary",
    )
    captured = {}

    def _retry(result, scope, **kwargs):
        captured["skill_id"] = result.skill_id
        captured["scope"] = scope
        captured.update(kwargs)
        return True

    monkeypatch.setattr(controller, "_execute_skill_route", _retry)
    controller._on_result_action_retry(run_id)

    assert captured["skill_id"] == "topos.product_ad_30s"
    assert captured["scope"] == inv.scope
    assert captured["parent_session_id"] == "session-1"
    assert captured["inputs_override"] == inv.inputs


def test_autonomous_replan_is_limited_to_one_fresh_pass(qapp, monkeypatch):
    """自主恢复只能重走一次当前 scope 的规划，避免失败循环。"""
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime

    controller, service, panel, host = _build(qapp)
    controller.runtime = HarnessRuntime()
    controller.router = object()
    host.get_project_id = lambda: "p1"
    host.get_active_timeline_id = lambda: "t1"
    inv = Invocation(
        source=InvocationSource.CHAT,
        skill_id="topos.product_ad_30s",
        scope=InvocationScope(project_id="p1", timeline_id="t1"),
        policy=RunPolicy(),
        inputs={"target_duration": "30s"},
    )
    run_id = controller.runtime.start(inv)
    calls = []

    def _replan(result, scope, **kwargs):
        calls.append((result.skill_id, scope, kwargs, controller._autonomous_replan_goal_id))
        return True

    controller._autonomous_goal_ids[run_id] = "goal-parent"
    monkeypatch.setattr(controller, "_execute_skill_route", _replan)
    assert controller._try_autonomous_replan(run_id, "STALE_PLAN") is True
    assert controller._try_autonomous_replan(run_id, "STALE_PLAN") is False
    assert len(calls) == 1
    assert calls[0][1].project_id == "p1"
    assert calls[0][3] == "goal-parent"
    assert controller._autonomous_replan_goal_id == ""


def test_autonomous_apply_reentrancy_is_guarded_per_run(qapp, monkeypatch):
    controller, service, panel, host = _build(qapp)
    run_id = "run-inflight"
    controller._autonomous_runs.add(run_id)
    calls = []

    def _apply_once(current_run_id):
        calls.append(current_run_id)
        controller._try_autonomous_apply(current_run_id)

    monkeypatch.setattr(controller, "_try_autonomous_apply_once", _apply_once)
    controller._try_autonomous_apply(run_id)

    assert calls == [run_id]
    assert run_id not in controller._autonomous_inflight


def test_router_export_delegates_to_host(qapp):
    """自然语言导出只委托给宿主，不直接触碰文件系统。"""
    from src.features.ai_harness.invocation_router import InvocationRouter

    controller, service, panel, host = _build(qapp)
    controller.router = InvocationRouter()
    calls = []
    host.request_ai_export = lambda scope, inputs: (
        calls.append((scope, inputs)), (True, "导出任务已启动。")
    )[1]

    assert controller._dispatch_via_router("导出成片") is True
    assert len(calls) == 1
    assert calls[0][0].timeline_id == "t1"


def test_generated_media_placement_uses_preview_approval_and_command_stack(qapp):
    """生成候选放置时间线必须先形成 plan、预览并消费一次性授权。"""
    from src.features.ai_harness.invocation_models import (
        ArtifactRef, Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime
    from src.features.edit.ai_editing.approval_service import ApprovalService
    from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

    controller, service, panel, host = _build(qapp)
    runtime = HarnessRuntime()
    controller.runtime = runtime
    host.get_ai_generated_media_asset_id = lambda asset_id: "a1"
    approval = ApprovalService()
    host._approval_service = approval

    def approve(plan, preview):
        return approval.issue_after_user_confirmation(
            _expected_commit_scope(plan, diff_hash=preview.diff_hash),
        )

    host.request_plan_approval = approve
    invocation = Invocation(
        source=InvocationSource.CHAT,
        skill_id="topos.generate_media",
        scope=InvocationScope(project_id="p1", timeline_id="t1"),
        policy=RunPolicy(),
        inputs={"placement": "seconds", "placement_seconds": 10.0},
    )
    run_id = runtime.start(invocation)
    runtime.record_artifact(run_id, ArtifactRef(
        artifact_id="gen-1", artifact_type="generated_video",
    ))
    runtime.finish(run_id, success=True)

    controller._on_place_generated_media(run_id)

    sequence = host._state.sequences["t1"]
    clips = sequence.get_track_by_id("v1").clips
    assert len(clips) == 1
    assert clips[0].asset_id == "a1"
    assert clips[0].timeline_start == 240
    assert approval.active_count() == 0


def test_apply_revalidates_stale_preview_before_consuming_grant(qapp):
    """M2.2：手动编辑令 preview 过期后，AI 不得绕过 revision 校验提交。"""
    from src.features.edit.ai_editing.approval_service import ApprovalService
    from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

    controller, service, panel, host = _build(qapp)
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )
    from src.features.edit.ai_editing.semantic_index import SemanticIndex

    idx = SemanticIndex()
    idx.add(SemanticSegment(
        segment_id="t0", asset_id="a1", source_range=FrameRange(0, 100),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "hi"}, confidence=0.9,
    ))
    host._index = idx
    controller.start(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        profile_id="p1", profile_connected=True,
    )
    panel.request_submitted.emit(AssistantRequest(
        request_id="r1", text="30 秒竖版", profile_id="p1",
    ))
    # M1.3：显式批准 Brief 后才规划
    assert panel.pending_brief is not None
    approved = panel.pending_brief.with_revision_bumped()
    panel.brief_patch_requested.emit(approved)
    variant = next(iter(service._variants.values()))
    panel.variant_selected.emit(variant.variant_id)
    plan = service.plan_repository.get_plan(variant.edit_plan_ref)
    assert plan is not None

    # 模拟用户在确认前完成了一次手动编辑：plan.base_revision=0 现在已失效。
    from dataclasses import replace
    stale_sequence = replace(host._state.sequences["t1"], revision=1)
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    host._state = ProjectSequences(
        project=host._state.project, sequences={"t1": stale_sequence},
    )
    host._approval_service = ApprovalService()
    host.approval_grant = host._approval_service.issue_after_user_confirmation(
        _expected_commit_scope(plan), issued_by="test",
    )

    panel.apply_requested.emit(variant.variant_id)

    assert host.committed == []
    assert service.state == AssistantState.FAILED
    # 没有走到授权 consume，因此 grant 保持可用；用户重规划后可重新确认。
    assert host._approval_service.active_count() == 1


# ---------------------------------------------------------------------- #
# B6: 历史完整正文（不再 500 字截断）
# ---------------------------------------------------------------------- #


def test_record_assistant_message_stores_full_text():
    """B6: 长回复（>500 字）在历史中完整存储，不被截断为 500 字摘要。"""
    from unittest.mock import MagicMock

    ctrl = AiEditController.__new__(AiEditController)
    ctrl.session_store = MagicMock()
    ctrl._refresh_history_menu = lambda: None

    # 构造一个 >500 字的回复。
    long_reply = "这是一段很长的 AI 回复。" * 100  # ~1000 字
    ctrl._record_assistant_message(long_reply)

    # 验证存储的文本是完整的（不是 text[:500]）。
    ctrl.session_store.add_message_summary.assert_called_once()
    stored = ctrl.session_store.add_message_summary.call_args[0][0]
    assert len(stored.text) > 500
    assert "这是一段很长的 AI 回复。" in stored.text


def test_record_assistant_message_sanitizes_secrets():
    """B6: 存储内容经过脱敏（MessageSummary.to_dict 调用 sanitize_for_storage）。"""
    from src.features.edit.ai_editing.session_models import MessageSummary

    # 构造含敏感信息的回复。
    sensitive_reply = "API key is sk-1234567890abcdef /Users/secret/path"
    summary = MessageSummary(role="assistant", text=sensitive_reply)
    serialized = summary.to_dict()
    # sanitize_for_storage 应脱敏路径和 key。
    assert "1234567890abcdef" not in serialized["text"]


def test_record_user_message_stores_full_text():
    """B6: 用户消息也存储完整正文。"""
    from unittest.mock import MagicMock

    ctrl = AiEditController.__new__(AiEditController)
    ctrl.session_store = MagicMock()
    ctrl._refresh_history_menu = lambda: None
    ctrl.session_store.get_active_session = lambda: None

    long_input = "用户输入了很长的描述。" * 100  # ~1000 字
    ctrl._record_user_message(long_input)

    ctrl.session_store.add_message_summary.assert_called_once()
    stored = ctrl.session_store.add_message_summary.call_args[0][0]
    assert len(stored.text) > 500
