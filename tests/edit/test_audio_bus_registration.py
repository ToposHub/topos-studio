"""
Bus 注册端到端测试（AM-02 阶段 1.5）。

验证音频引擎层（AudioPlaybackManager）的 Bus 注册、路由、EQ/FX 联动正确工作，
作为 edit_page 同步流程的底层保障。AM-02 的核心修复是 register_bus_track，
本测试验证该 API 及其与 routing/EQ 的集成。

不依赖真实音频文件，用 manager 直接操作（绕过 decoder 创建）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_playback import (
    AudioMixer,
    AudioPlaybackManager,
    AudioTrackState,
)


# AudioMixer 直接测试（无需真实文件 decoder）
class _DCDecoder:
    def __init__(self, amp=0.5):
        self.amp = float(amp)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


SR = 48000.0
FPS = 24.0
N = 480


class TestBusRegistration:
    """register_bus_track / unregister_bus_track API（AM-02）。"""

    def test_register_bus_creates_state(self, qapp):
        """注册 Bus 后 get_track_state 能查到（阶段 2 依赖）。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=0.8))
        state = mixer.get_track_state("bus1")
        assert state is not None
        assert state.volume == 0.8

    def test_register_bus_no_decoder(self, qapp):
        """Bus 不进 _decoders（不解码）——阶段 1 遍历 _decoders 时跳过 Bus。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1"))
        # Bus 不在 decoders（内部属性，验证其不存在）
        assert "bus1" not in mixer._decoders
        # Bus 在 tracks
        assert "bus1" in mixer._tracks

    def test_register_bus_default_state(self, qapp):
        """不传 state 时用默认 state。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1")
        state = mixer.get_track_state("bus1")
        assert state is not None
        assert state.volume == 1.0

    def test_unregister_bus_removes_state(self, qapp):
        """注销 Bus 后 state 被清理。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1"))
        assert mixer.get_track_state("bus1") is not None
        mixer.unregister_bus_track("bus1")
        assert mixer.get_track_state("bus1") is None

    def test_register_bus_solo_count(self, qapp):
        """注册 solo Bus 正确维护 solo 计数器。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", solo=True))
        assert mixer.has_solo_tracks() is True
        mixer.unregister_bus_track("bus1")
        assert mixer.has_solo_tracks() is False


class TestManagerBusRegistration:
    """AudioPlaybackManager 包装层的 Bus 注册（get_all_track_ids 含 Bus）。"""

    def test_manager_register_bus_in_track_ids(self, qapp):
        """manager.register_bus_track 后 get_all_track_ids 含 Bus。

        AM-05：签名比较的 active_track_ids 必须含 Bus，否则 Bus 变更
        会被签名比较误判为"集合不匹配"。
        """
        mgr = AudioPlaybackManager()
        mgr.register_bus_track("bus1", AudioTrackState(track_id="bus1"))
        ids = mgr.get_all_track_ids()
        assert "bus1" in ids

    def test_manager_unregister_bus(self, qapp):
        mgr = AudioPlaybackManager()
        mgr.register_bus_track("bus1")
        mgr.unregister_bus_track("bus1")
        assert "bus1" not in mgr.get_all_track_ids()


class TestBusRoutingIntegration:
    """Bus 路由 + EQ/FX 集成（AM-02 + AM-05 联合）。"""

    def test_track_to_bus_with_bus_eq(self, qapp):
        """t1 → bus1（带 EQ）→ master：Bus 的 EQ 生效。

        EQ 是 IIR biquad，DC 信号（非 EQ 设计频率）经滤波后增益不精确等于
        gain_db，但应显著偏离原始幅度，证明 EQ 真正处理了信号（而非旁路）。
        """
        from src.features.edit.core.audio_dsp import StripEQFilter  # noqa: F401
        from src.features.edit.core.timeline_v2 import StripEQParams

        # 先测无 EQ 基线
        mixer_base = AudioMixer()
        mixer_base.add_track("t1", _DCDecoder(0.4), AudioTrackState(track_id="t1"))
        mixer_base.set_track_routing("t1", "bus1", [], is_bus=False)
        mixer_base.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer_base.set_track_routing("bus1", None, [], is_bus=True)
        out_base, _ = mixer_base.mix_samples(0, FPS, N, 2)
        base_peak = float(np.max(np.abs(out_base)))
        # 无 EQ 时 DC 0.4 × PAN_CENTER²（AM-08: t1 + bus 两级 pan=0）
        from src.features.edit.core.audio_dsp import balance_gains
        pan_c = balance_gains(0.0)[0]
        assert abs(base_peak - 0.4 * pan_c * pan_c) < 1e-5

        # 再测有 EQ（+6dB 整体增益）
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.4), AudioTrackState(track_id="t1"))
        mixer.set_track_routing("t1", "bus1", [], is_bus=False)
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        mixer.set_track_strip_eq("bus1", StripEQParams(
            enabled=True, mode="3-band", bands=(0.0, 0.0, 0.0), gain=6.0,
            low_cut_enable=False, low_cut=20.0, high_cut_enable=False, high_cut=20000.0,
        ))
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        eq_peak = float(np.max(np.abs(out)))
        # EQ +6dB 应显著放大（远大于基线 0.4），证明 EQ 进入 Bus 链生效
        assert eq_peak > base_peak * 1.5, \
            f"Bus EQ 应放大信号，基线 {base_peak} → EQ 后 {eq_peak}"

    def test_bus_remove_via_remove_track(self, qapp):
        """remove_track 也能正确清理 Bus（同步流程重建时用 remove_track）。"""
        mixer = AudioMixer()
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1"))
        # 同步流程重建时对所有 track_id 调 remove_track（含 Bus）
        mixer.remove_track("bus1")
        assert mixer.get_track_state("bus1") is None

    def test_full_rebuild_with_bus(self, qapp):
        """模拟同步流程：移除全部 → 重建（含 Bus）→ Bus 有声。"""
        mixer = AudioMixer()
        # 第一轮：普通轨
        mixer.add_track("t1", _DCDecoder(0.3), AudioTrackState(track_id="t1"))
        # 第二轮重建：先移除全部
        for tid in list(mixer._tracks.keys()):
            mixer.remove_track(tid)
        # 重建：t1 → bus1 → master
        mixer.add_track("t1", _DCDecoder(0.5), AudioTrackState(track_id="t1"))
        mixer.set_track_routing("t1", "bus1", [], is_bus=False)
        mixer.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        mixer.set_track_routing("bus1", None, [], is_bus=True)
        out, _ = mixer.mix_samples(0, FPS, N, 2)
        # AM-08: t1 pan=0 + bus pan=0 各衰减 PAN_CENTER
        from src.features.edit.core.audio_dsp import balance_gains
        pan_c = balance_gains(0.0)[0]
        expected = 0.5 * pan_c * pan_c
        assert np.allclose(out[0], expected, atol=1e-5), "重建后 Bus 应正常路由"
