"""P0 · 预渲染成品 PCM 作为独立源（2026-11-19 计划第一阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- 真实 1 秒 WAV 以 50% 播放（栈内 time_stretch）生成 2 秒缓存后，
  实时播放第二秒不再全静音——源解码只负责输入，缓存成品定义有效
  输出范围；
- 成片和 Stem 的同例，最后一帧有应有能量；
- 实时、成片、Stem 共用同一 clip block provider
  （``clip_block_from_pre_render``）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_edit import (
    clip_block_from_pre_render,
    make_step,
    new_audio_edit,
)

RATE = 48000
FPS = 24.0
SPF = int(RATE // FPS)  # 2000


def _stretch_edit(total: int = RATE) -> dict:
    """栈内 50% 变速（time_stretch）——烘焙输出长度翻倍。"""
    return new_audio_edit([make_step(
        "time_stretch", 0, total, (0, 1),
        ratio_pct=50.0, output_length=2 * total)])


def _ramp_base(samples: int = 2 * RATE) -> np.ndarray:
    """可辨识的成品基底：左 0.4 方波交替 / 右线性坡（跨块可对位）。"""
    t = np.arange(samples, dtype=np.float64)
    left = 0.4 * ((t // 500) % 2)
    right = 0.3 * t / samples
    return np.stack([left, right]).astype(np.float32)


# ----------------------------------------------------------------------
# provider 单元
# ----------------------------------------------------------------------
class TestClipBlockProvider:
    def test_returns_none_without_base_or_requirement(self) -> None:
        edit = _stretch_edit()
        assert clip_block_from_pre_render(edit, None, 0, 100,
                                          sample_rate=RATE, channels=2) is None
        plain = new_audio_edit([make_step("mute", 0, 100, (0,))])
        assert clip_block_from_pre_render(
            plain, _ramp_base(200), 0, 100, sample_rate=RATE, channels=2) is None

    def test_supplies_base_content_past_source_eof(self) -> None:
        edit = _stretch_edit()
        base = _ramp_base()
        block = clip_block_from_pre_render(
            edit, base, RATE, 1000, sample_rate=RATE, channels=2)
        assert block is not None and block.shape == (2, 1000)
        np.testing.assert_array_equal(block, base[:, RATE:RATE + 1000])

    def test_zero_pads_beyond_base_end(self) -> None:
        edit = _stretch_edit()
        base = _ramp_base()
        block = clip_block_from_pre_render(
            edit, base, 2 * RATE - 100, 1000, sample_rate=RATE, channels=2)
        assert block is not None
        np.testing.assert_array_equal(block[:, :100], base[:, -100:])
        np.testing.assert_array_equal(block[:, 100:], np.zeros((2, 900)))

    def test_aligns_channel_width(self) -> None:
        edit = _stretch_edit()
        base = np.stack([np.full(2 * RATE, 0.25, dtype=np.float32)])
        block = clip_block_from_pre_render(
            edit, base, 0, 500, sample_rate=RATE, channels=2)
        assert block is not None and block.shape == (2, 500)
        np.testing.assert_array_equal(block[0], block[1])


# ----------------------------------------------------------------------
# 实时路径：源 EOF 后第二秒由成品供块
# ----------------------------------------------------------------------
class _EofDecoder:
    """源只有 1 s：边界帧短块、之后 None；片段时间线 2 s。"""

    _timeline_start = 0
    _timeline_duration = 48  # 2 s @ 24 fps

    def __init__(self, source: np.ndarray, audio_edit, base):
        self._source = source
        self._audio_edit = audio_edit
        self._pre_render_base = base

    def decode_samples_for_frame(self, frame, fps, num_samples):
        start = int(round(frame * RATE / float(fps)))
        if start >= self._source.shape[1]:
            return None  # 源 EOF
        block = self._source[:, start:start + num_samples]
        if block.shape[1] < num_samples:
            block = np.pad(block, ((0, 0),
                                   (num_samples - block.shape[1], 0)))
        return block.copy()

    def close(self):
        pass


class TestRealtimePlayback:
    def test_second_second_audible_after_source_eof(self, qapp) -> None:
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

        source = np.zeros((2, RATE), dtype=np.float32)   # 1 s 全零源
        edit = _stretch_edit()
        base = _ramp_base()
        mixer = AudioMixer()
        mixer.add_track("a1", _EofDecoder(source, edit, base),
                        AudioTrackState(track_id="a1", volume=1.0))
        # 第 36 帧（1.5 s）——源早已 EOF，成品必须供块。
        out, _ = mixer.mix_samples(36, FPS, SPF, 2)
        np.testing.assert_allclose(out, base[:, 72000:74000], atol=1e-6)
        assert np.max(np.abs(out)) > 0.1  # 有实际能量（非静音）
        mixer.remove_track("a1")

    def test_span_gate_still_ends_clip(self, qapp) -> None:
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

        source = np.zeros((2, RATE), dtype=np.float32)
        edit = _stretch_edit()
        base = _ramp_base()
        mixer = AudioMixer()
        mixer.add_track("a1", _EofDecoder(source, edit, base),
                        AudioTrackState(track_id="a1", volume=1.0))
        # 第 48 帧 = 片段时间线末尾之外 → 即便有成品也不供块。
        out, _ = mixer.mix_samples(48, FPS, SPF, 2)
        assert out is not None
        assert np.max(np.abs(out)) == 0.0

    def test_no_base_keeps_skip_semantics(self, qapp) -> None:
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

        source = np.zeros((2, RATE), dtype=np.float32)
        edit = _stretch_edit()
        mixer = AudioMixer()
        mixer.add_track("a1", _EofDecoder(source, edit, None),
                        AudioTrackState(track_id="a1", volume=1.0))
        out, _ = mixer.mix_samples(36, FPS, SPF, 2)
        assert np.max(np.abs(out)) == 0.0  # 缓存缺席：维持静音回退


# ----------------------------------------------------------------------
# Stem 路径：decode None + base_fn → 最后一帧有能量
# ----------------------------------------------------------------------
class TestStemExport:
    def _sequence(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        clip = ClipInstance(
            "c1", "asset-1", "t1", 0, 50, 0, 25,
            audio_edit=_stretch_edit(),
        )
        track = EditTrack("t1", "A1", "audio", 0, clips=(clip,), volume=1.0)
        return TimelineSequence(
            timeline_id="seq", name="S", fps=25.0, audio_tracks=(track,),
        )

    def test_stem_reads_base_past_source_eof(self) -> None:
        from src.features.edit.core.stem_export import (
            StemSpec,
            StemType,
            render_stem_pcm,
        )

        base = _ramp_base()
        seq = self._sequence()
        stem = StemSpec(stem_type=StemType.TRACK, name="T1",
                        track_ids=("t1",), include_master=False)

        def decode(track, clip, frame_idx, fps, n_samples):
            return None  # 源 EOF（真实路径中解码器耗尽）

        pcm = render_stem_pcm(
            seq, stem, sample_rate=RATE, channels=2,
            decode_clip_fn=decode,
            pre_render_base_fn=lambda clip: base,
        )
        assert pcm is not None
        total = 50 * (RATE // 25)
        assert pcm.shape == (2, total)
        # 逐块对位成品；最后一帧有应有能量。
        np.testing.assert_allclose(pcm[:, :base.shape[1]],
                                   base[:, :total], atol=1e-6)
        assert np.max(np.abs(pcm[:, -100:])) > 0.05

    def test_stem_none_decode_without_base_stays_silent(self) -> None:
        from src.features.edit.core.stem_export import (
            StemSpec,
            StemType,
            render_stem_pcm,
        )

        seq = self._sequence()
        stem = StemSpec(stem_type=StemType.TRACK, name="T1",
                        track_ids=("t1",), include_master=False)
        pcm = render_stem_pcm(
            seq, stem, sample_rate=RATE, channels=2,
            decode_clip_fn=lambda *a: None,
            pre_render_base_fn=lambda clip: None,
        )
        # 无成品保持原跳过语义：整轨静音（零块），不报错。
        assert pcm is not None
        assert np.max(np.abs(pcm)) == 0.0


# ----------------------------------------------------------------------
# 成片路径：真实 1 s WAV + 真实 WSOLA 预渲染 → 最后一帧有能量
# ----------------------------------------------------------------------
class TestTimelineExport:
    def test_master_last_frame_energy_after_source_eof(self, tmp_path) -> None:
        pytest.importorskip("scipy")
        from src.features.edit.core.project import EditProject, MediaAsset
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

        # 真实 1 s 440 Hz WAV（振幅 0.3）。
        t = np.arange(RATE) / RATE
        sine = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        pcm_src = np.stack([sine, sine])
        import av

        wav = tmp_path / "src.wav"
        container = av.open(str(wav), mode="w", format="wav")
        stream = container.add_stream("pcm_s16le", rate=RATE)
        stream.layout = "stereo"
        frame = av.AudioFrame.from_ndarray(
            np.clip(pcm_src, -1.0, 1.0).astype(np.float32),
            format="fltp", layout="stereo")
        frame.sample_rate = RATE
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
        container.close()

        asset = MediaAsset("asset-1", str(wav), "audio", "Media", {})
        project = EditProject("p", "P", assets={"asset-1": asset})
        clip = ClipInstance(
            "c1", "asset-1", "a0", 0, 50, 0, 25,
            audio_edit=_stretch_edit(),
        )
        track = EditTrack("a0", "A1", "audio", 0, clips=(clip,), volume=1.0)
        seq = TimelineSequence(
            timeline_id="seq-a", name="A", width=64, height=48,
            fps=Rational(25, 1), audio_tracks=(track,),
        )

        exporter = TimelineExporter(project, seq, Mock())
        master = exporter._render_audio_master(
            0, 50, TimelineExportConfig(output_path=str(tmp_path / "out.mov")),
            encoder=Mock(),
        )
        assert master is not None
        total = 50 * (RATE // 25)
        assert master.shape[-1] >= total - 10
        # 第一秒有能量（源 + 50% 拉长 → 频率减半但持续出声）。
        assert np.max(np.abs(master[:, :RATE])) > 0.05
        # 验收：第二秒（源已 EOF）不再全静音，最后一帧有应有能量
        # （末窗锥形收敛属 WSOLA 边界语义，能量须覆盖到 ≥99% 合同长度）。
        second = master[:, RATE:total]
        assert np.max(np.abs(second)) > 0.05
        frame = RATE // 25
        last_frame = master[:, total - frame:total]
        assert np.max(np.abs(last_frame)) > 0.05
        energy_end = np.nonzero(
            np.max(np.abs(master[:, :total]), axis=0) > 1e-3)[0][-1]
        assert energy_end >= int(total * 0.99)
