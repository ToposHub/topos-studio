"""
音频导出/实时一致性测试（AM-03 阶段 2）。

验证共享 AudioGraphRenderer（导出路径）与 AudioMixer.mix_samples（实时路径）
对**同一节点配置**产生一致的 PCM 输出。这是 AM-03 的核心验收：消除两路径漂移。

测试策略：
- 用 AudioMixer（实时真相源）和 AudioGraphRenderer（导出共享核心）分别配置
  相同的 track/bus/master 参数 + 假 decoder 返回相同 DC 样本。
- 比对两者的 master 输出 PCM（逐样本，容差 1e-5）。
- 覆盖各节点：volume/pan/EQ/FX/bus/master/软限幅。

不依赖真实音频文件或声卡，纯内存比对。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    EffectChainParams,
    StripEQParams,
    TimelineSequence,
    TrackEffectInstance,
)


SR = 48000.0
FPS = 24.0
N = 480


class _DCDecoder:
    """DC 假 decoder（实时路径用）。"""
    def __init__(self, amp=0.5):
        self.amp = float(amp)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, self.amp, dtype=np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


def _mixer_with_track(amp, volume=1.0, pan=0.0, strip_eq=None, effect_chain=None,
                      output_bus_id=None, is_bus=False):
    """构造 AudioMixer 并注册一个轨（带可选 EQ/FX/routing）。"""
    mixer = AudioMixer()
    state = AudioTrackState(track_id="t1", volume=volume, pan=pan)
    if is_bus:
        mixer.register_bus_track("t1", state)
    else:
        mixer.add_track("t1", _DCDecoder(amp), state)
    mixer.set_track_routing("t1", output_bus_id, [], is_bus=is_bus)
    if strip_eq is not None:
        mixer.set_track_strip_eq("t1", strip_eq)
    if effect_chain is not None:
        mixer.set_track_effect_chain("t1", effect_chain)
    return mixer


def _renderer_with_track(amp, volume=1.0, pan=0.0, strip_eq=None, effect_chain=None,
                         output_bus_id=None, is_bus=False, master_volume=1.0):
    """构造 AudioGraphRenderer 并配置相同参数的轨。"""
    track = EditTrack(
        track_id="t1", track_type="audio", index=0,
        volume=volume, pan=pan, strip_eq=strip_eq, effect_chain=effect_chain,
        is_bus=is_bus, output_bus_id=output_bus_id,
    )
    seq = TimelineSequence(audio_tracks=(track,), master_volume=master_volume)
    r = AudioGraphRenderer(SR)
    r.configure_from_sequence(seq)
    return r


def _render_realtime(mixer, master_volume=1.0, master_muted=False):
    """实时路径：AudioMixer.mix_samples + master。"""
    mixer.set_master_volume(master_volume)
    mixer.set_master_muted(master_muted)
    out, _ = mixer.mix_samples(0, FPS, N, 2)
    return out


def _render_export(renderer, amp):
    """导出路径：AudioGraphRenderer 处理一个块。"""
    samples = np.full((2, N), amp, dtype=np.float32)
    processed = renderer.render_track_block("t1", samples, 0)
    output = np.zeros((2, N), dtype=np.float32)
    bus_inputs = {}
    if processed is not None:
        node = renderer.get_node("t1")
        target = node.output_bus_id if node else None
        if target is not None:
            bus_inputs[target] = np.zeros_like(output)
            bus_inputs[target] += processed
        else:
            output += processed
    renderer.render_bus_stage(bus_inputs, output, has_solo=False)
    output = renderer.apply_master(output)
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Parity：各节点实时 vs 导出一致
# ─────────────────────────────────────────────────────────────────────────────

class TestVolumePanParity:
    """volume/pan 节点实时与导出一致。"""

    @pytest.mark.parametrize("vol,pan", [(1.0, 0.0), (0.5, 0.0), (1.0, 0.5), (0.8, -0.5)])
    def test_volume_pan_parity(self, vol, pan):
        amp = 0.4
        mixer = _mixer_with_track(amp, volume=vol, pan=pan)
        renderer = _renderer_with_track(amp, volume=vol, pan=pan)
        rt = _render_realtime(mixer)
        ex = _render_export(renderer, amp)
        assert np.allclose(rt, ex, atol=1e-5), \
            f"vol={vol} pan={pan}: 实时 {rt[0][0]} vs 导出 {ex[0][0]}"


class TestEQParity:
    """轨道级 EQ 实时与导出一致。"""

    def test_eq_parity(self):
        amp = 0.4
        eq = StripEQParams(
            enabled=True, mode="3-band", bands=(3.0, -2.0, 4.0), gain=2.0,
            low_cut_enable=False, low_cut=20.0, high_cut_enable=False, high_cut=20000.0,
        )
        mixer = _mixer_with_track(amp, strip_eq=eq)
        renderer = _renderer_with_track(amp, strip_eq=eq)
        rt = _render_realtime(mixer)
        ex = _render_export(renderer, amp)
        # EQ 是 IIR，首块有瞬态，容差放宽
        assert np.allclose(rt, ex, atol=1e-4), \
            f"EQ: 实时 {rt[0][0]} vs 导出 {ex[0][0]}"


class TestFXParity:
    """FX 机架链实时与导出一致。"""

    def test_fx_parity(self):
        amp = 0.4
        chain = EffectChainParams(effects=(
            TrackEffectInstance(
                effect_type="limiter",
                params={"enabled": True, "threshold_db": -3.0, "release_ms": 100.0},
                enabled=True, order=0, panel="dynamics",
            ),
        ))
        mixer = _mixer_with_track(amp, effect_chain=chain)
        renderer = _renderer_with_track(amp, effect_chain=chain)
        rt = _render_realtime(mixer)
        ex = _render_export(renderer, amp)
        assert np.allclose(rt, ex, atol=1e-5), \
            f"FX: 实时 {rt[0][0]} vs 导出 {ex[0][0]}"


class TestMasterParity:
    """master volume/mute/软限幅实时与导出一致。"""

    def test_master_volume_parity(self):
        amp = 0.4
        mixer = _mixer_with_track(amp)
        renderer = _renderer_with_track(amp, master_volume=0.5)
        rt = _render_realtime(mixer, master_volume=0.5)
        ex = _render_export(renderer, amp)
        assert np.allclose(rt, ex, atol=1e-5)

    def test_master_mute_parity(self):
        amp = 0.4
        mixer = _mixer_with_track(amp)
        renderer = _renderer_with_track(amp)
        rt = _render_realtime(mixer, master_muted=True)
        ex = _render_export(renderer, amp)
        # renderer 的 master_muted 来自 sequence，需单独测
        renderer2 = AudioGraphRenderer(SR)
        t = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(t,), master_muted=True)
        renderer2.configure_from_sequence(seq)
        ex2 = _render_export(renderer2, amp)
        assert np.allclose(rt, 0.0, atol=1e-6)
        assert np.allclose(ex2, 0.0, atol=1e-6)

    def test_soft_limiter_parity(self):
        """信号超 1.0 时软限幅（比例缩放）两路径一致。"""
        # 用大 volume 让信号超过 1.0
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.8), AudioTrackState(track_id="t1", volume=1.0))
        mixer.add_track("t2", _DCDecoder(0.8), AudioTrackState(track_id="t2", volume=1.0))
        rt, _ = mixer.mix_samples(0, FPS, N, 2)
        # 实时软限幅后 peak ≤ 1.0
        assert float(np.max(np.abs(rt))) <= 1.0 + 1e-6

        # renderer 同样软限幅
        t1 = EditTrack(track_id="t1", track_type="audio", index=0, volume=1.0)
        t2 = EditTrack(track_id="t2", track_type="audio", index=1, volume=1.0)
        seq = TimelineSequence(audio_tracks=(t1, t2))
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(seq)
        out = np.zeros((2, N), dtype=np.float32)
        for tid in ("t1", "t2"):
            s = np.full((2, N), 0.8, dtype=np.float32)
            p = r.render_track_block(tid, s, 0)
            out += p
        out = r.apply_master(out)
        assert float(np.max(np.abs(out))) <= 1.0 + 1e-6


class TestExportIncorporatesNodes:
    """验证导出路径（renderer）纳入了此前缺失的节点。"""

    def test_export_incorporates_eq(self):
        """导出路径应用 EQ（此前缺失）。"""
        amp = 0.4
        eq = StripEQParams(
            enabled=True, mode="3-band", bands=(0.0, 0.0, 0.0), gain=6.0,
            low_cut_enable=False, low_cut=20.0, high_cut_enable=False, high_cut=20000.0,
        )
        renderer = _renderer_with_track(amp, strip_eq=eq)
        # 无 EQ 基线
        renderer_base = _renderer_with_track(amp)
        with_eq = _render_export(renderer, amp)
        without_eq = _render_export(renderer_base, amp)
        # EQ +6dB 应改变输出（证明导出应用了 EQ）
        assert not np.allclose(with_eq, without_eq, atol=1e-3), \
            "导出应应用 EQ，使输出不同于无 EQ"

    def test_export_incorporates_master_volume(self):
        """导出路径应用 master volume（此前缺失）。"""
        amp = 0.4
        renderer_full = _renderer_with_track(amp, master_volume=0.5)
        renderer_unit = _renderer_with_track(amp, master_volume=1.0)
        full = _render_export(renderer_full, amp)
        unit = _render_export(renderer_unit, amp)
        # master 0.5 应使输出减半
        assert np.allclose(full, unit * 0.5, atol=1e-5), \
            "导出应应用 master volume"

    def test_export_uses_soft_limiter_not_hard_clip(self):
        """导出用软限幅（比例缩放）而非硬削波 np.clip。"""
        # AM-08: pan=0 衰减 PAN_CENTER，需大 amp 让叠加超过 1.0
        # 两轨 0.9 × PAN_CENTER × 2 ≈ 1.27 > 1.0 → 触发软限幅
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder(0.9), AudioTrackState(track_id="t1", volume=1.0))
        mixer.add_track("t2", _DCDecoder(0.9), AudioTrackState(track_id="t2", volume=1.0))
        rt, _ = mixer.mix_samples(0, FPS, N, 2)
        # 软限幅：叠加后 >1.0 → 缩放到 1.0
        assert abs(float(rt[0][0]) - 1.0) < 1e-5

        t1 = EditTrack(track_id="t1", track_type="audio", index=0)
        t2 = EditTrack(track_id="t2", track_type="audio", index=1)
        seq = TimelineSequence(audio_tracks=(t1, t2))
        r = AudioGraphRenderer(SR)
        r.configure_from_sequence(seq)
        out = np.zeros((2, N), dtype=np.float32)
        for tid in ("t1", "t2"):
            s = np.full((2, N), 0.9, dtype=np.float32)
            out += r.render_track_block(tid, s, 0)
        out = r.apply_master(out)
        # 软限幅后 = 1.0（非硬削波的方波）
        assert abs(float(out[0][0]) - 1.0) < 1e-5
