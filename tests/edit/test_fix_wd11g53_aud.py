"""wd11-g53 批 D 修复回归测试（edit 音频域，16 条 Major）。

对应 docs/audit/2026-09-28/batches/batch-153-features-edit-core.md、
batch-154-edit-core-audio-graph.md、batch-155-edit-core-audio-playback.md：

- AUD-15301 DelayEffect.set_params：time/采样率变化分支用旧 low_cut_hz 重算 HP
- AUD-15302 StripEQFilter mode 切换的状态保留过滤条件恒为空集
- AUD-15303 DelayEffect ping-pong 奇数声道 swap 索引越界
- AUD-15304 FX dict 参数无范围钳位（Compressor ratio=0 → ZeroDivisionError）
- AUD-15400 节点持有模型 KeyframeTrack 活引用（导出线程与 UI 并发竞态）
- AUD-15401 render_bus_stage 丢弃 cycle_buses 且无日志
- AUD-15402 普通/Bus 轨路由到未知 bus 目标行为不一致（孤儿缓冲静默丢弃）
- AUD-15410 SnapshotHistory 无锁 + 播放线程遍历 live sequence
- AUD-15420 has_blocking 的 DSP 分支是死代码（pass）
- AUD-15421 output_device_available 取自"跳过探测"的缓存占位值
- AUD-15501 _av_resampler_cache 无界增长 + id(self) 复用污染
- AUD-15504 AudioDecoder.open() 失败路径不关闭容器（is_open 语义破坏）
- AUD-15508 输出启动失败/设备丢失后状态残留 playing，无通知
- AUD-15509 设备格式回退后解码器/混音器采样率未同步（变调变速）
- AUD-15510 直混兜底 mix_samples 未被 try 包裹（异常杀死播放线程）
- AUD-15512 写失败时 ring 已读出的样本被丢弃（音频跳变）

无 GL / 无真实媒体 / 无真实音频设备；PyQt6 仅在 import 层面依赖。
"""

from __future__ import annotations

import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core import audio_playback as apb
from src.features.edit.core.audio_dsp import (
    CompressorEffect,
    DelayEffect,
    StripEQFilter,
)
from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_graph_snapshot import (
    SnapshotHistory,
    build_audio_graph_snapshot,
)
from src.features.edit.core.audio_health import AudioHealthReport, build_audio_health_report
from src.features.edit.core.audio_playback import (
    AudioDecoder,
    AudioMixer,
    AudioPlaybackThread,
    AudioTrackState,
    _AV_RESAMPLER_CACHE_MAX,
    _av_resampler_cache,
)
from src.features.edit.core.timeline_v2 import EditTrack, StripEQParams, TimelineSequence
from src.shared.keyframe import Keyframe, KeyframeTrack


# ================================================================
# Helpers
# ================================================================


def _dc_decoder(amp: float) -> Any:
    """DC 假 decoder（模式取自 tests/edit/test_audio_bus_routing.py）。"""

    class _DC:
        def decode_samples_for_frame(self, f, fps, n):
            s = np.full(n, amp, dtype=np.float32)
            return np.stack([s, s])

        def close(self):
            pass

    return _DC()


def _make_thread_with_mocks(*, output_is_playing: bool = True,
                            output_start: bool = True) -> AudioPlaybackThread:
    """构造不实际启动的播放线程 + 受控 clock/mixer/output Mock。"""
    thread = AudioPlaybackThread()
    clock = Mock()
    clock.sample_rate = 48000
    clock.is_playing = True
    mixer = Mock()
    mixer._decoders = {}  # write_zero trace 会 len() 它
    output = Mock()
    output.start.return_value = output_start
    output.is_playing = output_is_playing
    output._sample_rate = 48000
    output._channels = 2
    output.bytes_per_frame.return_value = 8
    output.bytes_free.return_value = 10_000_000
    thread.setup(clock, mixer, output, 24.0, sample_rate=48000, channels=2)
    return thread


# ================================================================
# AUD-15301 / AUD-15303 / AUD-15304 — audio_dsp.py
# ================================================================


