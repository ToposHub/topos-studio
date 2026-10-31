"""
Batch 6 tests: PlaybackBudgetController back-pressure。
对应缺陷计划 RP-P1-14 / §4。

核心验收：
- 每帧渲染有 deadline（基于 fps 预算）。
- 连续超预算触发 back-pressure（drop/hold）。
- back-pressure 时 before_render 返回 False。
- budget 内的渲染解除 back-pressure。
- in-flight 渲染未完成时新请求被 drop（防并发雪崩）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    EngineState,
    PlaybackMode,
    RealtimePlaybackEngine,
)
from src.features.edit.core.realtime_playback.playback_budget import (
    PlaybackBudgetController,
)


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


# =============================================================================
# BudgetController 单元测试
# =============================================================================


class TestBudgetController:
    def test_budget_based_on_fps(self) -> None:
        b = PlaybackBudgetController(target_fps=25.0)
        # 25fps → 40ms/帧，留 20% 余量 → 32ms
        assert 30.0 <= b.budget_ms <= 34.0

    def test_set_target_fps_updates_budget(self) -> None:
        b = PlaybackBudgetController(target_fps=25.0)
        b.set_target_fps(60.0)
        # 60fps → 16.67ms，留 20% → ~13.3ms
        assert 12.0 <= b.budget_ms <= 15.0

    def test_before_render_allows_when_idle(self) -> None:
        b = PlaybackBudgetController(target_fps=25.0)
        assert b.before_render(0) is True

    def test_in_flight_blocks_second_render(self) -> None:
        """已有 in-flight 渲染时新请求被 drop（防并发雪崩）。"""
        b = PlaybackBudgetController(target_fps=25.0)
        assert b.before_render(0) is True  # 开始渲染
        assert b.before_render(1) is False  # in-flight，drop
        b.after_render(0, start_ns=time.time_ns(), presented=True)
        assert b.before_render(2) is True  # 完成后可继续

    def test_over_budget_triggers_pressure(self) -> None:
        """连续超预算触发 back-pressure。"""
        b = PlaybackBudgetController(
            target_fps=1000.0,  # 极高 fps → 极小预算，容易超
            over_budget_threshold=2,
        )
        # 模拟两次超预算渲染
        for i in range(2):
            ok = b.before_render(i)
            assert ok
            # 模拟慢渲染（sleep 超过预算）
            time.sleep(0.01)
            b.after_render(i, start_ns=time.time_ns() - 10_000_000, presented=True)
        assert b.is_back_pressured

    def test_pressure_drops_subsequent_renders(self) -> None:
        """back-pressure 时 before_render 返回 False。"""
        b = PlaybackBudgetController(target_fps=1000.0, over_budget_threshold=1)
        ok = b.before_render(0)
        assert ok
        b.after_render(0, start_ns=time.time_ns() - 10_000_000, presented=True)
        assert b.is_back_pressured
        assert b.before_render(1) is False  # drop
        assert b.stats()["total_drops"] >= 1

    def test_pressure_probe_is_delayed_but_eventually_admitted(self) -> None:
        """压力状态既要真正丢帧，也要定期放行探测帧以便恢复。"""
        b = PlaybackBudgetController(target_fps=1000.0, over_budget_threshold=1)
        assert b.before_render(0) is True
        b.after_render(0, start_ns=time.time_ns() - 10_000_000, presented=True)
        assert b.is_back_pressured

        assert b.before_render(1) is False
        assert b.before_render(2) is True
        b.after_render(2, start_ns=time.time_ns(), presented=True)
        assert not b.is_back_pressured

    def test_fast_render_relieves_pressure(self) -> None:
        """回到预算内解除 back-pressure。"""
        b = PlaybackBudgetController(target_fps=1000.0, over_budget_threshold=1)
        # 触发 pressure
        b.before_render(0)
        b.after_render(0, start_ns=time.time_ns() - 10_000_000, presented=True)
        assert b.is_back_pressured
        b.reset_pressure()
        assert not b.is_back_pressured

    def test_stats_tracking(self) -> None:
        b = PlaybackBudgetController(target_fps=25.0)
        b.before_render(0)
        b.after_render(0, start_ns=time.time_ns(), presented=True)
        s = b.stats()
        assert s["total_renders"] == 1
        assert "avg_ms" in s
        assert "p95_ms" in s


# =============================================================================
# Engine 集成 back-pressure
# =============================================================================


class TestEngineBudgetIntegration:
    def test_engine_has_budget(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        assert engine.budget is not None
        assert engine.budget.budget_ms > 0

    def test_engine_drops_on_pressure(self, qapp) -> None:
        """back-pressure 时 request_present 返回 False。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True)
        engine.set_mode(PlaybackMode.REALTIME)
        # 手动触发 back-pressure
        engine.budget._back_pressure = True
        ok = engine.request_present(0)
        assert ok is False
        assert engine.stats()["total_misses"] >= 1

    def test_engine_relieves_pressure_on_seek(self, qapp) -> None:
        """seek 后重置 back-pressure。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True)
        engine.set_mode(PlaybackMode.REALTIME)
        engine.budget._back_pressure = True
        engine.seek(100)
        assert not engine.budget.is_back_pressured

    def test_engine_records_back_pressure_miss(self, qapp) -> None:
        """back-pressure miss 在 telemetry 记录 reason=back_pressure。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True)
        engine.set_mode(PlaybackMode.REALTIME)
        engine.budget._back_pressure = True
        traces = []
        engine.telemetry_recorded.connect(traces.append)
        engine.request_present(0)
        assert traces[0].miss_reason == "back_pressure"
