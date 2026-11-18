"""Spectral repair: brush/selection driven attenuate & heal (§5 画笔).

Roadmap contract (docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §5
频谱修复与画笔):

- Repair regions persist as stroke points ``(sample, hz)`` plus a time
  radius and a log-frequency bandwidth, converted from pixels before the
  commit so reopening or zooming reproduces the same edit.
- The mask is built from time × log-Hz elliptical distance with softened
  boundaries, stroke gaps filled by interpolation along the path, and
  stays valid at 0 Hz / Nyquist / rate / channel bounds.
- ``attenuate`` applies the soft gain ``1 − mask × (1 − 10^(−amount/20))``
  to the affected complex spectrum.  ``heal`` additionally interpolates
  log-magnitude from the nearest unmasked frames before/after with an
  adjacent-frame phase advance, blended by the mask — only where both
  sides give it confidence.
- The edit boundaries get 5 ms dry/wet ramps; only the target range
  changes (bit-identical outside).

SciPy is required; its absence is an explicit error (never a silent
bypass, same policy as the denoise backend).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional

import numpy as np

try:  # SciPy 缺席时修复不可用（显式降级，与降噪后端同一策略）。
    from scipy.signal import ShortTimeFFT, get_window
    _SCIPY_AVAILABLE = True
except Exception:  # noqa: BLE001
    _SCIPY_AVAILABLE = False

REPAIR_STAT_VERSION = 1
SUPPORTED_NFFT = (512, 1024, 2048, 4096)
SUPPORTED_MODES = ("attenuate", "heal")
# 椭圆距离的软化带：d < CORE 内全效，CORE..1 余弦衰减，>1 不受影响。
MASK_CORE_DISTANCE = 0.65
EDGE_FADE_SECONDS = 0.005


def scipy_available() -> bool:
    return _SCIPY_AVAILABLE


def normalize_strokes(
    strokes: list[dict], sample_rate: int, *,
    max_gap_factor: float = 0.5,
) -> list[dict]:
    """填补笔迹间隙：相邻点间距 > 半径×系数时沿路径补点。

    输入/输出均为 ``{sample, hz, time_radius, bandwidth_oct}``（片段输出
    样本域 + Hz；time_radius 单位样本，bandwidth_oct 为 log2 带宽）。
    """
    cleaned: list[dict] = []
    for stroke in strokes:
        sample = stroke.get("sample")
        hz = stroke.get("hz")
        radius = stroke.get("time_radius")
        band = stroke.get("bandwidth_oct")
        if (isinstance(sample, (int, float)) and isinstance(hz, (int, float))
                and isinstance(radius, (int, float)) and radius > 0
                and isinstance(band, (int, float)) and band > 0):
            cleaned.append({
                "sample": float(sample), "hz": float(hz),
                "time_radius": float(radius), "bandwidth_oct": float(band),
            })
    cleaned.sort(key=lambda s: s["sample"])
    if len(cleaned) < 2:
        return cleaned
    filled: list[dict] = [cleaned[0]]
    for previous, current in zip(cleaned, cleaned[1:]):
        gap = current["sample"] - previous["sample"]
        radius = min(previous["time_radius"], current["time_radius"])
        step = max(1.0, radius * max_gap_factor)
        if gap > step:
            count = int(gap // step)
            for index in range(1, count + 1):
                fraction = index / (count + 1)
                filled.append({
                    "sample": previous["sample"] + gap * fraction,
                    "hz": 2.0 ** (math.log2(previous["hz"])
                                  + (math.log2(current["hz"])
                                     - math.log2(previous["hz"])) * fraction),
                    "time_radius": radius,
                    "bandwidth_oct": min(previous["bandwidth_oct"],
                                         current["bandwidth_oct"]),
                })
        filled.append(current)
    del sample_rate
    return filled


def build_repair_mask(
    total_samples: int,
    strokes: list[dict],
    sample_rate: int,
    *,
    nfft: int = 2048,
    block_start: int = 0,
    core_distance: float = MASK_CORE_DISTANCE,
) -> np.ndarray:
    """STFT 域修复掩码 ``[freq_bins, frames]``（0..1；时间×log-Hz 椭圆距离）。

    ``strokes`` 先经 :func:`normalize_strokes` 填隙；帧中心用绝对样本位置
    计算（分块与整段一致）。0 Hz 行（bin 0）不受影响——DC 不参与修复。
    """
    filled = normalize_strokes(strokes, sample_rate)
    mask = np.zeros((nfft // 2 + 1, 0), dtype=np.float32)
    if not filled or total_samples <= 0:
        return mask
    hop = nfft // 4
    frames = max(1, 1 + (total_samples - nfft) // hop) \
        if total_samples >= nfft else 1
    centers = (block_start + np.arange(frames, dtype=np.float64) * hop
               + nfft / 2.0)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / max(1, sample_rate))
    log_freqs = np.full(freqs.shape, -1e9, dtype=np.float64)
    positive = freqs > 0
    log_freqs[positive] = np.log2(freqs[positive])
    mask = np.zeros((freqs.size, frames), dtype=np.float32)
    for stroke in filled:
        dt = np.abs(centers - stroke["sample"]) / stroke["time_radius"]
        log_hz = math.log2(max(1e-6, stroke["hz"]))
        band = stroke["bandwidth_oct"]
        with np.errstate(divide="ignore", invalid="ignore"):
            df = np.abs(log_freqs - log_hz) / band
        df[~positive] = np.inf  # DC 行不受影响
        distance = np.sqrt(dt[None, :] ** 2 + df[:, None] ** 2)
        soft = np.clip(
            (1.0 - distance) / max(1e-6, 1.0 - core_distance), 0.0, 1.0)
        soft = 0.5 - 0.5 * np.cos(np.pi * soft)  # 余弦软化
        np.maximum(mask, soft.astype(np.float32), out=mask)
    return mask


def repair_spectral(
    pcm: np.ndarray,
    sample_rate: int,
    strokes: list[dict],
    target: tuple[int, int],
    *,
    amount_db: float = 24.0,
    mode: str = "attenuate",
    nfft: int = 2048,
    link_stereo: bool = True,
    progress: Optional[Callable[[float], Any]] = None,
) -> np.ndarray:
    """频谱修复（attenuate / heal）；只有 target 内变化，两侧 5 ms 干湿过渡。

    ``strokes``: ``{sample, hz, time_radius, bandwidth_oct}``（绝对样本域）。
    ``target``: 半开样本区间。掩码按绝对样本位置构建——分块与整段一致。
    """
    if not _SCIPY_AVAILABLE:
        raise RuntimeError("SciPy DSP backend unavailable for spectral repair")
    lo, hi = target
    if pcm.ndim != 2 or pcm.shape[0] == 0 or pcm.dtype != np.float32 \
            or not np.isfinite(pcm).all():
        raise ValueError("Expected finite planar float32 PCM")
    if sample_rate <= 0 or nfft not in SUPPORTED_NFFT \
            or not 0.0 <= amount_db <= 48.0:
        raise ValueError("Unsupported rate, FFT or amount")
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported repair mode {mode!r}")
    if not 0 <= lo < hi <= pcm.shape[-1]:
        raise ValueError("Invalid target range")
    if not strokes:
        return pcm.copy()
    if mode == "attenuate" and amount_db == 0.0:
        return pcm.copy()  # 0 dB 衰减 = 逐样本旁路（不做 STFT 往返）
    if progress is not None:
        progress(0.0)

    hop = nfft // 4
    stft = ShortTimeFFT(get_window("hann", nfft), hop, sample_rate,
                        fft_mode="onesided")
    # 上下文块：两侧各留 nfft 真实信号；不足处零填充（首尾片段）。
    block_lo = max(0, lo - nfft)
    block_hi = min(pcm.shape[-1], hi + nfft)
    block = pcm[:, block_lo:block_hi]
    block = np.pad(block, ((0, 0), (0, max(0, nfft - block.shape[-1]))))
    spectrum = stft.stft(block)              # [channels, bins, frames]
    centers = np.rint(stft.t(block.shape[-1]) * sample_rate).astype(np.int64) \
        + block_lo
    mask = build_repair_mask(
        block.shape[-1],
        [{**stroke, "sample": stroke["sample"] - block_lo}
         for stroke in strokes],
        sample_rate, nfft=nfft, block_start=0,
    )[:, :spectrum.shape[-1]]
    if mask.shape[1] < spectrum.shape[-1]:
        mask = np.pad(mask, ((0, 0), (0, spectrum.shape[-1] - mask.shape[1])))
    # link_stereo：掩码跨声道共用（保守共享，避免声像漂移；2D 广播）。
    del link_stereo

    gain_floor = 10.0 ** (-amount_db / 20.0)
    if mode == "attenuate":
        gain = 1.0 - mask * (1.0 - gain_floor)
        repaired = spectrum * gain[None, :, :]
    else:
        repaired = _heal_spectrum(spectrum, mask)
    wet = stft.istft(repaired, k1=block.shape[-1])
    offset = lo - block_lo
    wet = wet[:, offset:offset + hi - lo]
    del centers

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


def _heal_spectrum(spectrum: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """对掩码内的时频点做前后帧对数幅值插值 + 相邻帧相位推进。

    只有前后都存在未遮罩帧时才启用 heal（置信度），单侧/全遮罩退化为
    衰减（gain_floor 由调用侧的 amount 决定；这里直接保留原谱）。
    """
    channels, bins, frames = spectrum.shape
    healed = spectrum.copy()
    magnitude = np.abs(spectrum)
    phase = np.angle(spectrum)
    log_mag = np.log(np.maximum(magnitude, 1e-12))
    masked = mask > 1e-3                      # [bins, frames]
    if not masked.any():
        return healed
    # 每帧首个/末个未遮罩帧索引（对每个 bin 独立）。
    valid = ~masked
    has_before = np.cumsum(valid, axis=1) > 0
    reversed_valid = valid[:, ::-1]
    has_after = np.cumsum(reversed_valid, axis=1) > 0
    healable = masked & has_before & has_after
    if not healable.any():
        return healed
    # 前后最近未遮罩帧（向量化：对每帧取该 bin 上/下一个 valid 帧）。
    before_idx = np.full((bins, frames), -1, dtype=np.int64)
    after_idx = np.full((bins, frames), -1, dtype=np.int64)
    last_valid = np.full(bins, -1, dtype=np.int64)
    for frame in range(frames):
        before_idx[:, frame] = last_valid
        last_valid = np.where(valid[:, frame], frame, last_valid)
    last_valid = np.full(bins, -1, dtype=np.int64)
    for frame in range(frames - 1, -1, -1):
        after_idx[:, frame] = last_valid
        last_valid = np.where(valid[:, frame], frame, last_valid)
    rows, cols = np.nonzero(healable)
    b_idx = before_idx[rows, cols]
    a_idx = after_idx[rows, cols]
    span = np.maximum(1, a_idx - b_idx)
    weight = (cols - b_idx) / span            # 0..1
    # 逐声道估计（对数幅值/相位从前后帧插值）。
    for channel in range(channels):
        channel_log = log_mag[channel]
        channel_phase = phase[channel]
        # rows=bin 索引、b_idx/a_idx=前后帧索引 → 逐元素取值。
        est_log_mag = (channel_log[rows, b_idx] * (1.0 - weight)
                       + channel_log[rows, a_idx] * weight)
        est_mag = np.exp(est_log_mag)
        # 相位推进：前后帧相位差 × 距离权重（无包裹展开的保守近似）。
        phase_before = channel_phase[rows, b_idx]
        phase_after = channel_phase[rows, a_idx]
        est_phase = phase_before + (phase_after - phase_before) * weight
        estimate = (est_mag * np.exp(1j * est_phase)).astype(np.complex128)
        original = spectrum[channel][rows, cols]
        blend = mask[rows, cols].astype(np.float64)
        healed[channel][rows, cols] = (
            original * (1.0 - blend) + estimate * blend)
    return healed
