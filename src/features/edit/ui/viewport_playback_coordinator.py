"""VP-B01／I01：Viewport 播放协调器——app 适配层（QObject）。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B01 +
docs/plans/2026-11-09-viewport-integration-review-followup.md §I01。

职责与边界：

- 把纯领域仲裁器 ``ViewportPlaybackArbiter`` 接到既有对象上：Source 侧
  鸭子类型 ``SourceViewerWidget``（play/pause/current_asset/is_playing/
  ``playback_state_changed(bool)``），Timeline 侧 ``EditPlaybackController``
  （play/pause/is_playing + ``state_changed(str)``），Viewport 侧只按
  信号名连接（``active_monitor_changed(str)``/``mode_changed(bool)``）；
- 前端 ID "program" ↔ 领域 ID "timeline" 的映射只发生在这一层；
- 命令出口（``_sink``）应用到真实控制器后**核验效果**（暂停后仍播放 /
  起播后未播放都算失败，I01-5：不让静默拒绝虚报成功），异常带原因记录；
- 反馈闭环：控制器自发状态变化（播到末尾、外部 pause 等）经
  ``state_changed``／``playback_state_changed`` 回填，**快照实际变化时**
  广播 ``snapshot_changed``（I01-5：反馈路径也发快照）；
- 入口仲裁（I01-2）：真实入口链自带准备步骤（时长同步、JKL 档位、
  scrub 恢复），不改为仲裁器代发 play；入口先经 ``authorize_*_play``
  取得"可播 + 对侧已暂停"授权，再走原链起播。授权即一次领域请求；
- 上下文同步（I01-1）：安装时与每次决策前读取真实宿主状态（当前素材、
  时间线可用性、两路实际播放、布局/激活监视器、页面模式），不依赖
  "稍后用户再点一次"来初始化快照。

接线入口：``install_viewport_playback_coordinator``（宿主创建 Viewport
处调用一次；重复安装的所有权归宿主——持有引用并只装一次）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.core.viewport_playback_arbiter import (
    PAGE_DELIVER,
    PAGE_EDIT,
    PAGE_OTHER,
    SOURCE,
    TIMELINE,
    ArbiterCommand,
    PauseMonitor,
    StartMonitor,
    ViewportPlaybackArbiter,
    describe_reason,
)

logger = logging.getLogger(__name__)

# 前端 EditViewport 的监视器叫 "program"/"source"；领域 ID 是
# "timeline"/"source"。映射只在这一层发生。
_VIEWPORT_TO_DOMAIN = {"program": TIMELINE, "source": SOURCE}
_DOMAIN_TO_VIEWPORT = {TIMELINE: "program", SOURCE: "source"}


class ViewportPlaybackCoordinator(QObject):
    """仲裁器 ↔ 既有播放控制器/Viewport 的信号适配层。"""

    snapshot_changed = pyqtSignal(object)  # MonitorSnapshot
    status_message = pyqtSignal(str)       # 拒绝原因回填（稳定键或文案）

    def __init__(self, source_widget: Any,
                 timeline_playback_controller: Any,
                 timeline_panel: Any = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._source_widget = source_widget
        self._timeline_controller = timeline_playback_controller
        # I01-1：时间线可用性从真实 panel 读取（仅鸭子读取，不依赖其类型）
        self._timeline_panel = timeline_panel
        self._arbiter = ViewportPlaybackArbiter()
        self._arbiter.set_command_sink(self._sink)
        self._viewport: Any = None
        self._connect_controller_feedback()

    # ── 接线 ────────────────────────────────────────────────────────

    def attach_viewport(self, viewport: Any) -> None:
        """按信号名连接前端容器（不 import 其模块，避免耦合提交线）。"""
        self._viewport = viewport
        changed = getattr(viewport, "active_monitor_changed", None)
        if changed is not None:
            changed.connect(self._on_viewport_active_changed)
        mode_changed = getattr(viewport, "mode_changed", None)
        if mode_changed is not None:
            mode_changed.connect(self._on_viewport_mode_changed)

    @property
    def arbiter(self) -> ViewportPlaybackArbiter:
        return self._arbiter

    def _connect_controller_feedback(self) -> None:
        state_changed = getattr(self._timeline_controller, "state_changed", None)
        if state_changed is not None:
            state_changed.connect(self._on_controller_state_changed)
        # I01-4：Source 的播放状态信号（play/pause/自然到尾/open/close/
        # shutdown 都翻转它）——没有它，Source 自然停止会让快照过期。
        source_state = getattr(self._source_widget, "playback_state_changed",
                               None)
        if source_state is not None:
            source_state.connect(self._on_source_state_changed)

    # ── 宿主入口（每处恰好一次领域请求） ─────────────────────────────

    def open_source_asset(self, asset_id: Optional[str], *, activate: bool,
                          via: str = "media_pool_double_click") -> None:
        decision = self._arbiter.open_source_asset(
            asset_id, activate=activate, via=via)
        self._finish(decision)

    def request_play_source(self) -> None:
        self._finish(self._arbiter.request_play(SOURCE))

    def request_play_timeline(self) -> None:
        self._finish(self._arbiter.request_play(TIMELINE))

    def request_pause_source(self) -> None:
        self._finish(self._arbiter.request_pause(SOURCE))

    def request_pause_timeline(self) -> None:
        self._finish(self._arbiter.request_pause(TIMELINE))

    def stop_source_in_place(self) -> None:
        """B05：Source 的 Stop 按钮语义 = 原位暂停（不归零）。"""
        self._finish(self._arbiter.request_stop_in_place(SOURCE))

    def stop_timeline_in_place(self) -> None:
        self._finish(self._arbiter.request_stop_in_place(TIMELINE))

    # ── 入口授权（I01-2：入口链自带准备步骤，仲裁器只授权不代发） ────

    def authorize_source_play(self, via: str = "button") -> Optional[str]:
        """Source 起播入口（按钮/Space/scrub 恢复/倒放）的仲裁门。

        返回 ``None`` = 已授权（对侧若在播已被暂停），入口链继续自己的
        起播；返回原因键 = 入口必须中止起播（原因已发 ``status_message``）。
        """
        self._refresh_context()
        decision = self._arbiter.authorize_play(SOURCE)
        self._finish(decision)
        logger.debug("authorize source play via=%s accepted=%s reason=%s",
                     via, decision.accepted, decision.reason)
        return None if decision.accepted else decision.reason

    def authorize_timeline_play(self, via: str = "button") -> Optional[str]:
        """Timeline 起播入口（按钮/Space/J/L/start_playback）的仲裁门。"""
        self._refresh_context()
        decision = self._arbiter.authorize_play(TIMELINE)
        self._finish(decision)
        logger.debug("authorize timeline play via=%s accepted=%s reason=%s",
                     via, decision.accepted, decision.reason)
        return None if decision.accepted else decision.reason

    def activate_source(self, via: str = "explicit") -> None:
        self._finish(self._arbiter.request_activate(SOURCE, via=via))

    def activate_timeline(self, via: str = "explicit") -> None:
        self._finish(self._arbiter.request_activate(TIMELINE, via=via))

    def set_layout_mode(self, dual: bool) -> None:
        self._finish(self._arbiter.set_layout_mode(
            "dual" if dual else "single"))

    def set_text_input_active(self, active: bool) -> None:
        self._arbiter.set_ui_gates(text_input_active=active)

    def set_menu_open(self, open_: bool) -> None:
        self._arbiter.set_ui_gates(menu_open=open_)

    def set_timeline_availability(self, has_timeline: bool) -> None:
        self._arbiter.set_availability(has_timeline=has_timeline)

    def enter_deliver_page(self) -> None:
        self._finish(self._arbiter.set_page_mode(PAGE_DELIVER))

    def leave_edit_page(self) -> None:
        self._finish(self._arbiter.set_page_mode(PAGE_OTHER))

    def return_to_edit_page(self) -> None:
        """回到 Edit：恢复页面模式，并把快照对齐回 Viewport 的真实显示。

        Deliver 期间仲裁器强制 active=Timeline；归还后 Viewport 仍保留
        用户原本的单/双与激活选择（借还不改显示偏好），快照必须跟着
        显示事实走，而不是让旧仲裁状态覆盖用户选择。
        """
        self._finish(self._arbiter.set_page_mode(PAGE_EDIT))
        self.sync_display_state()

    def mark_shutdown(self) -> None:
        self._arbiter.mark_shutdown()

    # ── 上下文同步（I01-1：决策前读取真实宿主状态） ──────────────────

    def _refresh_context(self) -> None:
        """读取真实 widget/panel 的当前素材与时间线可用性进快照。"""
        asset = getattr(self._source_widget, "current_asset", None)
        self._arbiter.set_source_asset_id(
            getattr(asset, "asset_id", None) if asset is not None else None)
        if self._timeline_panel is not None:
            view = getattr(self._timeline_panel, "timeline_view", None)
            timeline = getattr(view, "_timeline", None)
            self._arbiter.set_availability(has_timeline=timeline is not None)

    def sync_initial_state(self, *, page_mode: str = PAGE_EDIT) -> None:
        """安装时一次性同步：内容/可用性/两路实际播放/布局/激活/页面。

        宿主在 install 后立即调用（页面模式按当前页传入）。直接调领域
        件（不经 ``_finish``），接受类同步不冒状态栏噪声；结束时广播
        一次权威快照。
        """
        self._refresh_context()
        if page_mode != self._arbiter.snapshot().page_mode:
            self._arbiter.set_page_mode(page_mode)
        self.sync_display_state()
        self._report_current_states()
        self.snapshot_changed.emit(self._arbiter.snapshot())

    def sync_display_state(self) -> None:
        """把 Viewport 的真实布局/激活监视器对齐进快照（零播放命令）。

        Deliver 借还或安装时的对齐用：快照跟随显示事实，不反向驱动显示。
        """
        if self._viewport is None:
            return
        dual = getattr(self._viewport, "dual_view", None)
        if dual is not None:
            self._arbiter.set_layout_mode("dual" if bool(dual) else "single")
        active = getattr(self._viewport, "active_monitor", None)
        domain = _VIEWPORT_TO_DOMAIN.get(active)
        if domain is not None:
            self._arbiter.request_activate(domain)

    # ── 查询 ────────────────────────────────────────────────────────

    def snapshot(self):
        return self._arbiter.snapshot()

    # ── 内部：命令出口 + 反馈 ───────────────────────────────────────

    def _target_for(self, monitor: str) -> Any:
        return (self._source_widget if monitor == SOURCE
                else self._timeline_controller)

    def _sink(self, command: ArbiterCommand) -> bool:
        """应用命令并**核验效果**：状态没变 = 失败（I01-5 不虚报成功）。"""
        target = self._target_for(command.monitor)
        try:
            if isinstance(command, PauseMonitor):
                target.pause()
                if bool(getattr(target, "is_playing", True)):
                    logger.warning(
                        "viewport playback sink: pause of %s had no effect "
                        "(still playing)", command.monitor)
                    return False
            elif isinstance(command, StartMonitor):
                target.play()
                if not bool(getattr(target, "is_playing", False)):
                    logger.warning(
                        "viewport playback sink: start of %s was silently "
                        "rejected (not playing)", command.monitor)
                    return False
            else:  # pragma: no cover - 领域件扩展命令时显式失败
                logger.warning("viewport playback sink: unknown command %r",
                               command)
                return False
        except Exception as exc:  # noqa: BLE001 — 单侧命令失败不拖垮另一侧
            logger.warning("viewport playback sink: %s %s raised %s: %s",
                           type(command).__name__, command.monitor,
                           type(exc).__name__, exc, exc_info=True)
            return False
        # 应用成功后立即反馈（快照永远 truthful），变化经信号广播
        self._handle_feedback()
        return True

    def _report_current_states(self) -> bool:
        """把两路真实播放状态回填仲裁器；返回快照是否发生变化。"""
        before = self._arbiter.snapshot()
        for monitor, obj in ((SOURCE, self._source_widget),
                             (TIMELINE, self._timeline_controller)):
            is_playing = getattr(obj, "is_playing", None)
            if is_playing is None:
                continue  # 宿主对象尚未暴露公开播放状态（见 B04 快照）
            speed = getattr(obj, "playback_speed", None)
            direction = getattr(obj, "playback_direction", None)
            direction_value = getattr(direction, "value", direction)
            self._arbiter.report_playback_state(
                monitor,
                playing=bool(is_playing),
                direction=str(direction_value or "forward"),
                speed=float(speed) if speed is not None else 1.0)
        return self._arbiter.snapshot() != before

    def _handle_feedback(self) -> None:
        if self._report_current_states():
            self.snapshot_changed.emit(self._arbiter.snapshot())

    def _on_controller_state_changed(self, state: str) -> None:
        # 控制器自发状态变化（播放到末尾、外部 pause 等）也回填并广播
        logger.debug("timeline controller state -> %s", state)
        self._handle_feedback()

    def _on_source_state_changed(self, playing: bool) -> None:
        logger.debug("source widget playback state -> %s", playing)
        self._handle_feedback()

    def _on_viewport_active_changed(self, monitor: str) -> None:
        domain = _VIEWPORT_TO_DOMAIN.get(monitor)
        if domain is None:
            return
        # 显示策略：激活零播放命令；拒绝（如 Deliver 中点 Source）回原因
        self._finish(self._arbiter.request_activate(domain))

    def _on_viewport_mode_changed(self, dual: bool) -> None:
        self._finish(self._arbiter.set_layout_mode(
            "dual" if dual else "single"))

    def _finish(self, decision: Any) -> None:
        if not decision.accepted and decision.reason:
            logger.debug("viewport playback request rejected: %s",
                         decision.reason)
            self.status_message.emit(describe_reason(decision.reason))
        self.snapshot_changed.emit(decision.snapshot)


def install_viewport_playback_coordinator(
        *, viewport: Any, source_widget: Any, timeline_panel: Any,
        parent: Optional[QObject] = None) -> ViewportPlaybackCoordinator:
    """单入口接线：viewport + source widget + timeline panel → 协调器。

    ``timeline_panel`` 需提供 ``get_playback_controller()`` 与
    ``timeline_view``（现有 TimelinePanel 即有）。安装后宿主应调用
    ``sync_initial_state(page_mode=...)`` 完成初始状态对齐，并通过
    ``authorize_source_play``/``authorize_timeline_play`` 安装入口仲裁门
    （``set_playback_gate``）。重复安装的所有权归宿主：持有返回引用，
    只安装一次；窗口关闭时调用 ``mark_shutdown``。
    """
    controller = timeline_panel.get_playback_controller()
    coordinator = ViewportPlaybackCoordinator(
        source_widget, controller, timeline_panel=timeline_panel,
        parent=parent)
    coordinator.attach_viewport(viewport)
    return coordinator
