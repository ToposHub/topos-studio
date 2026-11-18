"""Noise print capture and soft spectral-subtraction denoise (P1-6).

Faithful adaptation of the roadmap's reference implementation
(docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §5): median noise
power per band over a pure-noise capture range, soft subtraction mask
``sqrt(max(1 − α·Pnoise/Psignal, floor²))`` smoothed over frequency and
time, input-phase reconstruction via inverse STFT, and 5 ms dry/wet edge
ramps at the edit boundaries so the surrounding audio stays untouched.

Contract highlights (verified in tests):

- The capture needs ≥ max(0.25 s, nfft + 5 hops) of real (non zero-padded)
  signal; shorter selections are rejected with a clear error.
- Only ``[lo, hi)`` changes; everything before/after is bit-identical.
- ``reduction_db == 0`` is a sample-exact bypass.
- Stereo channels may share a conservative (max) mask to avoid image
  shifts; anti-phase channels survive because masks are per-channel
  magnitudes, not sums.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

try:  # SciPy 存在时使用精确的 ShortTimeFFT；缺席则降噪不可用（显式降级）。
    from scipy.ndimage import uniform_filter1d
    from scipy.signal import ShortTimeFFT, get_window
    _SCIPY_AVAILABLE = True
except Exception:  # noqa: BLE001
    _SCIPY_AVAILABLE = False

NOISE_PROFILE_STAT_VERSION = 1
MIN_NOISE_SECONDS = 0.25
SUPPORTED_NFFT = (512, 1024, 2048, 4096)
# 与参考实现一致的过减因子与功率下限。
OVERSUBTRACTION_ALPHA = 1.25
SIGNAL_POWER_FLOOR = 1e-12
EDGE_FADE_SECONDS = 0.005


def scipy_available() -> bool:
    return _SCIPY_AVAILABLE


@dataclass(frozen=True)
class NoiseProfile:
    """一次噪声采集的可重建载荷（采样阶段不使用零填充帧估计功率）。"""

    source_signature: str          # 输入图签名（PCM 指纹）
    sample_rate: int
    noise_start: int               # 半开样本区间
    noise_end: int
    nfft: int
    hop: int
    reduction_db: float
    stat_version: int = NOISE_PROFILE_STAT_VERSION
    # 中位数噪声功率谱 [channels, bins]（重算用；诊断时展示）。
    profile_power: np.ndarray = field(repr=False, default=None)

    @property
    def is_valid(self) -> bool:
        return (
            self.profile_power is not None
            and self.noise_end - self.noise_start
            >= max(round(MIN_NOISE_SECONDS * self.sample_rate),
                   self.nfft + 5 * self.hop)
        )

    def invalidate_reason(self) -> Optional[str]:
        if not _SCIPY_AVAILABLE:
            return "SciPy DSP backend unavailable"
        if self.noise_end - self.noise_start < max(
                round(MIN_NOISE_SECONDS * self.sample_rate),
                self.nfft + 5 * self.hop):
            return (f"Select a longer section of background noise "
                    f"(≥ {MIN_NOISE_SECONDS:.2f} s)")
        if self.profile_power is None:
            return "Noise profile was never captured"
        return None


def pcm_signature(pcm: np.ndarray) -> str:
    """输入图签名：形状 + 字节哈希（调 FFT/重连/改 step 后 profile 失效）。"""
    import hashlib

    digest = hashlib.sha256()
    digest.update(str(pcm.shape).encode("utf-8"))
    digest.update(np.ascontiguousarray(pcm, dtype=np.float32).tobytes())
    return digest.hexdigest()[:24]


def _usable_profile_frames(stft: "ShortTimeFFT", length: int, nfft: int) -> np.ndarray:
    """只取完整落在采集区间内（非零填充）的帧中心。"""
    centers = np.rint(stft.t(length) * stft.fs).astype(np.int64)
    return (centers >= nfft // 2) & (centers + nfft // 2 <= length)


def capture_noise_profile(
    pcm: np.ndarray,
    sample_rate: int,
    noise_start: int,
    noise_end: int,
    *,
    nfft: int = 2048,
    reduction_db: float = 12.0,
) -> NoiseProfile:
    """在纯噪声区间上采集每频带的中位数功率（值域/区间校验与参考一致）。"""
    if not _SCIPY_AVAILABLE:
        raise RuntimeError("SciPy DSP backend unavailable")
    if pcm.ndim != 2 or pcm.shape[0] == 0 or pcm.dtype != np.float32 or not np.isfinite(pcm).all():
        raise ValueError("Expected finite planar float32 PCM")
    if sample_rate <= 0 or nfft not in SUPPORTED_NFFT or not 0.0 <= reduction_db <= 48.0:
        raise ValueError("Unsupported rate, FFT or reduction")
    if not (0 <= noise_start < noise_end <= pcm.shape[-1]):
        raise ValueError("Invalid noise range")
    hop = nfft // 4
    if noise_end - noise_start < max(round(MIN_NOISE_SECONDS * sample_rate), nfft + 5 * hop):
        raise ValueError("Select a longer section of background noise")

    stft = ShortTimeFFT(get_window("hann", nfft), hop, sample_rate, fft_mode="onesided")
    profile_spectrum = stft.stft(pcm[:, noise_start:noise_end])
    centers = np.rint(stft.t(noise_end - noise_start) * sample_rate).astype(np.int64)
    usable = (centers >= nfft // 2) & (centers + nfft // 2 <= noise_end - noise_start)
    profile_power = np.median(
        np.abs(profile_spectrum[..., usable]) ** 2, axis=-1, keepdims=True)
    return NoiseProfile(
        source_signature=pcm_signature(pcm),
        sample_rate=int(sample_rate),
        noise_start=int(noise_start),
        noise_end=int(noise_end),
        nfft=int(nfft),
        hop=int(hop),
        reduction_db=float(reduction_db),
        profile_power=profile_power,
    )


def denoise_spectral_subtraction(
    pcm: np.ndarray,
    sample_rate: int,
    noise: tuple[int, int],
    target: tuple[int, int],
    reduction_db: float = 12.0,
    nfft: int = 2048,
    link_stereo: bool = True,
    *,
    progress: Optional[Callable[[float], bool]] = None,
) -> np.ndarray:
    """软频谱减法（参考实现的直接移植；progress 返回 False 时取消）。

    ``noise``/``target`` 均为半开样本区间；只有 target 内变化，两侧加
    5 ms 干湿过渡。固定块 + 上下文（nfft 余量）+ overlap-add，按绝对
    位置累积，首尾长度不变。
    """
    if not _SCIPY_AVAILABLE:
        raise RuntimeError("SciPy DSP backend unavailable")
    a, b = noise
    lo, hi = target
    if pcm.ndim != 2 or pcm.shape[0] == 0 or pcm.dtype != np.float32 or not np.isfinite(pcm).all():
        raise ValueError("Expected finite planar float32 PCM")
    if sample_rate <= 0 or nfft not in SUPPORTED_NFFT or not 0.0 <= reduction_db <= 48.0:
        raise ValueError("Unsupported rate, FFT or reduction")
    if not (0 <= a < b <= pcm.shape[-1] and 0 <= lo < hi <= pcm.shape[-1]):
        raise ValueError("Invalid range")
    hop = nfft // 4
    if b - a < max(round(MIN_NOISE_SECONDS * sample_rate), nfft + 5 * hop):
        raise ValueError("Select a longer section of background noise")
    if reduction_db == 0.0:
        return pcm.copy()
    if progress is not None and progress(0.0) is False:
        return pcm.copy()

    stft = ShortTimeFFT(get_window("hann", nfft), hop, sample_rate, fft_mode="onesided")
    profile_spectrum = stft.stft(pcm[:, a:b])
    centers = np.rint(stft.t(b - a) * sample_rate).astype(np.int64)
    usable = (centers >= nfft // 2) & (centers + nfft // 2 <= b - a)
    profile_power = np.median(np.abs(profile_spectrum[..., usable]) ** 2,
                              axis=-1, keepdims=True)

    # 选区两侧读取真实上下文（可用时），只保留选区内的结果。
    block_lo, block_hi = max(0, lo - nfft), min(pcm.shape[-1], hi + nfft)
    block = pcm[:, block_lo:block_hi]
    block = np.pad(block, ((0, 0), (0, max(0, nfft - block.shape[-1]))))
    spectrum = stft.stft(block)
    signal_power = np.maximum(np.abs(spectrum) ** 2, SIGNAL_POWER_FLOOR)
    floor = 10.0 ** (-reduction_db / 20.0)
    mask = np.sqrt(np.clip(1.0 - OVERSUBTRACTION_ALPHA * profile_power / signal_power,
                           floor ** 2, 1.0))
    if link_stereo and pcm.shape[0] == 2:
        mask = np.max(mask, axis=0, keepdims=True)
    mask = uniform_filter1d(uniform_filter1d(mask, 3, axis=-2), 3, axis=-1)
    wet = stft.istft(spectrum * mask, k1=block.shape[-1])
    offset = lo - block_lo
    wet = wet[:, offset:offset + hi - lo]

    edge = min((hi - lo) // 2, round(EDGE_FADE_SECONDS * sample_rate))
    mix = np.ones(hi - lo, dtype=np.float32)
    if edge:
        ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        mix[:edge], mix[-edge:] = ramp, ramp[::-1]
    out = pcm.copy()
    dry = pcm[:, lo:hi]
    out[:, lo:hi] = dry + (wet - dry) * mix
    if progress is not None:
        progress(1.0)
    return out


def measure_rms(pcm: np.ndarray, start: int, end: int) -> float:
    """区间 RMS（线性幅值；测试测量独立静噪区用）。"""
    seg = pcm[:, start:end]
    if seg.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))


def estimate_tone_amplitude(pcm: np.ndarray, sample_rate: int,
                            freq_hz: float, start: int, end: int) -> float:
    """Goertzel 式单频幅度估计（测试验证 440 Hz 分量保真）。"""
    seg = pcm[:, start:end].astype(np.float64)
    if seg.size == 0:
        return 0.0
    t = np.arange(seg.shape[-1]) / sample_rate
    cosine = np.cos(2.0 * np.pi * freq_hz * t)
    sine = np.sin(2.0 * np.pi * freq_hz * t)
    real = float(np.mean(seg * cosine)) * 2.0
    imag = float(np.mean(seg * sine)) * 2.0
    return math.hypot(real, imag)
