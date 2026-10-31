"""
音频图 PCM Oracle 测试（阶段 0.4）。

本文件是计划表 §7.1 "PCM oracle" 测试层级的种子：使用确定性的
tone / DC / impulse / noise fixture 喂入实时混音器（AudioMixer.mix_samples），
用**精确的数学预期值**（而非声卡回放）断言每个混音节点的输出 PCM。

覆盖范围（本切片）：volume / pan / mute / solo 四个核心节点。
后续阶段（1-3）会在此基础上扩展 clip fade / speed / EQ / FX / automation /
bus routing 的 oracle，并最终接入实时/离线渲染的一致性比对（plan §2.1）。

设计原则（plan §5）：
- 单一音频图：本 oracle 验证的是实时混音链的数学正确性；阶段 2 完成后，
  同一份 fixture 会同时喂给 realtime 与 offline renderer 比对，确保二者
  收敛到同一份 PCM。
- 不依赖声卡：全部用假 decoder 返回确定性样本，输出与预期逐样本比对。
- 误差阈值固定进测试：peak 容差 1 LSB 级（浮点，用 1e-5），不随实现漂移。

Oracle 公式（当前实现，linear pan）：
  left_gain  = 1.0 - max(0.0, pan)
  right_gain = 1.0 + min(0.0, pan)
  per_track_peak_l = amp * volume * left_gain
  master_sample = Σ(未跳过轨的 amp * volume * gain) × master_volume
  软限幅：若 master peak > 1.0，整体 ×(1/peak)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState


# ─────────────────────────────────────────────────────────────────────────────
# 确定性 fixture：假 decoder 系列
# ─────────────────────────────────────────────────────────────────────────────

SR = 48000.0
FPS = 24.0
N = 480  # 每块样本数（10ms @ 48kHz，典型音频块大小）

# RMA-08: DC decoder 返回立体声（np.stack([sig,sig])），故 pan 用 balance
# （unity-center）：pan=0 时每侧增益 1.0（0dB），pan≠0 时只衰减远侧。
# 旧 AM-08 用 equal-power（center -3dB）对立体声是错的（整体降低+改变声像）。
from src.features.edit.core.audio_dsp import balance_gains as _balance_gains
PAN_CENTER = _balance_gains(0.0)[0]  # = 1.0（stereo balance unity-center）


class _DCDecoder:
    """返回恒定 DC 电平的假 decoder——预期值最简单（无相位问题）。

    用于 volume/pan/mute/solo 的精确峰值与波形比对。
    """

    def __init__(self, amp: float = 0.5):
        self.amp = float(amp)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        # 立体声，左右同幅
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


class _ToneDecoder:
    """返回固定频率正弦的假 decoder——用于听感无关的频域校验。"""

    def __init__(self, amp: float = 0.5, freq: float = 1000.0, phase: float = 0.0):
        self.amp = float(amp)
        self.freq = float(freq)
        self.phase = float(phase)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        # 基于绝对样本位置生成（跨块连续），保持相位一致
        start = int(round(frame * SR / fps))
        t = (np.arange(start, start + num_samples) + self.phase) / SR
        sig = (self.amp * np.sin(2 * np.pi * self.freq * t)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


class _ImpulseDecoder:
    """返回脉冲（首样本为 amp，余为 0）的假 decoder——用于瞬态/路由校验。"""

    def __init__(self, amp: float = 1.0):
        self.amp = float(amp)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.zeros(num_samples, dtype=np.float32)
        if int(frame) == 0:
            sig[0] = self.amp
        return np.stack([sig, sig])

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Oracle 辅助：预期值计算（与实现公式保持一致）
# ─────────────────────────────────────────────────────────────────────────────

def linear_pan_gains(pan: float) -> tuple[float, float]:
    """当前实现的 pan 增益（left_gain, right_gain），与实现同步。

    RMA-08: DC decoder 是立体声，故用 balance（unity-center）。
    """
    from src.features.edit.core.audio_dsp import balance_gains
    return balance_gains(pan)


@dataclass
class _TrackSpec:
    """oracle 测试用的轨道规格。"""
    track_id: str
    amp: float = 0.5
    volume: float = 1.0
    pan: float = 0.0
    muted: bool = False
    solo: bool = False
    enabled: bool = True


def _build_mixer(qapp, specs: list[_TrackSpec], master_volume: float = 1.0,
                 master_muted: bool = False) -> AudioMixer:
    """按规格构造 AudioMixer 并注册轨道。"""
    mixer = AudioMixer()
    for s in specs:
        state = AudioTrackState(
            track_id=s.track_id, volume=s.volume, pan=s.pan,
            muted=s.muted, solo=s.solo, enabled=s.enabled,
        )
        mixer.add_track(s.track_id, _DCDecoder(s.amp), state)
    mixer.set_master_volume(master_volume)
    mixer.set_master_muted(master_muted)
    return mixer


def _expected(specs: list[_TrackSpec], master_volume: float = 1.0
              ) -> tuple[dict[str, float], dict[str, float], dict[str, tuple[float, float]]]:
    """计算 oracle 预期值。

    Returns:
        master_l/r: master 左右声道的预期恒定样本值（软限幅前）。
        peak: {tid: (peak_l, peak_r)} 每轨预期峰值。
    """
    has_solo = any(s.solo for s in specs)
    master_l = master_r = 0.0
    peaks: dict[str, tuple[float, float]] = {}
    for s in specs:
        if not s.enabled or s.muted:
            continue
        if has_solo and not s.solo:
            continue
        lg, rg = linear_pan_gains(s.pan)
        contrib_l = s.amp * s.volume * lg
        contrib_r = s.amp * s.volume * rg
        master_l += contrib_l
        master_r += contrib_r
        if contrib_l > 1e-9 or contrib_r > 1e-9:
            peaks[s.track_id] = (contrib_l, contrib_r)
    master_l *= master_volume
    master_r *= master_volume
    # 软限幅
    peak = max(abs(master_l), abs(master_r))
    if peak > 1.0:
        scale = 1.0 / peak
        master_l *= scale
        master_r *= scale
    return ({"l": master_l, "r": master_r}, peaks)


PEAK_TOL = 2e-2   # 峰值容差（与现有测试一致，正弦 RMS 等有微小差异）
WAVE_TOL = 1e-5   # DC 波形逐样本容差（浮点精确级，DC 无 RMS 平均误差）


# ─────────────────────────────────────────────────────────────────────────────
# 1. Volume 节点 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestVolumeOracle:
    """volume（线性增益）节点的 PCM oracle。"""

    @pytest.mark.parametrize("vol", [0.0, 0.25, 0.5, 1.0, 1.5, 2.0])
    def test_volume_scales_master_pcm(self, qapp, vol):
        """单轨 DC × volume → master 输出逐样本等于 amp*volume。"""
        specs = [_TrackSpec("t1", amp=0.4, volume=vol)]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)

        out, _ = mixer.mix_samples(0, FPS, N, 2)

        # vol=0 时输出应为静音（可能含极小浮点噪声）
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL), \
            f"vol={vol}: L 期望 {exp['l']}, 实际均值 {out[0].mean()}"
        assert np.allclose(out[1], exp["r"], atol=WAVE_TOL)

    def test_volume_clamped_to_max(self, qapp):
        """update_track_volume 对越界值 clamp 到 [0, MAX_VOLUME]。"""
        from src.features.edit.core.audio_playback import MAX_VOLUME
        mixer = _build_mixer(qapp, [_TrackSpec("t1", amp=0.3, volume=1.0)])
        # update_track_volume 路径会 clamp（构造时 __post_init__ 直接抛错）
        mixer.update_track_volume("t1", 5.0)
        state = mixer.get_track_state("t1")
        assert state.volume == MAX_VOLUME

    def test_master_volume_applies(self, qapp):
        """master_volume 对最终输出线性缩放。"""
        specs = [_TrackSpec("t1", amp=0.5, volume=1.0)]
        mixer = _build_mixer(qapp, specs, master_volume=0.5)
        exp, _ = _expected(specs, master_volume=0.5)

        out, _ = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Pan 节点 oracle（linear pan）
# ─────────────────────────────────────────────────────────────────────────────

class TestPanOracle:
    """pan（声像）节点的 PCM oracle。

    AM-08: 已迁移到 equal-power pan（中心 -3dB 恒功率）。
    oracle 预期通过 pan_gains() 计算，与实现始终同步。
    """

    @pytest.mark.parametrize("pan", [-1.0, -0.5, 0.0, 0.5, 1.0])
    def test_pan_per_channel(self, qapp, pan):
        """pan 决定左右声道增益：DC × volume × pan_gain。"""
        specs = [_TrackSpec("t1", amp=0.5, volume=1.0, pan=pan)]
        mixer = _build_mixer(qapp, specs)
        exp, peaks = _expected(specs)

        out, peaks_actual = mixer.mix_samples(0, FPS, N, 2)

        lg, rg = linear_pan_gains(pan)
        exp_l = 0.5 * 1.0 * lg
        exp_r = 0.5 * 1.0 * rg
        assert np.allclose(out[0], exp_l, atol=WAVE_TOL), \
            f"pan={pan}: L 期望 {exp_l}, 实际 {out[0][0]}"
        assert np.allclose(out[1], exp_r, atol=WAVE_TOL)

    def test_pan_center_equal_both_channels(self, qapp):
        """pan=0 时左右声道相等（equal-power 中心 0.707/0.707，-3dB 恒功率）。"""
        specs = [_TrackSpec("t1", amp=0.6, pan=0.0)]
        mixer = _build_mixer(qapp, specs)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        lg, rg = linear_pan_gains(0.0)
        assert np.allclose(out[0], out[1], atol=WAVE_TOL)
        # equal-power 中心：0.6 × cos(π/4) ≈ 0.424
        assert np.allclose(out[0], 0.6 * lg, atol=WAVE_TOL)

    def test_pan_full_left_right_zero(self, qapp):
        """pan=-1 全左：右声道为 0。"""
        specs = [_TrackSpec("t1", amp=0.5, pan=-1.0)]
        mixer = _build_mixer(qapp, specs)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out[0], 0.5, atol=WAVE_TOL)
        assert np.allclose(out[1], 0.0, atol=WAVE_TOL)

    def test_stereo_balance_unity_center(self, qapp):
        """RMA-08 验收：立体声源 pan=0（center）时每通道保持 0dB（unity）。

        旧 AM-08 用 equal-power（center 每侧 -3dB）对立体声是错的——会整体降低
        响度并改变声像。RMA-08 改为 balance：center 每侧 1.0（unity）。
        DC decoder 是立体声，故走 balance 路径。
        """
        specs = [_TrackSpec("t1", amp=0.5, volume=1.0, pan=0.0)]
        mixer = _build_mixer(qapp, specs)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # center: 每通道应保持原振幅 0.5（balance unity，非 equal-power 的 0.354）
        assert np.allclose(out[0], 0.5, atol=WAVE_TOL), \
            f"stereo center 左通道应 unity（0.5），got {out[0][0]}"
        assert np.allclose(out[1], 0.5, atol=WAVE_TOL), \
            f"stereo center 右通道应 unity（0.5），got {out[1][0]}"

    def test_stereo_balance_attenuates_far_side(self, qapp):
        """RMA-08: stereo balance pan 偏离时只衰减远侧声道（近侧保持 unity）。"""
        # pan=0.5（偏右）：左衰减，右保持
        specs = [_TrackSpec("t1", amp=0.5, volume=1.0, pan=0.5)]
        mixer = _build_mixer(qapp, specs)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # balance: left = 1-max(0,0.5)=0.5, right = 1+min(0,0.5)=1.0
        assert np.allclose(out[0], 0.5 * 0.5, atol=WAVE_TOL), "左（远侧）应衰减"
        assert np.allclose(out[1], 0.5 * 1.0, atol=WAVE_TOL), "右（近侧）应 unity"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Mute 节点 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestMuteOracle:
    """mute 节点的 PCM oracle。"""

    def test_muted_track_silent_in_master(self, qapp):
        """muted 轨对 master 无贡献。"""
        specs = [
            _TrackSpec("t1", amp=0.5, volume=1.0),
            _TrackSpec("t2", amp=0.3, volume=1.0, muted=True),
        ]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)

        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        # master 只含 t1
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL)
        # muted 轨不在 peaks
        assert "t2" not in peaks
        assert "t1" in peaks

    def test_muted_priority_over_solo(self, qapp):
        """muted 优先于 solo：muted+solo 轨仍静音。"""
        specs = [
            _TrackSpec("t1", amp=0.5, muted=True, solo=True),
            _TrackSpec("t2", amp=0.4, solo=True),
        ]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)

        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        # t1 muted 被跳过，只剩 t2（pan=0 衰减 PAN_CENTER）
        assert "t1" not in peaks
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL)

    def test_master_mute_silences_all(self, qapp):
        """master muted 时输出全静音。"""
        specs = [_TrackSpec("t1", amp=0.5)]
        mixer = _build_mixer(qapp, specs, master_muted=True)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out, 0.0, atol=WAVE_TOL)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Solo 节点 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestSoloOracle:
    """solo 节点的 PCM oracle。"""

    def test_solo_excludes_others_from_master(self, qapp):
        """solo 一轨后，其他轨对 master 无贡献。"""
        specs = [
            _TrackSpec("t1", amp=0.5, solo=True),
            _TrackSpec("t2", amp=0.4, solo=False),
            _TrackSpec("t3", amp=0.3, solo=False),
        ]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)

        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        # 只有 t1 贡献（pan=0 衰减 PAN_CENTER）
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL)
        assert "t1" in peaks
        assert "t2" not in peaks
        assert "t3" not in peaks

    def test_multiple_solo_all_pass(self, qapp):
        """多轨 solo 时所有 solo 轨都贡献，非 solo 轨静音。"""
        specs = [
            _TrackSpec("t1", amp=0.3, solo=True),
            _TrackSpec("t2", amp=0.2, solo=True),
            _TrackSpec("t3", amp=0.4, solo=False),
        ]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)

        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL)
        assert set(peaks.keys()) == {"t1", "t2"}

    def test_no_solo_all_pass(self, qapp):
        """无 solo 时所有轨都贡献。"""
        specs = [
            _TrackSpec("t1", amp=0.3),
            _TrackSpec("t2", amp=0.2),
        ]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)
        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out[0], exp["l"], atol=WAVE_TOL)
        assert set(peaks.keys()) == {"t1", "t2"}


# ─────────────────────────────────────────────────────────────────────────────
# 5. 组合节点：volume × pan × mute × solo 联合 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestCombinedOracle:
    """四节点联合的 PCM oracle——验证节点顺序与交互正确。"""

    def test_volume_pan_combined(self, qapp):
        """volume 与 pan 同时作用：amp × volume × pan_gain。"""
        specs = [_TrackSpec("t1", amp=0.5, volume=0.8, pan=0.5)]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # AM-08: 用 pan_gains 动态计算（equal-power）
        lg, rg = linear_pan_gains(0.5)
        assert np.allclose(out[0], 0.5 * 0.8 * lg, atol=WAVE_TOL)
        assert np.allclose(out[1], 0.5 * 0.8 * rg, atol=WAVE_TOL)

    def test_full_matrix(self, qapp):
        """完整路由矩阵：3 轨不同 vol/pan/mute/solo 组合。"""
        specs = [
            _TrackSpec("ta", amp=0.4, volume=1.0, pan=0.0, solo=True),   # 通过
            _TrackSpec("tb", amp=0.5, volume=0.6, pan=-1.0, solo=True),  # 通过，全左
            _TrackSpec("tc", amp=0.3, volume=1.0, pan=0.5, solo=False),  # 被 solo 排除
        ]
        mixer = _build_mixer(qapp, specs)
        exp, _ = _expected(specs)
        out, peaks = mixer.mix_samples(0, FPS, N, 2)

        # AM-08: 用 pan_gains 动态计算各轨贡献
        ta_l, ta_r = linear_pan_gains(0.0)  # pan=0
        tb_l, tb_r = linear_pan_gains(-1.0)  # 全左
        exp_l = 0.4 * 1.0 * ta_l + 0.5 * 0.6 * tb_l
        exp_r = 0.4 * 1.0 * ta_r + 0.5 * 0.6 * tb_r
        assert np.allclose(out[0], exp_l, atol=WAVE_TOL)
        assert np.allclose(out[1], exp_r, atol=WAVE_TOL)
        assert set(peaks.keys()) == {"ta", "tb"}

    def test_soft_limiter_engages(self, qapp):
        """多轨叠加超过 1.0 时软限幅整体缩放。"""
        specs = [
            _TrackSpec("t1", amp=0.8, volume=1.0),
            _TrackSpec("t2", amp=0.8, volume=1.0),
        ]
        mixer = _build_mixer(qapp, specs)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # 叠加 1.6 > 1.0 → 软限幅缩放到 1.0
        peak_actual = float(np.max(np.abs(out)))
        assert peak_actual <= 1.0 + 1e-6, f"软限幅后峰值应 ≤1.0，实际 {peak_actual}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tone fixture：频域校验（正弦保真）
# ─────────────────────────────────────────────────────────────────────────────

class TestToneFidelity:
    """用正弦 fixture 验证混音链不引入频率失真（增益/路由只改幅度）。"""

    def test_tone_frequency_preserved(self, qapp):
        """1kHz 正弦经 volume 缩放后主频仍是 1kHz。"""
        mixer = AudioMixer()
        mixer.add_track("t1", _ToneDecoder(amp=0.5, freq=1000.0),
                        AudioTrackState(track_id="t1", volume=0.5))
        out, _ = mixer.mix_samples(0, FPS, N, 2)

        # FFT 找主频
        spectrum = np.abs(np.fft.rfft(out[0]))
        freqs = np.fft.rfftfreq(N, 1.0 / SR)
        main_freq = freqs[np.argmax(spectrum)]
        assert abs(main_freq - 1000.0) < 50.0, f"主频应为 1kHz，实际 {main_freq}Hz"

        # 幅度应 ≈ 0.5 * 0.5 * PAN_CENTER 的正弦峰值（AM-08: pan=0 衰减）
        expected_peak = 0.5 * 0.5 * PAN_CENTER
        assert abs(np.max(np.abs(out[0])) - expected_peak) < 0.02


# ─────────────────────────────────────────────────────────────────────────────
# 7. Bus 路由 oracle（AM-02）
# ─────────────────────────────────────────────────────────────────────────────

class TestBusRoutingOracle:
    """Bus→Master 路由的 PCM oracle（AM-02 修复验证）。

    AM-02 根因：Bus 无 clips 被同步跳过，引擎阶段 2 查不到 Bus state，
    丢弃其输入。本组测试验证 register_bus_track 让 Bus 正确接收路由输入。
    """

    def _build_track_to_bus(self, qapp, track_amp=0.4, bus_volume=1.0,
                            track_volume=1.0, bus_muted=False, bus_solo=False):
        """构造 1 普通轨 → 1 Bus → Master 的图。"""
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(track_amp),
                        AudioTrackState(track_id="t1", volume=track_volume))
        mixer.set_track_routing("t1", "bus1", [], is_bus=False)
        mixer.register_bus_track(
            "bus1",
            AudioTrackState(track_id="bus1", volume=bus_volume,
                            muted=bus_muted, solo=bus_solo),
        )
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        return mixer

    def test_track_to_bus_to_master(self, qapp):
        """t1(0.4) → bus1(1.0) → master。

        AM-08: track pan=0 + bus pan=0 各衰减 PAN_CENTER，合计 PAN_CENTER²。
        """
        mixer = self._build_track_to_bus(qapp, track_amp=0.4, bus_volume=1.0)
        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        # AM-02 核心：Bus 不再丢声；AM-08: 两级 pan=0 衰减
        expected = 0.4 * PAN_CENTER * PAN_CENTER
        assert np.allclose(out[0], expected, atol=WAVE_TOL), \
            f"Bus 路由后 master 应为 {expected}, 实际 {out[0][0]}"
        assert "bus1" in peaks

    def test_bus_volume_scales(self, qapp):
        """Bus volume 缩放其输出：t1(0.4) → bus1(0.5) → master。"""
        mixer = self._build_track_to_bus(qapp, track_amp=0.4, bus_volume=0.5)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        expected = 0.4 * PAN_CENTER * PAN_CENTER * 0.5
        assert np.allclose(out[0], expected, atol=WAVE_TOL)

    def test_bus_muted_silences(self, qapp):
        """Bus muted 时其输入不达 master。"""
        mixer = self._build_track_to_bus(qapp, bus_muted=True)
        out, peaks = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out, 0.0, atol=WAVE_TOL)
        assert "bus1" not in peaks  # muted bus 不报告电平

    def test_bus_not_registered_falls_back_to_master(self, qapp, caplog):
        """AUD-15402：不调 register_bus_track 时未知 bus 目标兜底进 master + 告警。

        旧行为（本测试此前锁定）：样本累积进永不被消费的孤儿缓冲，整轨音频
        静默丢失且无提示——违反"不允许无声跳过"原则。修复后与 Bus 阶段及
        topological_sort_buses 的"未知目标视为 Master"语义统一。
        register_bus_track 仍然是必需的：未注册的 Bus 没有自己的
        volume/EQ/FX 处理，也不出现在电平表（peaks）中。
        """
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.4),
                        AudioTrackState(track_id="t1", volume=1.0))
        mixer.set_track_routing("t1", "bus1", [], is_bus=False)
        # 故意不注册 bus1（模拟 Bus 已删除但轨的路由未清理）
        with caplog.at_level("WARNING"):
            out, peaks = mixer.mix_samples(0, FPS, N, 2)
        assert np.allclose(out[0], 0.4, atol=WAVE_TOL), (
            "未注册 Bus 的轨应兜底进 master（0.4×PAN_CENTER），而非静默丢失")
        assert any("unknown bus" in r.getMessage() for r in caplog.records)
        assert "bus1" not in peaks  # 未注册 Bus 不参与处理、不报告电平

    def test_multiple_tracks_to_one_bus(self, qapp):
        """多轨路由到同一 Bus：输入累加。"""
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.3), AudioTrackState(track_id="t1"))
        mixer.add_track("t2", _DCDecoder(0.2), AudioTrackState(track_id="t2"))
        mixer.set_track_routing("t1", "bus1", [], is_bus=False)
        mixer.set_track_routing("t2", "bus1", [], is_bus=False)
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # (0.3 + 0.2) × PAN_CENTER(track) × PAN_CENTER(bus)
        expected = (0.3 + 0.2) * PAN_CENTER * PAN_CENTER
        assert np.allclose(out[0], expected, atol=WAVE_TOL)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Send 路由 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestSendOracle:
    """post-fader send 的 PCM oracle。"""

    def test_send_parallel_to_direct(self, qapp):
        """t1 同时直接到 master + send 到 bus1（并行）：master 收到两份。"""
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.4), AudioTrackState(track_id="t1"))
        # output_bus_id=None → 直接到 master；同时 send 到 bus1
        mixer.set_track_routing("t1", None, [("bus1", 0.5)], is_bus=False)
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # AM-08: direct 0.4×PAN_CENTER + send 0.4×0.5×PAN_CENTER(track)×PAN_CENTER(bus)
        direct = 0.4 * PAN_CENTER
        send = 0.4 * 0.5 * PAN_CENTER * PAN_CENTER
        assert np.allclose(out[0], direct + send, atol=WAVE_TOL)

    def test_send_only_to_bus(self, qapp):
        """t1 仅 send 到 bus1（output_bus_id=None 但无 direct？实际 send 是并行）。

        注：当前实现 send 始终并行于 output_bus_id。output_bus_id=None 时
        直接进 master + send 进 bus。验证 send level=0 时不贡献。
        """
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.4), AudioTrackState(track_id="t1"))
        mixer.set_track_routing("t1", None, [("bus1", 0.0)], is_bus=False)  # send level=0
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # 只有 direct 0.4×PAN_CENTER（send level=0 被跳过）
        assert np.allclose(out[0], 0.4 * PAN_CENTER, atol=WAVE_TOL)


# ─────────────────────────────────────────────────────────────────────────────
# 9. EQ 生效 oracle（AM-05 验证轨道级 EQ 进入引擎）
# ─────────────────────────────────────────────────────────────────────────────

class TestStripEQOracle:
    """轨道级 Strip EQ 生效验证（AM-05：EQ 同步到引擎）。

    用 DC 信号 + 高频段提升，验证 EQ 改变输出幅度（DC 经 EQ 后幅度变化）。
    """

    def test_eq_changes_output(self, qapp):
        """启用 EQ（非平直响应）应改变输出，证明 EQ 真正生效而非旁路。"""
        from src.features.edit.core.audio_dsp import StripEQFilter
        from src.features.edit.core.timeline_v2 import StripEQParams

        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.5), AudioTrackState(track_id="t1", volume=1.0))
        # 给 t1 设置一个非平直 EQ（整体增益 +6dB → linear ≈ 2.0）
        eq_params = StripEQParams(
            enabled=True, mode="3-band", bands=(0.0, 0.0, 0.0),
            gain=6.0, low_cut_enable=False, low_cut=20.0,
            high_cut_enable=False, high_cut=20000.0,
        )
        mixer.set_track_strip_eq("t1", eq_params)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # DC 0.5 × EQ gain 6dB(×2.0) × PAN_CENTER ≈ 0.707
        peak = float(np.max(np.abs(out)))
        assert peak > 0.6, f"EQ +6dB 应显著放大 DC，实际峰值 {peak}"

    def test_eq_disabled_no_change(self, qapp):
        """EQ 禁用时不改变输出（旁路）。"""
        from src.features.edit.core.audio_dsp import StripEQFilter
        from src.features.edit.core.timeline_v2 import StripEQParams

        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.5), AudioTrackState(track_id="t1", volume=1.0))
        eq_params = StripEQParams(
            enabled=False, mode="3-band", bands=(0.0, 0.0, 0.0),
            gain=6.0, low_cut_enable=False, low_cut=20.0,
            high_cut_enable=False, high_cut=20000.0,
        )
        mixer.set_track_strip_eq("t1", eq_params)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # 禁用 EQ → 直通 0.5 × PAN_CENTER（pan=0 衰减）
        assert np.allclose(out[0], 0.5 * PAN_CENTER, atol=WAVE_TOL)

