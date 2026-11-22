"""P1 · Convolution Reverb（2026-11-19 计划第三阶段 #6）。

验收：单位脉冲 IR 逐样本复现 IR；交叉路径（true stereo 四路径）正确；
延迟/尾长精确报告；块一致；IR 装载重采样/裁切/规范化。
"""
from __future__ import annotations

import os
import sys
import wave
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects import convolution_reverb
from src.features.edit.core.audio_effects.convolution_reverb import (
    CONV_BLOCK,
    ConvolutionReverbEffect,
    IrResource,
    PartitionedConvolver,
    load_ir,
)

RATE = 48000


def _impulse_response(n: int = 4096, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    decay = np.exp(-np.arange(n) / (n / 6.0))
    return (decay * rng.standard_normal(n)).astype(np.float32).reshape(1, -1)


class TestPartitionedConvolver:
    def test_unit_impulse_reproduces_ir(self) -> None:
        """x = 单位脉冲 → 输出逐样本复现 IR（卷积正确性锚点）。"""
        ir = _impulse_response()
        conv = PartitionedConvolver(ir, block=1024)
        x = np.zeros((1, 4096), dtype=np.float32)
        x[0, 0] = 1.0
        out = conv.process(x)
        np.testing.assert_allclose(
            out[0, :ir.shape[-1]], ir[0], atol=1e-4)

    def test_block_consistency(self) -> None:
        ir = _impulse_response()
        x = np.random.default_rng(7).standard_normal(
            (1, CONV_BLOCK * 3)).astype(np.float32) * 0.2
        whole = PartitionedConvolver(ir, block=CONV_BLOCK * 3).process(x)
        stream = PartitionedConvolver(ir, block=777)
        pieces = []
        for start in range(0, x.shape[-1], 777):
            pieces.append(stream.process(x[:, start:start + 777]))
        stitched = np.concatenate(pieces, axis=1)
        # 稳态段一致（不同块长=不同 FFT 尺寸——float32 舍入容差）。
        np.testing.assert_allclose(
            whole[:, 1000:-1000], stitched[:, 1000:-1000], atol=5e-3)

    def test_latency_matches_measured_onset(self) -> None:
        """修复合同：申报延迟 == 实测 onset（2026-11-21 计划 FX-11）。"""
        for ir_len, block in ((2048, 1024), (4096, 2048), (3000, 777)):
            conv = PartitionedConvolver(_impulse_response(ir_len), block=block)
            pulse = np.zeros((1, 512), dtype=np.float32)
            pulse[0, 0] = 1.0
            response = conv.process(pulse)[0]
            onset = int(np.flatnonzero(np.abs(response) > 1e-6)[0])
            assert onset == conv.latency == 0

    def test_small_kernel_exact_vs_direct(self) -> None:
        """矩阵目标精度：短 IR 与直接卷积逐样本一致（≤1.5e-6）。"""
        kernel = np.array([[1.0, 0.25, -0.5]], dtype=np.float32)
        conv = PartitionedConvolver(kernel, block=256)
        rng = np.random.default_rng(9)
        x = (rng.standard_normal((2, 1000)) * 0.3).astype(np.float32)
        out = conv.process(x)
        for ch in range(2):
            ref = np.convolve(x[ch].astype(np.float64),
                              kernel[0].astype(np.float64))[:1000]
            np.testing.assert_allclose(out[ch], ref, rtol=0, atol=1.5e-6)

    def test_single_sample_stereo_ir(self) -> None:
        """长度仅 1 样本的双声道 IR：逐声道增益直通。"""
        conv = PartitionedConvolver(
            np.array([[0.5], [0.25]], dtype=np.float32), block=256)
        x = np.array([[1.0, -2.0], [4.0, 0.5]], dtype=np.float32)
        np.testing.assert_allclose(
            conv.process(x), x * np.array([[0.5], [0.25]]), atol=1e-7)


class TestEffectMatrix:
    def test_mono_ir_applies_to_stereo(self) -> None:
        ir = _impulse_response(2048)
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0})
        fx.set_ir(ir)
        x = np.zeros((2, 1024), dtype=np.float32)
        x[:, 0] = 1.0
        out = fx.process(x)
        # 两声道同一卷积结果（mono→stereo 广播）。
        np.testing.assert_allclose(out[0], out[1], atol=1e-6)

    def test_true_stereo_cross_paths(self) -> None:
        """四路径：LL/LR/RL/RR 各自的脉冲落在对应位置。"""
        n = 512
        ones = np.ones((1, 1), dtype=np.float32)
        ll = np.roll(np.eye(1, n, 0, dtype=np.float32).reshape(1, -1), 10)
        lr = np.roll(np.eye(1, n, 0, dtype=np.float32).reshape(1, -1), 20)
        rl = np.roll(np.eye(1, n, 0, dtype=np.float32).reshape(1, -1), 30)
        rr = np.roll(np.eye(1, n, 0, dtype=np.float32).reshape(1, -1), 40)
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0})
        fx.set_ir(ones, true_stereo=True,
                  matrix={"LL": ll, "LR": lr, "RL": rl, "RR": rr})
        # 左声道单位脉冲 → L 输出 = LL@10；R 输出 = RL@30。
        x = np.zeros((2, 256), dtype=np.float32)
        x[0, 0] = 1.0
        out = fx.process(x)
        assert float(np.max(out[0])) == pytest.approx(1.0, abs=1e-3)
        assert int(np.argmax(np.abs(out[0]))) == 10
        assert int(np.argmax(np.abs(out[1]))) == 30
        # 右声道脉冲 → L = LR@20；R = RR@40。
        x2 = np.zeros((2, 256), dtype=np.float32)
        x2[1, 0] = 1.0
        fx.reset_state()
        out2 = fx.process(x2)
        assert int(np.argmax(np.abs(out2[0]))) == 20
        assert int(np.argmax(np.abs(out2[1]))) == 40

    def test_tail_and_latency_reported(self) -> None:
        fx = ConvolutionReverbEffect(RATE)
        fx.set_ir(_impulse_response(3000))
        assert fx.tail_estimate_samples() >= 3000
        assert fx.latency_samples() >= 0

    def test_bypass_passthrough(self) -> None:
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"enabled": False})
        x = np.random.default_rng(1).standard_normal(
            (2, 512)).astype(np.float32)
        np.testing.assert_array_equal(fx.process(x.copy()), x)


