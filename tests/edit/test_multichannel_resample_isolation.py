"""P0-2 · 多声道跨率解码的逐通道隔离与源选道统一。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- 44.1→48、96→48、48→44.1 与同率：1 秒每种速率输出应为目标采样率
  的样本数（EOF 冲刷重采样器延迟尾）；
- 左击脉冲、右静音不串漏（逐通道独立 mono resampler，不再共用一个
  状态型实例——实测共用时静音声道串漏 ≈0.281 峰值 / 只收 4458 样本）；
- 片段取样 spec 携带绑定/patch 选道并进入 cache_key：切换选道后旧
  缓存失效；绑定选道优先于工程 patch。
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_pcm import (
    ClipAudioRenderSpec,
    decode_audio_range,
    resolve_source_channel_map,
)


def _write_wav(path: Path, pcm: np.ndarray, sample_rate: int) -> Path:
    """多声道 16-bit WAV（stdlib wave，交错）。"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.shape[0])
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        interleaved = (np.clip(pcm.T, -1.0, 1.0) * 32767.0).astype("<i2")
        w.writeframes(interleaved.tobytes())
    return path


def _sine(freq: float, seconds: float, amp: float, rate: int) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _four_ch_44100(amp: float = 0.25, seconds: float = 1.0) -> np.ndarray:
    """4ch 44.1 kHz：仅 CH1 有信号（2026-11-21 复现素材的合成版）。"""
    rate = 44100
    n = int(seconds * rate)
    pcm = np.zeros((4, n), dtype=np.float32)
    pcm[1] = _sine(440.0, seconds, amp, rate)
    return pcm