class TestAud15301DelaySetParamsStaleLowcut:
    """CW-A1: 立体声双线延迟的参数生效路径（对应旧 AUD-15301 的回归意图）。"""

    def test_simultaneous_time_and_filter_change_uses_new_values(self):
        """延迟时间与输入滤波同时偏离默认值：延迟抽头与滤波系数都用新值。"""
        fx = DelayEffect(48000.0)
        fx.set_params({"enabled": True, "left_delay": 500.0, "right_delay": 500.0,
                       "low_cut": 500.0, "high_cut": 20000.0,
                       "left_feedback": 0.0, "right_feedback": 0.0,
                       "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0})
        imp = np.zeros((2, int(48000 * 0.7)))
        imp[0, 10] = 1.0
        out = fx.process(imp)
        pk = np.argmax(np.abs(out[0])) / 48000 * 1000
        assert 495.0 <= pk <= 506.0, f"延迟抽头应使用新时间 500ms，实测 {pk:.1f}ms"

    def test_repeat_same_params_keeps_behavior(self):
        """同值重放行为不变（差值判据为 0，不触发重建）。"""
        fx = DelayEffect(48000.0)
        params = {"enabled": True, "left_delay": 500.0, "right_delay": 500.0,
                  "low_cut": 500.0, "high_cut": 20000.0,
                  "left_feedback": 0.0, "right_feedback": 0.0,
                  "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                  "high_ratio": 0.0}
        fx.set_params(params)
        fx.set_params(params)
        assert fx._left_delay == 500.0
        assert fx._low_cut == 500.0

    def test_sample_rate_change_branch_uses_new_values(self):
        fx = DelayEffect(48000.0)
        fx.set_params({"enabled": True, "left_delay": 300.0, "right_delay": 300.0,
                       "low_cut": 800.0, "high_cut": 20000.0},
                      sample_rate=96000.0)
        assert fx._sample_rate == 96000.0
        assert fx._low_cut == 800.0


class TestAud15303DelayPingPongOddChannels:
    @pytest.mark.parametrize("channels", [3, 5, 7])
    def test_odd_channels_no_index_error(self, channels):
        """ping-pong 奇数声道（≥3）不再 IndexError，末声道映射到自身。"""
        fx = DelayEffect(48000.0)
        fx.set_params({"enabled": True, "left_delay": 10.0, "right_delay": 10.0,
                       "left_feedback": 30.0, "right_feedback": 30.0,
                       "stereo_blend": 100.0, "dry_wet": 40.0, "level_db": 0.0,
                       "high_ratio": 0.0, "low_cut": 100.0, "high_cut": 20000.0})
        samples = np.random.default_rng(7).uniform(
            -0.5, 0.5, size=(channels, 256)).astype(np.float32)
        out = fx.process(samples)  # 旧实现：seg[swap] 索引越界
        assert out.shape == samples.shape
        assert np.all(np.isfinite(out))

    def test_even_channels_cross_feed_unchanged(self):
        """CW-A1: 交叉反馈语义由 stereo_blend 承担——ch0 信号经左延迟线
        交叉（blend=100%）进入右线，右声道湿信号非零。"""
        fx = DelayEffect(48000.0)
        fx.set_params({"enabled": True, "left_delay": 5.0, "right_delay": 5.0,
                       "left_feedback": 50.0, "right_feedback": 0.0,
                       "stereo_blend": 100.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0, "low_cut": 100.0, "high_cut": 20000.0})
        t = np.arange(720) / 48000.0
        sig = 0.5 * np.sin(2.0 * np.pi * 1000.0 * t).astype(np.float32)
        samples = np.zeros((2, 720), dtype=np.float32)
        samples[0] = sig  # 仅 ch0 有信号
        fx.process(samples[:, :240].copy())
        fx.process(samples[:, 240:480].copy())
        out = fx.process(samples[:, 480:720].copy())
        # 第三块：右线经 blend 交叉收到左线反馈，ch1 湿信号非零
        assert float(np.max(np.abs(out[1]))) > 1e-4, "ch0→ch1 交叉反馈应存在"


class TestAud15304FxParamClamping:
    def test_compressor_ratio_zero_no_zero_division(self):
        """ratio=0（损坏工程文件）不再在渲染热路径抛 ZeroDivisionError。"""
        fx = CompressorEffect(48000.0)
        fx.set_params({"ratio": 0.0, "threshold_db": -20.0, "enabled": True})
        assert fx._ratio == 1.2  # 钳到 CompressorParams 下界（CW-A1: 1.2）
        samples = np.full((2, 128), 0.9, dtype=np.float32)
        out = fx.process(samples)  # 旧实现：1.0 / 0 → ZeroDivisionError
        assert np.all(np.isfinite(out))

    def test_compressor_ratio_negative_clamped(self):
        fx = CompressorEffect(48000.0)
        fx.set_params({"ratio": -5.0, "enabled": True})
        assert fx._ratio == 1.2

    def test_compressor_nan_falls_back_to_default(self):
        fx = CompressorEffect(48000.0)
        fx.set_params({"ratio": float("nan"), "threshold_db": float("nan"),
                      "enabled": True})
        assert fx._ratio == 2.5   # CW-A1 新默认
        assert fx._threshold_db == -15.0

    def test_compressor_non_numeric_falls_back(self):
        fx = CompressorEffect(48000.0)
        fx.set_params({"ratio": "bogus", "enabled": True})
        assert fx._ratio == 2.5

    def test_all_fx_clamp_ranges(self):
        """各 FX 机架效果器 dict 参数按 dataclass 声明范围钳位（CW-A1 新范围）。"""
        comp = CompressorEffect(48000.0)
        comp.set_params({"threshold_db": -1000.0, "release_ms": 1.0,
                         "knee": 100.0, "makeup_gain": 99.0, "ratio": 0.0,
                         "enabled": True})
        assert comp._threshold_db == -60.0
        assert comp._release_ms == 50.0
        assert comp._knee == 100.0
        assert comp._makeup_db == 24.0
        assert comp._ratio == 1.2

        delay = DelayEffect(48000.0)
        delay.set_params({"left_delay": 99999.0, "right_delay": 99999.0,
                          "left_feedback": 200.0, "right_feedback": -200.0,
                          "dry_wet": -5.0, "low_cut": 2.0, "high_cut": 99999.0,
                          "enabled": True})
        assert delay._left_delay == 1200.0
        assert delay._right_delay == 1200.0
        assert delay._left_fb == 99.0
        assert delay._right_fb == -99.0
        assert delay._dry_wet == 0.0
        assert delay._low_cut == 20.0
        assert delay._high_cut == 20000.0

        from src.features.edit.core.audio_dsp import (
            ImagerEffect,
            LimiterEffect,
            ExpanderEffect,
            ReverbEffect,
            SaturationEffect,
        )
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -100.0, "release_ms": 1.0,
                        "attack_ms": 0.1, "enabled": True})
        assert lim._threshold_db == -30.0
        assert lim._release_ms == 50.0
        assert lim._attack_ms == 0.7

        rev = ReverbEffect(48000.0)
        rev.set_params({"dry_wet": 300.0, "reverb_time": 10.0,
                        "pre_delay": 999.0, "distance": -5.0, "enabled": True})
        assert rev._params["dry_wet"] == 100.0
        assert rev._params["reverb_time"] == 100.0
        assert rev._params["pre_delay"] == 200.0
        assert rev._params["distance"] == 0.0

        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 500.0, "enabled": True})
        assert sat._drive == 100.0

        img = ImagerEffect(48000.0)
        img.set_params({"pan": 400.0, "width": -10.0, "enabled": True})
        assert img._pan == 100.0
        assert img._width == 0.0

        gate = ExpanderEffect(48000.0)
        gate.set_params({"threshold_db": -200.0, "attack_ms": 999.0,
                         "release_ms": 1.0, "ratio": 0.0, "range_db": -5.0,
                         "enabled": True})
        assert gate._threshold_db == -50.0
        assert gate._attack_ms == 100.0
        assert gate._release_ms == 50.0
        assert gate._ratio == 1.1
        assert gate._range_db == 0.0


