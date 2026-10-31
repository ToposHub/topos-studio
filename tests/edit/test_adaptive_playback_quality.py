"""
P1-10（自适应播放质量）单元测试。

三层覆盖：
- AdaptiveQualityController 纯状态机（迟滞 / 驻留 / 归因感知 / 用户门控）。
- RealtimePlaybackEngine 观测与 quality_pressure_changed 信号。
- TimelinePreviewRenderer 档位 → auto2k decode scale 的映射与失效链。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    AdaptiveQualityController,
    PlaybackMode,
    QualityPressureLevel,
    RealtimePlaybackEngine,
)
from src.features.edit.core.realtime_playback.adaptive_quality import (
    LOAD_PRESSURE_REASONS,
)
from src.features.edit.core.realtime_playback.types import MissReason
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def make_controller(**overrides) -> AdaptiveQualityController:
    params = dict(down_streak=3, up_streak=5, min_dwell_frames=0)
    params.update(overrides)
    return AdaptiveQualityController(**params)


# =============================================================================
# 控制器状态机
# =============================================================================


class TestAdaptiveQualityController:
    def test_pressure_streak_downgrades_after_threshold(self) -> None:
        ctrl = make_controller(down_streak=3)
        events = [ctrl.observe(presented=False, miss_reason="decode failed")
                  for _ in range(3)]
        # 前两次无变化，第 3 次降档。
        assert events[0] is None and events[1] is None
        assert events[2] is not None
        assert ctrl.level is QualityPressureLevel.ELEVATED
        assert ctrl.scale == pytest.approx(0.5)
        assert events[2].old_level is QualityPressureLevel.NORMAL

    def test_single_pressure_blip_does_not_downgrade(self) -> None:
        ctrl = make_controller(down_streak=3)
        ctrl.observe(presented=False, miss_reason="gpu busy")
        ctrl.observe(presented=True)
        ctrl.observe(presented=False, miss_reason="gpu busy")
        assert ctrl.level is QualityPressureLevel.NORMAL

    def test_two_downgrades_reach_critical_and_stay(self) -> None:
        ctrl = make_controller(down_streak=2, min_dwell_frames=0)
        for _ in range(2):
            ctrl.observe(presented=False, miss_reason="decode failed")
        assert ctrl.level is QualityPressureLevel.ELEVATED
        for _ in range(2):
            ctrl.observe(presented=False, miss_reason="decode failed")
        assert ctrl.level is QualityPressureLevel.CRITICAL
        # 已到底：继续压力不再产生事件。
        assert ctrl.observe(presented=False, miss_reason="decode failed") is None
        assert ctrl.scale == pytest.approx(0.25)

    def test_healthy_streak_upgrades_back(self) -> None:
        ctrl = make_controller(down_streak=2, up_streak=3)
        for _ in range(2):
            ctrl.observe(presented=False, miss_reason="cache warming")
        assert ctrl.level is QualityPressureLevel.ELEVATED
        events = [ctrl.observe(presented=True) for _ in range(3)]
        assert events[0] is None and events[1] is None
        assert events[2] is not None
        assert ctrl.level is QualityPressureLevel.NORMAL
        assert ctrl.scale == pytest.approx(1.0)

    def test_dwell_prevents_immediate_flapping(self) -> None:
        """最小驻留内不得换档（防解码会话缓存键抖动），双向生效。

        驻留从进入当前档起重新计数：dwell=4 时，第 4 帧观测起才允许
        首次降档；降档后同样需要计满驻留才允许升档。
        """
        ctrl = make_controller(down_streak=1, up_streak=1, min_dwell_frames=4)
        # 驻留未满：持续压力不得降档。
        for _ in range(3):
            assert ctrl.observe(presented=False, miss_reason="decode failed") is None
        assert ctrl.level is QualityPressureLevel.NORMAL
        # 第 4 帧驻留满足 → 降档。
        assert ctrl.observe(presented=False, miss_reason="decode failed") is not None
        assert ctrl.level is QualityPressureLevel.ELEVATED
        # 降档后驻留重新计数：健康帧不得立刻升档。
        for _ in range(3):
            assert ctrl.observe(presented=True) is None
        assert ctrl.level is QualityPressureLevel.ELEVATED
        assert ctrl.observe(presented=True) is not None
        assert ctrl.level is QualityPressureLevel.NORMAL

    def test_non_load_reasons_are_not_pressure(self) -> None:
        """proxy pending / unsupported effect 降分辨率无益，不计压力。"""
        ctrl = make_controller(down_streak=1)
        for reason in ("proxy generating", "unsupported_effect"):
            ctrl.observe(presented=False, miss_reason=reason)
        assert ctrl.level is QualityPressureLevel.NORMAL

    def test_load_reasons_are_pressure(self) -> None:
        for reason in ("decode failed", "gpu busy", "stream not ready",
                       "back_pressure", "delegate_miss"):
            assert MissReason.__members__  # 词表仍可用
            ctrl = make_controller(down_streak=1)
            ctrl.observe(presented=False, miss_reason=reason)
            assert ctrl.level is QualityPressureLevel.ELEVATED, reason

    def test_back_pressure_flag_counts_even_when_presented(self) -> None:
        ctrl = make_controller(down_streak=1)
        ctrl.observe(presented=True, back_pressured=True)
        assert ctrl.level is QualityPressureLevel.ELEVATED

    def test_disabled_pins_normal_and_reverts(self) -> None:
        ctrl = make_controller(down_streak=1)
        ctrl.observe(presented=False, miss_reason="decode failed")
        assert ctrl.level is QualityPressureLevel.ELEVATED
        event = ctrl.set_enabled(False)
        assert event is not None and ctrl.level is QualityPressureLevel.NORMAL
        # 关闭后压力不再降档。
        assert ctrl.observe(presented=False, miss_reason="decode failed") is None
        assert ctrl.level is QualityPressureLevel.NORMAL

    def test_reset_returns_normal_with_event(self) -> None:
        ctrl = make_controller(down_streak=1)
        ctrl.observe(presented=False, miss_reason="decode failed")
        event = ctrl.reset("seek")
        assert event is not None
        assert event.new_level is QualityPressureLevel.NORMAL
        assert ctrl.stats()["pressure_streak"] == 0

    def test_stats_shape(self) -> None:
        stats = make_controller().stats()
        for key in ("enabled", "level", "scale", "pressure_streak",
                    "healthy_streak", "total_downgrades", "total_upgrades"):
            assert key in stats


# =============================================================================
# engine 观测与信号
# =============================================================================


class TestEngineAdaptiveQuality:
    def _engine(self, delegate) -> RealtimePlaybackEngine:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=delegate)
        engine.configure_adaptive_quality(
            down_streak=2, up_streak=3, min_dwell_frames=0
        )
        engine.set_mode(PlaybackMode.REALTIME)
        return engine

    def test_miss_streak_emits_downgrade(self, qapp) -> None:
        engine = self._engine(lambda f, g, d: False)
        levels: list = []
        engine.quality_pressure_changed.connect(
            lambda level, detail: levels.append((level, detail))
        )
        engine.request_present(0)
        engine.request_present(1)
        assert engine.adaptive_quality.level is QualityPressureLevel.ELEVATED
        assert levels and levels[-1][0] == "elevated"
        assert levels[-1][1]["scale"] == pytest.approx(0.5)
        assert levels[-1][1]["old_level"] == "normal"

    def test_recovery_upgrades_back(self, qapp) -> None:
        results = [False, False, True, True, True]
        engine = self._engine(
            lambda f, g, d: results.pop(0) if results else True
        )
        levels: list = []
        engine.quality_pressure_changed.connect(
            lambda level, detail: levels.append(level)
        )
        for i in range(5):
            engine.request_present(i)
        assert engine.adaptive_quality.level is QualityPressureLevel.NORMAL
        assert "elevated" in levels and levels[-1] == "normal"

    def test_seek_resets_level(self, qapp) -> None:
        engine = self._engine(lambda f, g, d: False)
        engine.request_present(0)
        engine.request_present(1)
        assert engine.adaptive_quality.level is QualityPressureLevel.ELEVATED
        engine.seek(100)
        assert engine.adaptive_quality.level is QualityPressureLevel.NORMAL

    def test_playback_restart_resets_level(self, qapp) -> None:
        engine = self._engine(lambda f, g, d: False)
        engine.request_present(0)
        engine.request_present(1)
        assert engine.adaptive_quality.level is QualityPressureLevel.ELEVATED
        engine.set_mode(PlaybackMode.PAUSED)
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.adaptive_quality.level is QualityPressureLevel.NORMAL

    def test_preference_disable_blocks_downgrade(self, qapp) -> None:
        engine = self._engine(lambda f, g, d: False)
        levels: list = []
        engine.quality_pressure_changed.connect(
            lambda level, detail: levels.append(level)
        )
        engine.configure_adaptive_quality(enabled=False)
        for i in range(6):
            engine.request_present(i)
        assert engine.adaptive_quality.enabled is False
        assert engine.adaptive_quality.level is QualityPressureLevel.NORMAL
        assert not levels

    def test_unsupported_effect_miss_not_pressure(self, qapp) -> None:
        """P1-05 归因贯通：unsupported effect miss 不驱动降档。"""
        engine = self._engine(lambda f, g, d: False)
        for i in range(5):
            engine.note_playback_miss(
                "unsupported_effect", generation_id=engine.generation_id
            )
            engine.request_present(i)
        assert engine.adaptive_quality.level is QualityPressureLevel.NORMAL

    def test_back_pressure_drop_counts_as_pressure(self, qapp) -> None:
        engine = self._engine(lambda f, g, d: True)
        engine._budget.before_render = lambda frame: False
        engine.request_present(0)
        engine.request_present(1)
        assert engine.adaptive_quality.level is QualityPressureLevel.ELEVATED


# =============================================================================
# renderer 档位映射
# =============================================================================


def make_renderer_shell(sequence=(3840, 2160)) -> TimelinePreviewRenderer:
    renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
    renderer._preview_quality = 0.0
    renderer._adaptive_playback_scale = 1.0
    renderer._engine_playback_active = False
    renderer._scrub_active = False
    renderer._max_preview_edge = 3840
    renderer._get_sequence_resolution = lambda: sequence  # type: ignore[method-assign]
    return renderer


class TestRendererAdaptiveScale:
    def test_scale_applies_to_auto2k_while_playing(self) -> None:
        renderer = make_renderer_shell()
        renderer._engine_playback_active = True
        renderer._adaptive_playback_scale = 0.5
        # 3840 序列 auto2k → 1920×1080；×0.5 → 960×540。
        assert renderer._auto2k_decode_target_size() == (960, 540)

    def test_scale_inactive_when_not_playing(self) -> None:
        renderer = make_renderer_shell()
        renderer._adaptive_playback_scale = 0.5
        assert renderer._auto2k_decode_target_size() == (1920, 1080)

    def test_scale_inactive_during_scrub(self) -> None:
        renderer = make_renderer_shell()
        renderer._engine_playback_active = True
        renderer._scrub_active = True
        renderer._adaptive_playback_scale = 0.5
        assert renderer._auto2k_decode_target_size() == (1920, 1080)

    def test_manual_quality_choice_not_overridden(self) -> None:
        """用户显式分辨率档是手动覆盖：自适应不介入。"""
        renderer = make_renderer_shell()
        renderer._engine_playback_active = True
        renderer._preview_quality = 0.5
        renderer._adaptive_playback_scale = 0.25
        assert renderer._auto2k_decode_target_size() == (1920, 1080)

    def test_even_dimensions_after_scale(self) -> None:
        renderer = make_renderer_shell(sequence=(3841, 2161))
        renderer._engine_playback_active = True
        renderer._adaptive_playback_scale = 0.25
        w, h = renderer._auto2k_decode_target_size()
        assert w % 2 == 0 and h % 2 == 0

    def test_set_adaptive_scale_clamps_and_invalidates(self) -> None:
        renderer = make_renderer_shell()
        with patch.object(
            TimelinePreviewRenderer, "_invalidate_preview_frame_caches"
        ) as inv_caches, patch.object(
            TimelinePreviewRenderer, "_invalidate_render_generation_for_quality"
        ) as inv_gen:
            renderer._set_adaptive_playback_scale(0.05, "test")
            assert renderer._adaptive_playback_scale == pytest.approx(0.25)
            assert inv_caches.call_count == 1 and inv_gen.call_count == 1
            # 同值不重复失效。
            renderer._set_adaptive_playback_scale(0.25, "test")
            assert inv_caches.call_count == 1

    def test_engine_signal_maps_to_scale(self) -> None:
        renderer = make_renderer_shell()
        with patch.object(
            TimelinePreviewRenderer, "_invalidate_preview_frame_caches"
        ), patch.object(
            TimelinePreviewRenderer, "_invalidate_render_generation_for_quality"
        ):
            renderer._on_engine_quality_pressure_changed("critical", {})
            assert renderer._adaptive_playback_scale == pytest.approx(0.25)
            renderer._on_engine_quality_pressure_changed("normal", {})
            assert renderer._adaptive_playback_scale == pytest.approx(1.0)

    def test_preference_passthrough_to_engine(self, qapp) -> None:
        renderer = make_renderer_shell()
        engine = RealtimePlaybackEngine()
        renderer._realtime_engine = engine
        renderer.set_adaptive_playback_quality_enabled(False)
        assert engine.adaptive_quality.enabled is False
        renderer.set_adaptive_playback_quality_enabled(True)
        assert engine.adaptive_quality.enabled is True

    def test_no_deblock_passthrough_to_decode_manager(self, qapp) -> None:
        """预览关滤波开关：渲染器 setter → worker → 解码管理器 set_no_deblock。

        只钉透传与幂等（真正落像素与否由 native 端到端用例
        tests/media/test_topos_source.py::test_no_deblock_request_skips_output_filter
        与 C 单测钉死）。
        """
        from types import SimpleNamespace

        renderer = make_renderer_shell()
        # shell 缺渲染器侧的缓存/预热方法：补最小桩，避免碰 Qt 渲染状态
        renderer._no_deblock = False
        renderer._invalidate_preview_frame_caches = lambda: None  # type: ignore[method-assign]
        renderer._clear_prebuffer_state = lambda: None  # type: ignore[method-assign]
        renderer._reset_emitted_frame_guard = lambda: None  # type: ignore[method-assign]
        renderer._schedule_current_frame_warmup = lambda: None  # type: ignore[method-assign]
        calls = []
        manager = SimpleNamespace(
            set_no_deblock=lambda enabled: calls.append(bool(enabled)))
        worker = SimpleNamespace(
            _gpu_decode_manager=manager,
            set_no_deblock=lambda enabled: manager.set_no_deblock(enabled))
        renderer._worker = worker  # type: ignore[assignment]
        renderer._render_in_flight = False

        renderer.set_no_deblock(True)
        assert renderer._no_deblock is True
        assert calls == [True]
        # 重复设置同一值：幂等（不再触发 source 失效）
        renderer.set_no_deblock(True)
        assert calls == [True]
        renderer.set_no_deblock(False)
        assert calls == [True, False]

    def test_unknown_level_ignored(self) -> None:
        renderer = make_renderer_shell()
        renderer._on_engine_quality_pressure_changed("bogus", {})
        assert renderer._adaptive_playback_scale == pytest.approx(1.0)
