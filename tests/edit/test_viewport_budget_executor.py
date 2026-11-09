"""I06+R03：双视图预算执行器——用户偏好分离、真实执行值、及时更新。

I06（既有契约，R03 后口径更新）：

- 输入来自真实宿主状态（active/hidden/playing/pane 尺寸），经
  ``DualViewBudgetPolicy`` 出决策；
- Timeline ``request_size_cap`` 应用到渲染器**监看域预算面**
  （``set_budget_size_cap``），不再借用户质量选择器
  （``set_preview_quality``）之名写临时值；
- Source 的 queue cap=1 / hidden 门控为结构性已达成（单在飞 + B02），
  执行器核验并记录；Source 请求无尺寸/优先级通道 → 如实标记未执行；
- ``diagnostics()`` 含政策决策 + 实际生效值（渲染器报告的真实目标
  像素）+ 真实计数器（JSON 可序列化）。

R03（2026-11-09 复查）新增验收：

- 手选 Full/Half 不被预算覆盖或写入临时值；解除后回用户档；
- 1080p/4K/8K/竖幅/非整数缩放：实际解码目标像素不超 cap（full/scrub
  旁路档同样受约束）；cap 低于 0.125 档位下限时如实报 unsatisfied；
- pane resize/show/hide → 合并防抖重评（诊断不滞留旧值）；
- 渲染器懒建/替换后限制自动重应用；mark_page_left/mark_shutdown 解除；
- 真实可见性并入 hidden（页面切走/借出后隐藏），离屏环境保留逻辑值。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional, Tuple

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

from src.features.edit.core.timeline_preview_renderer import (
    PreviewRenderWorker, TimelinePreviewRenderer,
)
from src.features.edit.ui.viewport_budget_executor import (
    ViewportBudgetExecutor,
)

pytestmark = pytest.mark.edit_ui


class _Pane(QWidget):
    def __init__(self, width: int = 800, height: int = 450,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.resize(width, height)  # 真实尺寸（executor 读 width()/height()）


class _Viewport(QWidget):
    def __init__(self, active: str = "program", dual: bool = False) -> None:
        super().__init__()
        self.active_monitor = active
        self.dual_view = dual
        self.source_pane = _Pane(800, 450, self)
        self.program_pane = _Pane(800, 450, self)


class _Source:
    def __init__(self) -> None:
        self.current_asset = None
        self.is_playing = False
        self._gpu_pending_frame = -1
        self.gpu_slot_stats = {"decode_submitted": 3, "slots_acked": 2,
                               "results_discarded": 1}


class _Controller:
    def __init__(self) -> None:
        self.is_playing = False


class _Panel:
    def __init__(self) -> None:
        self._controller = _Controller()

    def get_playback_controller(self):
        return self._controller


class _Renderer:
    """I06 双打：预算面记录调用；状态由真实 shell 语义模拟。"""

    def __init__(self, seq: Tuple[int, int] = (3840, 2160),
                 user_quality: float = 0.0) -> None:
        self._seq = seq
        self._user = user_quality
        self._cap: Optional[Tuple[int, int]] = None
        self.budget_calls: list[Optional[Tuple[int, int]]] = []
        self.quality_calls: list[float] = []  # 用户选择器：预算绝不得调用

    def set_budget_size_cap(self, cap) -> None:
        self.budget_calls.append(
            None if cap is None else (int(cap[0]), int(cap[1])))
        self._cap = None if cap is None else (int(cap[0]), int(cap[1]))

    def set_preview_quality(self, scale: float) -> None:
        self.quality_calls.append(float(scale))  # 用户路径（不应被执行器调）

    def preview_quality_preference(self) -> float:
        return float(self._user)

    def budget_size_cap_status(self) -> dict:
        if self._cap is None:
            return {"cap": None, "actual": None, "satisfied": None}
        actual = self._constrained(self._cap)
        return {"cap": [self._cap[0], self._cap[1]],
                "actual": [actual[0], actual[1]],
                "satisfied": (actual[0] <= self._cap[0]
                              and actual[1] <= self._cap[1])}

    def _constrained(self, cap: Tuple[int, int]) -> Tuple[int, int]:
        s = min(cap[0] / self._seq[0], cap[1] / self._seq[1], 1.0)
        s = max(0.125, min(1.0, s))
        dw = max(64, int(round(self._seq[0] * s)))
        dh = max(64, int(round(self._seq[1] * s)))
        return (dw + dw % 2, dh + dh % 2)


def _executor(qtbot: Any, *, viewport=None, renderer=None) -> tuple[
        ViewportBudgetExecutor, _Renderer]:
    renderer = renderer if renderer is not None else _Renderer()
    executor = ViewportBudgetExecutor(
        viewport=viewport or _Viewport(),
        source_widget=_Source(),
        timeline_panel=_Panel(),
        renderer_provider=lambda: renderer)
    return executor, renderer


# ── I06 契约（R03 后口径） ──────────────────────────────────────────

def test_i06_applies_size_cap_via_budget_surface(qtbot):
    executor, renderer = _executor(qtbot)
    diagnostics = executor.diagnostics()
    assert renderer.budget_calls == [(800, 450)]   # 只走预算面
    assert renderer.quality_calls == []            # 用户选择器零调用
    size_cap = diagnostics["applied"]["timeline"]["size_cap"]
    assert size_cap["cap"] == [800, 450]
    assert size_cap["policy"] == [800, 450]
    assert size_cap["user_preference"] == 0.0
    assert diagnostics["timeline"]["policy"]["reason"]


def test_i06_no_cap_without_pane_size(qtbot, qapp):
    viewport = _Viewport()
    viewport.program_pane.resize(0, 0)  # 未布局：不喂假尺寸
    executor, renderer = _executor(qtbot, viewport=viewport)
    # 无 cap → 解除预算（初始 sentinel ≠ None → 一次显式 None）
    assert renderer.budget_calls[-1] is None
    size_cap = executor.diagnostics()["applied"]["timeline"]["size_cap"]
    assert size_cap["cap"] is None and size_cap["policy"] is None


def test_i06_renderer_unavailable_recorded_not_silent(qtbot):
    executor = ViewportBudgetExecutor(
        viewport=_Viewport(), source_widget=_Source(), timeline_panel=_Panel(),
        renderer_provider=lambda: None)
    applied = executor.diagnostics()["applied"]["timeline"]
    assert applied["renderer_attached"] is False
    assert applied["size_cap"] == "renderer-unavailable"


def test_i06_budget_surface_failure_logged_not_crashing(qtbot):
    renderer = _Renderer()

    def _boom(cap):
        raise RuntimeError("budget knob broken")

    renderer.set_budget_size_cap = _boom
    executor = ViewportBudgetExecutor(
        viewport=_Viewport(), source_widget=_Source(), timeline_panel=_Panel(),
        renderer_provider=lambda: renderer)
    applied = executor.diagnostics()["applied"]["timeline"]
    assert applied["size_cap"] is False


def test_i06_source_structural_caps_verified_honestly(qtbot):
    executor, _ = _executor(qtbot)
    applied = executor.diagnostics()["applied"]["source"]
    assert applied["single_flight_verified"] is True
    assert applied["queue_cap"] == {"structural": 1, "policy": 1}
    # R03：无请求通道的约束如实标记未执行，不冒充已应用
    assert applied["request_size_cap"]["enforced"] is False
    assert applied["priority"]["enforced"] is False


def test_i06_diagnostics_serializable_with_counters(qtbot):
    executor, _ = _executor(qtbot)
    data = json.loads(json.dumps(executor.diagnostics()))
    assert data["source"]["counters"]["decode_submitted"] == 3
    assert data["timeline"]["policy"]["reason"]
    assert "applied" in data
    assert data["executor_shutdown"] is False


def test_i06_coordinator_snapshot_refreshes_budget(qtbot, qapp):
    class _Coordinator(QObject):
        snapshot_changed = pyqtSignal(object)

    coordinator = _Coordinator()
    renderer = _Renderer()
    viewport = _Viewport()
    executor = ViewportBudgetExecutor(
        viewport=viewport, source_widget=_Source(), timeline_panel=_Panel(),
        renderer_provider=lambda: renderer,
        coordinator=coordinator)
    viewport.active_monitor = "source"   # timeline 转非激活 → 重评
    coordinator.snapshot_changed.emit(object())
    # R03：防抖合并（60ms）后重评必须落地。cap 未变时不重触碰渲染器
    # （同值 no-op），但输入状态快照必须更新到最新。
    from PyQt6.QtTest import QTest
    QTest.qWait(120)
    assert executor._last_states["source"].active is True
    assert executor._last_states["timeline"].hidden is True
    assert not renderer.quality_calls  # 用户选择器始终零调用


# ── R03：渲染器域——用户偏好分离 + 真实像素不超 cap ─────────────────

class _ShellRenderer(TimelinePreviewRenderer):
    """轻量 shell：绕开重初始化，只覆写会触达未初始化 C++ 态的方法。"""

    def _invalidate_preview_frame_caches(self) -> None:
        pass

    def _clear_prebuffer_state(self) -> None:
        pass

    def _reset_emitted_frame_guard(self) -> None:
        pass

    def _schedule_current_frame_warmup(self) -> None:
        pass


class _ShellWorker(PreviewRenderWorker):
    def _clear_direct_preview_sources(self) -> None:
        pass


def _renderer_shell(seq: Tuple[int, int], *, user_quality: float = 0.0,
                    viewport: Tuple[int, int] = (1920, 1080)):
    """真实 TimelinePreviewRenderer 的轻量 shell（绕开重初始化）。"""
    shell = _ShellRenderer.__new__(_ShellRenderer)
    shell._sequence = SimpleNamespace(width=seq[0], height=seq[1])
    shell._viewport_size = viewport
    shell._preview_quality = user_quality
    shell._budget_size_cap = None
    shell._max_preview_edge = 3840
    shell._scrub_preview_edge = 3840
    shell._decode_quality_stage = "auto2k"
    shell._worker = None
    return shell


def _worker_shell():
    shell = _ShellWorker.__new__(_ShellWorker)
    shell._preview_quality = 0.0
    shell._budget_size_cap = None
    shell._max_preview_edge = 3840
    return shell


class TestBudgetScaleDomain:
    @pytest.mark.parametrize("seq,cap", [
        ((1920, 1080), (1280, 720)),   # 1080p 序列
        ((3840, 2160), (960, 540)),    # 4K
        ((7680, 4320), (1920, 1080)),  # 8K（auto/smooth 也被压到 cap）
        ((2160, 3840), (540, 960)),    # 竖幅
        ((2048, 858), (1000, 500)),    # 非整数缩放
    ])
    def test_auto_mode_actual_pixels_within_cap(self, seq, cap):
        r = _renderer_shell(seq)
        r.set_budget_size_cap(cap)
        target = r.preview_decode_target_size()
        assert target[0] <= cap[0] and target[1] <= cap[1]
        assert r.budget_size_cap_status()["satisfied"] is True

    def test_user_full_preference_never_overwritten_and_released(self):
        r = _renderer_shell((3840, 2160), user_quality=1.0)  # 用户手选 Full
        r.set_budget_size_cap((960, 540))
        assert r.preview_quality_preference() == 1.0          # 偏好不动
        target = r.preview_decode_target_size()
        assert target == (960, 540)                           # 预算仍生效
        r.set_budget_size_cap(None)                           # 解除
        assert r.preview_quality_preference() == 1.0
        assert r._effective_preview_quality_scale(3840, 2160) == 1.0

    def test_8k_user_full_capped_by_preview_edge_and_budget(self):
        r = _renderer_shell((7680, 4320), user_quality=1.0)
        assert r._effective_preview_quality_scale(7680, 4320) == 0.5  # 4K 顶
        r.set_budget_size_cap((1920, 1080))
        assert r._effective_preview_quality_scale(7680, 4320) == 0.25

    def test_full_stage_and_scrub_targets_also_constrained(self):
        r = _renderer_shell((3840, 2160))
        r.set_budget_size_cap((960, 540))
        r._decode_quality_stage = "full"    # 暂停 FULL 刷新档
        assert r._decode_target_size <= (960, 540)
        scrub = r._scrub_decode_target_size
        assert scrub[0] <= 960 and scrub[1] <= 540

    def test_tiny_cap_below_tier_floor_reports_unsatisfied(self):
        r = _renderer_shell((3840, 2160))
        r.set_budget_size_cap((200, 112))   # < 0.125×序列（480×270）
        status = r.budget_size_cap_status()
        assert status["satisfied"] is False  # 如实不谎报
        assert status["actual"][0] >= 480    # 档位下限兜底

    def test_same_cap_is_noop_and_worker_propagates(self):
        r = _renderer_shell((1920, 1080))
        r.set_budget_size_cap((960, 540))
        again = r._budget_size_cap
        r.set_budget_size_cap((960, 540))
        assert r._budget_size_cap is again
        w = _worker_shell()
        w.set_budget_size_cap((960, 540))
        assert w._effective_preview_quality_scale(1920, 1080) == 0.5


# ── R03：执行器生命周期与更新时机 ────────────────────────────────────

class TestExecutorLifecycleR03:
    def test_pane_resize_updates_diagnostics_promptly(self, qtbot, qapp):
        viewport = _Viewport()
        qtbot.addWidget(viewport)
        viewport.show()
        qtbot.waitUntil(lambda: viewport.isVisible(), timeout=2000)
        renderer = _Renderer(seq=(3840, 2160))
        executor = ViewportBudgetExecutor(
            viewport=viewport, source_widget=_Source(),
            timeline_panel=_Panel(), renderer_provider=lambda: renderer)
        assert renderer.budget_calls == [(800, 450)]

        viewport.program_pane.resize(960, 540)
        qtbot.waitUntil(
            lambda: renderer.budget_calls
            and renderer.budget_calls[-1] == (960, 540), timeout=2000)
        size_cap = executor.diagnostics()["applied"]["timeline"]["size_cap"]
        assert size_cap["cap"] == [960, 540]
        assert size_cap["actual"][0] <= 960 and size_cap["actual"][1] <= 540

    def test_page_left_releases_budget_user_pref_intact(self, qtbot):
        viewport = _Viewport()
        renderer = _Renderer(seq=(3840, 2160), user_quality=1.0)
        executor = ViewportBudgetExecutor(
            viewport=viewport, source_widget=_Source(),
            timeline_panel=_Panel(), renderer_provider=lambda: renderer)
        executor.mark_page_left()
        assert renderer.budget_calls[-1] is None
        assert renderer.quality_calls == []
        assert renderer.preview_quality_preference() == 1.0

    def test_shutdown_stops_updates_and_releases(self, qtbot, qapp):
        class _Coordinator(QObject):
            snapshot_changed = pyqtSignal(object)

        coordinator = _Coordinator()
        viewport = _Viewport()
        renderer = _Renderer()
        executor = ViewportBudgetExecutor(
            viewport=viewport, source_widget=_Source(),
            timeline_panel=_Panel(), renderer_provider=lambda: renderer,
            coordinator=coordinator)
        executor.mark_shutdown()
        assert renderer.budget_calls[-1] is None
        assert executor.diagnostics()["executor_shutdown"] is True

        calls = len(renderer.budget_calls)
        coordinator.snapshot_changed.emit(object())
        viewport.program_pane.resize(400, 300)
        from PyQt6.QtTest import QTest
        QTest.qWait(120)
        assert len(renderer.budget_calls) == calls  # 关门后零更新

    def test_renderer_replacement_reapplies_cap(self, qtbot, qapp):
        viewport = _Viewport()
        current: dict[str, _Renderer] = {"r": _Renderer(seq=(3840, 2160))}
        executor = ViewportBudgetExecutor(
            viewport=viewport, source_widget=_Source(),
            timeline_panel=_Panel(),
            renderer_provider=lambda: current["r"])
        assert current["r"].budget_calls == [(800, 450)]

        current["r"] = _Renderer(seq=(3840, 2160))  # 懒建/替换
        executor.refresh()
        assert current["r"].budget_calls == [(800, 450)]  # 限制重应用

    def test_hidden_uses_real_visibility_when_window_shown(self, qtbot, qapp):
        viewport = _Viewport(active="source", dual=True)
        executor = ViewportBudgetExecutor(
            viewport=viewport, source_widget=_Source(),
            timeline_panel=_Panel(), renderer_provider=lambda: _Renderer())
        # 整窗未显示（离屏构造）：保留逻辑值（dual+激活 → not hidden）
        assert executor._source_state().hidden is False

        viewport.show()
        qtbot.waitUntil(lambda: viewport.isVisible(), timeout=2000)
        viewport.source_pane.hide()
        executor.refresh()
        # 窗在屏上而 pane 被隐藏（页面切走/借出后隐藏）→ hidden
        assert executor._source_state().hidden is True

    def test_source_explicit_seek_unaffected_by_policy(self, qtbot):
        """预算 allow_periodic_requests=False 只约束周期请求；Source 精确
        一次性 seek（B02 显式路径）不经执行器，不受关门影响。"""
        viewport = _Viewport()
        source = _Source()
        executor = ViewportBudgetExecutor(
            viewport=viewport, source_widget=source,
            timeline_panel=_Panel(), renderer_provider=lambda: _Renderer())
        executor.mark_shutdown()
        before = dict(source.gpu_slot_stats)
        executor.refresh()  # 关门后 no-op：不伪造计数、不拦截请求面
        assert source.gpu_slot_stats == before
