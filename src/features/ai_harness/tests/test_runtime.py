"""HarnessRuntime 单元测试（plan §10.2 P0.2）。

覆盖：

- start 分配 run_id 并发布 run.created；get_run 返回 DRAFT。
- mark_running 推进到 RUNNING；finish 成功 / 失败 / 取消终态。
- cancel 设置 cancel token；is_cancelled 反映；幂等。
- retry 分配新 run_id，旧 run 不变；detach session 后拒绝。
- resume 仅从 WAITING_FOR_USER。
- 订阅者收到事件；抛错被吞、不影响 run。
- detach_session 后活跃 run 被冻结；后续 set_status / finish / cancel 被拒。
- list_runs 按 created_at 升序，可按 session 过滤。

红线对照（plan §10.2 P0.6 / §1.4）：

- 取消是真实 cancel token 传播（is_cancelled 可被下游轮询）。
- 工程关闭后 run 不再回写（detach_session）。
- 状态迁移受控（不能从 SUCCEEDED 回 RUNNING）。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    ArtifactRef,
    Invocation,
    InvocationScope,
    JobRef,
    PermissionLevel,
    RunPolicy,
    RunStatus,
)
from src.features.ai_harness.runtime import HarnessRuntime


def _make_invocation(*, session_id: str = "sess1", source: str = "chat",
                     skill_id: str = "") -> Invocation:
    return Invocation(
        source=source,
        skill_id=skill_id,
        scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
        policy=RunPolicy(permission=PermissionLevel.PROPOSE),
        parent_session_id=session_id,
        user_text="生成字幕",
    )


class TestStartAndGet:
    def test_start_assigns_run_id_and_emits_created(self) -> None:
        rt = HarnessRuntime()
        events = []
        rt.add_subscriber(lambda e: events.append((e.run_id, e.kind)))
        rid = rt.start(_make_invocation())
        assert rid
        rec = rt.get_run(rid)
        assert rec is not None
        assert rec.status == RunStatus.DRAFT
        assert rec.run_id == rid
        assert events == [(rid, "run.created")]

    def test_get_run_unknown_returns_none(self) -> None:
        rt = HarnessRuntime()
        assert rt.get_run("nope") is None

    def test_get_invocation_returns_original(self) -> None:
        rt = HarnessRuntime()
        inv = _make_invocation()
        rid = rt.start(inv)
        assert rt.get_invocation(rid) is inv


class TestStatusProgression:
    def test_mark_running_advances_to_running(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        assert rt.mark_running(rid) is True
        assert rt.get_run(rid).status == RunStatus.RUNNING
        assert rt.get_run(rid).started_at is not None

    def test_finish_success(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        assert rt.finish(rid) is True
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.SUCCEEDED
        assert rec.progress == 1.0
        assert rec.finished_at is not None

    def test_finish_with_warnings(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        rt.finish(rid, with_warnings=True)
        assert rt.get_run(rid).status == RunStatus.SUCCEEDED_WITH_WARNINGS

    def test_finish_failure_records_error(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        rt.finish(rid, success=False, error_code="MODEL_MISSING",
                  error_message="Qwen3-ASR not installed")
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "MODEL_MISSING"
        assert "Qwen3-ASR" in rec.error_message

    def test_set_status_rejects_illegal_transition(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        rt.finish(rid)
        # SUCCEEDED -> RUNNING 非法
        assert rt.set_status(rid, RunStatus.RUNNING) is False
        assert rt.get_run(rid).status == RunStatus.SUCCEEDED

    def test_mark_waiting_for_user_then_resume(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        assert rt.mark_waiting_for_user(rid, message="确认字幕轨道") is True
        assert rt.get_run(rid).status == RunStatus.WAITING_FOR_USER
        assert rt.resume(rid) is True
        assert rt.get_run(rid).status == RunStatus.RUNNING

    def test_resume_rejected_when_not_waiting(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())  # DRAFT
        assert rt.resume(rid) is False


class TestCancel:
    def test_cancel_sets_token(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        assert rt.is_cancelled(rid) is False
        assert rt.cancel(rid) is True
        assert rt.is_cancelled(rid) is True

    def test_cancel_is_idempotent(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        assert rt.cancel(rid) is True
        assert rt.cancel(rid) is True  # 第二次幂等

    def test_cancel_then_finish_goes_cancelled(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        rt.cancel(rid)
        rt.finish(rid, success=False)
        assert rt.get_run(rid).status == RunStatus.CANCELLED
        assert rt.get_run(rid).cancel_reason == "user_requested"

    def test_cancel_emits_event(self) -> None:
        rt = HarnessRuntime()
        kinds = []
        rt.add_subscriber(lambda e: kinds.append(e.kind))
        rid = rt.start(_make_invocation())
        kinds.clear()
        rt.cancel(rid, reason="user_aborted")
        assert "run.cancel_requested" in kinds
        # 事件 payload 含 reason
        rec = rt.get_run(rid)
        cancel_events = [e for e in rec.events if e.kind == "run.cancel_requested"]
        assert cancel_events and cancel_events[-1].payload["reason"] == "user_aborted"

    def test_cancel_unknown_run_returns_false(self) -> None:
        rt = HarnessRuntime()
        assert rt.cancel("nope") is False


class TestManualHandoff:
    def test_handoff_sets_cancel_token_and_audit_event(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        assert rt.handoff(rid) is True
        assert rt.is_cancelled(rid) is True
        rec = rt.get_run(rid)
        assert rec.cancel_reason == "manual_handoff"
        assert any(e.kind == "run.handoff_requested" for e in rec.events)

    def test_handoff_rejects_terminal_run(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        rt.finish(rid)
        assert rt.handoff(rid) is False


class TestRetry:
    def test_retry_creates_new_run_id(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.mark_running(rid)
        rt.finish(rid, success=False, error_code="TRANSIENT")
        new = rt.retry(rid)
        assert new is not None
        assert new != rid
        assert rt.get_run(new).status == RunStatus.DRAFT
        # 旧 run 状态不变
        assert rt.get_run(rid).status == RunStatus.FAILED

    def test_retry_emits_retry_event_on_new_run(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        new = rt.retry(rid)
        rec = rt.get_run(new)
        kinds = [e.kind for e in rec.events]
        assert "run.created" in kinds
        assert "run.retry_requested" in kinds

    def test_retry_unknown_returns_none(self) -> None:
        rt = HarnessRuntime()
        assert rt.retry("nope") is None


class TestSubscribers:
    def test_subscriber_receives_events(self) -> None:
        rt = HarnessRuntime()
        received = []
        unsub = rt.add_subscriber(lambda e: received.append(e.kind))
        rid = rt.start(_make_invocation())
        assert "run.created" in received
        unsub()
        received.clear()
        rt.mark_running(rid)
        assert received == []  # 取消订阅后不再收到

    def test_subscriber_exception_does_not_break_runtime(self) -> None:
        rt = HarnessRuntime()
        def bad(_):
            raise RuntimeError("boom")
        rt.add_subscriber(bad)
        good = []
        rt.add_subscriber(lambda e: good.append(e.kind))
        rid = rt.start(_make_invocation())  # bad 抛错应被吞
        assert "run.created" in good
        assert rt.get_run(rid) is not None


class TestDetachSession:
    def test_detach_freezes_active_runs(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation(session_id="sessA"))
        rt.mark_running(rid)
        n = rt.detach_session("sessA")
        assert n == 1
        # 关闭后 finish / set_status / cancel 都不应回写
        assert rt.finish(rid) is False
        assert rt.get_run(rid).status == RunStatus.RUNNING  # 未变
        assert rt.set_status(rid, RunStatus.SUCCEEDED) is False

    def test_cancel_after_detach_returns_false(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation(session_id="sessA"))
        rt.detach_session("sessA")
        assert rt.cancel(rid) is False

    def test_retry_after_detach_returns_none(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation(session_id="sessA"))
        rt.detach_session("sessA")
        assert rt.retry(rid) is None

    def test_already_finished_run_not_counted_as_active(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation(session_id="sessA"))
        rt.mark_running(rid)
        rt.finish(rid)
        # 已终态的 run detach 时不算活跃
        assert rt.detach_session("sessA") == 0

    def test_new_run_on_detached_session_is_marked_detached(self) -> None:
        rt = HarnessRuntime()
        rt.detach_session("sessA")
        rid = rt.start(_make_invocation(session_id="sessA"))
        # detach session 下的新 run 一切写操作都被冻结（mark_running 也无法推进）
        assert rt.mark_running(rid) is False
        assert rt.finish(rid) is False
        assert rt.get_run(rid).status == RunStatus.DRAFT  # 完全未动

    def test_other_sessions_unaffected(self) -> None:
        rt = HarnessRuntime()
        ridA = rt.start(_make_invocation(session_id="sessA"))
        ridB = rt.start(_make_invocation(session_id="sessB"))
        rt.mark_running(ridA)
        rt.mark_running(ridB)
        rt.detach_session("sessA")
        assert rt.finish(ridB) is True
        assert rt.get_run(ridB).status == RunStatus.SUCCEEDED


class TestJobsArtifactsCost:
    def test_attach_and_update_job(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        j = JobRef(job_id="j1", kind="analysis", label="ASR", status="queued", progress=0.0)
        assert rt.attach_job(rid, j) is True
        rec = rt.get_run(rid)
        assert len(rec.jobs) == 1 and rec.jobs[0].job_id == "j1"
        rt.update_job_progress(rid, "j1", "running", 0.5)
        rec = rt.get_run(rid)
        assert rec.jobs[0].status == "running" and rec.jobs[0].progress == 0.5

    def test_attach_job_dedupes(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.attach_job(rid, JobRef(job_id="j1", kind="analysis", status="queued"))
        rt.attach_job(rid, JobRef(job_id="j1", kind="analysis", status="running", progress=0.3))
        rec = rt.get_run(rid)
        assert len(rec.jobs) == 1
        assert rec.jobs[0].status == "running"

    def test_record_artifact(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        a = ArtifactRef(artifact_id="art1", artifact_type="transcript_ref",
                        producer_node_id="asr_node")
        assert rt.record_artifact(rid, a) is True
        assert rt.get_run(rid).artifacts[0].artifact_id == "art1"

    def test_record_cost_accumulates(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.record_cost(rid, 0.1)
        rt.record_cost(rid, 0.2)
        assert abs(rt.get_run(rid).cost - 0.3) < 1e-9

    def test_progress_clamped(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.record_progress(rid, 0.5)
        rt.record_progress(rid, -1.0)  # clamp
        rt.record_progress(rid, 2.0)
        assert rt.get_run(rid).progress == 1.0

    def test_record_diagnostic_redacted(self) -> None:
        rt = HarnessRuntime()
        rid = rt.start(_make_invocation())
        rt.record_diagnostic(rid, {"path": "/Users/testuser/secret", "code": "X"})
        diag = rt.get_run(rid).diagnostics[0]
        assert "/Users/testuser" not in str(diag)
        assert diag["code"] == "X"


class TestListRuns:
    def test_list_runs_sorted_by_created(self) -> None:
        rt = HarnessRuntime()
        r1 = rt.start(_make_invocation(session_id="s1"))
        r2 = rt.start(_make_invocation(session_id="s2"))
        runs = rt.list_runs()
        assert [r.run_id for r in runs] == [r1, r2]

    def test_list_runs_filter_by_session(self) -> None:
        rt = HarnessRuntime()
        ra = rt.start(_make_invocation(session_id="sA"))
        rb = rt.start(_make_invocation(session_id="sB"))
        a_runs = rt.list_runs(session_id="sA")
        assert [r.run_id for r in a_runs] == [ra]
        b_runs = rt.list_runs(session_id="sB")
        assert [r.run_id for r in b_runs] == [rb]


class TestCrossProcessSnapshot:
    def test_snapshot_roundtrip_preserves_terminal_and_interrupts_active(self) -> None:
        source = HarnessRuntime()
        active_invocation = replace(
            _make_invocation(),
            user_text="/Users/testuser/private/clip.mp4 token=abcdefghijklmnop",
            inputs={"path": "/Users/testuser/private/clip.mp4"},
        )
        active_id = source.start(active_invocation)
        source.mark_running(active_id)
        source.attach_job(active_id, JobRef(
            job_id="job-1", kind="analysis", label="ASR", status="running", progress=0.4,
        ))
        source.record_artifact(active_id, ArtifactRef(
            artifact_id="artifact-1", artifact_type="transcript_ref", producer_node_id="asr",
        ))
        terminal_id = source.start(_make_invocation(session_id="sess-terminal"))
        source.mark_running(terminal_id)
        source.finish(terminal_id)

        snapshot = source.export_snapshot()
        serialized = json.dumps(snapshot, ensure_ascii=False)
        assert "/Users/testuser" not in serialized
        assert "abcdefghijklmnop" not in serialized

        restored = HarnessRuntime()
        report = restored.restore_snapshot(json.loads(serialized))

        assert report.ok
        assert report.restored_run_ids == (active_id, terminal_id)
        assert report.interrupted_run_ids == (active_id,)
        active = restored.get_run(active_id)
        assert active is not None
        assert active.status == RunStatus.FAILED
        assert active.error_code == "RUN_INTERRUPTED"
        assert active.cancel_reason == "application_restart"
        assert any(item.get("code") == "RUN_INTERRUPTED" for item in active.diagnostics)
        assert restored.get_run(terminal_id).status == RunStatus.SUCCEEDED

        retry_id = restored.retry(active_id)
        assert retry_id is not None
        assert retry_id != active_id
        assert restored.get_run(retry_id).status == RunStatus.DRAFT

    def test_restore_skips_malformed_and_duplicate_rows(self) -> None:
        source = HarnessRuntime()
        run_id = source.start(_make_invocation())
        snapshot = source.export_snapshot()
        snapshot["runs"].extend([None, dict(snapshot["runs"][0])])

        restored = HarnessRuntime()
        report = restored.restore_snapshot(snapshot)

        assert run_id in report.restored_run_ids
        assert any("not_object" in item for item in report.skipped_entries)
        assert any("duplicate" in item for item in report.skipped_entries)
        assert report.ok is False

    def test_restore_rejects_unknown_schema_without_mutating_runtime(self) -> None:
        restored = HarnessRuntime()

        report = restored.restore_snapshot({"schema_version": 99, "runs": []})

        assert report.ok is False
        assert report.skipped_entries == ("snapshot_schema_unsupported",)
        assert restored.list_runs() == []