class TestCrossRateIsolation:
    def test_44100_4ch_exact_length_and_no_crosstalk(self, tmp_path) -> None:
        """复现案：44.1k 4ch 仅 CH1=0.25 → 48k 全长 48000、静音声道为零。"""
        wav = _write_wav(tmp_path / "mc441.wav", _four_ch_44100(), 44100)
        pcm = decode_audio_range(str(wav), 0, 48000, 48000)
        assert pcm is not None
        # 1 秒 @48k：不得短缺（共用 resampler 实测只收 4458 样本）。
        assert pcm.shape[1] == 48000
        assert pcm.shape[0] == 2  # BS.775 折到立体声
        # CH1（后右）静音：折混输出只含 CH1 贡献；两侧幅度有限且非零。
        assert float(np.abs(pcm).max()) > 1e-3
        # 逐声道独立：选 CH2（静音）为唯一输出时必须逐样本为零。
        silent = decode_audio_range(
            str(wav), 0, 48000, 48000, channel_map=((2, 0), (2, 1)))
        assert silent is not None and silent.shape[1] == 48000
        assert float(np.abs(silent).max()) == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.parametrize("src_rate,dst_rate", [
        (44100, 48000), (96000, 48000), (48000, 44100), (48000, 48000),
    ])
    def test_one_second_exact_length(self, tmp_path, src_rate, dst_rate) -> None:
        """1 秒立体声在四种率组合下输出恰为目标采样率样本数。"""
        t = np.arange(src_rate) / src_rate
        stereo = np.stack([
            (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32),
            (0.3 * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32),
        ])
        wav = _write_wav(tmp_path / f"s{src_rate}.wav", stereo, src_rate)
        pcm = decode_audio_range(str(wav), 0, dst_rate, dst_rate)
        assert pcm is not None
        assert pcm.shape == (2, dst_rate)

    def test_96000_4ch_pulse_position_and_isolation(self, tmp_path) -> None:
        """>2ch 跨率：脉冲落点精确、静音声道零串漏。"""
        rate = 96000
        n = rate
        pcm = np.zeros((4, n), dtype=np.float32)
        pcm[0, rate // 2] = 0.9  # CH0（前左）单样本脉冲
        wav = _write_wav(tmp_path / "p96.wav", pcm, rate)
        out = decode_audio_range(str(wav), 0, 48000, 48000)
        assert out is not None and out.shape[1] == 48000
        peak_pos = int(np.argmax(np.abs(out[0])))
        assert peak_pos == pytest.approx(24000, abs=2)
        # CH1..3 静音 → 折混右声道只含 BS.775 的 Ls/Rs…（4ch 布局
        # L R Ls Rs）——右声道来自 CH1（静音）与 CH3（静音）= 零。
        assert float(np.abs(out[1]).max()) == pytest.approx(0.0, abs=1e-5)

    def test_channel_map_selection_overrides_fold(self, tmp_path) -> None:
        """选道先于降混：选 CH1（信号声道）→ 两侧都是该声道。"""
        wav = _write_wav(tmp_path / "mc441b.wav", _four_ch_44100(), 44100)
        pcm = decode_audio_range(
            str(wav), 0, 48000, 48000, channel_map=((1, 0), (1, 1)))
        assert pcm is not None
        assert pcm.shape == (2, 48000)
        np.testing.assert_allclose(pcm[0], pcm[1], atol=1e-6)
        assert float(np.abs(pcm).max()) == pytest.approx(0.25, abs=0.01)


class TestSpecChannelMap:
    def _spec(self, **overrides) -> ClipAudioRenderSpec:
        base = dict(
            project_id="p", timeline_id="t", clip_id="c", revision=1,
            asset_id="a", source_path="/x.wav",
            source_fingerprint="fp", sample_rate=48000, channel_count=4,
            channel_layout="4.0", start_sample=0, end_sample=48000,
            reverse=False,
        )
        base.update(overrides)
        return ClipAudioRenderSpec(**base)

    def test_cache_key_includes_channel_map(self) -> None:
        plain = self._spec().cache_key()
        mapped = self._spec(source_channel_map=((1, 0), (1, 1))).cache_key()
        assert plain != mapped

    def test_resolve_binding_priority_over_patch(self) -> None:
        from src.features.edit.core.audio_sync import AudioSyncBinding
        from src.features.edit.core.source_patch import SourcePatchSpec

        binding = AudioSyncBinding(
            wav_asset_id="w", offset_seconds=0.0,
            audio_channel_map=((3, 0), (3, 1)))
        patch = SourcePatchSpec(
            audio_enabled=True, audio_channel_map=((1, 0), (1, 1)))
        project = SimpleNamespace(
            audio_sync_bindings={"a": binding},
            source_patch_overrides={"a": patch},
        )
        asset = SimpleNamespace(asset_id="a")
        assert resolve_source_channel_map(project, asset) == ((3, 0), (3, 1))

    def test_resolve_patch_without_binding(self) -> None:
        from src.features.edit.core.source_patch import SourcePatchSpec

        patch = SourcePatchSpec(
            audio_enabled=True, audio_channel_map=((2, 0), (2, 1)))
        project = SimpleNamespace(
            audio_sync_bindings={}, source_patch_overrides={"a": patch})
        asset = SimpleNamespace(asset_id="a")
        assert resolve_source_channel_map(project, asset) == ((2, 0), (2, 1))

    def test_resolve_disabled_patch_is_passthrough(self) -> None:
        from src.features.edit.core.source_patch import SourcePatchSpec

        patch = SourcePatchSpec(
            audio_enabled=False, audio_channel_map=((2, 0), (2, 1)))
        project = SimpleNamespace(
            audio_sync_bindings={}, source_patch_overrides={"a": patch})
        asset = SimpleNamespace(asset_id="a")
        assert resolve_source_channel_map(project, asset) == ()

    def test_worker_consumes_spec_channel_map(self, qtbot, tmp_path) -> None:
        """片段取样链路（Provider/worker）用 spec 选道解码（选道生效）。"""
        from src.features.edit.core.clip_pcm import ClipPcmProvider

        ClipPcmProvider._instance = None
        wav = _write_wav(tmp_path / "mc.wav", _four_ch_44100(), 44100)
        spec = self._spec(
            source_path=str(wav), source_fingerprint="fp2",
            start_sample=0, end_sample=48000,
            source_channel_map=((2, 0), (2, 1)))
        provider = ClipPcmProvider.instance()
        results: list = []
        try:
            assert provider.request(spec, results.append) is False
            qtbot.waitUntil(lambda: bool(results), timeout=10000)
            pcm = results[0].pcm
            assert pcm.shape == (2, 48000)
            # 选中 CH2（静音）→ 输出为零（worker 真正传了 channel_map）。
            assert float(np.abs(pcm).max()) == pytest.approx(0.0, abs=1e-6)
        finally:
            provider.shutdown()
            ClipPcmProvider._instance = None
