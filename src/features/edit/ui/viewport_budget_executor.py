"""I06+R03：双视图预算执行器——把政策真实作用到既有调度器。

VP-B03 交付的 ``DualViewBudgetPolicy`` 只出 DTO；本模块是它的**执行端**
（QObject，宿主装配），周期性与事件驱动地：

1. 收集真实输入（I01 协调器快照：布局/激活/两路播放；pane 尺寸与真实
   可见性；渲染器在场），构造 ``MonitorLoadState`` 喂给政策；
2. **应用**决策到既有调度面：
   - Timeline ``request_size_cap`` → 渲染器**监看域预算面**
     （``set_budget_size_cap``，R03 与用户预览质量偏好严格分离——绝
     不再借 ``set_preview_quality`` 之名覆盖用户的 Full/Half 选择）；
     诊断记录渲染器报告的**实际**目标像素与是否满足 cap，而非 setter
     调用次数；
   - Source 的 queue cap=1 与 hidden 门控是**结构性已达成**（CPU worker
     单槽 latest-wins、GPU 单在飞 ``_gpu_pending_frame``、B02 hidden
     调度/show 单帧刷新）——执行器核验并如实记录；Source 请求无尺寸/
     优先级通道，如实标记 ``enforced: False``（不拿结构推断冒充执行）；
3. R03 触发面：协调器快照、pane resize/show/hide（事件过滤＋合并防抖）、
   渲染器懒建/替换后的下一次 refresh；``mark_shutdown`` 后一切更新停止
   并解除预算（精确暂停 seek 走 B02 显式路径，不受影响）；
4. 输出可查询诊断（政策决策 + 实际生效值 + 真实计数器），应用失败记
   warning——不允许"政策说受限，执行端原样跑"却无迹可查。

真实 4K/HDR 性能验收（呈现率、p95、10 分钟内存）不在此模块承诺范围；
本模块承诺的是：限制**可观测且真实生效于既有队列/分辨率面**。
"""

from __future__ import annotations

import logging
import weakref
from typing import Any, Callable, Optional, Tuple

from PyQt6.QtCore import QEvent, QObject, QTimer

from src.features.edit.core.viewport_playback_budget import (
    MonitorLoadState,
    collect_dual_view_diagnostics,
)

logger = logging.getLogger(__name__)

_REFRESH_DEBOUNCE_MS = 60  # 连续 resize 合并为一次重评（不丢尾包）


