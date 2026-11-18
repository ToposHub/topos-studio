"""P1 · Convolution Reverb（2026-11-19 计划第三阶段 #6）。

验收：单位脉冲 IR 逐样本复现 IR；交叉路径（true stereo 四路径）正确；
延迟/尾长精确报告；块一致；IR 装载重采样/裁切/规范化。
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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

    def test_latency_reported(self) -> None:
        conv = PartitionedConvolver(_impulse_response(2048), block=1024)
        # fft = next_pow2(1024 + 2047) = 4096 → latency = 4096 − 1024。
        assert conv.latency == 4096 - 1024


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