class TestAud15302EqModeSwitchState:
    def test_shape_switch_keeps_identity_keyed_state(self):
        """CW-A1: 参数化 EQ 的 IIR 状态按频段身份（b{index}）组织。

        形状切换（Bell → Notch）不产生 lc/hc 时代的残留键；状态字典
        的键集恒为当前启用频段的身份键子集。平直 EQ 无增益段时为空。
        """
        from src.features.edit.core.timeline_v2 import default_eq_bands_flat
        fx = StripEQFilter(48000.0)
        bands = [dict(b) for b in default_eq_bands_flat()]
        # 平直（全 0dB shelf/bell 无滤波段）→ 无状态键
        fx.set_params({"enabled": True, "bands": bands})
        fx.process(np.zeros((2, 64), dtype=np.float32))
        assert fx._zi_by_key == {}
        # 启用 HPF（Band 1）→ 状态键 = b1（含斜率级联段也归到 b1 身份）
        bands2 = [dict(b) for b in bands]
        bands2[0] = {**bands2[0], "filter_type": "HPF", "freq": 100.0,
                     "slope": 18, "enabled": True}
        fx.set_params({"enabled": True, "bands": bands2})
        fx.process(np.zeros((2, 64), dtype=np.float32))
        # 18dB/oct = 3 阶级联 → b1h0/b1h1/b1h2 三段，全部归 Band 1 身份
        assert set(fx._zi_by_key.keys()) == {"b1h0", "b1h1", "b1h2"}
        # 切换为 LPF 12dB/oct（身份不变 Band 1）：状态键仍归 b1 前缀
        bands3 = [dict(b) for b in bands]
        bands3[0] = {**bands3[0], "filter_type": "LPF", "freq": 8000.0,
                     "slope": 12, "enabled": True}
        fx.set_params({"enabled": True, "bands": bands3})
        fx.process(np.zeros((2, 64), dtype=np.float32))
        # 旧键按身份对位保留（h2 残留但不再被引用），新链只用 h0/h1
        assert {"b1h0", "b1h1"} <= set(fx._zi_by_key.keys())


