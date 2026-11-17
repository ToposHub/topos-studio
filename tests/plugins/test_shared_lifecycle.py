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
    controller.jobs.start("hang-1", cancel=lambda: None,
                          plugin_id="files.hang")              # 任务归属该插件
    report = controller.deactivate("files.hang", wait_jobs=True)
    release.set()
    thread.join(1)
    assert report["fully_stopped"] is False                    # 未确认终止不谎报
    assert "hang-1" in report["jobs_still_terminating"]


# ---------------------------------------------------------------------------
# PR-02 回归：按插件归属清理、失败聚合与可重试停用
# ---------------------------------------------------------------------------

def test_stopping_plugin_a_never_cancels_plugin_b_jobs():
    """复现 5：停用 A 不取消同一 controller 下 B 的任务。"""
    controller = PluginLifecycleController()
    controller.activate("files.a", lambda cs: None)
    controller.activate("files.b", lambda cs: None)
    controller.jobs.start("job-b", plugin_id="files.b")
    controller.deactivate("files.a")
    assert controller.jobs.state("job-b") == "running"          # B 的任务原样
    assert controller.is_stopped("files.a") is True
    assert controller.accepting_calls("files.b") is True        # B 完全不受影响


def test_rollback_continues_when_one_release_raises():
    """复现 4：后注册句柄 release 抛错，其余句柄仍被释放且可重试。"""
    released: list[str] = []
    calls = {"flaky": 0}

    def flaky() -> None:
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("release 失败一次")

    fresh = ContributionSet("files.x")
    fresh.register("route", "r1", lambda: released.append("r1"))
    fresh.register("route", "r2", flaky)                        # 后注册：回滚时先弹出行内错误
    fresh.register("menu", "m1", lambda: released.append("m1"))
    assert fresh.rollback() == 2                                # r1/m1 仍被释放
    assert sorted(released) == ["m1", "r1"]                     # 逆序释放没有中断
    assert fresh.release_failures != ()
    assert fresh.size == 1                                      # 失败句柄仍在集合内
    assert fresh.retry_failed_releases() == 1                   # 重试成功
    assert fresh.release_failures == () and fresh.size == 0


def test_deactivate_keeps_ownership_until_jobs_confirm_and_cleanup_succeeds():
    """任务未确认终止不释放贡献；迟到完成后重试 deactivate 才真正停。"""
    controller = PluginLifecycleController(job_wait_timeout=0.05)
    released: list[str] = []

    def registrar(contributions: ContributionSet) -> None:
        contributions.register("route", "r", lambda: released.append("r"))

    controller.activate("files.tool", registrar)
    controller.jobs.start("slow-job", plugin_id="files.tool", cancel=lambda: None)
    first = controller.deactivate("files.tool")
    assert first["fully_stopped"] is False
    assert released == []                                       # 贡献未被提前释放
    assert controller.is_stopped("files.tool") is False
    controller.jobs.finish("slow-job")                          # 迟到的完成确认
    second = controller.deactivate("files.tool")
    assert second["fully_stopped"] is True
    assert released == ["r"]                                    # 确认终止后才释放
    assert controller.is_stopped("files.tool") is True


def test_cleanup_step_failure_is_aggregated_and_retryable():
    """close_windows 抛错不阻断 unbind/release；重试从断点继续。"""
    events: list[str] = []
    attempts = {"close": 0}

    def close_windows(plugin_id: str) -> None:
        events.append("close")
        attempts["close"] += 1
        if attempts["close"] == 1:
            raise RuntimeError("窗口句柄失效")

    controller = PluginLifecycleController(
        close_windows=close_windows,
        unbind_events=lambda pid: events.append("unbind"))

    def registrar(contributions: ContributionSet) -> None:
        contributions.register("route", "r", lambda: events.append("release"))

    controller.activate("files.tool", registrar)
    report = controller.deactivate("files.tool")
    assert report["fully_stopped"] is False
    assert any(item.startswith("close_windows:") for item in report["cleanup_failures"])
    assert events == ["close", "unbind", "release"]             # 独立资源继续清理，不互相阻断
    assert controller.is_stopped("files.tool") is False         # 未谎报停止
    retry = controller.deactivate("files.tool")                 # 幂等重试：close 成功
    assert retry["fully_stopped"] is True
    assert events == ["close", "unbind", "release", "close", "unbind"]  # 宿主回调幂等，重试重跑清理序列
    assert controller.is_stopped("files.tool") is True


def test_activation_failure_with_dirty_rollback_stays_retryable():
    """激活半失败且回滚也有句柄失败：所有权保留，deactivate 可清理。"""
    calls = {"flaky": 0}

    def flaky() -> None:
        calls["flaky"] += 1
        if calls["flaky"] < 2:
            raise RuntimeError("release 失败")

    controller = PluginLifecycleController()

    def registrar(contributions: ContributionSet) -> None:
        contributions.register("route", "r", flaky)
        raise ValueError("setup() 第三步抛异常")

    with pytest.raises(RuntimeError, match="回滚清理未完成"):
        controller.activate("files.broken", registrar)
    assert controller.accepting_calls("files.broken") is False
    report = controller.deactivate("files.broken")              # 重试清理
    assert report["fully_stopped"] is True
    assert calls["flaky"] == 2                                  # 第二次重试成功释放


def test_jobs_owner_scoped_cancellation():
    jobs = PluginJobs()
    jobs.start("owned", plugin_id="files.a")
    jobs.start("free")                                          # 兼容旧用法：无归属
    assert jobs.owner("owned") == "files.a" and jobs.owner("free") == ""
    assert jobs.active_for("files.a") == ("owned",)
    assert jobs.cancel_owned("files.a") == 1
    assert jobs.state("owned") == "cancel_requested"
    assert jobs.state("free") == "running"                      # 无归属任务不受影响
    assert jobs.cancel_all() == 1                               # 宿主整体关闭才全取消


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
