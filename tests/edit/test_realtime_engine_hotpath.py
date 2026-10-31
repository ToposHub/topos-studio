"""
Batch 3 tests: RealtimePlaybackEngine 唯一热路径入口。
对应缺陷计划 RP-P0-05 / §3.2。

验证：
- engine.request_present 是播放热路径唯一入口。
- engine 状态机 IDLE->READY->PLAYING->REBUFFERING。
- mode 切换（realtime/paused）正确。
- miss/hold/rebuffer 处理。
- seek 递增 generation。
- telemetry 记录。
- master fallback 在 realtime 模式被 policy 禁止（通过 engine 决策路径）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    EngineState,
    PlaybackMode,
    RealtimePlaybackEngine,
)
from src.features.edit.core.realtime_playback.policy import RealtimePlaybackPolicy
from src.features.edit.core.edit_playback_controller import EditPlaybackController
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


# =============================================================================
# 状态机 / mode
# =============================================================================


class TestEngineStateMachine:
    def test_initial_state_idle(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        assert engine.state is EngineState.IDLE

    def test_configure_transitions_to_ready(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        assert engine.state is EngineState.READY

    def test_set_mode_realtime_transitions_to_playing(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.state is EngineState.PLAYING
        assert engine.mode is PlaybackMode.REALTIME

    def test_set_mode_paused(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        engine.set_mode(PlaybackMode.PAUSED)
        assert engine.state is EngineState.PAUSED

    def test_state_changed_signal(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        states = []
        engine.state_changed.connect(lambda s: states.append(s))
        engine.configure(renderer=MagicMock())
        engine.set_mode(PlaybackMode.REALTIME)
        assert "ready" in states
        assert "playing" in states

    def test_controller_is_single_authoritative_activity_source(self, qapp) -> None:
        """Controller transitions drive engine mode and one derived signal."""
        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        activity: list[bool] = []
        engine.playback_active_changed.connect(activity.append)

        engine.bind_playback_controller(controller)
        controller.play()
        controller.pause()

        assert activity == [False, True, False]
        assert engine.mode is PlaybackMode.PAUSED
        assert engine.state is EngineState.PAUSED

    def test_rebinding_disconnects_old_controller(self, qapp) -> None:
        """Rebinding must not let an old transport mutate the engine."""
        first = EditPlaybackController()
        second = EditPlaybackController()
        first.set_timeline_duration(10)
        second.set_timeline_duration(10)
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        activity: list[bool] = []
        engine.playback_active_changed.connect(activity.append)

        engine.bind_playback_controller(first)
        engine.bind_playback_controller(second)
        first.play()
        assert engine.mode is PlaybackMode.PAUSED
        assert activity == [False, False]

        second.play()
        assert engine.mode is PlaybackMode.REALTIME
        assert activity == [False, False, True]

    def test_renderer_binds_controller_through_engine_bridge(self, qapp) -> None:
        """Renderer receives controller activity from engine, not panel calls."""
        controller = EditPlaybackController()
        controller.set_timeline_duration(10)
        engine = RealtimePlaybackEngine()
        renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
        renderer._realtime_engine = engine
        renderer._playback_controller = None
        renderer._playback_activity_signal = None
        renderer._on_engine_playback_active_changed = MagicMock()

        TimelinePreviewRenderer.set_playback_controller(renderer, controller)
        controller.play()

        assert engine.playback_controller is controller
        renderer._on_engine_playback_active_changed.assert_any_call(False)
        renderer._on_engine_playback_active_changed.assert_any_call(True)


# =============================================================================
# request_present（唯一热路径）
# =============================================================================


class TestRequestPresent:
    def test_attached_scheduler_receives_non_blocking_window_prefetch(self, qapp) -> None:
        """engine 应把 realtime tick 转为 scheduler 的窗口预取请求。"""
        scheduler = MagicMock()
        scheduler.prepare_window_async.return_value = MagicMock()
        engine = RealtimePlaybackEngine()
        engine.configure(
            delegate_render=lambda f, g, d: True,
            decode_scheduler=scheduler,
            scheduler_radius_before=2,
            scheduler_radius_after=8,
        )
        engine.set_mode(PlaybackMode.REALTIME)

        assert engine.request_present(100) is True
        scheduler.prepare_window_async.assert_called_once()
        kwargs = scheduler.prepare_window_async.call_args.kwargs
        assert kwargs["center_frame"] == 100
        assert kwargs["radius_before"] == 2
        assert kwargs["radius_after"] == 8
        assert kwargs["generation_id"] == engine.generation_id

    def test_scheduler_config_is_updated_only_when_signature_changes(self, qapp) -> None:
        """Engine must not rebuild scheduler streams on every playback tick."""
        scheduler = MagicMock()
        scheduler.prepare_window_async.return_value = MagicMock()
        provider_calls = []

        def config_provider(frame, decisions):
            provider_calls.append((frame, decisions))
            return {
                "signature": ("clip-A", "proxy-v1"),
                "layers": [],
                "media_source_factory": None,
            }

        engine = RealtimePlaybackEngine()
        engine.configure(
            delegate_render=lambda f, g, d: True,
            decode_scheduler=scheduler,
            scheduler_config_provider=config_provider,
        )
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.request_present(1) is True
        assert engine.request_present(2) is True
        assert len(provider_calls) == 2
        scheduler.configure.assert_called_once()

    def test_external_generation_sync_invalidates_scheduler(self, qapp) -> None:
        scheduler = MagicMock()
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock(), decode_scheduler=scheduler)
        engine.sync_generation_id(9)
        assert engine.generation_id == 9
        scheduler.reset_generation.assert_called_once_with(9)

    def test_scheduler_generation_is_flushed_on_seek(self, qapp) -> None:
        """seek 必须同时清理 scheduler 窗口并同步 generation。"""
        scheduler = MagicMock()
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock(), decode_scheduler=scheduler)
        engine.seek(240)
        scheduler.flush_seek.assert_called_once_with(240)
        scheduler.reset_generation.assert_called_once_with(engine.generation_id)


class TestSchedulerWindowSpeedAwareness:
    """P1-07: J/K/L 变速播放按倍率扩展行进方向 lookahead（带上限）。"""

    def _engine(self, scheduler, rate_provider) -> RealtimePlaybackEngine:
        engine = RealtimePlaybackEngine()
        engine.configure(
            delegate_render=lambda f, g, d: True,
            decode_scheduler=scheduler,
            scheduler_radius_before=2,
            scheduler_radius_after=8,
            scheduler_rate_provider=rate_provider,
        )
        engine.set_mode(PlaybackMode.REALTIME)
        return engine

    def test_rate_2x_doubles_lookahead(self, qapp) -> None:
        scheduler = MagicMock()
        engine = self._engine(scheduler, lambda: 2.0)
        engine.request_present(100)
        kwargs = scheduler.prepare_window_async.call_args.kwargs
        assert kwargs["radius_after"] == 16
        assert kwargs["radius_before"] == 2

    def test_rate_4x_and_8x_cap_at_32(self, qapp) -> None:
        scheduler = MagicMock()
        engine = self._engine(scheduler, lambda: 4.0)
        engine.request_present(100)
        assert scheduler.prepare_window_async.call_args.kwargs["radius_after"] == 32

        scheduler2 = MagicMock()
        engine2 = self._engine(scheduler2, lambda: 16.0)
        engine2.request_present(100)
        # 倍率截断到 8x → 8*8=64 → 上限 32。
        assert scheduler2.prepare_window_async.call_args.kwargs["radius_after"] == 32

    def test_slow_rate_never_shrinks_window(self, qapp) -> None:
        scheduler = MagicMock()
        engine = self._engine(scheduler, lambda: 0.25)
        engine.request_present(100)
        assert scheduler.prepare_window_async.call_args.kwargs["radius_after"] == 8

    def test_no_rate_provider_uses_default(self, qapp) -> None:
        scheduler = MagicMock()
        engine = self._engine(scheduler, None)
        engine.request_present(100)
        assert scheduler.prepare_window_async.call_args.kwargs["radius_after"] == 8

    def test_rate_provider_error_falls_back_to_default(self, qapp) -> None:
        scheduler = MagicMock()

        def broken() -> float:
            raise RuntimeError("boom")

        engine = self._engine(scheduler, broken)
        engine.request_present(100)
        assert scheduler.prepare_window_async.call_args.kwargs["radius_after"] == 8

    def test_direction_combines_with_rate(self, qapp) -> None:
        """反放 2x：方向翻转 + lookahead 扩展同时生效。"""
        scheduler = MagicMock()
        engine = RealtimePlaybackEngine()
        engine.configure(
            delegate_render=lambda f, g, d: True,
            decode_scheduler=scheduler,
            scheduler_radius_before=2,
            scheduler_radius_after=8,
            scheduler_direction_provider=lambda: -1,
            scheduler_rate_provider=lambda: 2.0,
        )
        engine.set_mode(PlaybackMode.REALTIME)
        engine.request_present(100)
        kwargs = scheduler.prepare_window_async.call_args.kwargs
        assert kwargs["direction"] == -1
        assert kwargs["radius_after"] == 16


class TestRendererRateProvider:
    """renderer 的倍率提供器契约（controller.playback_speed → float）。"""

    def _shell(self, controller) -> "TimelinePreviewRenderer":
        renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
        renderer.__dict__["_playback_controller"] = controller
        return renderer

    def test_reads_controller_speed(self, qapp) -> None:
        controller = MagicMock()
        controller.playback_speed = 4.0
        assert self._shell(controller)._playback_rate_multiplier() == 4.0

    def test_missing_controller_defaults_one(self, qapp) -> None:
        assert self._shell(None)._playback_rate_multiplier() == 1.0

    def test_bad_controller_defaults_one(self, qapp) -> None:
        class Broken:
            @property
            def playback_speed(self) -> float:
                raise RuntimeError("boom")

        assert self._shell(Broken())._playback_rate_multiplier() == 1.0

    def test_present_success_via_delegate(self, qapp) -> None:
        """delegate 返回 True → present 成功。"""
        engine = RealtimePlaybackEngine()
        calls = []
        def delegate(frame, gen, deadline):
            calls.append((frame, gen))
            return True
        engine.configure(delegate_render=delegate)
        engine.set_mode(PlaybackMode.REALTIME)

        presented_frames = []
        engine.frame_presented.connect(presented_frames.append)

        ok = engine.request_present(100, deadline_ns=0)
        assert ok is True
        assert presented_frames == [100]
        assert calls == [(100, 0)]

    def test_present_miss_returns_false(self, qapp) -> None:
        """delegate 返回 False → miss，request_present 返回 False。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False)
        engine.set_mode(PlaybackMode.REALTIME)

        ok = engine.request_present(50, deadline_ns=0)
        assert ok is False
        assert engine.stats()["total_misses"] == 1

    def test_consecutive_miss_triggers_rebuffer(self, qapp) -> None:
        """连续 miss 超阈值进入 REBUFFERING。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False, rebuffer_threshold=3)
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.state is EngineState.PLAYING

        warnings = []
        engine.playback_warning.connect(lambda r, d: warnings.append(r))

        for i in range(3):
            engine.request_present(i, deadline_ns=0)
        # 第 3 次 miss 触发 rebuffer
        assert engine.state is EngineState.REBUFFERING
        assert "rebuffering" in warnings

    def test_success_after_rebuffer_returns_to_playing(self, qapp) -> None:
        """rebuffer 后成功 present 回到 PLAYING。"""
        results = [False, False, False, True]  # 3 miss then success
        engine = RealtimePlaybackEngine()
        engine.configure(
            delegate_render=lambda f, g, d: results.pop(0) if results else True,
            rebuffer_threshold=3,
        )
        engine.set_mode(PlaybackMode.REALTIME)

        for i in range(3):
            engine.request_present(i, deadline_ns=0)
        assert engine.state is EngineState.REBUFFERING

        engine.request_present(3, deadline_ns=0)
        assert engine.state is EngineState.PLAYING


# =============================================================================
# seek / generation
# =============================================================================


class TestEngineSeek:
    def test_seek_increments_generation(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        gen0 = engine.generation_id
        engine.seek(100)
        assert engine.generation_id == gen0 + 1

    def test_seek_resets_miss_count(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False, rebuffer_threshold=2)
        engine.set_mode(PlaybackMode.REALTIME)
        engine.request_present(0, deadline_ns=0)
        engine.request_present(1, deadline_ns=0)
        assert engine.stats()["consecutive_misses"] == 2
        engine.seek(100)
        assert engine.stats()["consecutive_misses"] == 0

    def test_seek_during_playing_transitions_to_warming(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.state is EngineState.PLAYING
        engine.seek(50)
        assert engine.state is EngineState.WARMING


# =============================================================================
# telemetry
# =============================================================================


class TestEngineTelemetry:
    def test_telemetry_recorded_per_frame(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True)
        engine.set_mode(PlaybackMode.REALTIME)

        traces = []
        engine.telemetry_recorded.connect(traces.append)
        for i in range(10):
            engine.request_present(i, deadline_ns=0)
        assert len(traces) == 10
        assert traces[0].timeline_frame == 0
        assert traces[9].timeline_frame == 9
        assert traces[0].mode == "realtime"

    def test_telemetry_summary(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True)
        engine.set_mode(PlaybackMode.REALTIME)
        for i in range(20):
            engine.request_present(i, deadline_ns=0)
        s = engine.telemetry.summary()
        assert s["frames"] == 20
        assert s["session_total_frames"] == 20

    def test_miss_traces_recorded(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False)
        engine.set_mode(PlaybackMode.REALTIME)
        traces = []
        engine.telemetry_recorded.connect(traces.append)
        engine.request_present(0, deadline_ns=0)
        assert traces[0].miss_reason == "delegate_miss"


# =============================================================================
# P1-05: miss 语义统一（hold-first / proxy pending / unsupported effect /
# worker 归因上报 note_playback_miss）
# =============================================================================


class TestMissSemanticsP105:
    def test_first_delegate_miss_holds_not_rebuffers(self, qapp) -> None:
        """P1-05 修复：首次 delegate miss 应 hold，不得立即 REBUFFERING。

        修复前 engine 伪造 source_kind=PLACEHOLDER 的 miss，policy 对
        PLACEHOLDER 立即 rebuffer，hold 分支实际不可达。
        """
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False, rebuffer_threshold=3)
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.state is EngineState.PLAYING

        warnings: list = []
        engine.playback_warning.connect(lambda r, d: warnings.append((r, d)))

        assert engine.request_present(0) is False
        # hold：状态保持 PLAYING，无 rebuffer 警告，计入 hold 统计。
        assert engine.state is EngineState.PLAYING
        assert not warnings
        assert engine.stats()["total_holds"] == 1

    def test_delegate_miss_threshold_still_rebuffers(self, qapp) -> None:
        """瞬态 miss 连续达阈值后仍进入 REBUFFERING（hold-first 不失效）。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False, rebuffer_threshold=2)
        engine.set_mode(PlaybackMode.REALTIME)
        engine.request_present(0)
        assert engine.state is EngineState.PLAYING
        engine.request_present(1)
        assert engine.state is EngineState.REBUFFERING

    def test_noted_proxy_pending_rebuffers_immediately(self, qapp) -> None:
        """worker 上报 proxy pending → 首次 miss 即 REBUFFERING（等待无意义）。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False, rebuffer_threshold=3)
        engine.set_mode(PlaybackMode.REALTIME)
        engine.note_playback_miss(
            "proxy generating", generation_id=engine.generation_id
        )

        warnings: list = []
        engine.playback_warning.connect(lambda r, d: warnings.append((r, d)))

        assert engine.request_present(0) is False
        assert engine.state is EngineState.REBUFFERING
        assert warnings and warnings[0][0] == "rebuffering"
        assert warnings[0][1]["reason"] == "proxy generating"

    def test_noted_unsupported_effect_never_rebuffers(self, qapp) -> None:
        """unsupported effect：持续上报下持续 miss 也永不 REBUFFERING，只警告一次。

        归因槽是单次消费语义（真实渲染侧在每个 drop 点重复上报），
        这里逐帧模拟 worker 的持续归因。
        """
        engine = RealtimePlaybackEngine()
        engine.configure(
            delegate_render=lambda f, g, d: False, rebuffer_threshold=3,
        )
        engine.set_mode(PlaybackMode.REALTIME)

        warnings: list = []
        engine.playback_warning.connect(lambda r, d: warnings.append((r, d)))

        for i in range(10):
            engine.note_playback_miss(
                "unsupported_effect", generation_id=engine.generation_id
            )
            engine.request_present(i)
        assert engine.state is EngineState.PLAYING
        assert [w[0] for w in warnings] == ["unsupported_effect"]

    def test_back_pressure_miss_holds_first(self, qapp) -> None:
        """预算 back-pressure 是瞬态 drop：首次只 hold，不立即 REBUFFERING。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True, rebuffer_threshold=3)
        engine.set_mode(PlaybackMode.REALTIME)
        engine._budget.before_render = lambda frame: False  # 模拟持续背压

        warnings: list = []
        engine.playback_warning.connect(lambda r, d: warnings.append((r, d)))

        assert engine.request_present(0) is False
        assert engine.state is EngineState.PLAYING
        assert not warnings
        assert engine.stats()["total_holds"] == 1

    def test_noted_reason_used_once_then_reverts(self, qapp) -> None:
        """归因槽单次消费：第一次 miss 用上报 reason，之后回落 delegate_miss。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False)
        engine.set_mode(PlaybackMode.REALTIME)
        traces = []
        engine.telemetry_recorded.connect(traces.append)

        engine.note_playback_miss(
            "gpu_result_drop_playback_stale",
            generation_id=engine.generation_id,
        )
        engine.request_present(0)
        assert traces[-1].miss_reason == "gpu_result_drop_playback_stale"

        engine.request_present(1)
        assert traces[-1].miss_reason == "delegate_miss"

    def test_stale_generation_note_is_dropped(self, qapp) -> None:
        """旧 generation 的归因上报必须丢弃（seek 后不得污染新会话）。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False)
        engine.set_mode(PlaybackMode.REALTIME)
        traces = []
        engine.telemetry_recorded.connect(traces.append)

        engine.note_playback_miss("proxy generating", generation_id=engine.generation_id + 5)
        engine.request_present(0)
        # 旧代上报被忽略：miss 仍按瞬态 hold 处理。
        assert traces[-1].miss_reason == "delegate_miss"
        assert engine.state is EngineState.PLAYING

    def test_seek_invalidates_noted_reason(self, qapp) -> None:
        """seek 递增 generation 后，先前归因不再适用于新会话。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False)
        engine.set_mode(PlaybackMode.REALTIME)

        engine.note_playback_miss(
            "proxy generating", generation_id=engine.generation_id
        )
        engine.seek(50)
        engine.request_present(50)
        # seek 后该归因属于旧 generation：不得立即 rebuffer。
        assert engine.state is EngineState.WARMING

    def test_note_without_generation_binds_current(self, qapp) -> None:
        """无 generation 参数的上报绑定当前 generation（worker 便捷路径）。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: False)
        engine.set_mode(PlaybackMode.REALTIME)
        traces = []
        engine.telemetry_recorded.connect(traces.append)

        engine.note_playback_miss("gpu lane saturated")
        engine.request_present(0)
        assert traces[-1].miss_reason == "gpu lane saturated"


