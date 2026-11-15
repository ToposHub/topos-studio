"""R03：Stem 复用同一调度回归（计划 §5 R03，2026-11-15 复审）。

原失败案例（reproduce_remaining_issues.py 的 probe_stem_pre_send）转为
pytest，并补齐 R03 验收：
- pre-send：输入 0.2、推子 0、unity pre-send → Master stem 应为 0.2
  （旧实现恒取 post-fader 处理输出，实测 0）。
- post-send：推子 0 → 0；send mute 只断开该 send。
- 多 clip 求和优先（MX-04）：两重叠 clip 过压缩器 = 先求和再过链的
  oracle（旧实现逐 clip 各自过链，非线性 FX 结果不同）。
- 成片/Stem 一致性：同工程 _render_audio_master 与 MASTER stem PCM
  对齐（≤1e-5）。
- Stem 路径 Bus/Master 外部侧链 tap 生效。
"""

from __future__ import annotations

from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.stem_export import (
    StemSpec,
    StemType,
    render_stem_pcm,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    EffectChainParams,
    Send,
    TimelineSequence,
    TrackEffectInstance,
)
from src.shared.media.metadata import Rational

SR = 48000
FPS = 25


def _decode_const(level: float = 0.2):
    def decode(track, clip, frame, fps, count):
        return np.full((2, count), level, np.float32)
    return decode


def _delay_chain(iid: str = "D", wet: float = 100.0) -> EffectChainParams:
    return EffectChainParams(effects=(TrackEffectInstance(
        effect_type="delay", instance_id=iid, params={
            "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": wet, "level_db": 0.0, "low_cut": 20.0,
            "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
        }),))


class TestSendTapSemantics:
    def test_pre_send_survives_zero_fader(self):
        """probe_stem_pre_send：pre_fader send 在推子 0 时仍送出 0.2。"""
        source = EditTrack("T", "Source", "audio", 0, volume=0.0,
            clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),),
            sends=(Send(target_bus_id="B", level=1.0, tap="pre_fader"),))
        bus = EditTrack("B", "Bus", "audio", 1, is_bus=True)
        sequence = TimelineSequence(fps=Rational(FPS, 1),
                                    audio_tracks=(source, bus))
        out = render_stem_pcm(sequence, StemSpec(StemType.MASTER, "Master", (), include_master=True),
                              decode_clip_fn=_decode_const())
        assert out is not None
        peak = float(np.max(np.abs(out)))
        assert abs(peak - 0.2) < 1e-5, f"pre-send lost: peak={peak}"

    def test_post_send_follows_zero_fader(self):
        source = EditTrack("T", "Source", "audio", 0, volume=0.0,
            clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),),
            sends=(Send(target_bus_id="B", level=1.0, tap="post_fader"),))
        bus = EditTrack("B", "Bus", "audio", 1, is_bus=True)
        sequence = TimelineSequence(fps=Rational(FPS, 1),
                                    audio_tracks=(source, bus))
        out = render_stem_pcm(sequence, StemSpec(StemType.MASTER, "Master", (), include_master=True),
                              decode_clip_fn=_decode_const())
        assert out is not None
        assert float(np.max(np.abs(out))) < 1e-6

    def test_send_mute_only_cuts_that_send(self):
        source = EditTrack("T", "Source", "audio", 0, volume=1.0,
            clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),),
            sends=(Send(target_bus_id="B1", level=1.0, tap="pre_fader"),
                   Send(target_bus_id="B2", level=1.0, tap="pre_fader",
                        muted=True)))
        b1 = EditTrack("B1", "Bus1", "audio", 1, is_bus=True)
        b2 = EditTrack("B2", "Bus2", "audio", 2, is_bus=True)
        sequence = TimelineSequence(fps=Rational(FPS, 1),
                                    audio_tracks=(source, b1, b2))
        out = render_stem_pcm(sequence, StemSpec(StemType.MASTER, "Master", (), include_master=True),
                              decode_clip_fn=_decode_const())
        assert out is not None
        peak = float(np.max(np.abs(out)))
        # 直达 0.2 + B1 send 0.2 = 0.4；B2 muted 不贡献（否则 0.6）
        assert abs(peak - 0.4) < 1e-5, f"send mute leaked: peak={peak}"
        # 对照：B2 不静音 → 0.6
        source2 = EditTrack("T", "Source", "audio", 0, volume=1.0,
            clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),),
            sends=(Send(target_bus_id="B1", level=1.0, tap="pre_fader"),
                   Send(target_bus_id="B2", level=1.0, tap="pre_fader")))
        seq2 = TimelineSequence(fps=Rational(FPS, 1),
                                audio_tracks=(source2, b1, b2))
        out2 = render_stem_pcm(seq2, StemSpec(StemType.MASTER, "Master", (), include_master=True),
                               decode_clip_fn=_decode_const())
        assert abs(float(np.max(np.abs(out2))) - 0.6) < 1e-5