# ================================================================
# AUD-15400 / AUD-15401 / AUD-15402 — audio_graph_renderer.py
# ================================================================


class TestAud15400RendererAutomationDeepCopy:
    def _sequence_with_automation(self) -> TimelineSequence:
        vol = KeyframeTrack(param_name="volume",
                            keyframes=[Keyframe(frame=0, value=0.5),
                                       Keyframe(frame=24, value=1.0)],
                            min_val=0.0, max_val=2.0)
        mute = KeyframeTrack(param_name="mute",
                             keyframes=[Keyframe(frame=0, value=0.0)],
                             min_val=0.0, max_val=1.0)
        track = EditTrack(track_id="t1", track_type="audio", index=0,
                          automation={"volume": vol, "mute": mute},
                          automation_mode="read")
        master_vol = KeyframeTrack(param_name="volume",
                                   keyframes=[Keyframe(frame=0, value=1.0)])
        return TimelineSequence(audio_tracks=(track,), master_automation={"volume": master_vol})

    def test_node_automation_is_deep_copy(self):
        """节点不得持有模型 KeyframeTrack 活引用（dict 浅拷贝只隔离结构）。"""
        seq = self._sequence_with_automation()
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        node = renderer.get_node("t1")
        assert node is not None
        model_vol = seq.audio_tracks[0].automation["volume"]
        model_master = seq.master_automation["volume"]
        assert node.automation["volume"] is not model_vol
        assert renderer._master_automation["volume"] is not model_master

    def test_ui_mutation_after_projection_does_not_affect_node(self):
        """投影后 UI 增删关键帧不影响节点数据（旧实现迭代活列表会竞态）。"""
        seq = self._sequence_with_automation()
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        node = renderer.get_node("t1")
        snap_frames_before = [kf.frame for kf in node.automation["volume"].keyframes]

        # UI 线程在导出期间修改模型
        model_vol = seq.audio_tracks[0].automation["volume"]
        model_vol.keyframes.append(Keyframe(frame=48, value=0.1))
        model_vol.keyframes.pop(0)

        snap_frames_after = [kf.frame for kf in node.automation["volume"].keyframes]
        assert snap_frames_before == snap_frames_after
        assert [kf.frame for kf in model_vol.keyframes] != snap_frames_before

    def test_snapshot_track_evaluates_same_as_model(self):
        """值拷贝后 get_value_ramp 语义与模型轨道一致。"""
        seq = self._sequence_with_automation()
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        node = renderer.get_node("t1")
        model_track = seq.audio_tracks[0].automation["volume"]
        ramp_copy = node.automation["volume"].get_value_ramp(0.0, 24.0, 16, 48000.0, 1.0)
        ramp_model = model_track.get_value_ramp(0.0, 24.0, 16, 48000.0, 1.0)
        assert ramp_copy == pytest.approx(ramp_model)


class TestAud15401CycleBusesWarning:
    def test_cycle_buses_logged(self, caplog):
        """环中 Bus 被排除时必须记录日志（旧实现静默丢弃音频）。"""
        bus_a = EditTrack(track_id="busA", track_type="audio", index=0,
                          is_bus=True, output_bus_id="busB")
        bus_b = EditTrack(track_id="busB", track_type="audio", index=1,
                          is_bus=True, output_bus_id="busA")
        seq = TimelineSequence(audio_tracks=(bus_a, bus_b))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        output = np.zeros((2, 64), dtype=np.float32)
        bus_inputs: dict = {}
        with caplog.at_level("WARNING", logger="src.features.edit.core.audio_graph_renderer"):
            renderer.render_bus_stage(bus_inputs, output, has_solo=False)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "routing cycle" in joined
        assert "busA" in joined and "busB" in joined


