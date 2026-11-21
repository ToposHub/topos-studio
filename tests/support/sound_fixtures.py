"""B00 参考声音基线 fixture：确定性生成器、参考工程与三端对拍工具。

主计划 §6（docs/plans/2026-11-22-professional-film-mixing-plan.md）：

1. 固定 seed 生成可公开 WAV 信号：one-hot 脉冲、正弦、工频嗡声、粉噪/
   带限噪声、短点击、立体声反相、对白替身包络。不含任何私有素材。
2. 参考工程构建器（DX/ADR/Foley/FX/MX + DX Bus/FX Bus/Return Bus/Master，
   pre/post Send、侧链、自动化、延迟效果、长尾效果、非零源入点、采样淡化）。
3. 统一输出对拍工具：先对齐布局/长度/起点，再计算 max error / RMS / null。

坐标域约定（主计划 §5.3）：
- 样本一律 48 kHz 参考域绝对 int 索引；帧坐标用有理 fps（Fraction）。
- 帧边界用绝对边界式（相邻帧共享同一边界，不逐帧 round 累计）：

    start = frame * rate * fps_den // fps_num
    end   = (frame + 1) * rate * fps_den // fps_num

- 线性倍率与 dB 分开：0.5 线性 ≈ -6.0206 dB，不写 -0.5 dB。
"""
from __future__ import annotations

import struct
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

REFERENCE_RATE = 48000
REFERENCE_FPS = Fraction(24, 1)

# 发光频率选互不为谐波的素数频率，频谱身份可分离（B10/B14 fixture 同口径）。
TONE_DX_HZ = 997.0
TONE_MX_HZ = 1511.0
TONE_FX_HZ = 2017.0


# ─────────────────────────────────────────────────────────────────────────────
# 1. 确定性波形生成器（固定 seed，不依赖外部素材）
# ─────────────────────────────────────────────────────────────────────────────

def one_hot_pulse(channels: int, length: int, position: int,
                  amplitude: float = 1.0) -> np.ndarray:
    """每声道同一位置的单样本脉冲（布局身份探针）。"""
    if not 0 <= position < length:
        raise ValueError("pulse position out of range")
    pcm = np.zeros((channels, length), dtype=np.float32)
    pcm[:, position] = np.float32(amplitude)
    return pcm


def sine_tone(rate: int, seconds: float, freq: float = TONE_DX_HZ,
              amplitude: float = 0.5, channels: int = 2,
              phase: float = 0.0) -> np.ndarray:
    """连续相位正弦（跨块续接由外部 pos 偏移控制）。"""
    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64) / float(rate)
    wave_form = np.sin(2.0 * np.pi * float(freq) * t + float(phase))
    return (wave_form * float(amplitude)).astype(np.float32)[np.newaxis, :].repeat(
        channels, axis=0)


def mains_hum(rate: int, seconds: float, freq: float = 50.0,
              amplitude: float = 0.2, channels: int = 2) -> np.ndarray:
    """工频嗡声：基频 + 3/5 次谐波（DeHum 反例信号）。"""
    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64) / float(rate)
    signal = (np.sin(2.0 * np.pi * freq * t)
              + 0.3 * np.sin(2.0 * np.pi * 3.0 * freq * t)
              + 0.15 * np.sin(2.0 * np.pi * 5.0 * freq * t))
    return (signal * float(amplitude) / 1.45).astype(np.float32)[np.newaxis, :].repeat(
        channels, axis=0)


def pink_noise(rate: int, seconds: float, seed: int = 20261122,
               amplitude: float = 0.2, channels: int = 2) -> np.ndarray:
    """粉噪（FFT 域 1/f 谱整形；固定 seed）。"""
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    out = np.empty((channels, n), dtype=np.float32)
    for ch in range(channels):
        white = rng.standard_normal(n)
        spec = np.fft.rfft(white)
        freqs = np.fft.rfftfreq(n, d=1.0 / rate)
        shape = np.ones_like(freqs)
        nz = freqs > 0.0
        shape[nz] = 1.0 / np.sqrt(freqs[nz])
        shaped = np.fft.irfft(spec * shape, n=n)
        peak = float(np.max(np.abs(shaped))) or 1.0
        out[ch] = (shaped / peak * amplitude).astype(np.float32)
    return out