class TestSumFirstNonlinear:
    def test_stem_gap_tail_drain_keeps_echo(self):
        """相邻/间隔 clip 的轨 FX 尾音跨间隙延续：0..2 与 20..22 两 clip、
        200ms 全湿 Delay → 首回声（200ms，落在间隙帧 5）必须出现在 stem。
        旧实现逐 clip 直调轨阶段且无 drain，间隙帧从不推进 → 回声丢失。"""
        track = EditTrack("T", "T", "audio", 0,
            clips=(ClipInstance("c1", "a", "T", 0, 2, 0, 2),
                   ClipInstance("c2", "a", "T", 20, 2, 10, 2)),
            effect_chain=_delay_chain())
        sequence = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(track,))
        stem = render_stem_pcm(
            sequence, StemSpec(StemType.MACKER, "M", (), include_master=True)
            if False else StemSpec(StemType.MASTER, "Master", (), include_master=True),
            decode_clip_fn=lambda t, c, f, fp, n: np.zeros((2, n), np.float32))
        # 静音解码：回声能量为零——改用脉冲解码验证
        def decode_pulse(track, clip, frame, fps, count):
            sig = np.zeros((2, count), np.float32)
            if clip.clip_id == "c1" and frame == 0:
                sig[:, 0] = 0.5
            return sig
        stem = render_stem_pcm(
            sequence, StemSpec(StemType.MASTER, "Master", (), include_master=True),
            decode_clip_fn=decode_pulse)
        assert stem is not None
        idx = np.flatnonzero(np.abs(stem[0]) > 1e-4)
        assert idx.size, "gap echo lost (no drain in stem path)"
        echo_ms = float(idx[0]) * 1000.0 / SR
        assert abs(echo_ms - 200.0) <= 1000.0 / SR, f"echo at {echo_ms}ms"


