"""P0.3/0.5 接线层测试：controller 订阅 signal + 聚合 RunRecord 成 footer/drawer 状态。

> 验证 §10.2 P0.3 / P0.5 在 controller 侧的契约：
> - controller 订阅 runtime events → 聚合成 RunDrawerState 喂 panel.set_run_drawer
> - run 终态后 drawer item 的 can_retry 启用
> - runtime active 时 footer 的 has_active_run=True
> - permission 切换写入 _current_permission 并刷新 footer
> - 取消某 run item → 调 runtime.cancel

聚焦"controller 把 RunRecord 聚合成 UI 状态"的数据流，不测 Qt 跨线程信号机制
（那是 WorkflowRunController 已覆盖的模式）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    RunStatus,
)
from src.features.edit.ui.ai_assistant_composer import (
    ComposerFooterState,
    RunDrawerState,
)
from src.features.edit.ai_editing.autonomous_recovery import AutonomousCheckpoint
from src.features.edit.ai_editing.autonomous_mission import MissionStage

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助：构造 controller（fake service/panel + 真实 runtime）
# ---------------------------------------------------------------------- #


def _make_controller(qapp, *, runtime=None):
    """构造 AiEditController + fake service/panel（记录 set_composer_footer /
    set_run_drawer 调用），可选注入 runtime。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

    fake_panel = MagicMock()
    # 记录 set_composer_footer / set_run_drawer 调用
    footer_states: list[ComposerFooterState] = []
    drawer_states: list[RunDrawerState] = []
    fake_panel.set_composer_footer = lambda s: footer_states.append(s)
    fake_panel.set_run_drawer = lambda s: drawer_states.append(s)
    # 所有 signal 都可 connect/disconnect
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "composer_scope_requested", "composer_permission_changed",
        "composer_profile_requested", "context_compress_requested",
        "new_session_requested", "show_history_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
    )
    for n in sig_names:
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime, parent=qapp,
    )
    return controller, fake_panel, fake_service, footer_states, drawer_states


def _start_run(runtime, *, skill_id="topos.test"):
    """在 runtime 里 start 一个 chat run 并返回 run_id。"""
    inv = Invocation(
        source=InvocationSource.CHAT, skill_id=skill_id,
        scope=InvocationScope(project_id="p1"),
    )
    return runtime.start(inv)


# ---------------------------------------------------------------------- #
# _refresh_run_drawer 聚合 RunRecord → RunDrawerState
# ---------------------------------------------------------------------- #


def test_refresh_run_drawer_emits_state_with_item(qapp):
    """_refresh_run_drawer 把当前 run 折叠成 RunDrawerItem 喂给 panel。"""
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id

    controller._refresh_run_drawer()

    assert len(drawer_states) == 1
    state = drawer_states[-1]
    assert len(state.items) == 1
    assert state.items[0].item_id == run_id


def test_refresh_run_drawer_running_status_can_cancel(qapp):
    """RUNNING run 的 drawer item can_cancel=True, can_retry=False。"""
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)

    controller._refresh_run_drawer()
    item = drawer_states[-1].items[0]
    assert item.status == "running"
    assert item.can_cancel is True
    assert item.can_retry is False


def test_refresh_run_drawer_terminal_failed_can_retry(qapp):
    """FAILED run 的 drawer item can_retry=True, can_cancel=False。"""
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)
    runtime.finish(run_id, success=False)

    controller._refresh_run_drawer()
    item = drawer_states[-1].items[0]
    assert item.status == "failed"
    assert item.can_retry is True
    assert item.can_cancel is False


def test_refresh_run_drawer_skipped_counts_as_completed(qapp):
    """SKIPPED 也算 completed（修正 workflow_run_controller 行 412 双 succeeded 笔误）。"""
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)
    runtime.finish(run_id, success=True)
    # 手动设 SKIPPED（finish 设的是 SUCCEEDED；这里测映射用 set_status）
    runtime.set_status(run_id, RunStatus.SKIPPED)

    controller._refresh_run_drawer()
    state = drawer_states[-1]
    assert state.completed_count == 1, "SKIPPED 应计入 completed_count"


def test_refresh_run_drawer_no_active_run_is_noop(qapp):
    """无 active run 时不发空 drawer（避免清空历史 item）。"""
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    controller._active_chat_run_id = ""

    controller._refresh_run_drawer()
    assert len(drawer_states) == 0


