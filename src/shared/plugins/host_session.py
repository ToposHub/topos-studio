"""F5：宿主会话——window owner/singleton/context 更新 + 贡献注册注销。

共享层（``PluginLifecycleController``）已有激活半失败全量回滚与可重试
停用状态机（PR-02 契约测试锁定），但真实宿主从未接线：窗口无 owner/
singleton 语义，快捷键等贡献游离重建（无所有权、无注销句柄）。

本模块补宿主侧两个缺口：

* :class:`WindowRegistry`——**window owner/singleton/context**：同一
  ``(plugin_id, window_id)`` 的 singleton 声明第二次激活 **focus 现有**
  而不是开新窗；窗口归属插件（``close_all(plugin_id)`` 供停用接线）；
  context 按代数更新（``expected_generation`` 落后即拒绝——迟到的旧代
  更新不得污染新窗口）；
* :class:`PluginHostSession`——组合 lifecycle + windows + **可整组替换
  的快捷键贡献集**（``replace_shortcuts``：旧集先释放、失败保留可重试；
  与 lifecycle 的停用语义分离——刷新不等于停用）。激活走
  ``lifecycle.activate``（registrar 抛错 ⇒ 全量回滚，契约由共享层保证），
  ``retire_plugin`` 释放快捷键贡献并关闭名下窗口。

真机边界：当前 Files 原生宿主不拥有任何插件窗口（工具窗口由旧服务
创建）——本注册表随 G 阶段逐插件接管时被真实窗口宿主消费；快捷键贡献
的所有权/注销本批即在生产控制器生效。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from src.shared.plugins.lifecycle import (
    ContributionSet,
    PluginLifecycleController,
)

WindowCallback = Callable[[], Any]


@dataclass
class _WindowState:
    plugin_id: str
    window_id: str
    singleton: bool
    generation: int = 0
    context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    _focuser: WindowCallback | None = field(default=None, repr=False)
    _closer: WindowCallback | None = field(default=None, repr=False)
    closed: bool = False


class WindowRegistry:
    """窗口归属与 singleton 语义；线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._windows: dict[tuple[str, str], _WindowState] = {}

    def activate_window(self, plugin_id: str, window_id: str, *,
                        singleton: bool = True,
                        opener: WindowCallback | None = None,
                        focuser: WindowCallback | None = None,
                        closer: WindowCallback | None = None) -> tuple[str, int]:
        """激活（或聚焦）一个窗口 → ``("opened"|"focused", generation)``。

        singleton 窗口已存活时调 ``focuser`` 聚焦现有窗口，不开新窗；
        否则调 ``opener`` 开新窗并登记归属。
        """
        key = (plugin_id, window_id)
        with self._lock:
            existing = self._windows.get(key)
            if existing is not None and not existing.closed and singleton:
                if existing.singleton and focuser is not None:
                    focuser()
                return "focused", existing.generation
            state = _WindowState(plugin_id=plugin_id, window_id=window_id,
                                 singleton=singleton, generation=0,
                                 _focuser=focuser, _closer=closer)
            self._windows[key] = state
        if opener is not None:
            opener()
        return "opened", 0

    def close_window(self, plugin_id: str, window_id: str) -> bool:
        with self._lock:
            state = self._windows.get((plugin_id, window_id))
            if state is None or state.closed:
                return False
            state.closed = True
            closer = state._closer
        if closer is not None:
            closer()
        with self._lock:
            self._windows.pop((plugin_id, window_id), None)
        return True

    def close_all(self, plugin_id: str) -> int:
        """关闭归属 ``plugin_id`` 的全部窗口（停用接线用）。"""
        with self._lock:
            keys = [key for key, state in self._windows.items()
                    if key[0] == plugin_id and not state.closed]
        closed = 0
        for key in keys:
            if self.close_window(*key):
                closed += 1
        return closed

    def update_context(self, plugin_id: str, window_id: str,
                       context: Mapping[str, Any], *,
                       expected_generation: int | None = None) -> int | None:
        """更新窗口 context → 新代数；``expected_generation`` 落后拒绝。

        返回 None = 拒绝（窗口不存在，或旧代更新迟到）；调用方不得把
        拒绝当成功。
        """
        with self._lock:
            state = self._windows.get((plugin_id, window_id))
            if state is None or state.closed:
                return None
            if expected_generation is not None and \
                    int(expected_generation) < state.generation:
                return None   # 旧代更新迟到：不污染新窗口
            state.generation += 1
            state.context = dict(context)
            return state.generation

    def context_of(self, plugin_id: str, window_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            state = self._windows.get((plugin_id, window_id))
            return dict(state.context) if state is not None and not state.closed else None

    def windows_for(self, plugin_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(key[1] for key, state in self._windows.items()
                                if key[0] == plugin_id and not state.closed))

    def live_count(self) -> int:
        with self._lock:
            return sum(1 for state in self._windows.values() if not state.closed)


class PluginHostSession:
    """一个宿主域（如 Files 原生面板）的插件运行会话。"""

    def __init__(self, *, job_wait_timeout: float = 2.0) -> None:
        self.windows = WindowRegistry()
        self.lifecycle = PluginLifecycleController(
            close_windows=self.windows.close_all,
            job_wait_timeout=job_wait_timeout)
        self._lock = threading.RLock()
        self._shortcuts: dict[str, ContributionSet] = {}
        # 释放失败但所有权未完成的旧贡献集：保留可重试（不得随替换丢失）
        self._failed_sets: dict[str, list[ContributionSet]] = {}

    # ------------------------------------------------------------ 激活/停用
    def activate_plugin(self, plugin_id: str,
                        registrar: Callable[[ContributionSet], None]) -> ContributionSet:
        """注册插件贡献；registrar 抛错 ⇒ 全量回滚（共享层契约）。"""
        return self.lifecycle.activate(plugin_id, registrar)

    def reactivate_plugin(self, plugin_id: str,
                          registrar: Callable[[ContributionSet], None]) -> tuple[ContributionSet, int]:
        """[F7] 新代重激活 → (贡献集, 新代数)。旧代消息按代数拒绝。"""
        contributions = self.lifecycle.reactivate(plugin_id, registrar)
        return contributions, self.lifecycle.generation(plugin_id)

    def accepting(self, plugin_id: str, generation: int | None = None) -> bool:
        """受理判定（可带代数）：旧代在途消息不得污染新窗口/作业。"""
        return self.lifecycle.accepting_calls(plugin_id, generation=generation)

    def job_history(self, plugin_id: str | None = None,
                    limit: int | None = None):
        """[F7] 作业历史（终态、有界）——作业历史视图数据源。"""
        return self.lifecycle.jobs.history(plugin_id=plugin_id, limit=limit)

    def _release_shortcut_set(self, shortcuts: ContributionSet) -> int:
        """释放一个贡献集；失败保留在 _failed_sets 供重试（所有权不移除）。"""
        released = shortcuts.retry_failed_releases() + shortcuts.rollback()
        if shortcuts.release_failures:
            with self._lock:
                self._failed_sets.setdefault(shortcuts.plugin_id,
                                             []).append(shortcuts)
        return released

    def release_contributions(self, plugin_id: str) -> dict[str, Any]:
        """释放名下贡献（快捷键/窗口），不进停用状态机。

        用于 restart-bound 的启停（legacy 语义）：停用释放快捷键与窗口，
        再启用时贡献可重建——不触发 lifecycle 的不可逆停用。
        """
        with self._lock:
            shortcuts = self._shortcuts.pop(plugin_id, None)
        released = self._release_shortcut_set(shortcuts) \
            if shortcuts is not None else 0
        closed = self.windows.close_all(plugin_id)
        return {"released_shortcuts": released, "closed_windows": closed,
                "shortcut_failures": self.shortcut_failures(plugin_id)}

    def retire_plugin(self, plugin_id: str) -> dict[str, Any]:
        """退役：释放快捷键贡献 + 关闭名下窗口（停用状态机可续调）。"""
        with self._lock:
            shortcuts = self._shortcuts.pop(plugin_id, None)
        released_shortcuts = self._release_shortcut_set(shortcuts) \
            if shortcuts is not None else 0
        with self._lock:
            failed = self._failed_sets.pop(plugin_id, [])
        for stale in failed:
            released_shortcuts += self._release_shortcut_set(stale)
        closed_windows = self.windows.close_all(plugin_id)
        report = self.lifecycle.deactivate(plugin_id, wait_jobs=False)
        report["released_shortcuts"] = released_shortcuts
        report["closed_windows"] = closed_windows
        return report

    # ------------------------------------------------------------ 快捷键贡献
    def replace_shortcuts(self, plugin_id: str,
                          entries: list[tuple[str, Callable[[], None]]]) -> int:
        """整组替换某插件的快捷键贡献：旧集先释放（失败保留可重试）。

        返回旧集中成功释放的句柄数。刷新 ≠ 停用——不走 lifecycle 状态机。
        """
        with self._lock:
            previous = self._shortcuts.pop(plugin_id, None)
        released = self._release_shortcut_set(previous) \
            if previous is not None else 0
        fresh = ContributionSet(plugin_id)
        for key, release in entries:
            fresh.register("shortcut", key, release)
        fresh.commit()
        with self._lock:
            self._shortcuts[plugin_id] = fresh
        return released

    def retry_shortcut_release(self, plugin_id: str) -> int:
        """重试该插件全部未完成释放（当前集 + 历史失败集）。"""
        with self._lock:
            current = self._shortcuts.get(plugin_id)
            failed = list(self._failed_sets.get(plugin_id, ()))
        retried = 0
        for stale in failed:
            retried += stale.retry_failed_releases()
        with self._lock:
            self._failed_sets[plugin_id] = [
                item for item in self._failed_sets.get(plugin_id, ())
                if item.release_failures]
            if not self._failed_sets.get(plugin_id):
                self._failed_sets.pop(plugin_id, None)
        if current is not None:
            retried += current.retry_failed_releases()
        return retried

    def shortcut_failures(self, plugin_id: str) -> tuple[str, ...]:
        with self._lock:
            current = self._shortcuts.get(plugin_id)
            failed = list(self._failed_sets.get(plugin_id, ()))
        failures = list(current.release_failures) if current is not None else []
        for stale in failed:
            failures.extend(stale.release_failures)
        return tuple(failures)

    def shortcut_count(self, plugin_id: str) -> int:
        with self._lock:
            shortcuts = self._shortcuts.get(plugin_id)
        return shortcuts.size if shortcuts is not None else 0


__all__ = ["PluginHostSession", "WindowRegistry"]