# =============================================================================
# shutdown / stats
# =============================================================================


class TestEngineShutdown:
    def test_shutdown_blocks_requests(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        engine.shutdown()
        assert engine.state is EngineState.SHUTDOWN
        assert engine.request_present(0) is False

    def test_stats(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda f, g, d: True)
        engine.set_mode(PlaybackMode.REALTIME)
        for i in range(5):
            engine.request_present(i, deadline_ns=0)
        s = engine.stats()
        assert s["total_requests"] == 5
        assert s["total_presented"] == 5
        assert s["total_misses"] == 0


# =============================================================================
# master fallback 禁止（通过 policy 在 engine 决策路径）
# =============================================================================


class TestEngineMasterFallbackForbidden:
    """realtime 模式下 engine 的 policy 决策禁止 master。"""

    def test_policy_attached_to_engine(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        assert isinstance(engine.policy, RealtimePlaybackPolicy)
        assert engine.policy.may_use_master(PlaybackMode.REALTIME) is False
        assert engine.policy.may_use_master(PlaybackMode.PAUSED) is True

    def test_realtime_mode_set(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=MagicMock())
        engine.set_mode(PlaybackMode.REALTIME)
        # engine.mode 是 REALTIME，policy 在 realtime 禁止 master
        assert engine.mode is PlaybackMode.REALTIME
        assert engine.mode.allows_master is False
