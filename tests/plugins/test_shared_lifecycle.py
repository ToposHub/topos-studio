"""PL-03 回归：调用上下文冻结、贡献回滚、停用顺序与诚实取消。"""
from __future__ import annotations

import threading
import time

import pytest

from src.shared.plugins.lifecycle import (
    CancelToken,
    ContributionSet,
    InvocationCancelled,
    InvocationContext,
    PluginJobs,
    PluginLifecycleController,
    check_command_applicability,
    make_invocation,
)
from src.shared.plugins.manifest import PluginCapability


# ---------------------------------------------------------------------------
# InvocationContext：选择在手势时刻冻结
# ---------------------------------------------------------------------------

def test_selection_frozen_at_gesture_and_revalidated():
    context = make_invocation("files.format_factory.launch", "files.format_factory",
                              entry_paths=("/a.mov", "/b.mov"))
    assert context.selection_valid(("/a.mov", "/b.mov", "/c.png")) is True
    assert context.selection_valid(("/a.mov",)) is False      # b 已消失：失败要响亮
    assert context.invocation_id                              # 宿主生成唯一 id


def test_params_are_frozen_and_cancel_token_cooperative():
    context = make_invocation("cmd", "files.x", params={"template": "{name}"})
    with pytest.raises(TypeError):
        context.params["template"] = "evil"                   # type: ignore[index]
    token = context.cancel_token
    assert token.cancelled is False
    token.request()
    with pytest.raises(InvocationCancelled):
        context.raise_if_cancelled()


def test_deadline_and_cancel_facts():
    context = make_invocation("cmd", "files.x", deadline=time.monotonic() - 1)
    assert context.timed_out() is True
    assert make_invocation("cmd", "files.x").timed_out() is False


# ---------------------------------------------------------------------------
# ContributionSet：激活半失败全回滚
# ---------------------------------------------------------------------------

def test_activation_failure_rolls_back_every_registered_handle():
    released: list[str] = []

    def registrar(contributions: ContributionSet) -> None:
        contributions.register("route", "/api/a", lambda: released.append("a"))
        contributions.register("menu", "工具", lambda: released.append("menu"))
        raise RuntimeError("setup() 第三步抛异常")            # 半失败

    controller = PluginLifecycleController()
    with pytest.raises(RuntimeError):
        controller.activate("files.broken", registrar)
    assert released == ["menu", "a"]                          # 全部释放（逆序）
    assert controller.accepting_calls("files.broken") is False


def test_successful_activation_commits_and_duplicate_registration_fails():
    controller = PluginLifecycleController()
    contributions = controller.activate("files.ok", lambda cs: cs.register("route", "r", lambda: None))
    assert contributions.committed
    with pytest.raises(RuntimeError):
        contributions.register("route", "r2", lambda: None)   # 已提交不能再注册
    with pytest.raises(RuntimeError):
        controller.activate("files.ok", lambda cs: None)      # 重复激活


def test_duplicate_contribution_key_rejected_before_commit():
    fresh = ContributionSet("files.dup")
    fresh.register("route", "r", lambda: None)
    with pytest.raises(ValueError):
        fresh.register("route", "r", lambda: None)            # 同 kind+key 重复


def test_reactivation_after_stop_is_refused_until_clean_reenable():
    controller = PluginLifecycleController()
    controller.activate("files.tool", lambda cs: None)
    controller.deactivate("files.tool")
    assert controller.is_stopped("files.tool") is True
    with pytest.raises(RuntimeError, match="已停用"):
        controller.activate("files.tool", lambda cs: None)    # 停用后不能直接复活


def test_deactivate_order_and_idempotency():
    events: list[str] = []
    controller = PluginLifecycleController(
        close_windows=lambda pid: events.append(f"close:{pid}"),
        unbind_events=lambda pid: events.append(f"unbind:{pid}"))

    def registrar(contributions: ContributionSet) -> None:
        contributions.register("route", "r", lambda: events.append("release"))

    controller.activate("files.tool", registrar)
    controller.jobs.start("job-1")
    controller.jobs.finish("job-1")                           # 已结束的任务不阻塞停用
    first = controller.deactivate("files.tool")
    assert first["first_stop"] is True
    assert first["released_handles"] == 1
    assert events == ["close:files.tool", "unbind:files.tool", "release"]
    assert controller.accepting_calls("files.tool") is False  # 先禁新调用
    second = controller.deactivate("files.tool")
    assert second["first_stop"] is False                      # 幂等
    assert second["released_handles"] == 0


