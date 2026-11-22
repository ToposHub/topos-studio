"""B00 反例回归：把审计脚本五类反例固化为可执行 pytest 断言（主计划 §6/§7-10）。

来源：docs/validation/2026-11-22-mixer-professional-audit.{py,json}（2026-11-22，
revision 37dd3acc）。这些测试声明**正确合同**（oracle 独立于生产函数推导），
修复前失败、修复后通过；不允许把当前错误输出存成 golden。

覆盖：
1. 串联 Bus PDC（B02）：dry + wet 经 bus1(100)→bus2(200) 必须在共同样本汇合。
2. Trim 渲染（B03）：离线图必须消费 EditTrack.automation_trim。
3. 逐声音字段签名（B01）：send.tap/muted、trim、VCA 均须同时改变实时签名
   与 AudioGraphSnapshot.stable_hash。
4. 缺测量 QC（B04）：仅 integrated_lufs 不能通过交付评估；NaN/bool 同样阻断。
5. VCA×Read 自动化（B03）：实时 ramp 分支不得丢失 VCA 倍率。
另有：有理帧时钟边界（§6）与 fixture 生成器确定性、WAV 往返、对拍工具自检。
"""
from __future__ import annotations

import math
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tests.support import sound_fixtures as sf

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.delivery_preset import evaluate_delivery, preset_by_id
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
    VcaGroup,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


class DelayOnlyEffect:
    """确定性纯延迟效果：声明真实算法延迟（与审计探针同构）。"""

    def __init__(self, latency_samples: int) -> None:
        self._latency = latency_samples
        self._history = np.zeros((2, latency_samples), dtype=np.float32)

    def latency_samples(self) -> int:
        return self._latency

    def tail_estimate_samples(self) -> int:
        return self._latency

    def sidechain_sources(self) -> set[str]:
        return set()

    def set_spectrum_metering(self, _enabled: bool) -> None:
        return None

    def process(
        self, samples: np.ndarray, sidechain_by_id: object = None,
    ) -> np.ndarray:
        combined = np.concatenate((self._history, samples), axis=1)
        count = samples.shape[-1]
        self._history = combined[:, count:]
        return combined[:, :count]


# ─────────────────────────────────────────────────────────────────────────────
# 1. B02 反例：串联 Bus PDC
# ─────────────────────────────────────────────────────────────────────────────