class TestIrLoading:
    def _write_wav(self, path: Path, pcm: np.ndarray, rate: int) -> None:
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(pcm.shape[0])
            wav.setsampwidth(2)
            wav.setframerate(rate)
            interleaved = np.clip(pcm.T, -1.0, 1.0)
            wav.writeframes(
                (interleaved * 32767.0).astype("<i2").tobytes())

    def test_load_resample_trim_normalize(self, tmp_path) -> None:
        # 24 kHz 的 IR（头部 0.1s 静音）→ 装载到 48 kHz。
        src_rate = 24000
        rng = np.random.default_rng(2)
        n = 2400  # 0.1 s @24k
        body = (0.5 * np.exp(-np.arange(n) / 400.0)
                * rng.standard_normal(n)).astype(np.float32)
        raw = np.concatenate([np.zeros(2400, dtype=np.float32), body])
        pcm = raw.reshape(1, -1)
        wav = tmp_path / "ir.wav"
        self._write_wav(wav, pcm, src_rate)

        ir, resource = load_ir(str(wav), RATE)
        assert resource.sample_rate == RATE
        assert resource.channels == 1
        assert len(resource.sha256) == 64
        # 静音头被裁切（长度 < 重采样后的全长）。
        assert ir.shape[-1] < int(0.2 * RATE)
        # 峰值规范化到 −6 dB。
        peak = float(np.max(np.abs(ir)))
        assert peak == pytest.approx(10.0 ** (-6.0 / 20.0), rel=0.05)

    def test_resource_round_trip(self) -> None:
        res = IrResource(uri="/a/ir.wav", sha256="ab" * 32,
                         sample_rate=48000, channels=2, samples=4800,
                         label="Room A")
        restored = IrResource.from_dict(res.to_dict())
        assert restored == res


