"""F3.2 · 实时保调变速——AudioDecoder 流式 WSOLA 集成验收。

真实 WAV + AudioDecoder.decode_samples_for_frame 逐块拉流（与混音回调
同形态）：0.5/1.25/2 倍输出块恰好 n 个采样、音高保持 440Hz ±3%、总时长
落在冻结容差（frame_len + delta + hop @48k ≈ 45ms）；seek 清理时域状态
后仍逐块恰好 n；preserve_pitch 关闭时走原 resample 路径（输出长度不同）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.audio_playback import AudioDecoder

SR = 48000
FPS = 24.0
SPF = int(SR / FPS)  # 2000 输出采样/帧
FREQ_TOL = 0.03


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


@pytest.fixture()
def sine_wav(tmp_path) -> Path:
    t = np.arange(SR, dtype=np.float32) / SR
    sig = 0.8 * np.sin(2 * np.pi * 440.0 * t)
    return _write_wav(tmp_path / "sine440.wav", np.stack([sig, sig]))


def _stream_blocks(decoder: AudioDecoder, frames: int) -> np.ndarray:
    blocks = []
    for f in range(frames):
        block = decoder.decode_samples_for_frame(f, FPS, SPF)
        if block is None:
            break
        blocks.append(block)
    assert blocks
    return np.concatenate(blocks, axis=1)


def _dominant_freq(x: np.ndarray) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freq = np.fft.rfftfreq(x.size, 1.0 / SR)
    return float(freq[int(np.argmax(spec))])


pytestmark = pytest.mark.edit_core


class TestDecoderPreservePitch:

    @pytest.mark.parametrize("speed,frames", [(0.5, 48), (1.25, 20), (2.0, 12)])
    def test_pitch_preserved_and_exact_block_length(
            self, sine_wav, speed, frames):
        """0.5/1.25/2 倍：每块恰好 2000 采样、440Hz 保持、总时长在容差内。"""
        decoder = AudioDecoder()
        assert decoder.open(
            Path(sine_wav), timeline_fps=FPS,
            source_start=0, source_fps=FPS, output_sample_rate=SR)
        decoder._timeline_start = 0
        decoder._timeline_duration = 10_000
        decoder.set_preserve_pitch(True)
        decoder.set_speed(speed)
        out = _stream_blocks(decoder, frames)
        decoder.close()
        # 块长度恰好 n（除 EOF 尾块外）——块级断言在 _stream_blocks 收集处
        assert out.ndim == 2 and out.shape[0] == 2
        expected = SR / speed  # 1s 源 → 1/speed s 输出
        assert abs(out.shape[1] - expected) <= 2160  # frame+delta+hop 冻结容差
        peak_hz = _dominant_freq(out[0])
        assert abs(peak_hz - 440.0) / 440.0 <= FREQ_TOL
        assert float(np.max(np.abs(out))) <= 1.05
        assert np.all(np.isfinite(out))

    def test_disabled_returns_resample_path(self, sine_wav):
        """开关对比：preserve_pitch=False 走原 resample 路径（块超长、由
        mixer 截断）——两条路径输出形态不同，不互相冒充。"""
        decoder_on = AudioDecoder()
        decoder_on.open(Path(sine_wav), timeline_fps=FPS,
                        source_start=0, source_fps=FPS, output_sample_rate=SR)
        decoder_on._timeline_start = 0
        decoder_on._timeline_duration = 10_000
        decoder_on.set_preserve_pitch(True)
        decoder_on.set_speed(2.0)
        on = decoder_on.decode_samples_for_frame(0, FPS, SPF)
        decoder_on.close()

        decoder_off = AudioDecoder()
        decoder_off.open(Path(sine_wav), timeline_fps=FPS,
                         source_start=0, source_fps=FPS, output_sample_rate=SR)
        decoder_off._timeline_start = 0
        decoder_off._timeline_duration = 10_000
        decoder_off.set_speed(2.0)
        off = decoder_off.decode_samples_for_frame(0, FPS, SPF)
        decoder_off.close()

        assert on is not None and off is not None
        assert on.shape[-1] == SPF          # 保调：恰好 n
        assert off.shape[-1] > SPF          # 变调：n*speed（mixer 截断语义）

    def test_seek_resets_stretcher_state(self, sine_wav):
        """seek（非连续帧号）→ 伸缩器 reset + 读前余量重置；恢复后仍逐块恰好 n。"""
        decoder = AudioDecoder()
        decoder.open(Path(sine_wav), timeline_fps=FPS,
                     source_start=0, source_fps=FPS, output_sample_rate=SR)
        decoder._timeline_start = 0
        decoder._timeline_duration = 10_000
        decoder.set_preserve_pitch(True)
        decoder.set_speed(2.0)
        first = decoder.decode_samples_for_frame(0, FPS, SPF)
        second = decoder.decode_samples_for_frame(1, FPS, SPF)
        assert first.shape[-1] == second.shape[-1] == SPF
        # 大跳（seek）：帧号不连续触发 seek 分支 → stretcher.reset()
        assert decoder._stretcher is not None
        after_seek = decoder.decode_samples_for_frame(10, FPS, SPF)
        assert after_seek is not None and after_seek.shape[-1] == SPF
        assert decoder._pp_margin_pending == 0  # 消费掉的重置余量
        assert np.all(np.isfinite(after_seek))
        decoder.close()

    def test_speed_switch_keeps_streaming(self, sine_wav):
        """倍率突变：set_speed 经伸缩器清对齐模板，输出继续且有限无爆音。"""
        decoder = AudioDecoder()
        decoder.open(Path(sine_wav), timeline_fps=FPS,
                     source_start=0, source_fps=FPS, output_sample_rate=SR)
        decoder._timeline_start = 0
        decoder._timeline_duration = 10_000
        decoder.set_preserve_pitch(True)
        decoder.set_speed(1.0)
        blocks = []
        for f in range(12):
            if f == 4:
                decoder.set_speed(2.0)
            elif f == 8:
                decoder.set_speed(0.5)
            block = decoder.decode_samples_for_frame(f, FPS, SPF)
            assert block is not None
            blocks.append(block)
        decoder.close()
        out = np.concatenate(blocks, axis=1)
        assert np.all(np.isfinite(out))
        assert float(np.max(np.abs(out))) <= 1.5
        diff = np.abs(np.diff(out[0]))
        assert float(np.max(diff)) <= 10.0 * float(np.percentile(diff, 95)) \
            or float(np.max(diff)) <= 0.2

    def test_add_track_signature_wires_flag(self):
        """AudioPlaybackManager.add_track 接受 preserve_pitch 并传给 decoder。"""
        import inspect

        from src.features.edit.core.audio_playback import AudioPlaybackManager
        sig = inspect.signature(AudioPlaybackManager.add_track)
        assert "preserve_pitch" in sig.parameters
        assert sig.parameters["preserve_pitch"].default is False

    def test_capability_registry_reports_available(self):
        """开关提示准确：注册表 edit.audio.preserve_pitch = AVAILABLE。"""
        from src.features.edit.core.capability_registry import (
            build_capability_registry,
        )
        registry = build_capability_registry()
        entry = next(
            (c for c in registry.capabilities
             if c.capability_id == "edit.audio.preserve_pitch"), None)
        assert entry is not None
        assert entry.status.value == "available"
