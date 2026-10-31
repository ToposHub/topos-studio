"""第七轮审计 0A-P1 回归：音频输出正确性（R03/R04/R05/R06/R07）。

R03：非零入点变速不得输出整段 DC 常量（采样端不得叠加绝对 source_start）。
R04：解码器返回短块（AAC 帧粒度/流尾）不得让整个 clip 余下音频静音。
R05：preserve-pitch（WSOLA）真实生效——成片与 Stem 同一共享实现，
     2x 保调输出仍为原频率（旧成片 helper np 未定义静默回退变调）。
R06：混帧率音频用源时钟取源——50fps 源在 25fps 序列 1x，1 秒时间线
     读 1 秒源（旧实现按序列 fps 定位读到 2 秒源）。
R07：完整倒放最后一帧不是常量块（末端外推不得被钳回起点）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import SpeedParams
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
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core

SR = 48000
FPS = 25
SPF = SR // FPS


def _write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SR) -> Path:
    import av

    channels = pcm.shape[0]
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")
    container = av.open(str(path), mode="w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=sample_rate)
    stream.layout = layout
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


def _ramp(seconds: float) -> np.ndarray:
    """0 → 0.05*seconds 的线性坡度（斜率 0.05/s，12s 内不超 0.6 不被
    WAV 削顶），用于读源窗口断言。"""
    t = np.arange(int(seconds * SR)) / SR
    sig = (0.05 * t).astype(np.float32)
    return np.stack([sig, sig])


def _sine(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


def _dominant_freq(pcm: np.ndarray, sample_rate: int = SR) -> float:
    sig = pcm[0] if pcm.ndim == 2 else pcm
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
    return float(np.fft.rfftfreq(sig.shape[0], 1.0 / sample_rate)[np.argmax(spec)])


def _timeline(
    asset_id: str,
    *,
    timeline_duration: int,
    source_duration: int,
    source_start: int = 0,
    speed=1.0,
    source_fps: tuple[int, int] | None = None,
) -> TimelineSequence:
    kwargs = {}
    if source_fps is not None:
        kwargs = {
            "_source_fps_num": source_fps[0],
            "_source_fps_den": source_fps[1],
            "_timeline_fps_num": FPS,
            "_timeline_fps_den": 1,
        }
    clip = ClipInstance(
        "clip-1", asset_id, "a0", 0, timeline_duration,
        source_start, source_duration,
        speed=speed,
        **kwargs,
    )
    track = EditTrack("a0", "A1", "audio", 0, clips=(clip,), volume=1.0)
    return TimelineSequence(
        timeline_id="seq-a", name="A", width=64, height=48,
        fps=Rational(FPS, 1), audio_tracks=(track,),
    )


def _project(tmp_path: Path, pcm: np.ndarray, name: str = "sine.wav"):
    wav = _write_wav(tmp_path / name, pcm)
    asset = MediaAsset("asset-1", str(wav), "audio", "Media", {})
    return EditProject("p", "P", assets={"asset-1": asset})


def _render_master(project, sequence, end_frame: int, tmp_path) -> np.ndarray:
    exporter = TimelineExporter(project, sequence, Mock())
    pcm = exporter._render_audio_master(
        0, end_frame, TimelineExportConfig(output_path=str(tmp_path / "out.mov")),
        encoder=Mock(),
    )
    assert pcm is not None
    return pcm


# ── R03：非零入点变速无 DC 常量块 ──


class TestR03NonzeroInpointRetime:
    def test_2x_from_inpoint_25_tracks_ramp(self, tmp_path):
        """入点 25（1s）2x：源 [1s,3s) 坡度 0.25→0.75，输出首样本 ~0.25、
        末样本 ~0.75，尾部 std 远大于常量块的 ~1e-9。"""
        project = _project(tmp_path, _ramp(12.0), "ramp.wav")
        seq = _timeline(
            "asset-1", timeline_duration=25, source_duration=50,
            source_start=25, speed=SpeedParams(speed=2.0))
        pcm = _render_master(project, seq, 25, tmp_path)
        assert pcm.shape[-1] == 25 * SPF
        assert abs(float(pcm[0, 0]) - 0.05) < 0.01, \
            f"首样本应 ~0.05（源 1s 处），实测 {pcm[0, 0]:.4f}"
        assert abs(float(pcm[0, -1]) - 0.15) < 0.02, \
            f"末样本应 ~0.15（源 3s 处），实测 {pcm[0, -1]:.4f}"
        tail_std = float(np.std(pcm[0, -SPF:]))
        assert tail_std > 1e-3, f"尾部 1 秒 std={tail_std:.3e}——DC 常量块"

    @pytest.mark.parametrize("inpoint", [0, 25, 100])
    def test_retime_tail_not_constant(self, tmp_path, inpoint):
        """0/25/100 入点 × 2x：每一段尾部都不是常量块。"""
        project = _project(tmp_path, _ramp(12.0), "ramp.wav")
        seq = _timeline(
            "asset-1", timeline_duration=25, source_duration=50,
            source_start=inpoint, speed=SpeedParams(speed=2.0))
        pcm = _render_master(project, seq, 25, tmp_path)
        tail_std = float(np.std(pcm[0, -SPF:]))
        assert tail_std > 1e-3, f"inpoint={inpoint}: 尾部 std={tail_std:.3e}"


# ── R04：AAC 短块不炸整段 ──


class _ShortBlockDecoder:
    """第 5 帧返回 block_len-16 的短块（模拟 AAC 帧粒度/流尾），其余恒 0.5。"""

    instances: list["_ShortBlockDecoder"] = []

    def __init__(self):
        self.closed = False
        self.source_layout = None
        self.calls = 0
        _ShortBlockDecoder.instances.append(self)

    def open(self, path, **kwargs):
        return True

    def decode_samples_for_frame(self, frame, fps, num_samples):
        self.calls += 1
        n = num_samples - 16 if 4 <= (frame % 1000) <= 4 else num_samples
        sig = np.full(n, 0.5, dtype=np.float32)
        return np.stack([sig, sig])

    def seek_to_time(self, t):
        return True

    def close(self):
        self.closed = True


class TestR04ShortBlockNotSilent:
    def test_short_block_padded_and_clip_continues(self, tmp_path, monkeypatch):
        """帧 4 短块 16 样本：只允许这 16 个补零样本，帧 5 起仍为 0.5——
        旧实现 += 广播异常被外层 except 吞掉，帧 4 起整段静音仍 done。"""
        _ShortBlockDecoder.instances = []
        monkeypatch.setattr(
            "src.features.edit.core.audio_playback.AudioDecoder",
            _ShortBlockDecoder)
        asset = MediaAsset("asset-1", "/unused.wav", "audio", "DC", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        seq = _timeline("asset-1", timeline_duration=24, source_duration=24)
        pcm = _render_master(project, seq, 24, tmp_path)
        assert pcm.shape[-1] == 24 * SPF
        zeros = int(np.count_nonzero(pcm[0] == 0.0))
        assert zeros <= 16, f"整段应只有短块补零 ≤16 个零样本，实测 {zeros}"
        after = pcm[0, 5 * SPF:6 * SPF]
        np.testing.assert_allclose(after, 0.5, atol=1e-6,
                                   err_msg="短块之后音频必须继续（旧实现整段静音）")
        assert all(d.closed for d in _ShortBlockDecoder.instances)


# ── R05：保调（WSOLA）成片 + Stem 共享 ──


class TestR05PreservePitch:
    def test_master_2x_preserves_frequency(self, tmp_path):
        """440Hz 2x 保调：输出主频仍 ~440Hz（旧实现 np 未定义回退变调→880）。"""
        project = _project(tmp_path, _sine(440.0, 4.0))
        seq = _timeline(
            "asset-1", timeline_duration=50, source_duration=100,
            speed=SpeedParams(speed=2.0, preserve_pitch=True))
        pcm = _render_master(project, seq, 50, tmp_path)
        hz = _dominant_freq(pcm)
        assert abs(hz - 440.0) < 8.0, f"2x 保调应 ~440Hz，实测 {hz:.0f}Hz"

    def test_stem_2x_preserves_frequency_and_matches_master(self, tmp_path):
        """Stem 路径同样保调，且与成片逐样本一致（共享 PreservedPitchReader）。"""
        project = _project(tmp_path, _sine(440.0, 4.0))
        seq = _timeline(
            "asset-1", timeline_duration=50, source_duration=100,
            speed=SpeedParams(speed=2.0, preserve_pitch=True))
        master = _render_master(project, seq, 50, tmp_path)

        decode, close = create_clip_decode_fn(project, FPS, SR)
        try:
            stem = render_stem_pcm(
                seq, StemSpec(StemType.MASTER, "Master", include_master=True),
                sample_rate=SR, channels=2, decode_clip_fn=decode)
        finally:
            close()
        assert stem is not None
        hz = _dominant_freq(stem)
        assert abs(hz - 440.0) < 8.0, f"Stem 2x 保调应 ~440Hz，实测 {hz:.0f}Hz"
        n = min(master.shape[-1], stem.shape[-1])
        diff = float(np.max(np.abs(master[:, :n] - stem[:, :n])))
        assert diff < 1e-4, f"保调 Stem 与成片 maxdiff={diff}"

    def test_half_speed_preserves_frequency(self, tmp_path):
        """0.5x 保调：440Hz → ~440Hz（变调路径会掉到 ~220Hz）。"""
        project = _project(tmp_path, _sine(440.0, 3.0))
        seq = _timeline(
            "asset-1", timeline_duration=50, source_duration=25,
            speed=SpeedParams(speed=0.5, preserve_pitch=True))
        pcm = _render_master(project, seq, 50, tmp_path)
        hz = _dominant_freq(pcm)
        assert abs(hz - 440.0) < 8.0, f"0.5x 保调应 ~440Hz，实测 {hz:.0f}Hz"


# ── R06：混帧率源时钟 ──


class TestR06MixedFpsSourceClock:
    def test_1x_50fps_source_on_25fps_timeline_reads_one_second(self, tmp_path):
        """源 50fps / 序列 25fps 1x：时间线 25 帧（1s）读源 [0,1s) 坡度
        0→0.25——旧实现按序列 fps 定位，1 秒时间线读到 2 秒源（0→0.5）。"""
        project = _project(tmp_path, _ramp(4.0), "ramp.wav")
        seq = _timeline(
            "asset-1", timeline_duration=25, source_duration=50,
            source_fps=(50, 1))
        pcm = _render_master(project, seq, 25, tmp_path)
        assert pcm.shape[-1] == 25 * SPF
        assert abs(float(pcm[0, -1]) - 0.05) < 0.01, \
            f"1x 混帧率末样本应 ~0.05（源 1s），实测 {pcm[0, -1]:.4f}"
        assert abs(float(pcm[0, SPF - 1]) - 0.05 * (SPF - 1) / SR) < 0.01

    def test_2x_50fps_source_on_25fps_timeline(self, tmp_path):
        """源 50fps 2x：时间线 1s 覆盖源 [0,2s) 坡度 0→0.5。"""
        project = _project(tmp_path, _ramp(4.0), "ramp.wav")
        seq = _timeline(
            "asset-1", timeline_duration=25, source_duration=100,
            speed=SpeedParams(speed=2.0), source_fps=(50, 1))
        pcm = _render_master(project, seq, 25, tmp_path)
        assert abs(float(pcm[0, -1]) - 0.10) < 0.02, \
            f"2x 混帧率末样本应 ~0.10（源 2s），实测 {pcm[0, -1]:.4f}"

    def test_stem_mixed_fps_matches_master(self, tmp_path):
        """混帧率 Stem 与成片一致（两条路径同一源时钟）。"""
        project = _project(tmp_path, _ramp(4.0), "ramp.wav")
        seq = _timeline(
            "asset-1", timeline_duration=25, source_duration=50,
            source_fps=(50, 1))
        master = _render_master(project, seq, 25, tmp_path)
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
        assert diff < 1e-4, f"混帧率 1x Stem 与成片 maxdiff={diff}"


# ── R07：完整倒放末帧 ──


class TestR07ReverseTail:
    def test_full_reverse_last_frame_varies(self, tmp_path):
        """完整倒放 100 帧：末帧（源帧 0）应仍在变化（坡度起点附近），
        旧实现外推 -1 被钳回 0 → x0==x1 整帧常量（std≈0）。"""
        project = _project(tmp_path, _ramp(4.0), "ramp.wav")
        seq = _timeline(
            "asset-1", timeline_duration=100, source_duration=100,
            speed=SpeedParams(speed=1.0, reverse=True))
        pcm = _render_master(project, seq, 100, tmp_path)
        assert pcm.shape[-1] == 100 * SPF
        tail = pcm[0, -SPF:]
        tail_std = float(np.std(tail))
        # 坡度 0.05/s 一帧内的理论 std ≈ 0.05*0.04/√12 ≈ 5.8e-4；常量块 ~1e-9
        assert tail_std > 1e-4, f"倒放末帧 std={tail_std:.3e}——常量块"
        # 末帧对应源帧 0（坡度起点 ~0），均值应接近 0 而非源尾
        assert float(np.mean(tail)) < 0.02, \
            f"倒放末帧均值 {float(np.mean(tail)):.4f}——不是源起点内容"

    def test_reverse_interior_frames_still_match_forward(self, tmp_path):
        """R07 修复不破坏第六轮的倒放内部帧语义。"""
        project = _project(tmp_path, _sine(440.0, 4.0))
        fwd = _render_master(
            project, _timeline("asset-1", timeline_duration=100,
                               source_duration=100), 100, tmp_path)
        rev = _render_master(
            project, _timeline("asset-1", timeline_duration=100,
                               source_duration=100,
                               speed=SpeedParams(speed=1.0, reverse=True)),
            100, tmp_path)
        for f in (1, 10, 50, 98):
            rev_block = rev[:, f * SPF:(f + 1) * SPF]
            fwd_block = fwd[:, (99 - f) * SPF:(100 - f) * SPF]
            np.testing.assert_allclose(
                rev_block, fwd_block[:, ::-1], atol=5e-3,
                err_msg=f"reverse frame {f} mismatch")