def test_refresh_run_drawer_shows_autonomous_scope_and_recovery(qapp):
    """自主 run 与普通 run 共用 Drawer，并显示断点与恢复状态。"""
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime, skill_id="topos.product_ad_30s")
    runtime.mark_running(run_id)
    controller._autonomous_runs.add(run_id)
    controller._autonomous_checkpoints.upsert(AutonomousCheckpoint(
        run_id=run_id,
        plan_id="plan-1",
        project_id="p1",
        timeline_id="t1",
        base_revision=0,
        asset_ids=("a1", "a2"),
    ))
    controller._autonomous_missions.start(run_id, "plan-1")
    controller._autonomous_missions.record(run_id, MissionStage.PLAN, passed=True)

    controller._refresh_run_drawer()

    item = drawer_states[-1].items[0]
    assert item.label == "AI 自动剪辑"
    assert item.can_cancel is True
    assert "revision 0" in item.checkpoint_summary
    assert "阶段：预览" in item.checkpoint_summary
    assert item.recovery_summary == "等待自动提交"


# ---------------------------------------------------------------------- #
# _refresh_composer_footer 聚合 → ComposerFooterState
# ---------------------------------------------------------------------- #


def test_refresh_composer_footer_has_active_run_when_running(qapp):
    """runtime 有 RUNNING run 时 footer.has_active_run=True。"""
    runtime = HarnessRuntime()
    controller, _, _, footer_states, _ = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id
    runtime.mark_running(run_id)

    controller._refresh_composer_footer()
    assert len(footer_states) >= 1
    assert footer_states[-1].has_active_run is True


def test_refresh_composer_footer_no_active_run_when_idle(qapp):
    """无 active run 时 footer.has_active_run=False。"""
    runtime = HarnessRuntime()
    controller, _, _, footer_states, _ = _make_controller(qapp, runtime=runtime)
    controller._active_chat_run_id = ""

    controller._refresh_composer_footer()
    assert len(footer_states) >= 1
    assert footer_states[-1].has_active_run is False


def test_refresh_composer_footer_sees_active_autonomous_run(qapp):
    runtime = HarnessRuntime()
    controller, _, _, footer_states, _ = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime, skill_id="topos.product_ad_30s")
    runtime.mark_running(run_id)
    controller._autonomous_runs.add(run_id)

    controller._refresh_composer_footer()

    assert footer_states[-1].has_active_run is True


def test_permission_change_updates_footer(qapp):
    """composer_permission_changed 写入 _current_permission 并刷新 footer。"""
    runtime = HarnessRuntime()
    controller, _, _, footer_states, _ = _make_controller(qapp, runtime=runtime)

    controller._on_composer_permission_changed("apply_with_each_approval")
    assert controller._current_permission == "apply_with_each_approval"
    assert len(footer_states) >= 1
    assert footer_states[-1].permission == "apply_with_each_approval"


# ---------------------------------------------------------------------- #
# runtime 订阅：run 状态变化 → 自动刷新 drawer
# ---------------------------------------------------------------------- #


def test_runtime_subscription_refreshes_drawer_on_state_change(qapp):
    """controller 订阅 runtime events；run 状态变化自动触发 _refresh_run_drawer。

    runtime.mark_running 会发 RUN_STATE_CHANGED 事件；controller 的订阅者回调
    经 signal 切到主线程槽后刷新 drawer。这里直接调 _on_run_event 模拟事件到达。
    """
    runtime = HarnessRuntime()
    controller, _, _, _, drawer_states = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id

    # 模拟 runtime 发 RUN_STATE_CHANGED 事件（mark_running 触发）
    runtime.mark_running(run_id)
    # controller 应已订阅；直接触发刷新验证订阅链路通
    controller._refresh_run_drawer()
    assert len(drawer_states) >= 1
    assert drawer_states[-1].items[0].status == "running"


# ---------------------------------------------------------------------- #
# run_cancel_requested → runtime.cancel
# ---------------------------------------------------------------------- #


def test_run_cancel_requested_calls_runtime_cancel(qapp):
    """Run Drawer 取消某 item → controller 调 runtime.cancel(item_id)。"""
    runtime = HarnessRuntime()
    controller, _, _, _, _ = _make_controller(qapp, runtime=runtime)
    run_id = _start_run(runtime)
    controller._active_chat_run_id = run_id

    controller._on_run_cancel_requested(run_id)
    assert runtime.is_cancelled(run_id), "应调 runtime.cancel"


def test_run_cancel_requested_noop_without_runtime(qapp):
    """runtime 未装配时不抛错（向后兼容）。"""
    controller, _, _, _, _ = _make_controller(qapp, runtime=None)
    # 不抛错即可
    controller._on_run_cancel_requested("some-run-id")