class TestWavMatrixMapping:
    """四路 WAV 显式 LL/LR/RL/RR（第一字母=输出）；两路对角退化策略。"""

    def _write_wav(self, path: Path, pcm: np.ndarray, rate: int) -> None:
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(pcm.shape[0])
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(
                (np.clip(pcm.T, -1.0, 1.0) * 32000.0).astype("<i2").tobytes())

    def _delta_channel(self, taps: dict[int, float], n: int = 64) -> np.ndarray:
        out = np.zeros((1, n), dtype=np.float32)
        for pos, value in taps.items():
            out[0, pos] = value
        return out

    def test_four_channel_wav_explicit_matrix(self, tmp_path) -> None:
        n = 64
        ir = np.concatenate([
            self._delta_channel({10: 0.9}, n),   # LL：L入→L出 @10
            self._delta_channel({20: 0.9}, n),   # LR：R入→L出 @20
            self._delta_channel({30: 0.9}, n),   # RL：L入→R出 @30
            self._delta_channel({40: 0.9}, n),   # RR：R入→R出 @40
        ], axis=0)
        wav = tmp_path / "true_stereo_4ch.wav"
        self._write_wav(wav, ir, RATE)
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0, "true_stereo": True,
                       "ir_path": str(wav)})
        assert fx.is_active
        # 峰值规范化到 −6 dB（load_ir 合同），四路等幅。
        peak = pytest.approx(10.0 ** (-6.0 / 20.0), abs=0.02)
        # 左入脉冲 → L=LL@10，R=RL@30（交叉路径不串）。
        x = np.zeros((2, 256), dtype=np.float32)
        x[0, 0] = 1.0
        out = fx.process(x)
        assert int(np.argmax(np.abs(out[0]))) == 10
        assert float(np.max(out[0])) == peak
        assert int(np.argmax(np.abs(out[1]))) == 30
        # 右入脉冲 → L=LR@20，R=RR@40。
        fx.reset_state()
        x2 = np.zeros((2, 256), dtype=np.float32)
        x2[1, 0] = 1.0
        out2 = fx.process(x2)
        assert int(np.argmax(np.abs(out2[0]))) == 20
        assert int(np.argmax(np.abs(out2[1]))) == 40

    def test_two_channel_wav_diagonal_fallback(self, tmp_path) -> None:
        n = 64
        ir = np.concatenate([
            self._delta_channel({10: 0.9}, n),
            self._delta_channel({40: 0.6}, n),
        ], axis=0)
        wav = tmp_path / "true_stereo_2ch.wav"
        self._write_wav(wav, ir, RATE)
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0, "true_stereo": True,
                       "ir_path": str(wav)})
        # 对角策略：L←ch0、R←ch1，交叉路径缺数据（不串）。
        x = np.zeros((2, 256), dtype=np.float32)
        x[0, 0] = 1.0
        out = fx.process(x)
        assert int(np.argmax(np.abs(out[0]))) == 10
        assert float(np.max(np.abs(out[1]))) < 1e-6


class TestLoadLifecycle:
    """清空/失败停用旧 kernel；指纹键驱动重载；失败键不逐块重试。"""

    def _write_wav(self, path: Path, pcm: np.ndarray, rate: int) -> None:
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(pcm.shape[0])
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(
                (np.clip(pcm.T, -1.0, 1.0) * 32000.0).astype("<i2").tobytes())

    def _delta(self, pos: int, n: int = 64) -> np.ndarray:
        out = np.zeros((1, n), dtype=np.float32)
        out[0, pos] = 1.0
        return out

    def test_clear_ir_path_unloads_kernel(self, tmp_path) -> None:
        wav = tmp_path / "ir.wav"
        self._write_wav(wav, self._delta(10), RATE)
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0, "ir_path": str(wav)})
        assert fx.is_active and fx.tail_estimate_samples() > 0
        fx.set_params({"ir_path": ""})
        assert not fx.is_active
        assert fx.tail_estimate_samples() == 0
        x = np.zeros((2, 128), dtype=np.float32)
        x[:, 0] = 1.0
        np.testing.assert_array_equal(fx.process(x.copy()), x)

    def test_corrupt_ir_unloads_and_does_not_retry(self, tmp_path) -> None:
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0})
        fx.set_ir(self._delta(10))
        assert fx.is_active
        bad = tmp_path / "broken.wav"
        bad.write_bytes(b"not a wav file")
        calls = []
        original = convolution_reverb.load_ir

        def counting(path, rate, **kwargs):
            calls.append(path)
            return original(path, rate, **kwargs)

        with mock.patch.object(convolution_reverb, "load_ir", counting):
            fx.set_params({"ir_path": str(bad)})
            assert not fx.is_active  # 确认失败：旧 kernel 停用、尾音归零
            assert fx.tail_estimate_samples() == 0
            for _ in range(5):  # 逐块 set_params 不应反复读盘
                fx.set_params({"ir_path": str(bad)})
            assert len(calls) == 1

    def test_same_path_replacement_reloads(self, tmp_path) -> None:
        wav = tmp_path / "ir.wav"
        self._write_wav(wav, self._delta(10), RATE)
        fx = ConvolutionReverbEffect(RATE)
        fx.set_params({"wet": 1.0, "dry": 0.0, "ir_path": str(wav)})
        x = np.zeros((2, 128), dtype=np.float32)
        x[0, 0] = 1.0
        out_a = fx.process(x.copy())
        assert int(np.argmax(np.abs(out_a[0]))) == 10
        # 同路径写入不同内容（指纹变化）→ 重载新 kernel。
        self._write_wav(wav, self._delta(50), RATE)
        os.utime(wav, (0, 0))  # 确保 mtime 变化（写内容已不同）
        fx.reset_state()
        fx.set_params({"ir_path": str(wav)})
        out_b = fx.process(x.copy())
        assert int(np.argmax(np.abs(out_b[0]))) == 50
