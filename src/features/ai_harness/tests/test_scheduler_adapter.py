"""scheduler_adapter 单元测试（plan §10.2 P0.6 取消贯通）。

覆盖：

- ``job_ref_from_handle``：从 ``JobHandle`` 构造只读 JobRef。
- ``sync_job_to_run``：首次 attach / 后续 update。
- ``poll_cancel_to_job``：run cancel token → ``JobHandle.request_cancel``。
- ``JobRunBridge``：bind / sync / propagate_cancel。

红线对照（plan §10.2 P0.6）：

- 取消是真实 cancel token 传播，不是仅变更 UI 状态文案。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.scheduler_adapter import (
    JobRunBridge,
    job_ref_from_handle,
    poll_cancel_to_job,
    sync_job_to_run,
)


# 复用现有 JobHandle（纯数据合约）
from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobRegistry, JobStatus


def _start_run(rt: HarnessRuntime) -> str:
    return rt.start(Invocation(
        source="chat",
        scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
        parent_session_id="sess1",
    ))


class TestJobRefFromHandle:
    def test_converts_active_job(self):
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING,
                      progress=0.4, label="ASR")
        ref = job_ref_from_handle(h)
        assert ref.job_id == h.job_id
        assert ref.kind == "analysis"
        assert ref.status == "running"
        assert ref.progress == 0.4
        assert ref.label == "ASR"

    def test_converts_terminal_job(self):
        h = JobHandle(kind=JobKind.GENERATION, status=JobStatus.SUCCEEDED, progress=1.0)
        ref = job_ref_from_handle(h)
        assert ref.status == "succeeded"


class TestSyncJobToRun:
    def test_first_sync_attaches(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.QUEUED)
        assert sync_job_to_run(rt, rid, h) is True
        rec = rt.get_run(rid)
        assert len(rec.jobs) == 1
        assert rec.jobs[0].job_id == h.job_id

    def test_second_sync_updates_progress(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.QUEUED)
        sync_job_to_run(rt, rid, h)
        # job 推进
        h.mark_running()
        h.update_progress(0.5)
        assert sync_job_to_run(rt, rid, h) is True
        rec = rt.get_run(rid)
        assert len(rec.jobs) == 1  # 不重复
        assert rec.jobs[0].status == "running"
        assert rec.jobs[0].progress == 0.5

    def test_sync_unknown_run_returns_false(self):
        rt = HarnessRuntime()
        h = JobHandle(kind=JobKind.ANALYSIS)
        assert sync_job_to_run(rt, "nope", h) is False


class TestPollCancelToJob:
    def test_cancel_propagates_to_active_job(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING)
        rt.cancel(rid)
        assert poll_cancel_to_job(rt, rid, h) is True
        assert h.status == JobStatus.CANCELLING  # 真实标记

    def test_no_cancel_no_action(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING)
        assert poll_cancel_to_job(rt, rid, h) is False
        assert h.status == JobStatus.RUNNING  # 未变

    def test_terminal_job_not_touched(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        rt.cancel(rid)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.SUCCEEDED)
        assert poll_cancel_to_job(rt, rid, h) is False
        assert h.status == JobStatus.SUCCEEDED

    def test_already_cancelling_idempotent(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        rt.cancel(rid)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.CANCELLING)
        assert poll_cancel_to_job(rt, rid, h) is False


class TestJobRunBridge:
    def test_bind_and_sync_via_registry(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        registry = JobRegistry()
        bridge = JobRunBridge(rt)
        bridge.bind(rid, registry)
        handle = registry.submit(JobKind.ANALYSIS, label="ASR")
        # 提交后 job 在 registry 里；bridge.sync 据此同步进 run
        assert bridge.sync(rid, handle.job_id) is True
        rec = rt.get_run(rid)
        assert len(rec.jobs) == 1

    def test_propagate_cancel_marks_all_jobs(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        registry = JobRegistry()
        bridge = JobRunBridge(rt)
        bridge.bind(rid, registry)
        h1 = registry.submit(JobKind.ANALYSIS)
        h2 = registry.submit(JobKind.GENERATION)
        h1.mark_running()
        h2.mark_running()
        bridge.sync(rid, h1.job_id)
        bridge.sync(rid, h2.job_id)
        rt.cancel(rid)
        n = bridge.propagate_cancel(rid)
        assert n == 2
        assert registry.get(h1.job_id).status == JobStatus.CANCELLING
        assert registry.get(h2.job_id).status == JobStatus.CANCELLING

    def test_sync_without_bind_returns_false(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        bridge = JobRunBridge(rt)
        assert bridge.sync(rid, "j1") is False


class TestDetachSessionFreezesJobs:
    """plan §1.4 红线：工程关闭后 run 不再回写。"""

    def test_sync_returns_false_after_detach(self):
        rt = HarnessRuntime()
        rid = _start_run(rt)
        h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING)
        sync_job_to_run(rt, rid, h)
        rt.detach_session("sess1")
        # detach 后再 sync 应被拒
        h.update_progress(0.9)
        assert sync_job_to_run(rt, rid, h) is False
        # run 状态未推进
        rec = rt.get_run(rid)
        assert rec.jobs[0].progress == 0.0