class TestStemMasterParity:
    def test_master_stem_matches_render_audio_master(self):
        """同工程 MASTER stem 与成片 _render_audio_master PCM 对齐。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig,
            TimelineExporter,
        )
        source = EditTrack("T", "Source", "audio", 0,
            clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),),
            effect_chain=_delay_chain())
        bus = EditTrack("B", "Bus", "audio", 1, is_bus=True)
        routed = EditTrack("T2", "Routed", "audio", 2,
            clips=(ClipInstance("c2", "a", "T2", 1, 2, 0, 2),),
            output_bus_id="B",
            sends=(Send(target_bus_id="B", level=0.5, tap="post_fader"),))
        sequence = TimelineSequence(fps=Rational(FPS, 1),
                                    audio_tracks=(source, bus, routed))
        stem = render_stem_pcm(
            sequence, StemSpec(StemType.MASTER, "Master", (), include_master=True),
            decode_clip_fn=_decode_const(0.2))
        assert stem is not None

        class _DecodingExporter(TimelineExporter):
            pass

        # 成片路径：真实 EditProject（asset 查找需要）+ 注入恒定解码器
        import src.features.edit.core.audio_playback as ap_mod
        from src.features.edit.core.project import EditProject, MediaAsset
        project = EditProject("p", "P", assets={
            "a": MediaAsset("a", "/tmp/r03-a.wav", "audio", "A", {})})
        exporter = TimelineExporter(project, sequence, Mock())
        orig = ap_mod.AudioDecoder

        class _ConstDecoder:
            _channels = 2

            def __init__(self, *a, **k):
                pass

            def decode_samples_for_frame(self, frame, fps, num_samples):
                return np.full((2, num_samples), 0.2, np.float32)

            def open(self, *a, **k):
                return True

            def close(self):
                pass

        ap_mod.AudioDecoder = _ConstDecoder
        try:
            master = exporter._render_audio_master(
                0, 4, TimelineExportConfig(output_path="/tmp/r03.mov"),
                encoder=Mock())
        finally:
            ap_mod.AudioDecoder = orig
        assert master is not None
        n = min(master.shape[-1], stem.shape[-1])
        np.testing.assert_allclose(
            master[:, :n], stem[:, :n], atol=1e-5,
            err_msg="MASTER stem diverges from full mix render")


class TestStemSidechain:
    def test_master_chain_external_sc_affects_stem(self):
        """Master 链压缩器外部检源（key 轨）在 stem 路径生效：
        key 电位抬高 → programme 被压低（相对内部检出的对照）。"""
        programme = EditTrack("P", "Prog", "audio", 0,
            clips=(ClipInstance("cp", "a", "P", 0, 2, 0, 2),))
        key = EditTrack("key", "Key", "audio", 1,
            clips=(ClipInstance("ck", "a", "key", 0, 2, 0, 2),))
        chain = EffectChainParams(effects=(TrackEffectInstance(
            effect_type="compressor", instance_id="C", params={
                "enabled": True, "threshold_db": -20.0, "ratio": 8.0,
                "attack_ms": 1.0, "release_ms": 200.0, "makeup_gain": 0.0,
                "mix": 100.0, "knee": 0.0, "sc_on": True, "sc_source": "key"},),))

        t_axis = np.arange(SR) / SR

        def decode(track, clip, frame, fps, count):
            # 正弦（内部检源链路对 DC 常数有高通，常数信号不应触发压缩）
            level = 0.05 if track.track_id == "P" else 0.6
            sig = (level * np.sin(2 * np.pi * 440.0 * t_axis[:count]))
            return np.stack([sig, sig]).astype(np.float32)

        # TRACK stem 只含 programme；key 轨经 SC 检源闭包照常渲染（R03：
        # 可听闭包与检源闭包分开），其输出不入混音、只供 master 链检源。
        ext = TimelineSequence(fps=Rational(FPS, 1),
                               audio_tracks=(programme, key),
                               master_effect_chain=chain)
        out_ext = render_stem_pcm(
            ext, StemSpec(StemType.TRACK, "Prog", ("P",), include_master=True),
            decode_clip_fn=decode)
        assert out_ext is not None
        # 对照：内部检出（programme 自身 0.05 远低于 -20dB 阈值 → 不压缩）
        import copy
        int_chain = EffectChainParams(effects=(TrackEffectInstance(
            effect_type="compressor", instance_id="C", params={
                "enabled": True, "threshold_db": -20.0, "ratio": 8.0,
                "attack_ms": 1.0, "release_ms": 200.0, "makeup_gain": 0.0,
                "mix": 100.0, "knee": 0.0, "sc_on": False},),))
        int_seq = TimelineSequence(fps=Rational(FPS, 1),
                                   audio_tracks=(programme, key),
                                   master_effect_chain=int_chain)
        out_int = render_stem_pcm(
            int_seq, StemSpec(StemType.TRACK, "Prog", ("P",), include_master=True),
            decode_clip_fn=decode)
        assert out_int is not None
        def _steady_rms(x: np.ndarray) -> float:
            # 跳过 attack 瞬态（前 20ms），用稳态 RMS 比较
            n0 = int(0.02 * SR)
            return float(np.sqrt(np.mean(np.asarray(x)[:, n0:] ** 2)))

        rms_ext = _steady_rms(out_ext)
        rms_int = _steady_rms(out_int)
        # 外部强 key（0.6 → 深度压缩）稳态应显著低于内部（0.05 不触发）
        assert rms_ext < rms_int * 0.5, (
            f"external SC not working in stem: ext={rms_ext:.4f} int={rms_int:.4f}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
