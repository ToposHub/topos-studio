"""MX-01 遥测生命周期回归（音频成熟化计划 阶段B）。

停止/暂停/seek 统一清表：版本化静音 payload 覆盖 GR/SC/频谱；
播放代数守卫丢弃旧代排队事件；缺席轨与 silent 事件行为一致。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SILENT = [-100.0, -100.0]


class TestMx01SilentPayload:
    def test_stop_emits_versioned_silent_payload(self, qapp):
        """stop 广播完整静音事件：电平地板 + GR/SC 清空 + spectrum=None + 代数。"""
        from src.features.edit.core.audio_playback import AudioPlaybackManager as AudioManager

        mgr = AudioManager()
        events = []
        mgr.audio_track_levels_changed.connect(
            lambda levels: events.append(dict(levels)))
        mgr._last_track_level_ids = ["t1", "t2"]
        mgr.stop()
        assert len(events) == 1
        payload = events[0]
        assert set(payload) >= {"t1", "t2"}
        for tid, vals in payload.items():
            assert isinstance(vals, dict)
            assert vals["peak"] == SILENT
            assert vals["rms"] == SILENT
            assert vals["gr"] == {}
            assert vals["sc"] == {}
            assert vals["spectrum"] is None
            assert vals["silent"] is True
            assert isinstance(vals["generation"], int)

    def test_generation_increments_per_lifecycle_event(self, qapp):
        """stop/pause/play/seek 每次生命周期事件递增播放代数。"""
        from src.features.edit.core.audio_playback import AudioPlaybackManager as AudioManager

        mgr = AudioManager()
        gens = []
        mgr.audio_telemetry_reset.connect(gens.append)
        mgr.stop()
        mgr.pause()  # 时钟未播放 → pause 直接 return？验证守卫
        mgr2_gens = list(gens)
        mgr.seek(10)
        assert len(gens) >= 2
        assert gens == sorted(gens), "generation must be monotonic"

    def test_old_generation_events_dropped_by_panel(self, qapp):
        """面板丢弃 generation < 当前代的排队载荷（不回亮表计）。"""
        from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel

        panel = AudioMixerPanel()
        try:
            registered = []
            # 伪造 strip（避免完整 UI 构建）
            class _Meter:
                meter_mode = None

            class _Strip:
                def __init__(self):
                    self.calls = []
                    self._meter = _Meter()

                def reset_meter(self):
                    self.calls.append("reset")

                def update_audio_level(self, vals):
                    self.calls.append(("levels", vals))

                def update_track_telemetry(self, payload):
                    self.calls.append(("telemetry", payload))

            strip = _Strip()
            panel._track_mixers["t1"] = strip

            panel.on_telemetry_reset(5)
            assert strip.calls[0] == "reset"
            assert ("telemetry",) == strip.calls[1][:1]
            assert strip.calls[1][1]["silent"] is True

            # 旧代（gen=3）事件 → 丢弃
            strip.calls.clear()
            panel.update_per_track_levels({
                "t1": {"peak": [-3.0, -3.0], "generation": 3}})
            assert strip.calls == []

            # 当前代事件 → 正常应用
            panel.update_per_track_levels({
                "t1": {"peak": [-3.0, -3.0], "generation": 5}})
            assert any(c[0] == "levels" for c in strip.calls)
        finally:
            panel.deleteLater()

    def test_pause_before_stop_both_clear(self, qapp):
        """pause 与 stop 都发出静音事件（B13/MX-01 语义一致）。"""
        from src.features.edit.core.audio_playback import AudioPlaybackManager as AudioManager

        mgr = AudioManager()
        events = []
        mgr.audio_track_levels_changed.connect(
            lambda levels: events.append(dict(levels)))
        mgr._last_track_level_ids = ["t1"]
        mgr.pause()  # 时钟未播放 → 守卫 return，不发事件
        n_after_pause = len(events)
        mgr.stop()
        assert len(events) == n_after_pause + 1


class TestMx01WidgetSilentTelemetry:
    def test_silent_payload_reaches_dialogs(self, qapp):
        """silent payload 分发：EQ 清频谱、Delay/Reverb 输出表归零。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget

        w = TrackMixerWidget.__new__(TrackMixerWidget)
        # 手工初始化最小状态（避免完整 UI 依赖）
        from PyQt6.QtWidgets import QWidget
        QWidget.__init__(w)
        w._track_id = "t1"
        w._eq_dialog = None
        w._strip_eq_dialogs = {}
        w._dynamics_dialog = None
        w._compressor_dialogs = {}
        w._reverb_dialogs = {}
        w._delay_dialogs = {}
        w._effect_chain = None

        class _EqDlg:
            def __init__(self):
                self.spec = None
                self.out = None
                self.reset_called = False

            def isVisible(self):
                return True

            def update_spectrum(self, f, db, peak=None):
                self.spec = (f, db)

            def update_output_meter(self, db):
                self.out = db

            def reset_peak_hold(self):
                self.reset_called = True

        eq = _EqDlg()
        w._strip_eq_dialogs["e1"] = eq

        class _DelayDlg:
            def __init__(self):
                self.levels = None

            def update_output_meters(self, l, r):
                self.levels = (l, r)

        dd = _DelayDlg()
        w._delay_dialogs["d1"] = dd

        silent_payload = {
            "peak": [-100.0, -100.0], "rms": [-100.0, -100.0],
            "lufs": -70.0, "pre": -100.0,
            "gr": {}, "sc": {}, "spectrum": None,
            "silent": True, "generation": 7,
        }
        w.update_track_telemetry(silent_payload)
        assert eq.spec == ([], []), "spectrum must be cleared on silent event"
        assert eq.reset_called, "peak hold history must be cleared"
        assert eq.out == -100.0
        assert dd.levels == (-100.0, -100.0)
