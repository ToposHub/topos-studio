"""F5：宿主会话——window owner/singleton/context、贡献注册注销、半失败回滚。

共享层 lifecycle 的激活回滚/可重试停用由 PR-02 契约测试锁定；本批验证
宿主侧组合：WindowRegistry 的 singleton/context 代数语义、快捷键贡献
的所有权（整组替换/失败重试/停用释放）、激活半失败经真实宿主会话完整
回滚。
"""
from __future__ import annotations

import threading

import pytest

from src.shared.plugins.host_session import PluginHostSession, WindowRegistry


# ---------------------------------------------------------------------------
# WindowRegistry：owner / singleton / context 代数
# ---------------------------------------------------------------------------

def test_singleton_second_activation_focuses_existing_window():
    registry = WindowRegistry()
    opened: list[str] = []
    focused: list[str] = []
    action, gen = registry.activate_window(
        "files.a", "main", singleton=True,
        opener=lambda: opened.append("1"), focuser=lambda: focused.append("1"))
    assert (action, gen) == ("opened", 0) and opened == ["1"]
    action2, gen2 = registry.activate_window(
        "files.a", "main", singleton=True,
        opener=lambda: opened.append("2"), focuser=lambda: focused.append("2"))
    assert (action2, gen2) == ("focused", 0)   # 不开新窗，聚焦现有
    assert opened == ["1"] and focused == ["2"]
    assert registry.windows_for("files.a") == ("main",)


def test_non_singleton_allows_multiple_windows():
    registry = WindowRegistry()
    action1, _ = registry.activate_window("files.a", "clip", singleton=False,
                                          opener=lambda: None)
    action2, _ = registry.activate_window("files.a", "clip", singleton=False,
                                          opener=lambda: None)
    assert action1 == action2 == "opened"


def test_windows_are_owned_by_plugin_and_close_all():
    registry = WindowRegistry()
    closed: list[str] = []
    registry.activate_window("files.a", "main", closer=lambda: closed.append("main"))
    registry.activate_window("files.a", "clip", closer=lambda: closed.append("clip"))
    registry.activate_window("files.b", "main", closer=lambda: closed.append("b-main"))
    assert registry.close_all("files.a") == 2      # 只关 A 的窗口（归属）
    assert sorted(closed) == ["clip", "main"]
    assert registry.windows_for("files.a") == ()
    assert registry.windows_for("files.b") == ("main",)
    assert registry.live_count() == 1


def test_closed_window_can_reopen():
    registry = WindowRegistry()
    registry.activate_window("files.a", "main", closer=lambda: None)
    assert registry.close_window("files.a", "main") is True
    assert registry.close_window("files.a", "main") is False   # 幂等
    action, _ = registry.activate_window("files.a", "main", opener=lambda: None)
    assert action == "opened"                     # 关闭后可重开


def test_context_update_generation_rejects_stale():
    registry = WindowRegistry()
    registry.activate_window("files.a", "main")
    gen1 = registry.update_context("files.a", "main", {"selection": ["/a"]})
    assert gen1 == 1
    # 旧代更新（迟到）：拒绝，不污染
    assert registry.update_context("files.a", "main",
                                   {"selection": ["/stale"]},
                                   expected_generation=0) is None
    assert registry.context_of("files.a", "main") == {"selection": ["/a"]}
    gen2 = registry.update_context("files.a", "main",
                                   {"selection": ["/b"]}, expected_generation=1)
    assert gen2 == 2
    assert registry.context_of("files.a", "main") == {"selection": ["/b"]}
    # 不存在的窗口：拒绝（不得当成功）
    assert registry.update_context("files.x", "ghost", {}) is None


# ---------------------------------------------------------------------------
# PluginHostSession：快捷键贡献所有权 + 退役
# ---------------------------------------------------------------------------

