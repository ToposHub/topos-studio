"""第六轮审计 阶段1B 回归：音频输出正确性（D01/D02/D04/D05/D06/D07）。

D01：retime（reverse/速度曲线/普通变速）音频走 canonical 时间映射 +
     样本域重采样——440Hz 正弦 2x 未保调应变 880Hz、0.5x 变 220Hz、
     reverse 与 1x 逐样本时间反转一致。
D02：归一化分析保留真实声道布局（BS.1770 按声道能量）——反相立体声
     与同相立体声测得相同响度；静音素材拒绝正增益建议。
D04：音频片段 FX 的 animated_params 关键帧真实作用于成片/Stem PCM。
D05：同范围 Master stem 与成片 PCM 一致（clip FX/冻结/retime 同语义）。
D06：非整数帧率（30000/1001）逐帧精确块长——恒定信号无帧间零样本洞。
D07：取消路径也关闭全部 decoder。
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_retime import (
    ClipRetimeReader,
    needs_retime,
    resample_segment,
)
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.stem_export import (
    StemSpec,
    StemType,
    create_clip_decode_fn,
    render_stem_pcm,
)
from src.features.edit.core.timeline_export import (
    TimelineExporter,
    TimelineExportConfig,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core

SR = 48000
FPS = 25


def _write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SR) -> Path:
    """写一个 float WAV（声道 × 样本）。"""
    import av

    channels = pcm.shape[0]
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")
    container = av.open(str(path), mode="w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=sample_rate)
    stream.layout = layout
    # 按 ~0.2s 块编码
    block = sample_rate // 5
    for start in range(0, pcm.shape[1], block):
        chunk = np.clip(pcm[:, start:start + block], -1.0, 1.0)
        frame = av.AudioFrame.from_ndarray(
            chunk.astype(np.float32), format="fltp", layout=layout)
        frame.sample_rate = sample_rate
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path


def _sine(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


def _dominant_freq(pcm: np.ndarray, sample_rate: int = SR) -> float:
    sig = pcm[0] if pcm.ndim == 2 else pcm
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
    return float(np.fft.rfftfreq(sig.shape[0], 1.0 / sample_rate)[np.argmax(spec)])


def _audio_timeline(
    asset_id: str,
    *,
    timeline_duration: int,
    source_duration: int,
    source_start: int = 0,
    speed: SpeedParams | float | None = None,
    fps: Rational | float = FPS,
    freeze: int | None = None,
    effects=(),
) -> TimelineSequence:
    clip = ClipInstance(
        "clip-1", asset_id, "a0", 0, timeline_duration,
        source_start, source_duration,
        speed=speed if speed is not None else 1.0,
        freeze_source_frame=freeze,
        effects=tuple(effects),
    )
    track = EditTrack(
        "a0", "A1", "audio", 0, clips=(clip,), volume=1.0)
    return TimelineSequence(
        timeline_id="seq-a", name="A", width=64, height=48,
        fps=fps if isinstance(fps, Rational) else Rational(int(fps), 1),
        audio_tracks=(track,),
    )


def _exporter(project, sequence, fps: float = FPS) -> TimelineExporter:
    return TimelineExporter(project, sequence, Mock())


def _config(tmp_path) -> TimelineExportConfig:
    return TimelineExportConfig(output_path=str(tmp_path / "out.mov"))


def _render_master(exporter, end_frame: int, config) -> np.ndarray | None:
    return exporter._render_audio_master(0, end_frame, config, encoder=Mock())


# ── 单元级：重采样原语 ──


class TestResamplePrimitives:
    def test_resample_2x_doubles_frequency(self):
        """步长 2 的抽取使 440Hz → 880Hz（未保调 2x 的核心语义）。"""
        src = _sine(440.0, 1.0)
        n = src.shape[1] // 2
        out = resample_segment(src, 0.0, float(2 * n), n)
        assert abs(_dominant_freq(out) - 880.0) < 4.0

    def test_resample_reverse_reverses_time(self):
        """半开区间约定：out[i] 位置 = x0 + i*(x1-x0)/n。反向 span
        [n-1, -1) 的步长恰为 -1 → out[i] == src[n-1-i]，逐样本时间反转
        （ClipRetimeReader.read_frame 对反向 span 施加同样的边界移位）。"""
        src = _sine(440.0, 0.5)
        n = src.shape[1] - 2
        fwd = resample_segment(src, 0.0, float(n), n)
        rev = resample_segment(src, float(n - 1), -1.0, n)
        np.testing.assert_allclose(rev, fwd[:, ::-1], atol=1e-4)

    def test_needs_retime_detection(self):
        plain = ClipInstance("c", "a", "t", 0, 10, 0, 10)
        assert not needs_retime(plain)
        assert needs_retime(
            ClipInstance("c", "a", "t", 0, 10, 0, 20, speed=SpeedParams(speed=2.0)))
        assert needs_retime(
            ClipInstance("c", "a", "t", 0, 10, 0, 10,
                         speed=SpeedParams(speed=1.0, reverse=True)))
        assert needs_retime(
            ClipInstance("c", "a", "t", 0, 10, 0, 10,
                         speed=SpeedParams(speed=1.0,
                                           speed_points=(SpeedPoint(5, 2.0),))))
        frozen = ClipInstance("c", "a", "t", 0, 10, 0, 10, freeze_source_frame=3)
        assert not needs_retime(frozen)  # 冻结由渲染整体跳过


# ── D01：成片 retime 频率/时序语义 ──


class TestD01MasterRetime:
    @pytest.mark.parametrize("speed,tl_dur,src_dur,expect_hz", [
        (SpeedParams(speed=2.0), 50, 100, 880.0),
        (SpeedParams(speed=0.5), 200, 100, 220.0),
        (2.0, 50, 100, 880.0),
    ])
    def test_speed_changes_pitch(self, tmp_path, speed, tl_dur, src_dur, expect_hz):
        wav = _write_wav(tmp_path / "sine.wav", _sine(440.0, src_dur / FPS + 1.0))
        asset = MediaAsset("asset-1", str(wav), "audio", "Sine", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        seq = _audio_timeline(
            "asset-1", timeline_duration=tl_dur, source_duration=src_dur,
            speed=speed)
        exporter = _exporter(project, seq)
        pcm = _render_master(exporter, tl_dur, _config(tmp_path))
        assert pcm is not None and pcm.shape[-1] > 0
        assert abs(_dominant_freq(pcm) - expect_hz) < 6.0, \
            f"speed={speed}: 期望 ~{expect_hz}Hz，实测 {_dominant_freq(pcm):.0f}Hz"

    def test_reverse_matches_forward_time_reversed(self, tmp_path):
        wav = _write_wav(tmp_path / "sine.wav", _sine(440.0, 4.0))
        asset = MediaAsset("asset-1", str(wav), "audio", "Sine", {})
        project = EditProject("p", "P", assets={"asset-1": asset})

        fwd_seq = _audio_timeline("asset-1", timeline_duration=100, source_duration=100)
        fwd = _render_master(_exporter(project, fwd_seq), 100, _config(tmp_path))
        assert fwd is not None

        rev_seq = _audio_timeline(
            "asset-1", timeline_duration=100, source_duration=100,
            speed=SpeedParams(speed=1.0, reverse=True))
        rev = _render_master(_exporter(project, rev_seq), 100, _config(tmp_path))
        assert rev is not None

        # 倒放内部帧 = 对应正向帧的逐样本时间反转（半开块语义）：
        # rev[f] == reverse(fwd[99 - f])，f 取 1..98（两端为边界保持帧，
        # 与视频 timeline_to_source_frame 的钳制语义一致，另行放松）。
        for f in (1, 10, 50, 98):
            rev_block = rev[:, f * 1920:(f + 1) * 1920]
            fwd_block = fwd[:, (99 - f) * 1920:(100 - f) * 1920]
            np.testing.assert_allclose(
                rev_block, fwd_block[:, ::-1], atol=5e-3,
                err_msg=f"reverse frame {f} mismatch")

    def test_nonzero_source_start_reads_correct_window(self, tmp_path):
        """非零入点：2x 从 source_start=25 起——首帧源位置应是 25（不是 50）。"""
        clip = ClipInstance(
            "c", "a", "t", 0, 50, 25, 100, speed=SpeedParams(speed=2.0))
        lo, hi = clip.timeline_to_source_frame_pair(0), None
        from src.features.edit.core.audio_retime import position_span
        pos_lo, _ = position_span(clip, 0)
        assert pos_lo == pytest.approx(0.0, abs=1e-6)  # 源相对位置 0 → 绝对 25


# ── D05：Master stem 与成片一致 ──


class TestD05StemMasterParity:
    def _parity_case(self, tmp_path, seq, end_frame, fps):
        wav = _write_wav(tmp_path / "sine.wav", _sine(440.0, 8.0))
        asset = MediaAsset("asset-1", str(wav), "audio", "Sine", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        seq = seq("asset-1")
        exporter = _exporter(project, seq)
        master = _render_master(exporter, end_frame, _config(tmp_path))
        assert master is not None

        decode, close = create_clip_decode_fn(project, float(fps), SR)
        try:
            stem = render_stem_pcm(
                seq, StemSpec(StemType.MASTER, "Master", include_master=True),
                sample_rate=SR, channels=2, decode_clip_fn=decode)
        finally:
            close()
        assert stem is not None
        n = min(master.shape[-1], stem.shape[-1])
        diff = float(np.max(np.abs(master[:, :n] - stem[:, :n])))
        return master, stem, diff

    def test_plain_clip_parity(self, tmp_path):
        master, stem, diff = self._parity_case(
            tmp_path,
            lambda a: _audio_timeline(a, timeline_duration=100, source_duration=100),
            100, FPS)
        assert diff < 1e-4, f"普通片段 Master stem 与成片 maxdiff={diff}"

    def test_retime_clip_parity(self, tmp_path):
        """D01+D05：变速片段两条路径共享 retime 语义。"""
        master, stem, diff = self._parity_case(
            tmp_path,
            lambda a: _audio_timeline(a, timeline_duration=50, source_duration=100,
                                      speed=SpeedParams(speed=2.0)),
            50, FPS)
        assert diff < 1e-4, f"2x 片段 Master stem 与成片 maxdiff={diff}"

    def test_frozen_clip_silent_in_both(self, tmp_path):
        """冻结片段：成片与 Master stem 都无输出（此前 stem 仍出声）。"""
        wav = _write_wav(tmp_path / "sine.wav", _sine(440.0, 8.0))
        asset = MediaAsset("asset-1", str(wav), "audio", "Sine", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        seq = _audio_timeline("asset-1", timeline_duration=100, source_duration=100,
                              freeze=50)
        master = _render_master(_exporter(project, seq), 100, _config(tmp_path))
        assert master is None or float(np.max(np.abs(master))) < 1e-6

        decode, close = create_clip_decode_fn(project, FPS, SR)
        try:
            stem = render_stem_pcm(
                seq, StemSpec(StemType.MASTER, "Master", include_master=True),
                sample_rate=SR, channels=2, decode_clip_fn=decode)
        finally:
            close()
        assert stem is None or float(np.max(np.abs(stem))) < 1e-6

    def test_clip_fx_applied_in_both(self, tmp_path):
        """clip limiter：成片与 stem 都被压限（此前 stem 完全没有 clip FX）。"""
        from src.features.edit.core.effect_instance import AnimatableParam, EffectInstance

        fx = EffectInstance(
            effect_type="limiter",
            params={"threshold_db": -6.0, "enabled": True},
        )
        wav = _write_wav(tmp_path / "sine.wav", _sine(440.0, 8.0, amp=0.8))
        asset = MediaAsset("asset-1", str(wav), "audio", "Sine", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        seq = _audio_timeline("asset-1", timeline_duration=100, source_duration=100,
                              effects=(fx,))
        master = _render_master(_exporter(project, seq), 100, _config(tmp_path))
        assert master is not None
        assert float(np.max(np.abs(master))) < 0.55, \
            "成片应受 limiter 压限（0.8 输入 → ~0.5 上限）"

        decode, close = create_clip_decode_fn(project, FPS, SR)
        try:
            stem = render_stem_pcm(
                seq, StemSpec(StemType.MASTER, "Master", include_master=True),
                sample_rate=SR, channels=2, decode_clip_fn=decode)
        finally:
            close()
        assert stem is not None
        n = min(master.shape[-1], stem.shape[-1])
        diff = float(np.max(np.abs(master[:, :n] - stem[:, :n])))
        assert diff < 1e-4, f"limiter clip：stem 与成片 maxdiff={diff}"


# ── D04：动画 FX 关键帧作用于 PCM ──


class _DCDecoder:
    """恒定 0.5 DC 假 decoder（审计 D04 复现同款，明确非真实素材）。"""

    instances: list["_DCDecoder"] = []

    def __init__(self):
        self.closed = False
        self.source_layout = None
        _DCDecoder.instances.append(self)

    def open(self, path, **kwargs):
        return True

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, 0.5, dtype=np.float32)
        return np.stack([sig, sig])

    def decode_next_samples(self, num_samples):
        sig = np.full(num_samples, 0.5, dtype=np.float32)
        return np.stack([sig, sig])

    def seek_to_time(self, t):
        return True

    _sample_rate = SR

    def close(self):
        self.closed = True


@pytest.fixture
def dc_decoder(monkeypatch):
    _DCDecoder.instances = []
    monkeypatch.setattr(
        "src.features.edit.core.audio_playback.AudioDecoder", _DCDecoder)
    yield _DCDecoder
    _DCDecoder.instances = []


def _limiter_clip(animated_threshold=None):
    from src.features.edit.core.effect_instance import AnimatableParam, EffectInstance

    animated = {}
    if animated_threshold is not None:
        track = KeyframeTrack(
            param_name="threshold_db", min_val=-20.0, max_val=0.0,
            keyframes=[Keyframe(0, animated_threshold)])
        animated["threshold_db"] = AnimatableParam(
            base_value=0.0, keyframes=track, min_value=-20.0, max_value=0.0)
    fx = EffectInstance(
        effect_type="limiter",
        params={"threshold_db": 0.0, "enabled": True},
        animated_params=animated,
    )
    return _audio_timeline(
        "asset-1", timeline_duration=24, source_duration=24, effects=(fx,))


def _dc_project():
    asset = MediaAsset("asset-1", "/unused.wav", "audio", "DC", {})
    return EditProject("p", "P", assets={"asset-1": asset})


class TestD04AnimatedFxAudio:
    def test_keyframe_changes_master_pcm(self, tmp_path, dc_decoder):
        """frame0 threshold=-20 关键帧：输出 ≈0.1（静态 0 时为 0.5）。"""
        seq = _limiter_clip(animated_threshold=-20.0)
        pcm = _render_master(_exporter(_dc_project(), seq), 24, _config(tmp_path))
        assert pcm is not None
        peak = float(np.max(np.abs(pcm)))
        assert peak == pytest.approx(0.1, abs=0.02), \
            f"动画 threshold=-20 应把 DC 0.5 压到 ~0.1，实测 {peak}"

    def test_keyframe_changes_stem_pcm(self):
        """同上，但走 Stem 路径（decode_clip_fn 用 DC）。"""
        seq = _limiter_clip(animated_threshold=-20.0)

        def dc_decode(track, clip, frame_idx, fps, n):
            sig = np.full(n, 0.5, dtype=np.float32)
            return np.stack([sig, sig])

        stem = render_stem_pcm(
            seq, StemSpec(StemType.MASTER, "Master", include_master=True),
            sample_rate=SR, channels=2, decode_clip_fn=dc_decode)
        assert stem is not None
        peak = float(np.max(np.abs(stem)))
        assert peak == pytest.approx(0.1, abs=0.02), \
            f"Stem 动画 threshold 应同样生效，实测 {peak}"

    def test_static_baseline_unchanged(self, tmp_path, dc_decoder):
        """无关键帧时维持旧行为：threshold=0 → 0.5 直通。"""
        seq = _limiter_clip()
        pcm = _render_master(_exporter(_dc_project(), seq), 24, _config(tmp_path))
        assert pcm is not None
        assert float(np.max(np.abs(pcm))) == pytest.approx(0.5, abs=0.01)


# ── D06：非整数帧率无采样洞 ──


class TestD06NonIntegerFps:
    NTSC = Rational(30000, 1001)
    FRAMES = 300

    def _constant_timeline(self):
        return _audio_timeline(
            "asset-1", timeline_duration=self.FRAMES, source_duration=self.FRAMES,
            fps=self.NTSC)

    @staticmethod
    def _count_interior_holes(pcm: np.ndarray) -> int:
        sig = pcm[0]
        interior = sig[1:-1] if sig.shape[0] > 2 else sig
        return int(np.count_nonzero(interior == 0.0))

    def test_master_no_sample_holes(self, tmp_path):
        """恒定非零信号（幅度 0.5 正弦全正半周? 用 0.5 常量）无内部零洞。"""
        pcm_src = np.full((2, int(12.0 * SR)), 0.5, dtype=np.float32)
        wav = _write_wav(tmp_path / "dc.wav", pcm_src)
        asset = MediaAsset("asset-1", str(wav), "audio", "DC", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        exporter = _exporter(project, self._constant_timeline())
        pcm = _render_master(exporter, self.FRAMES, _config(tmp_path))
        assert pcm is not None
        # 300 帧 30000/1001 @48kHz → 480480 样本
        assert pcm.shape[-1] == 480480
        holes = self._count_interior_holes(pcm)
        assert holes == 0, f"恒定信号存在 {holes} 个内部零样本洞"

    def test_stem_no_sample_holes(self, tmp_path):
        pcm_src = np.full((2, int(12.0 * SR)), 0.5, dtype=np.float32)
        wav = _write_wav(tmp_path / "dc.wav", pcm_src)
        asset = MediaAsset("asset-1", str(wav), "audio", "DC", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        seq = self._constant_timeline()
        decode, close = create_clip_decode_fn(project, 30000 / 1001, SR)
        try:
            stem = render_stem_pcm(
                seq, StemSpec(StemType.MASTER, "Master", include_master=True),
                sample_rate=SR, channels=2, decode_clip_fn=decode)
        finally:
            close()
        assert stem is not None
        assert stem.shape[-1] == 480480
        holes = self._count_interior_holes(stem)
        assert holes == 0, f"Stem 恒定信号存在 {holes} 个内部零样本洞"


# ── D02：归一化分析（真实声道布局 + 静音门限） ──


class TestD02NormalizeAnalysis:
    def _worker_with_pcm(self, pcm, sample_rate=SR):
        from src.features.edit.ui.timeline_view import _NormalizeGainWorker

        worker = _NormalizeGainWorker("/unused.wav")
        worker._decode_source = lambda cancel_check=None: (pcm, float(sample_rate))
        return worker

    def test_antiphase_measured_same_as_inphase(self):
        """反相立体声 L/R=-L：BS.1770 按声道能量——与同相测得一致响度，
        且建议负增益（此前混单声道测成 -70LUFS 建议 +47dB）。"""
        t = np.arange(int(4 * SR)) / SR
        base = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        inphase = np.stack([base, base])
        antiphase = np.stack([base, -base])

        ok_i, _, gain_i = self._worker_with_pcm(inphase).analyze_sync()
        ok_a, text_a, gain_a = self._worker_with_pcm(antiphase).analyze_sync()
        assert ok_i and ok_a
        assert gain_i is not None and gain_a is not None
        assert abs(gain_i - gain_a) < 0.5, (
            f"反相 ({gain_a:+.2f}dB) 与同相 ({gain_i:+.2f}dB) 增益建议应一致")
        assert gain_a < 0.0, "440Hz/0.3 立体声应建议负增益，而不是放大"

    def test_silence_refuses_positive_gain(self):
        silent = np.zeros((2, int(2 * SR)), dtype=np.float32)
        ok, text, gain = self._worker_with_pcm(silent).analyze_sync()
        assert ok
        assert gain is None, "静音素材必须拒绝正增益建议"
        assert "拒绝" in text or "静音" in text


# ── D07：取消路径关闭 decoder ──


class TestD07CancelClosesDecoders:
    def test_cancel_inside_frame_loop_closes_decoder(self, tmp_path, dc_decoder):
        seq = _audio_timeline("asset-1", timeline_duration=24, source_duration=24)
        exporter = _exporter(_dc_project(), seq)
        exporter._cancel_requested = True
        result = _render_master(exporter, 24, _config(tmp_path))
        assert result is None
        opened = [d for d in _DCDecoder.instances if not d.closed]
        assert not opened, f"取消后 {len(opened)} 个 decoder 未关闭"
        assert _DCDecoder.instances, "应当创建过至少一个 decoder"

    def test_normal_path_closes_decoder_once(self, tmp_path, dc_decoder):
        seq = _audio_timeline("asset-1", timeline_duration=24, source_duration=24)
        exporter = _exporter(_dc_project(), seq)
        pcm = _render_master(exporter, 24, _config(tmp_path))
        assert pcm is not None
        assert _DCDecoder.instances
        assert all(d.closed for d in _DCDecoder.instances)