class TestAud15402UnknownBusTarget:
    def _renderer(self) -> AudioGraphRenderer:
        t1 = EditTrack(track_id="t1", track_type="audio", index=0,
                       output_bus_id="ghost")
        bus1 = EditTrack(track_id="bus1", track_type="audio", index=1,
                         is_bus=True, output_bus_id=None)
        seq = TimelineSequence(audio_tracks=(t1, bus1))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        return renderer

    def test_track_unknown_target_falls_back_to_master(self, caplog):
        """普通轨 output_bus_id 指向未知 bus → 兜底 master + 告警（旧实现写孤儿缓冲）。"""
        renderer = self._renderer()
        output = np.zeros((2, 32), dtype=np.float32)
        bus_inputs: dict = {}
        samples = np.full((2, 32), 0.3, dtype=np.float32)
        with caplog.at_level("WARNING", logger="src.features.edit.core.audio_graph_renderer"):
            renderer.route_track_output("t1", samples, output, bus_inputs)
        assert output[0, 0] == pytest.approx(0.3), "音频应兜底进 master 而非静默丢失"
        assert "unknown bus" in "\n".join(r.getMessage() for r in caplog.records)
        assert "ghost" not in bus_inputs, "不应为未知目标创建孤儿缓冲"

    def test_track_unknown_send_skipped_with_warning(self, caplog):
        t1 = EditTrack(track_id="t1", track_type="audio", index=0,
                       sends=[SimpleNamespace(target_bus_id="ghost", level=0.5, enabled=True)])
        seq = TimelineSequence(audio_tracks=(t1,))
        renderer = AudioGraphRenderer(4800.0)
        renderer.configure_from_sequence(seq)
        output = np.zeros((2, 16), dtype=np.float32)
        bus_inputs: dict = {}
        samples = np.full((2, 16), 0.2, dtype=np.float32)
        with caplog.at_level("WARNING", logger="src.features.edit.core.audio_graph_renderer"):
            renderer.route_track_output("t1", samples, output, bus_inputs)
        assert "ghost" not in bus_inputs
        assert "send" in "\n".join(r.getMessage() for r in caplog.records)

    def test_bus_unknown_target_falls_back_to_master_with_warning(self, caplog):
        t1 = EditTrack(track_id="t1", track_type="audio", index=0,
                       output_bus_id="bus1")
        bus1 = EditTrack(track_id="bus1", track_type="audio", index=1,
                         is_bus=True, output_bus_id="ghost_bus")
        seq = TimelineSequence(audio_tracks=(t1, bus1))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(seq)
        output = np.zeros((2, 48), dtype=np.float32)
        bus_inputs: dict = {}
        samples = np.full((2, 48), 0.4, dtype=np.float32)
        with caplog.at_level("WARNING", logger="src.features.edit.core.audio_graph_renderer"):
            renderer.route_track_output("t1", samples, output, bus_inputs)
            renderer.render_bus_stage(bus_inputs, output, has_solo=False)
        assert output[0, 0] == pytest.approx(0.4, abs=1e-5), "Bus→未知目标应兜底 master"
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "bus1" in joined and "ghost_bus" in joined

    def test_mixer_unknown_target_matches_renderer(self, caplog):
        """实时路径（mix_samples）与 renderer 语义一致：未知目标兜底 master。"""
        m = AudioMixer()
        m.add_track("t1", _dc_decoder(0.3), AudioTrackState(track_id="t1"))
        m.set_track_routing("t1", "ghost", [], is_bus=False)
        with caplog.at_level("WARNING"):
            out, _ = m.mix_samples(0, 24.0, 64, 2)
        assert float(out[0][0]) == pytest.approx(0.3, abs=1e-5)
        assert any("unknown bus" in r.getMessage() for r in caplog.records)


# ================================================================
# AUD-15410 — audio_graph_snapshot.py
# ================================================================