def test_replace_shortcuts_releases_previous_set():
    session = PluginHostSession()
    released: list[str] = []
    session.replace_shortcuts("files.a", [("Ctrl+Alt+1",
                                           lambda: released.append("old-1"))])
    assert session.shortcut_count("files.a") == 1
    session.replace_shortcuts("files.a", [("Ctrl+Alt+2",
                                           lambda: released.append("new-2"))])
    assert released == ["old-1"]                  # 旧集先释放
    assert session.shortcut_count("files.a") == 1


def test_shortcut_release_failure_kept_for_retry():
    session = PluginHostSession()
    calls = {"fail": 0}

    def flaky():
        calls["fail"] += 1
        if calls["fail"] == 1:
            raise RuntimeError("释放失败一次")

    session.replace_shortcuts("files.a", [("Ctrl+Alt+1", flaky)])
    session.replace_shortcuts("files.a", [])       # 替换时旧句柄释放失败
    assert session.shortcut_failures("files.a")    # 失败保留（所有权不移除）
    assert session.retry_shortcut_release("files.a") == 1
    assert session.shortcut_failures("files.a") == ()


def test_release_contributions_releases_shortcuts_and_windows():
    session = PluginHostSession()
    released: list[str] = []
    windows_closed: list[str] = []
    session.replace_shortcuts("files.a", [("Ctrl+Alt+K",
                                           lambda: released.append("k"))])
    session.windows.activate_window("files.a", "main",
                                    closer=lambda: windows_closed.append("main"))
    report = session.release_contributions("files.a")
    assert report["released_shortcuts"] == 1
    assert report["closed_windows"] == 1
    assert released == ["k"] and windows_closed == ["main"]
    assert session.shortcut_count("files.a") == 0


# ---------------------------------------------------------------------------
# 激活半失败完整回滚（真实宿主会话组合）
# ---------------------------------------------------------------------------

def test_activation_half_failure_rolls_back_completely():
    session = PluginHostSession()
    released: list[str] = []

    def registrar(contributions):
        contributions.register("menu", "main-menu",
                               lambda: released.append("menu"))
        contributions.register("event", "on-refresh",
                               lambda: released.append("event"))
        raise RuntimeError("第三个贡献注册失败")

    with pytest.raises(RuntimeError):
        session.activate_plugin("files.a", registrar)
    # 半失败：已注册的全部回滚（逆序），不留游离贡献
    assert sorted(released) == ["event", "menu"]
    assert session.lifecycle.accepting_calls("files.a") is False
    # 回滚干净后可再次激活
    def good(contributions):
        contributions.register("menu", "main-menu", lambda: None)
    session.activate_plugin("files.a", good)
    assert session.lifecycle.accepting_calls("files.a") is True


def test_retire_plugin_releases_everything_and_reports_honestly():
    session = PluginHostSession()
    released: list[str] = []
    session.replace_shortcuts("files.a", [("Ctrl+Alt+K",
                                           lambda: released.append("k"))])
    session.windows.activate_window("files.a", "main",
                                    closer=lambda: released.append("window"))
    session.activate_plugin("files.a", lambda contributions:
                            contributions.register("menu", "m",
                                                   lambda: released.append("menu")))
    report = session.retire_plugin("files.a")
    assert report["released_shortcuts"] == 1
    assert report["closed_windows"] == 1
    assert report["fully_stopped"] is True
    assert sorted(released) == ["k", "menu", "window"]


def test_session_is_thread_safe_for_window_operations():
    registry = WindowRegistry()
    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker(index: int):
        barrier.wait()
        action, _ = registry.activate_window(
            "files.a", "main", singleton=True,
            opener=lambda: results.append(f"open{index}"),
            focuser=lambda: results.append(f"focus{index}"))
        results.append(action)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    # singleton 并发：恰好一次 opened，其余 focused——不开第二个窗口
    assert results.count("opened") == 1
    assert results.count("focused") == 3
    assert registry.live_count() == 1