class ViewportBudgetExecutor(QObject):
    """政策 → 既有调度面的执行适配（宿主持有；输入变化时 refresh）。"""

    def __init__(self, *, viewport: Any, source_widget: Any,
                 timeline_panel: Any,
                 renderer_provider: Callable[[], Any],
                 coordinator: Any = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._viewport = viewport
        self._source_widget = source_widget
        self._timeline_panel = timeline_panel
        self._renderer_provider = renderer_provider
        self._last_decisions: tuple[Any, ...] = ()
        self._last_states: dict[str, MonitorLoadState] = {}
        self._applied: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, dict[str, int]] = {"source": {}, "timeline": {}}
        self._last_applied_cap: Optional[Tuple[int, int]] = ...
        self._last_budget_renderer: Any = None  # weakref→渲染器（替换后强制重应用）
        self._shutdown = False
        self._watched_panes: list[Any] = []
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_REFRESH_DEBOUNCE_MS)
        self._refresh_timer.timeout.connect(self.refresh)
        if coordinator is not None:
            # I01 快照变化（布局/激活/两路播放）→ 重评预算
            coordinator.snapshot_changed.connect(
                lambda _snap: self._schedule_refresh())
        self._install_pane_watchers()
        self.refresh()

    # ── 生命周期 ─────────────────────────────────────────────────────

    def mark_shutdown(self) -> None:
        """R03：关门——停更新、摘事件过滤、解除预算（幂等）。

        关闭后 refresh/调度均 no-op；用户预览质量偏好从未被触碰，预算
        解除后渲染器自动回到用户偏好。精确暂停 seek（B02 显式路径）不
        经过本执行器，不受关门影响。
        """
        if self._shutdown:
            return
        self._shutdown = True
        self._refresh_timer.stop()
        for pane in self._watched_panes:
            try:
                pane.removeEventFilter(self)
            except Exception:
                pass
        self._watched_panes = []
        self._release_budget("shutdown")

    def mark_page_left(self) -> None:
        """R03：离开宿主页（Edit/Deliver 借还）——解除监看域预算。

        用户预览质量偏好从未被触碰，预算解除即回到用户选择；页面重新
        可见时 pane Show 事件 → refresh 重评（Deliver 借还路径自愈）。
        """
        if self._shutdown:
            return
        self._release_budget("page-left")

    def _install_pane_watchers(self) -> None:
        """R03：pane resize/show/hide → 合并防抖重评（诊断不再滞留旧 cap）。"""
        for pane in (getattr(self._viewport, "source_pane", None),
                     getattr(self._viewport, "program_pane", None),
                     self._viewport):
            if pane is None:
                continue
            try:
                pane.installEventFilter(self)
                self._watched_panes.append(pane)
            except Exception:
                logger.debug("budget executor: pane watcher install failed",
                             exc_info=True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (event.type() in (QEvent.Type.Resize, QEvent.Type.Show,
                             QEvent.Type.Hide, QEvent.Type.ShowToParent,
                             QEvent.Type.HideToParent)):
            self._schedule_refresh()
        return super().eventFilter(watched, event)

    def _schedule_refresh(self) -> None:
        if self._shutdown:
            return
        self._refresh_timer.start()  # 单例定时器：连续事件合并为一次

    # ── 输入收集 ─────────────────────────────────────────────────────

    @staticmethod
    def _pane_size(pane: Any) -> Optional[tuple[int, int]]:
        if pane is None or not hasattr(pane, "width"):
            return None
        width, height = int(pane.width()), int(pane.height())
        if width <= 0 or height <= 0:
            return None  # 未布局/离屏未显示：不给政策喂假尺寸
        return (width, height)

    @staticmethod
    def _effective_hidden(logical_hidden: bool, pane: Any) -> bool:
        """R03：真实可见性并入 hidden 输入（含 Deliver 借走/页面切换）。

        顶层窗口在屏而 pane 不可见（页面切走、借出后隐藏）→ hidden；
        整窗未显示（离屏/测试环境）时保留逻辑推导值，不误杀。
        """
        if logical_hidden or pane is None:
            return logical_hidden
        is_visible = getattr(pane, "isVisible", None)
        if not callable(is_visible):
            return logical_hidden
        try:
            if is_visible():
                return False
            window = getattr(pane, "window", None)
            window_visible = (bool(window().isVisible())
                              if callable(window) else True)
        except RuntimeError:  # C++ 对象已销毁
            return logical_hidden
        return bool(window_visible)

    def _source_state(self) -> MonitorLoadState:
        active = str(getattr(self._viewport, "active_monitor", "program")) \
            == "source"
        dual = bool(getattr(self._viewport, "dual_view", False))
        pane = getattr(self._viewport, "source_pane", None)
        # 单视图下非激活 pane 会被容器隐藏；dual 时跟随 pane 实际可见性
        hidden = (not dual and not active)
        if pane is not None and dual and hasattr(pane, "isHidden"):
            hidden = bool(pane.isHidden())
        hidden = self._effective_hidden(hidden, pane)
        asset = getattr(self._source_widget, "current_asset", None)
        native = None
        width = getattr(asset, "width", None)
        height = getattr(asset, "height", None)
        if width and height:
            native = (int(width), int(height))
        return MonitorLoadState(
            monitor="source", active=active,
            playing=bool(getattr(self._source_widget, "is_playing", False)),
            hidden=hidden, viewport_size=self._pane_size(pane),
            native_size=native)

    def _timeline_state(self) -> MonitorLoadState:
        active = str(getattr(self._viewport, "active_monitor", "program")) \
            == "program"
        dual = bool(getattr(self._viewport, "dual_view", False))
        pane = getattr(self._viewport, "program_pane", None)
        hidden = self._effective_hidden(not dual and not active, pane)
        controller = None
        get_controller = getattr(self._timeline_panel,
                                 "get_playback_controller", None)
        if callable(get_controller):
            controller = get_controller()
        return MonitorLoadState(
            monitor="timeline", active=active,
            playing=bool(getattr(controller, "is_playing", False)),
            hidden=hidden,
            viewport_size=self._pane_size(pane))

    # ── 决策与应用 ───────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._shutdown:
            return
        from src.features.edit.core.viewport_playback_budget import (
            DualViewBudgetPolicy,
        )
        timeline_state, source_state = self._timeline_state(), \
            self._source_state()
        decisions = DualViewBudgetPolicy().decide(
            timeline=timeline_state, source=source_state)
        self._last_decisions = decisions
        self._last_states = {
            "timeline": timeline_state, "source": source_state}
        self._collect_counters()
        self._apply(decisions)

    def _apply(self, decisions: tuple) -> None:
        applied = {"timeline": {}, "source": {}}
        for decision in decisions:
            if decision.monitor == "timeline":
                applied["timeline"] = self._apply_timeline(decision)
            else:
                applied["source"] = self._apply_source(decision)
        self._applied = applied

    def _apply_timeline(self, decision: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "queue_cap": {"structural": 2, "policy": decision.queue_cap},
            # 面板可见性守卫（_on_timer_frame_ready）已按可见性门控渲染，
            # hidden 政策由既有守卫执行
            "hidden_gate": "panel-visibility-guard",
        }
        renderer = self._safe_renderer()
        budget_setter = getattr(renderer, "set_budget_size_cap", None)
        if renderer is None or not callable(budget_setter):
            record["renderer_attached"] = False
            record["size_cap"] = "renderer-unavailable"
            logger.warning(
                "budget executor: timeline decision (%s) has no renderer "
                "budget surface to apply size cap", decision.reason)
            return record
        record["renderer_attached"] = True
        cap = decision.request_size_cap
        cap_tuple = (int(cap[0]), int(cap[1])) if cap else None
        # 只在有效上限变化或渲染器实例变化（懒建/替换）时触碰渲染器
        # （同值 no-op，避免反复废缓存）。
        prev_renderer = (self._last_budget_renderer()
                         if callable(self._last_budget_renderer) else None)
        if cap_tuple != self._last_applied_cap or prev_renderer is not renderer:
            try:
                budget_setter(cap_tuple)
                self._last_applied_cap = cap_tuple
                self._last_budget_renderer = (
                    weakref.ref(renderer) if renderer is not None else None)
            except Exception:
                logger.warning(
                    "budget executor: set_budget_size_cap(%s) failed",
                    cap_tuple, exc_info=True)
                record["size_cap"] = False
                return record
        # R03：记录真实生效值——渲染器报告的实际目标像素与达标与否，
        # 以及未被触碰的用户偏好（菜单据此显示真实选择＋降级原因）。
        status = renderer.budget_size_cap_status()
        record["size_cap"] = {
            "policy": list(cap_tuple) if cap_tuple else None,
            "cap": status.get("cap"),
            "actual": status.get("actual"),
            "satisfied": status.get("satisfied"),
            "user_preference": renderer.preview_quality_preference(),
            "note": ("budget constrains only; user preview-quality "
                     "preference is never overwritten"),
        }
        if status.get("satisfied") is False:
            logger.warning(
                "budget executor: size cap %s cannot be satisfied "
                "(actual %s; 0.125 tier floor)",
                status.get("cap"), status.get("actual"))
        return record

    def _apply_source(self, decision: Any) -> dict[str, Any]:
        # 结构性已达成：CPU worker 单槽 latest-wins + GPU 单在飞
        # （_gpu_pending_frame）→ queue cap 1 为既成事实；hidden 门控与
        # show 单帧刷新由 B02 落地。此处核验并记录，不重复门控。
        single_flight_surface = hasattr(self._source_widget,
                                         "_gpu_pending_frame")
        if not single_flight_surface:
            logger.warning(
                "budget executor: source widget lacks the single-flight "
                "surface; queue cap %s unenforced", decision.queue_cap)
        cap = decision.request_size_cap
        return {
            "queue_cap": {"structural": 1, "policy": decision.queue_cap},
            "single_flight_verified": bool(single_flight_surface),
            "hidden_gate": "b02-hidden-scheduling",
            # R03：Source 解码请求不携带尺寸/优先级通道——如实标记未执行，
            # 不拿结构存在性冒充已应用（真实申请/ack 计数见 counters）。
            "request_size_cap": {
                "policy": [int(cap[0]), int(cap[1])] if cap else None,
                "enforced": False,
                "surface": "absent: source decode request carries no "
                           "size hint",
            },
            "priority": {
                "policy": decision.priority,
                "enforced": False,
                "surface": "absent: source requests are not "
                           "priority-tagged",
            },
        }

    def _release_budget(self, reason: str) -> None:
        """解除 Timeline 渲染器上的监看域预算（离开/关闭；用户偏好复位）。"""
        renderer = self._safe_renderer()
        budget_setter = getattr(renderer, "set_budget_size_cap", None)
        if callable(budget_setter):
            try:
                budget_setter(None)
            except Exception:
                logger.warning("budget executor: release budget failed (%s)",
                               reason, exc_info=True)
        self._last_applied_cap = None
        self._last_budget_renderer = None

    @staticmethod
    def _quality_scale_for(cap) -> Optional[float]:
        """request_size_cap → 既有质量档（遗留接口，仅供旧诊断对照）。"""
        if not cap:
            return None
        cap_edge = max(int(cap[0]), int(cap[1]))
        if cap_edge >= 1920:
            return 0.0
        if cap_edge >= 1280:
            return 0.5
        if cap_edge >= 960:
            return 0.333
        return 0.25

    def _safe_renderer(self) -> Any:
        try:
            return self._renderer_provider()
        except Exception:
            logger.debug("budget executor: renderer provider failed",
                         exc_info=True)
            return None

    # ── 计数器与诊断 ─────────────────────────────────────────────────

    def _collect_counters(self) -> None:
        stats = getattr(self._source_widget, "gpu_slot_stats", None)
        if isinstance(stats, dict):
            self._counters["source"] = {
                k: int(v) for k, v in stats.items()
                if isinstance(v, (int, float))}

    def diagnostics(self) -> dict[str, Any]:
        """可查询诊断：政策决策 + 实际生效值 + 真实计数器（JSON 可序列化）。"""
        base = collect_dual_view_diagnostics(
            self._last_decisions, self._counters, self._last_states)
        base["applied"] = self._applied
        base["executor_shutdown"] = self._shutdown
        return base
