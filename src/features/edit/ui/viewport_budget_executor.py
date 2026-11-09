"""I06（复查计划）：双视图预算执行器——把政策真实作用到既有调度器。

VP-B03 交付的 ``DualViewBudgetPolicy`` 只出 DTO；本模块是它的**执行端**
（QObject，宿主装配），周期性与事件驱动地：

1. 收集真实输入（I01 协调器快照：布局/激活/两路播放；pane 可见性；
   渲染器在场），构造 ``MonitorLoadState`` 喂给政策；
2. **应用**决策到既有调度面：
   - ``request_size_cap`` → Timeline ``TimelinePreviewRenderer.
     set_preview_quality``（按 viewport/native 比例，只在变化时调用，
     避免反复作废缓存）；
   - Source 的 queue cap=1 与 hidden 门控是**结构性已达成**（CPU worker
     单槽 latest-wins、GPU 单在飞 ``_gpu_pending_frame``、B02 hidden
     调度/show 单帧刷新）——执行器核验并如实记录"applied"，不重复门控；
   - Timeline 渲染队列（maxsize=2 + 优先级逐出）与面板可见性守卫同样
     是既有执行面，记录为 applied；
3. 输出可查询诊断（政策决策 + 实际生效值 + 真实计数器），应用失败记
   warning——不允许"政策说受限，执行端原样跑"却无迹可查。

真实 4K/HDR 性能验收（呈现率、p95、10 分钟内存）不在此模块承诺范围；
本模块承诺的是：限制**可观测且真实生效于既有队列/分辨率面**。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject

from src.features.edit.core.viewport_playback_budget import (
    MonitorLoadState,
    collect_dual_view_diagnostics,
)

logger = logging.getLogger(__name__)


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
        if coordinator is not None:
            # I01 快照变化（布局/激活/两路播放）→ 重评预算
            coordinator.snapshot_changed.connect(
                lambda _snap: self.refresh())
        self.refresh()

    # ── 输入收集 ────────────────────────────────────────────────────

    @staticmethod
    def _pane_size(pane: Any) -> Optional[tuple[int, int]]:
        if pane is None or not hasattr(pane, "width"):
            return None
        width, height = int(pane.width()), int(pane.height())
        if width <= 0 or height <= 0:
            return None  # 未布局/离屏未显示：不给政策喂假尺寸
        return (width, height)

    def _source_state(self) -> MonitorLoadState:
        active = str(getattr(self._viewport, "active_monitor", "program")) \
            == "source"
        dual = bool(getattr(self._viewport, "dual_view", False))
        pane = getattr(self._viewport, "source_pane", None)
        # 单视图下非激活 pane 会被容器隐藏；dual 时跟随 pane 实际可见性
        hidden = (not dual and not active)
        if pane is not None and dual and hasattr(pane, "isHidden"):
            hidden = bool(pane.isHidden())
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
        hidden = not dual and not active
        controller = None
        get_controller = getattr(self._timeline_panel,
                                 "get_playback_controller", None)
        if callable(get_controller):
            controller = get_controller()
        return MonitorLoadState(
            monitor="timeline", active=active,
            playing=bool(getattr(controller, "is_playing", False)),
            hidden=hidden,
            viewport_size=self._pane_size(
                getattr(self._viewport, "program_pane", None)))

    # ── 决策与应用 ──────────────────────────────────────────────────

    def refresh(self) -> None:
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
        if renderer is None or not callable(
                getattr(renderer, "set_preview_quality", None)):
            record["renderer_attached"] = False
            record["preview_quality_applied"] = "renderer-unavailable"
            logger.warning(
                "budget executor: timeline decision (%s) has no renderer "
                "surface to apply size cap", decision.reason)
            return record
        record["renderer_attached"] = True
        cap = decision.request_size_cap
        scale = self._quality_scale_for(cap)
        if scale is None:
            record["preview_quality_applied"] = "skipped-no-cap"
            return record
        try:
            renderer.set_preview_quality(scale)
            record["preview_quality_applied"] = scale
        except Exception:
            logger.warning("budget executor: set_preview_quality(%s) failed",
                           scale, exc_info=True)
            record["preview_quality_applied"] = False
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
        return {
            "queue_cap": {"structural": 1, "policy": decision.queue_cap},
            "single_flight_verified": bool(single_flight_surface),
            "hidden_gate": "b02-hidden-scheduling",
        }

    @staticmethod
    def _quality_scale_for(cap) -> Optional[float]:
        """request_size_cap → 既有质量档（0=auto2K/0.5/0.333/0.25）。"""
        if not cap:
            return None
        cap_edge = max(int(cap[0]), int(cap[1]))
        if cap_edge >= 1920:
            return 0.0          # auto：2K 目标（不高于 cap 的既有档）
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

    # ── 计数器与诊断 ────────────────────────────────────────────────

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
        return base
