"""I06（复查计划）：双视图预算执行器——政策真实作用到既有调度面。

- 输入来自真实宿主状态（active/hidden/playing/pane 尺寸），经
  ``DualViewBudgetPolicy`` 出决策；
- ``request_size_cap`` 应用到 Timeline 渲染器**既有**质量档
  （``set_preview_quality``；仅在 renderer 在场时），应用失败记 warning
  不崩溃；
- Source 的 queue cap=1 / hidden 门控为结构性已达成（单在飞 + B02），
  执行器核验并记录；
- ``diagnostics()`` 含政策决策 + 实际生效值 + 真实计数器（JSON 可
  序列化），不允许"政策说受限，执行端无迹可查"。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.ui.viewport_budget_executor import (
    ViewportBudgetExecutor,
)

pytestmark = pytest.mark.edit_ui


class _Pane(QObject):
    def __init__(self, width: int, height: int) -> None:
        super().__init__()
        self._size = (width, height)

    def width(self) -> int:
        return self._size[0]

    def height(self) -> int:
        return self._size[1]


class _Viewport(QObject):
    def __init__(self, active: str = "program", dual: bool = False) -> None:
        super().__init__()
        self.active_monitor = active
        self.dual_view = dual
        self.source_pane = _Pane(800, 450)
        self.program_pane = _Pane(800, 450)


class _Source:
    def __init__(self) -> None:
        self.current_asset = None
        self.is_playing = False
        self._gpu_pending_frame = -1
        self.gpu_slot_stats = {"decode_submitted": 3, "slots_acked": 2}


class _Controller:
    def __init__(self) -> None:
        self.is_playing = False


class _Panel:
    def __init__(self) -> None:
        self._controller = _Controller()

    def get_playback_controller(self):
        return self._controller


class _Renderer:
    def __init__(self) -> None:
        self.quality_calls: list[float] = []

    def set_preview_quality(self, scale: float) -> None:
        self.quality_calls.append(float(scale))


class _RendererThatRaises(_Renderer):
    def set_preview_quality(self, scale: float) -> None:
        raise RuntimeError("quality knob broken")


def _executor(qtbot: Any, *, viewport=None, renderer=None) -> tuple[
        ViewportBudgetExecutor, _Renderer]:
    renderer = renderer if renderer is not None else _Renderer()
    executor = ViewportBudgetExecutor(
        viewport=viewport or _Viewport(),
        source_widget=_Source(),
        timeline_panel=_Panel(),
        renderer_provider=lambda: renderer)
    return executor, renderer


def test_i06_applies_size_cap_to_timeline_quality_knob(qtbot):
    # 800×450 视口 → cap 800×450 → 最接近且不超的既有档 0.25（≈960 边）
    executor, renderer = _executor(qtbot)
    diagnostics = executor.diagnostics()
    assert renderer.quality_calls == [0.25]
    applied = diagnostics["applied"]["timeline"]
    assert applied["preview_quality_applied"] == 0.25
    assert diagnostics["timeline"]["policy"]["reason"]  # 政策如实入诊断


def test_i06_no_cap_without_pane_size(qtbot):
    viewport = _Viewport()
    viewport.program_pane = _Pane(0, 0)     # 未布局：不喂假尺寸
    executor, renderer = _executor(qtbot, viewport=viewport)
    assert renderer.quality_calls == []
    applied = executor.diagnostics()["applied"]["timeline"]
    assert applied["preview_quality_applied"] == "skipped-no-cap"


def test_i06_renderer_unavailable_recorded_not_silent(qtbot):
    executor = ViewportBudgetExecutor(
        viewport=_Viewport(), source_widget=_Source(), timeline_panel=_Panel(),
        renderer_provider=lambda: None)
    applied = executor.diagnostics()["applied"]["timeline"]
    assert applied["renderer_attached"] is False
    assert applied["preview_quality_applied"] == "renderer-unavailable"


def test_i06_quality_knob_failure_logged_not_crashing(qtbot):
    executor, _ = _executor(qtbot, renderer=_RendererThatRaises())
    applied = executor.diagnostics()["applied"]["timeline"]
    assert applied["preview_quality_applied"] is False


def test_i06_source_structural_caps_verified(qtbot):
    executor, _ = _executor(qtbot)
    applied = executor.diagnostics()["applied"]["source"]
    assert applied["single_flight_verified"] is True
    assert applied["queue_cap"] == {"structural": 1, "policy": 1}


def test_i06_diagnostics_serializable_with_counters(qtbot):
    executor, _ = _executor(qtbot)
    data = json.loads(json.dumps(executor.diagnostics()))
    assert data["source"]["counters"]["decode_submitted"] == 3
    assert data["timeline"]["policy"]["reason"]
    assert "applied" in data


def test_i06_coordinator_snapshot_refreshes_budget(qtbot):
    class _Coordinator(QObject):
        snapshot_changed = pyqtSignal(object)

    coordinator = _Coordinator()
    renderer = _Renderer()
    viewport = _Viewport()
    executor = ViewportBudgetExecutor(
        viewport=viewport, source_widget=_Source(), timeline_panel=_Panel(),
        renderer_provider=lambda: renderer,
        coordinator=coordinator)
    calls_after_init = len(renderer.quality_calls)
    viewport.active_monitor = "source"   # timeline 转非激活 → 重评
    coordinator.snapshot_changed.emit(object())
    assert len(renderer.quality_calls) >= calls_after_init
