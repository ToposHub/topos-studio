"""VP-B01：Viewport 播放协调器——app 适配层（QObject）。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B01（"由
app 适配现有控制器；UI 通过信号请求状态变更，处理完成后回填"）。

职责与边界：

- 把纯领域仲裁器 ``ViewportPlaybackArbiter`` 接到既有对象上：Source 侧
  鸭子类型 ``SourceViewerWidget``（play/pause/current_asset/is_playing），
  Timeline 侧 ``EditPlaybackController``（play/pause/is_playing +
  ``state_changed(str)``），Viewport 侧只按信号名连接
  （``active_monitor_changed(str)``/``mode_changed(bool)``），**不 import
  前端容器模块**——前端位于另一条未合流的提交线；
- 前端 ID "program" ↔ 领域 ID "timeline" 的映射只发生在这一层；
- 命令出口（``_sink``）应用到真实控制器后**立即**把播放状态反馈回仲裁
  器（快照永远 truthful）；控制器自发的 ``state_changed``（如播到末尾）
  同样回填——反馈不触发新命令，无递归；
- 拒绝原因经 ``status_message`` 回填宿主状态栏；快照经
  ``snapshot_changed`` 广播（宿主用它驱动按钮 checked 态，禁止单独切换
  样式冒充能力）；
- 宿主入口统一：媒体池单击/双击、Open in Source、Space、Deliver 往返
  都应经由本协调器（``open_source_asset``/``request_play_*``/
  ``set_page_mode``），保证"一次交互只产生一次领域请求"。

接线入口：``install_viewport_playback_coordinator``（一行完成全部连接，
前端容器合流后在宿主处调用）。
"""

from __future__ import annotations

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
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._source_widget = source_widget
        self._timeline_controller = timeline_playback_controller
        self._arbiter = ViewportPlaybackArbiter()
        self._arbiter.set_command_sink(self._sink)
        self._connect_controller_feedback()

    # ── 接线 ────────────────────────────────────────────────────────

    def attach_viewport(self, viewport: Any) -> None:
        """按信号名连接前端容器（不 import 其模块，避免耦合提交线）。"""
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
        signal = getattr(self._timeline_controller, "state_changed", None)
        if signal is not None:
            signal.connect(self._on_controller_state_changed)

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
        self._finish(self._arbiter.set_page_mode(PAGE_EDIT))

    def mark_shutdown(self) -> None:
        self._arbiter.mark_shutdown()

    # ── 查询 ────────────────────────────────────────────────────────

    def snapshot(self):
        return self._arbiter.snapshot()

    # ── 内部：命令出口 + 反馈 ───────────────────────────────────────

    def _sink(self, command: ArbiterCommand) -> bool:
        try:
            if isinstance(command, PauseMonitor):
                if command.monitor == SOURCE:
                    self._source_widget.pause()
                else:
                    self._timeline_controller.pause()
            elif isinstance(command, StartMonitor):
                if command.monitor == SOURCE:
                    self._source_widget.play()
                else:
                    self._timeline_controller.play()
            else:  # pragma: no cover - 领域件扩展命令时显式失败
                return False
        except Exception:
            return False
        # 应用后立即反馈（含控制器自发信号之外的兜底），快照 truthful
        self._report_current_states()
        return True

    def _report_current_states(self) -> None:
        for monitor, obj in ((SOURCE, self._source_widget),
                             (TIMELINE, self._timeline_controller)):
            is_playing = getattr(obj, "is_playing", None)
            if is_playing is None:
                continue  # 宿主对象尚未暴露公开播放状态（见 B04 快照）
            speed = getattr(obj, "playback_speed", None)
            direction = getattr(obj, "playback_direction", None)
            direction_name = getattr(direction, "name", None)
            self._arbiter.report_playback_state(
                monitor,
                playing=bool(is_playing),
                direction=str(direction_name or direction or "forward"),
                speed=float(speed) if speed is not None else 1.0)

    def _on_controller_state_changed(self, state: str) -> None:
        # 控制器自发状态变化（播放到末尾、外部 pause 等）也回填
        self._report_current_states()

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
            self.status_message.emit(describe_reason(decision.reason))
        self.snapshot_changed.emit(decision.snapshot)


def install_viewport_playback_coordinator(
        *, viewport: Any, source_widget: Any, timeline_panel: Any,
        parent: Optional[QObject] = None) -> ViewportPlaybackCoordinator:
    """单入口接线：viewport + source widget + timeline panel → 协调器。

    ``timeline_panel`` 需提供 ``get_playback_controller()``（现有
    TimelinePanel 即有）。前端容器合流后，宿主在创建 Viewport 处调用
    本函数一次即可完成 B01 全部接线。
    """
    controller = timeline_panel.get_playback_controller()
    coordinator = ViewportPlaybackCoordinator(
        source_widget, controller, parent=parent)
    coordinator.attach_viewport(viewport)
    return coordinator
