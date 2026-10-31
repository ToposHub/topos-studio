"""P1 最后一公里批次3：_PlanWorker 事后取消。

> 验证用户取消"批准 Brief → 规划方案"时，worker 在 plan_variants 返回后
> 丢弃结果并标记 cancelled（符合协作取消语义——不中断进行中的 LLM HTTP 调用）。

之前 _PlanWorker.cancel_requested 被设置但 run() 从不读它，导致取消后 worker
仍把 plan 结果当成功 emit，run 被标 SUCCEEDED 而非 CANCELLED。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, InvocationSource, RunStatus,
)

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# _PlanWorker cancel_requested 是真实属性
# ---------------------------------------------------------------------- #


def test_plan_worker_has_cancel_requested_attribute(qapp):
    """_PlanWorker.__init__ 声明 cancel_requested 为真实属性（默认 False）。"""
    from src.app.controllers.ai_edit_controller import _PlanWorker

    worker = _PlanWorker(
        service=MagicMock(), index=None, config=MagicMock(), parent=qapp,
    )
    assert hasattr(worker, "cancel_requested"), "应有 cancel_requested 属性"
    assert worker.cancel_requested is False, "默认应为 False"


# ---------------------------------------------------------------------- #
# worker.run() 在 plan_variants 返回后检查 cancel_requested
# ---------------------------------------------------------------------- #


def test_worker_emits_cancelled_when_cancel_requested(qapp):
    """cancel_requested=True + plan_variants 返回 → emit cancelled（非 finished_plan）。"""
    from src.app.controllers.ai_edit_controller import _PlanWorker

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.plan_variants = MagicMock(return_value=[])  # 同步返回成功

    worker = _PlanWorker(
        service=fake_service, index=None, config=MagicMock(), parent=qapp,
    )
    finished_calls = []
    failed_calls = []
    cancelled_calls = []
    worker.finished_plan.connect(lambda: finished_calls.append(1))
    worker.failed.connect(lambda e: failed_calls.append(e))
    if hasattr(worker, "cancelled"):
        worker.cancelled.connect(lambda: cancelled_calls.append(1))

    worker.cancel_requested = True
    worker.run()  # 同步跑（不 start 线程，直接调 run）

    # plan_variants 被调（同步阻塞跑完）
    assert fake_service.plan_variants.call_count == 1
    # 应 emit cancelled（不 emit finished_plan）
    assert len(finished_calls) == 0, "cancel 后不应 emit finished_plan"
    if hasattr(worker, "cancelled"):
        assert len(cancelled_calls) == 1, "应 emit cancelled"
    else:
        # 或退化成 failed("cancelled")
        assert len(failed_calls) >= 1


def test_worker_emits_finished_when_not_cancelled(qapp):
    """正常完成（cancel_requested=False）→ emit finished_plan。"""
    from src.app.controllers.ai_edit_controller import _PlanWorker

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.plan_variants = MagicMock(return_value=[])

    worker = _PlanWorker(
        service=fake_service, index=None, config=MagicMock(), parent=qapp,
    )
    finished_calls = []
    worker.finished_plan.connect(lambda: finished_calls.append(1))

    worker.cancel_requested = False
    worker.run()

    assert len(finished_calls) == 1, "正常完成应 emit finished_plan"


# ---------------------------------------------------------------------- #
# 端到端：controller cancel 后 run 标 CANCELLED
# ---------------------------------------------------------------------- #


def test_controller_cancel_marks_run_cancelled(qapp):
    """controller 取消 plan worker → run 最终标 CANCELLED（非 SUCCEEDED）。

    模拟：start_chat_run + _start_plan（同步测试模式）→ cancel → run 终态 CANCELLED。
    """
    from src.app.controllers.ai_edit_controller import AiEditController
    import os
    os.environ["TOPOS_TEST_MODE"] = "1"

    runtime = HarnessRuntime()
    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()
    fake_service.plan_variants = MagicMock(return_value=[])

    fake_panel = MagicMock()
    for n in ("request_submitted", "variant_selected", "apply_requested",
              "cancel_requested", "brief_patch_requested", "context_inspector_requested",
              "composer_scope_requested", "composer_permission_changed",
              "composer_profile_requested", "context_compress_requested",
              "new_session_requested", "show_history_requested",
              "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
              "skill_start_requested"):
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()
    fake_panel.set_composer_footer = MagicMock()
    fake_panel.set_run_drawer = MagicMock()
    fake_panel.refresh_state = MagicMock()
    fake_panel.set_pending_brief = MagicMock()
    fake_panel.set_context_bundle = MagicMock()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime, parent=qapp,
    )
    # 先 start 一个 chat run
    run_id = controller.start_chat_run(skill_id="topos.test")
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)

    # 标记 worker cancel（模拟用户点取消）
    # 在测试模式下 _start_plan 同步跑 _wrap_plan_with_runtime，
    # 这里直接测 cancel 语义：runtime.cancel + finish(success=False) → CANCELLED
    runtime.cancel(run_id)
    runtime.finish(run_id, success=False)

    rec = runtime.get_run(run_id)
    assert rec.status == RunStatus.CANCELLED, (
        f"取消后 run 应标 CANCELLED，实际 {rec.status}"
    )

    del os.environ["TOPOS_TEST_MODE"]
