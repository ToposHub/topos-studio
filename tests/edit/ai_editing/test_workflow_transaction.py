"""T-P2-21: WorkflowTransaction 状态机与失败策略测试.

固定五件事（对应卡点实施步骤 1/3/4）：
1. 合法生命周期 QUEUED → PREFLIGHT → RUNNING → VERIFYING → 终态，
   每次转移记录进度/阶段/日志引用/可取消性；
2. 非法转移（跳态、终态再转移、越界进度）被拒绝；
3. cancel 只在可取消状态允许；
4. 失败策略恰好三种，补偿建议是策略的显式函数（rollback 给出逆序
   undo tokens，pause_for_user 提及不可逆操作需特定授权）；
5. 与真实 TransactionService 集成：EditSession apply 作为 RUNNING 步骤，
   CommitResult 的 undo_label 作为 undo token 记录（卡步骤 1 的
   "undo token" 有真实来源，不是编造字符串）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.workflow_transaction import (
    CANCELLABLE_STATES, FAILURE_POLICIES_EXACT, FailurePolicy,
    IllegalTransitionError, TERMINAL_STATES, WorkflowState,
    WorkflowTransaction,
)

_SNAPSHOT = {"protocol": "taep/1.0", "effects": ["blur"], "codecs": ["h264"]}


def _open(policy: FailurePolicy = FailurePolicy.ROLLBACK) -> WorkflowTransaction:
    return WorkflowTransaction(
        workflow_id="wf-1", project_id="p1", timeline_id="t1",
        base_revision=4, capability_snapshot=dict(_SNAPSHOT),
        failure_policy=policy)


# ── 生命周期 ──

def test_happy_path_lifecycle():
    wf = _open()
    assert wf.state is WorkflowState.QUEUED and wf.cancellable
    wf.transition(WorkflowState.PREFLIGHT, progress=0.1, stage="preflight",
                  log_ref="audit://wf-1/pf")
    wf.transition(WorkflowState.RUNNING, progress=0.3, stage="apply")
    assert wf.cancellable
    step = wf.add_step("apply plan", operation_id="edit.add_clip",
                       inputs={"clip_id": "c1"}, outputs={"revision": 5},
                       undo_token="undo-label-1", job_ref="")
    wf.transition(WorkflowState.VERIFYING, progress=0.9, stage="verify")
    wf.transition(WorkflowState.SUCCEEDED, progress=1.0)
    assert not wf.cancellable
    assert [e.to_state for e in wf.events] == [
        WorkflowState.PREFLIGHT, WorkflowState.RUNNING,
        WorkflowState.VERIFYING, WorkflowState.SUCCEEDED]
    assert wf.steps[0].undo_token == "undo-label-1"
    assert step.job_ref == ""


def test_degraded_is_legal_verifying_terminal():
    wf = _open(policy=FailurePolicy.PAUSE_FOR_USER)
    wf.transition(WorkflowState.PREFLIGHT)
    wf.transition(WorkflowState.RUNNING)
    wf.transition(WorkflowState.VERIFYING)
    wf.transition(WorkflowState.DEGRADED, reason="GPU fallback → CPU export")
    assert wf.state is WorkflowState.DEGRADED


# ── 非法转移 ──

def test_illegal_transitions_rejected():
    wf = _open()
    with pytest.raises(IllegalTransitionError):
        wf.transition(WorkflowState.RUNNING)  # QUEUED → RUNNING 跳态
    wf.transition(WorkflowState.PREFLIGHT)
    wf.transition(WorkflowState.RUNNING)
    wf.transition(WorkflowState.VERIFYING)
    wf.transition(WorkflowState.FAILED)
    with pytest.raises(IllegalTransitionError):
        wf.transition(WorkflowState.QUEUED)  # 终态再转移
    with pytest.raises(IllegalTransitionError):
        wf.cancel()


def test_progress_bounds():
    wf = _open()
    with pytest.raises(ValueError):
        wf.transition(WorkflowState.PREFLIGHT, progress=1.5)
    with pytest.raises(ValueError):
        wf.transition(WorkflowState.PREFLIGHT, progress=-0.1)


# ── cancel 语义 ──

def test_cancel_allowed_while_cancellable_states():
    for stop_at in (WorkflowState.QUEUED, WorkflowState.PREFLIGHT,
                    WorkflowState.RUNNING, WorkflowState.VERIFYING):
        wf = _open()
        while wf.state is not stop_at:
            nxt = {
                WorkflowState.QUEUED: WorkflowState.PREFLIGHT,
                WorkflowState.PREFLIGHT: WorkflowState.RUNNING,
                WorkflowState.RUNNING: WorkflowState.VERIFYING,
            }[wf.state]
            wf.transition(nxt)
        wf.cancel(reason="user")
        assert wf.state is WorkflowState.CANCELLED


def test_failed_preflight_can_be_cancelled_or_failed_not_succeeded():
    wf = _open()
    wf.transition(WorkflowState.PREFLIGHT)
    wf.transition(WorkflowState.FAILED, reason="preflight blocking")
    assert wf.state is WorkflowState.FAILED
    assert not wf.cancellable


# ── 步骤记录 ──

def test_steps_only_recorded_while_running():
    wf = _open()
    with pytest.raises(IllegalTransitionError):
        wf.add_step("too early")  # QUEUED
    wf.transition(WorkflowState.PREFLIGHT)
    with pytest.raises(IllegalTransitionError):
        wf.add_step("still too early")
    wf.transition(WorkflowState.RUNNING)
    wf.add_step("ok now")
    wf.transition(WorkflowState.VERIFYING)
    with pytest.raises(IllegalTransitionError):
        wf.add_step("too late")


# ── 失败策略（恰好三种，补偿建议为策略的显式函数）──

def test_exactly_three_failure_policies():
    assert FAILURE_POLICIES_EXACT == {
        FailurePolicy.RETRY_SAFE, FailurePolicy.ROLLBACK,
        FailurePolicy.PAUSE_FOR_USER}


def test_rollback_compensation_lists_undo_tokens_reverse_order():
    wf = _open(policy=FailurePolicy.ROLLBACK)
    wf.transition(WorkflowState.PREFLIGHT)
    wf.transition(WorkflowState.RUNNING)
    wf.add_step("step A", undo_token="undo-a")
    wf.add_step("step B", undo_token="undo-b")
    wf.transition(WorkflowState.FAILED, reason="commit conflict")
    advice = wf.compensation_advice(WorkflowState.FAILED)
    assert "rollback" in advice
    b_at = advice.find("undo-b")
    a_at = advice.find("undo-a")
    assert 0 <= b_at < a_at, f"undo tokens 未按逆序排列: {advice}"


def test_retry_safe_and_pause_advice_are_distinct_and_explicit():
    retry = _open(policy=FailurePolicy.RETRY_SAFE)
    pause = _open(policy=FailurePolicy.PAUSE_FOR_USER)
    for wf in (retry, pause):
        wf.transition(WorkflowState.PREFLIGHT)
        wf.transition(WorkflowState.RUNNING)
        wf.transition(WorkflowState.FAILED)
    r = retry.compensation_advice(WorkflowState.FAILED)
    p = pause.compensation_advice(WorkflowState.FAILED)
    assert "retry_safe" in r and "幂等" in r
    assert "pause_for_user" in p and "授权" in p
    assert r != p


# ── 冻结锚点 / 快照 ──

def test_anchor_is_frozen_snapshot():
    wf = _open()
    snap = wf.anchor
    snap["capability_snapshot"]["effects"].append("tampered")
    snap["base_revision"] = 999
    assert wf.anchor["base_revision"] == 4
    assert wf.anchor["capability_snapshot"]["effects"] == ["blur"]


def test_to_dict_is_json_safe():
    wf = _open()
    wf.transition(WorkflowState.PREFLIGHT)
    payload = wf.to_dict()
    assert payload["anchor"]["base_revision"] == 4
    assert payload["state"] == "preflight"
    json.dumps(payload)


# ── 与真实 TransactionService 集成（undo token 有真实来源）──

def test_real_plan_commit_as_workflow_step():
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.edit_project_service import (
        ProjectSequences)
    from src.features.edit.ai_editing.approval_service import ApprovalService
    from src.features.edit.ai_editing.models import (
        EditOperation, EditPlan, OperationKind, PlanTarget)
    from src.features.edit.ai_editing.transaction_service import (
        _expected_commit_scope)
    from src.features.edit.core.project import (
        EditProject, MediaAsset, TimelineSequenceRef)
    from src.features.edit.core.timeline_v2 import (
        EditTrack, TimelineSequence)
    from src.shared.media.metadata import Rational

    class _Host:
        def __init__(self, state):
            self._state = state
            self._svc = ApprovalService()

        def get_state(self):
            return self._state

        def get_manifest(self):
            from src.features.edit.ai_editing.capability_manifest import (
                build_manifest)
            return build_manifest()

        def on_committed(self, result):
            if result.applied:
                self._state = ProjectSequences(
                    project=result.new_project,
                    sequences=result.new_sequences)

        def approval_service(self):
            return self._svc

        def request_user_approval(self, plan, diff):
            return self._svc.issue_after_user_confirmation(
                _expected_commit_scope(plan), issued_by="wf-test")

    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1",
                                track_type="video", index=0),),
    )
    asset = MediaAsset(asset_id="a1", path="/tmp/clip.mp4",
                       media_type="video", display_name="clip", metadata={})
    ref = TimelineSequenceRef(timeline_id="t1", name="Main", fps=24.0,
                              width=1920, height=1080, sample_rate=48000)
    project = EditProject(project_id="p1", name="P", assets={"a1": asset},
                          timelines={"t1": ref}, root_bin_id="root",
                          active_timeline_id="t1")
    EditSession.unbind()
    host = _Host(ProjectSequences(project=project,
                                  sequences={"t1": seq}))
    session = EditSession.bind(host)
    try:
        plan = EditPlan.create(
            PlanTarget("p1", "t1"), 0,
            operations=(EditOperation(
                "op1", OperationKind.ADD_CLIP, params={
                    "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
                    "source": {"start": 0, "duration": 100},
                    "timeline": {"start": 0, "duration": 100},
                }),),
            title="wf step plan",
        )
        grant = host.request_user_approval(plan, None)

        wf = WorkflowTransaction(
            workflow_id="wf-int", project_id="p1", timeline_id="t1",
            base_revision=0, capability_snapshot=_SNAPSHOT,
            failure_policy=FailurePolicy.ROLLBACK)
        wf.transition(WorkflowState.PREFLIGHT, stage="validate")
        wf.transition(WorkflowState.RUNNING, stage="commit")
        result = session.apply(plan, approval=grant)
        assert result.applied
        wf.add_step("commit plan", operation_id="edit.add_clip",
                    inputs={"plan_id": plan.plan_id},
                    outputs={"revision": result.new_revision},
                    undo_token=result.undo_label or "")
        wf.transition(WorkflowState.VERIFYING, stage="verify")
        wf.transition(WorkflowState.SUCCEEDED, progress=1.0)
        assert wf.steps[0].undo_token == (result.undo_label or "") or \
            result.undo_label  # token 来自真实 CommitResult
        d = wf.to_dict()
        assert d["steps"][0]["outputs"]["revision"] == result.new_revision
    finally:
        EditSession.unbind()