def bandlimited_noise(rate: int, seconds: float, lo_hz: float, hi_hz: float,
                      seed: int = 20261123, amplitude: float = 0.2,
                      channels: int = 2) -> np.ndarray:
    """带限噪声（带通能量探针，评价 EQ/滤波过渡带）。"""
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    out = np.empty((channels, n), dtype=np.float32)
    for ch in range(channels):
        white = rng.standard_normal(n)
        spec = np.fft.rfft(white)
        freqs = np.fft.rfftfreq(n, d=1.0 / rate)
        mask = ((freqs >= lo_hz) & (freqs <= hi_hz)).astype(np.float64)
        shaped = np.fft.irfft(spec * mask, n=n)
        peak = float(np.max(np.abs(shaped))) or 1.0
        out[ch] = (shaped / peak * amplitude).astype(np.float32)
    return out


def short_click(rate: int, seconds: float, position: int, width: int = 7,
                amplitude: float = 0.8, channels: int = 2,
                seed: int = 20261124) -> np.ndarray:
    """短点击（DeClick 探针：位置固定，波形为固定 seed 窄爆点）。"""
    rng = np.random.default_rng(seed)
    pcm = np.zeros((channels, int(rate * seconds)), dtype=np.float32)
    burst = rng.standard_normal(width).astype(np.float32)
    burst *= np.linspace(1.0, 0.0, width, dtype=np.float32)
    burst /= max(float(np.max(np.abs(burst))), 1e-9)
    pcm[:, position:position + width] = burst[None, :] * amplitude
    return pcm


def stereo_inverted(rate: int, seconds: float,
                    freq: float = TONE_MX_HZ, amplitude: float = 0.4) -> np.ndarray:
    """立体声反相对（L=+s, R=-s；M/S 与 mono 兼容性探针）。"""
    mono = sine_tone(rate, seconds, freq, amplitude, channels=1)
    return np.concatenate([mono, -mono], axis=0)