class TestSeriesBusPdc:
    """dry 直达 Master；wet 经 bus1(100)→bus2(200)。

    合同：同源两路在 Master 汇合处落在同一绝对样本（100 + 300 = 400），
    幅度 2.0；声明总 PDC = 最迟路径 300 样本。
    """

    def _build(self) -> tuple[AudioGraphRenderer, np.ndarray]:
        sequence = TimelineSequence(audio_tracks=(
            EditTrack("dry", "Dry", "audio", 0),
            EditTrack("wet", "Wet", "audio", 1, output_bus_id="bus1"),
            EditTrack("bus1", "Bus 1", "audio", 2, is_bus=True,
                      output_bus_id="bus2"),
            EditTrack("bus2", "Bus 2", "audio", 3, is_bus=True),
        ))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(sequence)
        bus1 = renderer.get_node("bus1")
        bus2 = renderer.get_node("bus2")
        assert bus1 is not None and bus2 is not None
        bus1.effect_chain = DelayOnlyEffect(100)
        bus2.effect_chain = DelayOnlyEffect(200)
        renderer._recompute_pdc()
        pulse = np.zeros((2, 2000), dtype=np.float32)
        pulse[:, 100] = 1.0
        return renderer, pulse

    def _render(self, renderer: AudioGraphRenderer, pulse: np.ndarray) -> np.ndarray:
        output = np.zeros_like(pulse)
        bus_inputs: dict[str, np.ndarray] = {}
        for track_id in ("dry", "wet"):
            processed = renderer.render_track_block(track_id, pulse, 0)
            assert processed is not None
            renderer.route_track_output(track_id, processed, output, bus_inputs)
        renderer.render_bus_stage(bus_inputs, output, False)
        return output

    def test_pulse_converges_at_common_sample(self) -> None:
        renderer, pulse = self._build()
        output = self._render(renderer, pulse)
        # 不得为通过测试平移候选音频：脉冲位置必须精确
        assert sf.pulse_positions(output) == [400], (
            f"pulse positions {sf.pulse_positions(output)} != [400]")
        assert math.isclose(float(output[0, 400]), 2.0, rel_tol=1e-6, abs_tol=1e-6)

    def test_declared_total_pdc_is_longest_path(self) -> None:
        renderer, _pulse = self._build()
        assert renderer.pdc_total_samples() == 300

    def test_parallel_buses_and_pre_send_converge(self) -> None:
        """并联 Bus + pre/post send 混合图：同源各路径仍同点汇合。"""
        sequence = TimelineSequence(audio_tracks=(
            EditTrack("src", "Src", "audio", 0, output_bus_id="busA",
                      sends=(Send(send_id="s1", target_bus_id="busB",
                                  level=1.0, tap="post_fader"),)),
            EditTrack("busA", "Bus A", "audio", 1, is_bus=True),
            EditTrack("busB", "Bus B", "audio", 2, is_bus=True),
        ))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(sequence)
        renderer.get_node("busA").effect_chain = DelayOnlyEffect(700)
        renderer.get_node("busB").effect_chain = DelayOnlyEffect(300)
        renderer._recompute_pdc()
        pulse = np.zeros((2, 4000), dtype=np.float32)
        pulse[:, 100] = 1.0
        output = np.zeros_like(pulse)
        bus_inputs: dict[str, np.ndarray] = {}
        processed = renderer.render_track_block("src", pulse, 0)
        assert processed is not None
        renderer.route_track_output("src", processed, output, bus_inputs)
        renderer.render_bus_stage(bus_inputs, output, False)
        # 最迟路径 = busA 700（post send 只取 300）→ 汇合点 100+700=800
        assert sf.pulse_positions(output) == [800]
        assert math.isclose(float(output[0, 800]), 2.0, rel_tol=1e-6, abs_tol=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# 2. B03 反例：Trim 渲染
# ─────────────────────────────────────────────────────────────────────────────

class TestTrimExport:
    """EditTrack volume trim=0.5；输入 0.5 → 输出 0.25（模型求值一致）。"""

    def test_offline_renderer_consumes_volume_trim(self) -> None:
        track = EditTrack("track", "Track", "audio", 0).with_automation_trim(
            "volume", 0.5)
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(TimelineSequence(audio_tracks=(track,)))
        source_amplitude = 0.5
        output = renderer.render_track_block(
            "track", np.full((2, 16), source_amplitude, dtype=np.float32), 0)
        assert output is not None
        model_gain = track.get_automation_value("volume", 0, 1.0)
        assert math.isclose(model_gain, 0.5)
        assert math.isclose(float(output[0, 0]), source_amplitude * model_gain,
                            rel_tol=1e-6, abs_tol=1e-6), (
                f"trim not consumed offline: {float(output[0, 0])}")

    def test_trim_with_read_automation_curve(self) -> None:
        """trim × 自动化曲线逐样本相乘（曲线 0→1 跨块边界，模型作 oracle）。"""
        track = EditTrack("track", "Track", "audio", 0).with_automation_trim(
            "volume", 0.5)
        curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        curve.add_keyframe(Keyframe(frame=0.0, value=0.0))
        curve.add_keyframe(Keyframe(frame=24.0, value=1.0))
        track = replace(track, automation={"volume": curve},
                        automation_mode="read")
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(TimelineSequence(audio_tracks=(track,)))
        samples = np.full((2, 480), 0.5, dtype=np.float32)
        output = renderer.render_track_block("track", samples, 0, fps=24.0)
        assert output is not None
        # 首点：曲线起点 0 → 输出 0（trim 不复活静音段）
        assert float(output[0, 0]) < 1e-6
        # 末点 oracle：模型 get_automation_value（已含 trim）× 源幅度；
        # 帧位置 = 样本 479 × fps / rate = 0.2395
        expected_last = 0.5 * track.get_automation_value(
            "volume", 479.0 * 24.0 / 48000.0, 1.0)
        assert math.isclose(float(output[0, -1]), expected_last,
                            rel_tol=1e-4, abs_tol=1e-7), (
                f"ramp×trim mismatch: {float(output[0, -1])} vs {expected_last}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. B01 反例：逐声音字段签名
# ─────────────────────────────────────────────────────────────────────────────

_AUDIBLE_VARIANTS = ("send_tap", "send_muted", "automation_trim", "vca_gain",
                     "clip_gain_control")


class TestSoundSignatureCompleteness:
    """只改一个可听字段 → 实时签名与快照 hash 都必须变化。"""

    def _variants(self) -> dict:
        clip = ClipInstance("clip", "asset", "track", 0, 100, 0, 100)
        track = EditTrack("track", "Track", "audio", 0, clips=(clip,),
                          sends=(Send(send_id="s", target_bus_id="bus",
                                      level=0.5),))
        bus = EditTrack("bus", "Bus", "audio", 1, is_bus=True)
        sequence = TimelineSequence(audio_tracks=(track, bus))
        variants = {
            "send_tap": replace(sequence, audio_tracks=(
                replace(track, sends=(replace(track.sends[0], tap="pre_fader"),)),
                bus)),
            "send_muted": replace(sequence, audio_tracks=(
                replace(track, sends=(replace(track.sends[0], muted=True),)), bus)),
            "automation_trim": replace(sequence, audio_tracks=(
                track.with_automation_trim("volume", 0.5), bus)),
            "vca_gain": replace(sequence, vcas=(
                VcaGroup(vca_id="vca", member_track_ids=("track",),
                         gain_db=-6.0),)),
            "clip_gain_control": replace(sequence, audio_tracks=(
                replace(track, clips=(replace(clip, audio_gain_db=-6.0),)), bus)),
        }
        return sequence, variants

    def _live_signature(self, sequence: TimelineSequence) -> object:
        from src.features.edit.ui.edit_page import EditPage
        project = SimpleNamespace(assets={
            "asset": SimpleNamespace(path="audit-synthetic.wav", fps=24.0),
        })
        host = SimpleNamespace(_pre_render_signature_state=lambda *_a: None)
        return EditPage._build_audio_timeline_signature(
            host, sequence.audio_tracks, project, 24.0, timeline=sequence)

    @pytest.mark.parametrize("variant", _AUDIBLE_VARIANTS)
    def test_field_changes_live_signature(self, variant: str) -> None:
        sequence, variants = self._variants()
        base = self._live_signature(sequence)
        assert self._live_signature(variants[variant]) != base, (
            f"live signature blind to audible field: {variant}")

    @pytest.mark.parametrize("variant", _AUDIBLE_VARIANTS)
    def test_field_changes_snapshot_hash(self, variant: str) -> None:
        sequence, variants = self._variants()
        base = build_audio_graph_snapshot(sequence).stable_hash
        assert build_audio_graph_snapshot(variants[variant]).stable_hash != base, (
            f"snapshot hash blind to audible field: {variant}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. B04 反例：缺测量 QC
# ─────────────────────────────────────────────────────────────────────────────

class TestDeliveryQcMissingMeasurements:
    """声明了限值的指标必须有有效测量；缺测/NaN/bool/字符串全部阻断。"""

    def _preset(self):
        preset = preset_by_id("broadcast-ebu-r128-v1")
        assert preset is not None
        return preset.with_stems(("FullMix",))

    def test_missing_peak_measurements_block(self) -> None:
        findings, passed = evaluate_delivery(
            self._preset(), {"FullMix": {"integrated_lufs": -23.0}})
        assert passed is False
        assert findings

    def test_nan_measurements_block(self) -> None:
        findings, passed = evaluate_delivery(self._preset(), {"FullMix": {
            "integrated_lufs": -23.0,
            "true_peak_dbtp": float("nan"),
            "peak_dbfs": -1.0,
        }})
        assert passed is False

    def test_bool_and_string_measurements_block(self) -> None:
        for bad in (True, "-2.0", None):
            findings, passed = evaluate_delivery(self._preset(), {"FullMix": {
                "integrated_lufs": -23.0,
                "true_peak_dbtp": bad,
                "peak_dbfs": -1.0,
            }})
            assert passed is False, f"non-numeric {bad!r} must block"

    def test_complete_measurements_pass(self) -> None:
        findings, passed = evaluate_delivery(self._preset(), {"FullMix": {
            "integrated_lufs": -23.0,
            "true_peak_dbtp": -2.0,
            "peak_dbfs": -1.0,
        }})
        assert passed is True, findings


# ─────────────────────────────────────────────────────────────────────────────
# 5. B03 反例：VCA × Read 自动化
# ─────────────────────────────────────────────────────────────────────────────

class TestVcaWithReadAutomation:
    """volume 自动化 ramp 分支必须保留 VCA 倍率（两分支同结果 0.25）。"""

    class _ConstantDecoder:
        _timeline_start = 0
        _timeline_duration = 48
        _channels = 2

        def decode_samples_for_frame(
            self, frame: int, fps: float, num_samples: int,
        ) -> np.ndarray:
            return np.full((2, num_samples), 0.5, dtype=np.float32)

        def close(self) -> None:
            pass

    def _mix(self) -> tuple[AudioMixer, float, float]:
        mixer = AudioMixer()
        mixer.add_track("track", self._ConstantDecoder(), AudioTrackState(
            track_id="track", volume=1.0))
        try:
            mixer.set_vca_overrides({"track": 0.5})
            static, _levels = mixer.mix_samples(0, 24.0, 480, 2)
            curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
            curve.add_keyframe(Keyframe(frame=0.0, value=1.0))
            curve.add_keyframe(Keyframe(frame=24.0, value=1.0))
            mixer.set_track_automation("track", {"volume": curve})
            mixer.set_track_automation_mode("track", "read")
            automated, _levels = mixer.mix_samples(0, 24.0, 480, 2)
            return mixer, float(np.max(np.abs(static))), float(
                np.max(np.abs(automated)))
        finally:
            mixer.remove_track("track")

    def test_vca_factor_preserved_under_read_automation(self) -> None:
        _mixer, static_peak, automated_peak = self._mix()
        assert math.isclose(static_peak, 0.25, rel_tol=1e-6, abs_tol=1e-6)
        assert math.isclose(automated_peak, 0.25, rel_tol=1e-6, abs_tol=1e-6), (
            f"VCA lost in ramp branch: {automated_peak}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. B00 基线工具自检：有理时钟、生成器确定性、WAV 往返、对拍工具
# ─────────────────────────────────────────────────────────────────────────────

class TestRationalFrameClock:
    """§6 绝对边界式：23.976/24/25/29.97 与 44.1/48/96 kHz 组合。"""

    @pytest.mark.parametrize("fps_num,fps_den,rate", [
        (24000, 1001, 48000),   # 23.976
        (24, 1, 48000),
        (25, 1, 48000),
        (30000, 1001, 48000),   # 29.97
        (24, 1, 44100),
        (24, 1, 96000),
    ])
    def test_adjacent_frames_share_boundary(self, fps_num: int, fps_den: int,
                                            rate: int) -> None:
        fps = Fraction(fps_num, fps_den)
        total = 0
        for frame in range(300):
            start, end = sf.frame_sample_bounds(frame, rate, fps)
            assert end > start
            if frame > 0:
                assert start == prev_end  # 相邻帧共享边界
            total += end - start
            prev_end = end
        expected = (300 * rate * fps.denominator) // fps.numerator
        assert total == expected  # 长片累计无漂移


class TestFixtureGenerators:
    """固定 seed 生成器：确定性 + 布局身份。"""

    def test_generators_deterministic(self) -> None:
        a = sf.pink_noise(48000, 0.5, seed=7)
        b = sf.pink_noise(48000, 0.5, seed=7)
        assert np.array_equal(a, b)
        assert sf.pink_noise(48000, 0.5, seed=8).shape == a.shape

    def test_wav_roundtrip_24bit(self, tmp_path: Path) -> None:
        pcm = sf.sine_tone(48000, 0.25, 1000.0, 0.5, channels=2)
        path = sf.write_wav(tmp_path / "t24.wav", pcm, 48000, bit_depth=24)
        back, rate = sf.read_wav(path)
        assert rate == 48000
        assert back.shape == pcm.shape
        # 24-bit 量化误差 ≤ 1/2 LSB
        assert float(np.max(np.abs(back - pcm))) <= 1.0 / 8388608.0 * 8

    def test_wav_roundtrip_float32(self, tmp_path: Path) -> None:
        pcm = sf.dialogue_envelope(48000, 0.2, channels=2)
        path = sf.write_wav(tmp_path / "t32.wav", pcm, 48000, bit_depth=32)
        back, rate = sf.read_wav(path)
        assert rate == 48000
        assert sf.null_test(back, pcm)

    def test_compare_pcm_detects_misalignment(self) -> None:
        a = sf.one_hot_pulse(2, 1000, 100)
        b = sf.one_hot_pulse(2, 1000, 101)  # 平移 1 样本必须失败
        assert sf.compare_pcm(a, b)["aligned"] is False


class TestReferenceProject:
    """§6 参考工程：五轨三 Bus、pre/post send、自动化、非零源入点、采样淡化。"""

    def test_reference_sequence_builds_and_hashes(self) -> None:
        from tests.support.reference_project import build_reference_sequence
        seq = build_reference_sequence()
        assert len(seq.audio_tracks) == 8  # DX/ADR/Foley/FX/MX + 3 Bus
        snapshot = build_audio_graph_snapshot(seq)
        assert snapshot.is_valid, snapshot.validation_errors
        # 重开等价：同序列再建快照 hash 一致
        again = build_audio_graph_snapshot(build_reference_sequence())
        assert snapshot.stable_hash == again.stable_hash