def test_deactivate_reports_jobs_still_terminating_honestly():
    controller = PluginLifecycleController(job_wait_timeout=0.05)
    started = threading.Event()
    release = threading.Event()

    def worker() -> None:
        started.set()
        release.wait(2)                                        # 模拟不协作任务

    controller.activate("files.hang", lambda cs: None)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    started.wait(1)
    controller.jobs.start("hang-1", cancel=lambda: None)       # cancel 不真正停止 worker
    report = controller.deactivate("files.hang", wait_jobs=True)
    release.set()
    thread.join(1)
    assert report["fully_stopped"] is False                    # 未确认终止不谎报
    assert "hang-1" in report["jobs_still_terminating"]


# ---------------------------------------------------------------------------
# PluginJobs：取消是请求，确认后才叫 cancelled
# ---------------------------------------------------------------------------

def test_job_cancellation_requires_worker_confirmation():
    jobs = PluginJobs()
    cancelled_flag: list[str] = []
    jobs.start("j1", cancel=lambda: cancelled_flag.append("requested"))
    assert jobs.request_cancel("j1") is True
    assert cancelled_flag == ["requested"]
    assert jobs.state("j1") == "cancel_requested"              # 还不能叫已取消
    assert jobs.active() == ("j1",)
    jobs.confirm_cancelled("j1")
    assert jobs.state("j1") == "cancelled"
    assert jobs.active() == ()
    assert jobs.request_cancel("j1") is False                  # 已终止：无重复取消
    assert jobs.request_cancel("ghost") is False


def test_job_finish_wins_and_cancel_all_counts():
    jobs = PluginJobs()
    for job_id in ("a", "b"):
        jobs.start(job_id)
    jobs.finish("a")
    assert jobs.state("a") == "finished"
    assert jobs.request_cancel("a") is False                   # 完成后取消无意义
    assert jobs.cancel_all() == 1                              # 只取消仍在跑的 b
    with pytest.raises(ValueError):
        jobs.start("b")                                        # 重复任务 id


# ---------------------------------------------------------------------------
# 命令适用性（PL-04 统一匹配）
# ---------------------------------------------------------------------------

def test_command_applicability_matrix():
    command = PluginCapability(capability_id="launch", kind="command",
                               selection_mode="single", applies_to_kinds=("video",))
    assert check_command_applicability(command, selected_kinds=("video",))[0] is True
    ok, reason = check_command_applicability(command, selected_kinds=())
    assert ok is False and "一个" in reason
    ok, reason = check_command_applicability(command, selected_kinds=("video", "video"))
    assert ok is False
    ok, reason = check_command_applicability(command, selected_kinds=("image",))
    assert ok is False and "类型" in reason
    many = PluginCapability(capability_id="batch", kind="command", selection_mode="many")
    assert check_command_applicability(many, selected_kinds=("image", "video"))[0] is True
    none = PluginCapability(capability_id="open", kind="command", selection_mode="none")
    assert check_command_applicability(none, selected_kinds=())[0] is True
    assert check_command_applicability(none, selected_kinds=("image",))[0] is False


def test_cancel_token_is_shareable_between_context_and_jobs():
    token = CancelToken()
    jobs = PluginJobs()
    jobs.start("j", cancel=token.request)
    jobs.request_cancel("j")
    assert token.cancelled is True                             # 任务取消联动调用取消


@pytest.mark.parametrize("first_terminal", ["finished", "cancelled"])
def test_job_terminal_state_survives_late_opposite_confirmation(first_terminal: str) -> None:
    jobs = PluginJobs()
    jobs.start("job")
    assert jobs.request_cancel("job")
    if first_terminal == "finished":
        jobs.finish("job")
        jobs.confirm_cancelled("job")
    else:
        jobs.confirm_cancelled("job")
        jobs.finish("job")
    assert jobs.state("job") == first_terminal
    assert jobs.active() == ()
    assert not jobs.request_cancel("job")
    jobs.finish("job")
    jobs.confirm_cancelled("job")
    assert jobs.state("job") == first_terminal


def test_cancel_confirmation_requires_an_existing_requested_job() -> None:
    jobs = PluginJobs()
    jobs.confirm_cancelled("unknown")
    assert jobs.state("unknown") == "unknown"
    jobs.start("job")
    jobs.confirm_cancelled("job")
    assert jobs.state("job") == "running"
    assert jobs.request_cancel("job")
    jobs.confirm_cancelled("job")
    assert jobs.state("job") == "cancelled"