def dialogue_envelope(rate: int, seconds: float, seed: int = 20261125,
                      amplitude: float = 0.35, channels: int = 2) -> np.ndarray:
    """对白替身：音节包络 × 共振峰结构（确定性；非真人素材）。"""
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64) / float(rate)
    # 音节率约 4 Hz 的伪随机通断包络
    syllable = 0.5 + 0.5 * np.sign(
        np.sin(2.0 * np.pi * 4.0 * t + rng.uniform(0.0, 2.0 * np.pi)))
    formants = (np.sin(2.0 * np.pi * 220.0 * t)
                + 0.6 * np.sin(2.0 * np.pi * 1100.0 * t)
                + 0.3 * np.sin(2.0 * np.pi * 2600.0 * t))
    signal = syllable * formants / 1.9
    return (signal * amplitude).astype(np.float32)[np.newaxis, :].repeat(
        channels, axis=0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 有理帧时钟（§6 绝对边界式）
# ─────────────────────────────────────────────────────────────────────────────

def frame_sample_bounds(frame: int, rate: int, fps: Fraction) -> tuple[int, int]:
    """帧 → 绝对样本边界（相邻帧共享边界；禁止逐帧 round 后累计）。"""
    fps = Fraction(fps)
    start = frame * rate * fps.denominator // fps.numerator
    end = (frame + 1) * rate * fps.denominator // fps.numerator
    return start, end


# ─────────────────────────────────────────────────────────────────────────────
# 3. WAV 读写（stdlib；float32 与 24-bit PCM，供 B04 真实文件回读）
# ─────────────────────────────────────────────────────────────────────────────

def write_wav(path: Path, pcm: np.ndarray, rate: int,
              bit_depth: int = 24) -> Path:
    """写交错 WAV。bit_depth ∈ {16, 24, 32f}；返回 path（便于链式断言）。"""
    path = Path(path)
    if bit_depth == 32:
        # float32 WAV（format tag 3）：stdlib wave 不支持，手工包头。
        data = pcm.T.astype("<f4").tobytes()
        block_align = pcm.shape[0] * 4
        fmt = struct.pack("<HHIIHH", 3, pcm.shape[0], rate,
                          rate * block_align, block_align, 32)
        with open(path, "wb") as fh:  # noqa: SIM115 — 非标准 fmt tag 3
            fh.write(b"RIFF")
            fh.write(struct.pack("<I", 36 + len(data)))
            fh.write(b"WAVE")
            fh.write(b"fmt ")
            fh.write(struct.pack("<I", 16))
            fh.write(fmt)
            fh.write(b"data")
            fh.write(struct.pack("<I", len(data)))
            fh.write(data)
        return path
    # 16/24-bit 整数路径走 stdlib wave
    if bit_depth == 24:
        clipped = np.clip(pcm.T, -1.0, 1.0)
        ints = (clipped * 8388607.0).astype(np.int64)
        raw = bytearray()
        for row in ints.tolist():
            for v in row:
                raw += int(v).to_bytes(3, "little", signed=True)
        sampwidth = 3
    elif bit_depth == 16:
        clipped = np.clip(pcm.T, -1.0, 1.0)
        raw = (clipped * 32767.0).astype("<i2").tobytes()
        sampwidth = 2
    else:
        raise ValueError("bit_depth must be 16/24/32")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(pcm.shape[0])
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(bytes(raw))
    return path


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """读 WAV → (float32 (channels, n), rate)。16/24-bit 整数与 float32 归一化。"""
    with open(path, "rb") as fh:
        header = fh.read(12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    # 手工遍历 chunk（float32 fmt tag 3 不在 stdlib wave 支持内）
    fmt_tag = channels = rate = None
    bits = 0
    data = b""
    with open(path, "rb") as fh:
        fh.read(12)
        while True:
            chunk_head = fh.read(8)
            if len(chunk_head) < 8:
                break
            cid, size = struct.unpack("<4sI", chunk_head)
            payload = fh.read(size)
            if size % 2:
                fh.read(1)
            if cid == b"fmt ":
                fmt_tag, channels, rate, _byte_rate, _align, bits = \
                    struct.unpack("<HHIIHH", payload[:16])
            elif cid == b"data":
                data = payload
    if fmt_tag == 3 and bits == 32:
        pcm = np.frombuffer(data, dtype="<f4").reshape(-1, channels).T.copy()
        return pcm.astype(np.float32), rate
    sampwidth = bits // 8
    if sampwidth == 2:
        data_arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        count = len(data) // 3
        bytes_arr = np.frombuffer(data[: count * 3], dtype=np.uint8).reshape(count, 3)
        ints = (bytes_arr[:, 0].astype(np.int32)
                | (bytes_arr[:, 1].astype(np.int32) << 8)
                | (bytes_arr[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data_arr = ints.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"unsupported sampwidth {sampwidth}")
    return data_arr.reshape(-1, channels).T.copy(), rate


# ─────────────────────────────────────────────────────────────────────────────
# 4. 输出对拍工具（先对齐再比较；不得平移候选音频掩盖 PDC 错位）
# ─────────────────────────────────────────────────────────────────────────────

def compare_pcm(candidate: np.ndarray, reference: np.ndarray,
                tol: float = 2e-6) -> dict:
    """逐样本对拍：布局/长度必须一致（不平移、不重采样）。

    返回 {max_error, rms_error, aligned, peak_candidate, peak_reference}。
    aligned=True 表示逐样本 max_error ≤ tol。
    """
    if candidate.shape != reference.shape:
        return {"max_error": float("inf"), "rms_error": float("inf"),
                "aligned": False, "shape_mismatch": True,
                "peak_candidate": 0.0, "peak_reference": 0.0}
    diff = candidate.astype(np.float64) - reference.astype(np.float64)
    max_error = float(np.max(np.abs(diff))) if diff.size else 0.0
    rms_error = float(np.sqrt(np.mean(diff * diff))) if diff.size else 0.0
    return {
        "max_error": max_error,
        "rms_error": rms_error,
        "aligned": max_error <= tol,
        "shape_mismatch": False,
        "peak_candidate": float(np.max(np.abs(candidate))) if candidate.size else 0.0,
        "peak_reference": float(np.max(np.abs(reference))) if reference.size else 0.0,
    }


def pulse_positions(pcm: np.ndarray, threshold: float = 1e-7) -> list[int]:
    """首个声道的非零样本位置列表（脉冲对齐探针）。"""
    return np.flatnonzero(np.abs(pcm[0]) > threshold).tolist()


def null_test(candidate: np.ndarray, reference: np.ndarray,
              tol: float = 2e-6) -> bool:
    """null test：两路输出相减应为零（≤ tol）。"""
    return compare_pcm(candidate, reference, tol)["aligned"]