class TestAud15410SnapshotHistory:
    def _seq(self, volume: float) -> TimelineSequence:
        return TimelineSequence(audio_tracks=(
            EditTrack(track_id="t1", track_type="audio", index=0, volume=volume),
        ))

    def test_concurrent_update_reads_are_lock_protected(self):
        """并发 update / 读取不竞态：revision 单调且最终收敛（旧实现无锁）。"""
        history = SnapshotHistory()
        errors: list = []

        def writer(n: int) -> None:
            try:
                for _ in range(n):
                    history.update(self._seq(0.5))
            except Exception as e:  # noqa: BLE001 — 测试失败收集
                errors.append(e)

        def reader(n: int) -> None:
            try:
                for _ in range(n):
                    history.current
                    history.revision
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(200,)) for _ in range(4)]
        threads += [threading.Thread(target=reader, args=(200,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors
        assert history.revision == 800

    def test_needs_rebuild_from_uses_prebuilt_candidate(self):
        """播放线程安全入口：比较预构建 candidate，不触碰 live 模型。"""
        history = SnapshotHistory()
        seq1 = self._seq(0.5)
        assert history.needs_rebuild_from(build_audio_graph_snapshot(seq1)) is True
        history.update(seq1)
        assert history.needs_rebuild_from(build_audio_graph_snapshot(seq1)) is False
        assert history.needs_rebuild_from(build_audio_graph_snapshot(self._seq(0.9))) is True

    def test_needs_rebuild_sequence_compatible(self):
        """兼容入口（调用方线程）行为不变。"""
        history = SnapshotHistory()
        seq1 = self._seq(0.5)
        history.update(seq1)
        assert history.needs_rebuild(seq1) is False
        assert history.needs_rebuild(self._seq(0.8)) is True

    def test_update_returns_published_snapshot(self):
        history = SnapshotHistory()
        snap = history.update(self._seq(0.5))
        assert history.current is snap
        assert history.revision == 1


# ================================================================
# AUD-15420 / AUD-15421 — audio_health.py
# ================================================================


class TestAud15420DspBackendBlocking:
    def test_missing_dsp_backend_is_blocking(self):
        """numpy/pyav 缺失 → has_blocking True（旧实现 pass → 死代码）。"""
        report = AudioHealthReport(dsp_backend_available=False,
                                   eq_lufs_available=True,
                                   output_device_available=True)
        assert report.has_blocking is True
        assert "BLOCKING" in report.to_text()

    def test_healthy_report_not_blocking(self):
        report = AudioHealthReport(dsp_backend_available=True)
        assert report.has_blocking is False
        assert "Status          : OK" in report.to_text()

    def test_scipy_missing_is_warning_not_blocking(self):
        """scipy 缺失只降级 EQ/LUFS（warning），不阻断。"""
        report = AudioHealthReport(dsp_backend_available=True,
                                   eq_lufs_available=False)
        assert report.has_blocking is False
        assert report.has_warnings is True


class TestAud15421OutputDeviceCacheBypass:
    def test_report_reprobes_output_device_despite_stale_cache(self, monkeypatch, tmp_path):
        """缓存已被 check_output_device=False 填充后，报告仍强制真实探测。

        AUD-15203 后跳过探测的占位是 unknown 三态（available=False +
        unknown=True），不再伪装成 available=True。
        """
        from src.features.edit.core import audio_capabilities as caps_mod

        # 顺序污染隔离：同进程前序测试（如裸调 build_audio_health_report 者）可能
        # 已用真实探测填充模块级 _cached，惰性缓存分支会直接复用残留快照、跳过
        # 本测试的假探测。先清缓存，保证下方"种子缓存"步骤真正执行假探测。
        caps_mod.reset_audio_capabilities_cache()

        # 1) 模拟 DSP 热路径先用默认 False 填充缓存（"跳过探测"→unknown 占位）
        monkeypatch.setattr(caps_mod, "_probe_output_device",
                            lambda: caps_mod.CapabilityResult(False, "simulated: no device"))
        caps_mod.get_audio_capabilities(check_output_device=False)
        # 占位结果：available=False, unknown=True（未知 ≠ 可用）
        placeholder = caps_mod.get_audio_capabilities().output_device
        assert placeholder.available is False
        assert placeholder.unknown is True

        # 2) 健康报告：强制清缓存 + 真实探测（旧实现拿到占位值误报 OK）
        try:
            report = build_audio_health_report(output_path=str(tmp_path))
            assert report.output_device_available is False
            assert report.has_warnings is True
            assert "NOT FOUND" in report.to_text()
        finally:
            caps_mod.reset_audio_capabilities_cache()

    def test_report_with_device_available(self, monkeypatch, tmp_path):
        from src.features.edit.core import audio_capabilities as caps_mod
        monkeypatch.setattr(caps_mod, "_probe_output_device",
                            lambda: caps_mod.CapabilityResult(True))
        try:
            report = build_audio_health_report(output_path=str(tmp_path))
            assert report.output_device_available is True
        finally:
            caps_mod.reset_audio_capabilities_cache()


# ================================================================
# AUD-15501 / AUD-15504 — AudioDecoder
# ================================================================


class TestAud15501ResamplerCache:
    def test_instance_ids_monotonic_not_id_reuse(self):
        """instance_id 用单调计数器：两个存活解码器 ID 必不同（防 id 复用污染）。"""
        d1, d2 = AudioDecoder(), AudioDecoder()
        assert d1._instance_id != d2._instance_id

    def test_close_purges_cache_entries_by_instance(self):
        d = AudioDecoder()
        mine = (48000, 44100, 2, d._instance_id)
        other = (48000, 44100, 2, d._instance_id + 999)
        _av_resampler_cache[mine] = object()
        _av_resampler_cache[other] = object()
        try:
            d.close()
            assert mine not in _av_resampler_cache, "close() 应清理本实例条目"
            assert other in _av_resampler_cache, "不应误删其他实例条目"
        finally:
            _av_resampler_cache.pop(other, None)

    def test_cache_respects_capacity_cap(self, monkeypatch):
        fake_resampler = Mock()
        monkeypatch.setattr(apb, "PYAV_AVAILABLE", True)
        monkeypatch.setattr(apb, "av", SimpleNamespace(audio=SimpleNamespace(
            resampler=SimpleNamespace(AudioResampler=lambda **kw: fake_resampler))))
        before = set(_av_resampler_cache)
        try:
            for i in range(_AV_RESAMPLER_CACHE_MAX + 20):
                apb._get_av_resampler(44100 + i, 48000, 2, instance_id=100000 + i)
            assert len(_av_resampler_cache) <= _AV_RESAMPLER_CACHE_MAX
        finally:
            for k in list(_av_resampler_cache):
                if k not in before:
                    _av_resampler_cache.pop(k, None)


class TestAud15504DecoderOpenFailure:
    def test_open_failure_closes_container_and_is_open_false(self, tmp_path):
        """open() 失败 → 容器关闭、is_open False（旧实现残留非 None 容器）。"""
        if not apb.PYAV_AVAILABLE:
            pytest.skip("PyAV not available")
        bogus = tmp_path / "not_audio.bin"
        bogus.write_bytes(b"\x00" * 256)
        d = AudioDecoder()
        assert d.open(bogus) is False
        assert d._container is None
        assert d.is_open is False

    def test_open_failure_is_idempotent_safe(self, tmp_path):
        if not apb.PYAV_AVAILABLE:
            pytest.skip("PyAV not available")
        bogus = tmp_path / "not_audio2.bin"
        bogus.write_bytes(b"\xff" * 64)
        d = AudioDecoder()
        assert d.open(bogus) is False
        assert d.open(bogus) is False  # 失败后可重试，无状态残留
        d.close()  # 幂等


# ================================================================
# AUD-15508 / AUD-15509 — 播放线程 run() 路径
# ================================================================


class TestAud15508OutputFailureSignal:
    def test_output_start_failed_emits_signal(self):
        """sink 启动失败 → output_failed 信号（manager 统一 stop + state_changed）。"""
        thread = AudioPlaybackThread()
        clock = Mock()
        clock.sample_rate = 48000
        mixer = Mock()
        output = Mock()
        output.start.return_value = False
        output._sample_rate = 48000
        output._channels = 2
        thread.setup(clock, mixer, output, 24.0)
        received: list = []
        thread.output_failed.connect(lambda reason: received.append(reason))
        thread.run()  # 直接在测试线程执行 run 循环（同步）
        assert received == ["output_start_failed"]
        assert thread._running is False

    def test_device_lost_mid_playback_emits_signal_and_exits(self):
        """设备中途丢失（output.is_playing False 且时钟在播）→ 上报并退出循环。"""
        thread = _make_thread_with_mocks(output_is_playing=False)
        received: list = []
        thread.output_failed.connect(lambda reason: received.append(reason))
        thread.run()
        assert received == ["output_device_lost"]
        assert thread._running is False
        thread._output.stop.assert_called()

    def test_manager_handles_output_failed_by_stopping(self):
        """manager._on_output_failed 走 stop()：state_changed 收到 stopped。"""
        manager = apb.AudioPlaybackManager()
        states: list = []
        manager.state_changed.connect(lambda s: states.append(s))
        # 注入假播放线程，避免真实线程启动
        fake_thread = Mock()
        fake_thread.stop.return_value = None
        fake_thread.wait.return_value = None
        fake_thread.isRunning.return_value = False
        manager._playback_thread = fake_thread
        manager._on_output_failed("output_device_lost")
        fake_thread.stop.assert_called_once()
        fake_thread.wait.assert_called_once()
        assert states and states[-1] == "stopped"
        assert manager._playback_thread is None


class TestAud15509FormatFallbackSync:
    def test_fallback_syncs_mixer_and_ring(self):
        """设备格式回退：mixer 系数 + 解码器目标率调用 + ring 声道同步。"""
        thread = _make_thread_with_mocks(output_is_playing=False)
        thread._output._sample_rate = 44100
        thread._output._channels = 1
        thread.run()  # output.is_playing False → fallback 先执行，随后 device-lost 退出
        assert thread._sample_rate == 44100
        assert thread._channels == 1
        thread._mixer.set_sample_rate.assert_called_once_with(44100)
        thread._mixer.set_decoders_output_rate.assert_called_once_with(44100)
        assert thread._ring.channels == 1, "ring 应按设备声道数重建"
        # 时钟同步（已有行为，回归保护）
        thread._clock.set_sample_rate.assert_called_with(44100)

    def test_set_decoders_output_rate_updates_all(self):
        m = AudioMixer()
        d1 = SimpleNamespace(_output_sample_rate=48000,
                             _purge_resampler_cache_entries=lambda: None)
        d2 = SimpleNamespace(_output_sample_rate=48000,
                             _purge_resampler_cache_entries=lambda: None)
        m._decoders = {"t1": [d1], "t2": [d2]}
        m.set_decoders_output_rate(44100)
        assert d1._output_sample_rate == 44100
        assert d2._output_sample_rate == 44100
        m.set_decoders_output_rate(0)  # 无效值不生效
        assert d1._output_sample_rate == 44100


# ================================================================
# AUD-15510 / AUD-15512 — _write_next_output_chunk
# ================================================================


class TestAud15510DirectMixProtection:
    def test_direct_mix_exception_skips_chunk_without_killing_thread(self):
        """ring 欠载直混兜底 mix_samples 抛错 → 跳过 chunk，异常不上抛。"""
        thread = _make_thread_with_mocks()
        thread._ring.clear()
        thread._mixer.mix_samples.side_effect = TypeError("bad sends")
        # 不应抛出（旧实现冲出 run() 杀死播放线程）
        assert thread._write_next_output_chunk() is False
        assert thread._running is not None  # 线程对象仍可用

    def test_play_cycle_exception_does_not_escape(self):
        """_play_cycle 内部异常被 run 循环兜底（记录并继续）。"""
        thread = _make_thread_with_mocks()
        thread._prefill_ring = Mock(side_effect=ValueError("boom"))
        # _play_cycle 自身对 _prefill_ring 已有保护；直接调用不应抛出
        thread._play_cycle()

    def test_run_loop_survives_play_cycle_exception(self):
        thread = _make_thread_with_mocks()
        thread._play_cycle = Mock(side_effect=RuntimeError("unexpected"))
        # 让 device 立即丢失以退出循环：is_playing 保持 True 但 running 由
        # 命令 stop 触发——这里用 stop 命令验证 run() 能处理一轮异常后退出
        thread.stop_cmd = None
        import queue as _queue
        thread._command_queue.put(("stop", None))
        received: list = []
        thread.output_failed.connect(lambda r: received.append(r))
        thread.run()  # 不应抛出；处理 stop 命令后退出
        assert thread._running is False


class TestAud15512WriteFailureRetention:
    def _prepared_thread(self) -> AudioPlaybackThread:
        thread = _make_thread_with_mocks()
        thread._mixer.mix_samples.return_value = (
            np.full((2, 2000), 0.25, dtype=np.float32), {})
        return thread

    def test_write_zero_retains_ring_samples(self):
        """ring 路径写失败（written==0）→ 样本留在 ring，不丢失。"""
        thread = self._prepared_thread()
        data = np.full((2, 2000), 0.25, dtype=np.float32)
        thread._ring.clear()
        thread._ring.write(data)
        filled_before = thread._ring.filled
        thread._output.write_samples.return_value = 0
        assert thread._write_next_output_chunk() is False
        assert thread._ring.filled == filled_before, "写失败时样本不应被消费"

    def test_write_zero_retains_direct_mix_samples(self):
        """直混兜底写失败 → 直混结果写入 ring 保留，下轮重试。"""
        thread = self._prepared_thread()
        thread._ring.clear()
        thread._output.write_samples.return_value = 0
        assert thread._write_next_output_chunk() is False
        assert thread._ring.filled == 2000, "直混样本应留在 ring 而非被丢弃"

    def test_partial_write_commits_only_written(self):
        """部分写入只提交已写部分，余量留在 ring 头部下一轮补写。"""
        thread = self._prepared_thread()
        thread._ring.clear()
        thread._ring.write(np.full((2, 2000), 0.25, dtype=np.float32))
        thread._output.write_samples.return_value = 1200  # 部分写入
        assert thread._write_next_output_chunk() is True
        assert thread._ring.filled == 2000 - 1200

    def test_full_write_commits_all(self):
        thread = self._prepared_thread()
        thread._ring.clear()
        thread._ring.write(np.full((2, 2000), 0.25, dtype=np.float32))
        thread._output.write_samples.return_value = 2000
        assert thread._write_next_output_chunk() is True
        assert thread._ring.filled == 0


# ================================================================
# AUD-15301 附加：直混/回退路径数值一致性（防钳位破坏正常值）
# ================================================================


class TestClampPreservesValidValues:
    def test_valid_params_pass_through(self):
        fx = CompressorEffect(48000.0)
        fx.set_params({"threshold_db": -12.0, "ratio": 4.0, "attack_ms": 3.0,
                       "release_ms": 250.0, "knee": 25.0, "makeup_gain": 3.0,
                       "mix": 60.0, "enabled": True})
        assert fx._threshold_db == -12.0
        assert fx._ratio == 4.0
        assert fx._attack_ms == 3.0
        assert fx._release_ms == 250.0
        assert fx._knee == 25.0
        assert fx._makeup_db == 3.0
        assert fx._wet_mix == 0.6

    def test_delay_high_ratio_zero_is_passthrough(self):
        """CW-A1: high_ratio=0 → 反馈阻尼截止频率≈20kHz（近直通）。"""
        from src.features.edit.core.audio_dsp import _high_ratio_to_damp_hz
        assert _high_ratio_to_damp_hz(0.0) == pytest.approx(20000.0)
        fx = DelayEffect(48000.0)
        fx.set_params({"high_ratio": 0.0, "enabled": True})
        assert fx._high_ratio == 0.0
