"""P1 · De-Click / De-Hum（2026-11-19 计划第三阶段 · 效果器接入 #2）。

纯函数（预渲染栈/Clip Editor 共用）：

- ``remove_hum``：50/60 Hz 基频与所选谐波的窄 notch（butter 带阻）；
  自动模式在安静窗上用 PSD 邻域显著性判断基频（50 vs 60）与谐波
  频漂（± 容差内跟随实测峰）；
- ``detect_clicks``：高通残差 + 滑窗 MAD 检测短离群；跨声道置信度
  （多声道同时超限提高置信）与宽带瞬态 veto（保护鼓点/爆破音——
  瞬态能量分散在全带时不过检）；
- ``remove_clicks``：候选点短区间双侧三次插值修复；长缺失（> 阈值）
  转频谱修复（``audio_repair.repair_spectral``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, sosfiltfilt, welch
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False


def scipy_available() -> bool:
    return _SCIPY


# ─────────────────────────────────────────────────────────────────────────────
# De-Hum
# ─────────────────────────────────────────────────────────────────────────────

def detect_hum_base(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    quiet_quantile: float = 0.2,
    prominence_db: float = 8.0,
) -> Optional[float]:
    """自动检测哼声基频（50 vs 60）：安静窗 PSD 邻域显著性。

    返回 50.0 / 60.0 / None（无可信峰）。安静窗 = 短时能量最低分位的
    分析窗（语音活跃段不参与，避免把基频误判成哼声）。
    """
    mono = pcm.mean(axis=0) if pcm.ndim > 1 else pcm
    win = int(0.1 * sample_rate)
    if mono.shape[0] < win * 4:
        return None
    frames = mono[:len(mono) // win * win].reshape(-1, win)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    keep = int(max(1, len(energy) * quiet_quantile))
    quiet = frames[np.argsort(energy)[:keep]].reshape(-1)
    freqs, psd = welch(quiet, fs=sample_rate, nperseg=min(8192, quiet.size))
    log_psd = 10.0 * np.log10(psd + 1e-18)

    def _prominence(freq: float) -> float:
        idx = int(np.argmin(np.abs(freqs - freq)))
        lo = max(0, idx - 24)
        hi = min(len(freqs), idx + 25)
        neighborhood = np.delete(log_psd[lo:hi], idx - lo) if hi > lo + 1 \
            else np.array([log_psd[idx]])
        return float(log_psd[idx] - np.median(neighborhood))

    p50 = _prominence(50.0)
    p60 = _prominence(60.0)
    best = max(p50, p60)
    if best < prominence_db:
        return None
    return 50.0 if p50 >= p60 else 60.0


def remove_hum(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    base_hz: Optional[float] = None,
    harmonics: Sequence[int] = (1, 2, 3, 4, 5, 6, 7, 8),
    auto_detect: bool = True,
    drift_hz: float = 0.6,
    q: float = 30.0,
    reduction_db: float = 48.0,
    region: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """窄 notch 移除哼声及谐波（零相位 filtfilt——不引入相位偏移）。

    ``base_hz`` 为 None 且 auto_detect 时先检测基频（检测不到按 50）；
    ``drift_hz`` 容差内用实测 PSD 峰跟随频漂（电网 50.02/59.94 等）。
    有用频带 ≤0.5 dB 扰动由窄带高 Q 保证。

    FX-07（2026-11-21 计划）：
    - ``reduction_db``（0..48）：湿声衰减受限——完全 notch 后与原声
      按 ``(1−k)·notched + k·original`` 混合（k = 10^(−red/20)），目标
      频点保留 k 幅度、其余频点增益中性；
    - ``region=(start, end)``：短选区借**整段**上下文滤波，最后只写回
      目标区间（半开区间；选区外逐样本与输入一致）。
    """
    if not _SCIPY or pcm is None or pcm.ndim != 2 or pcm.shape[-1] == 0:
        return pcm
    mono = pcm.mean(axis=0)
    if base_hz is None:
        base_hz = detect_hum(pcm, sample_rate) if auto_detect else 50.0
    if base_hz is None:
        base_hz = 50.0
    freqs, psd = welch(mono, fs=sample_rate, nperseg=min(16384, mono.size))
    out = pcm.astype(np.float32)
    nyquist = sample_rate / 2.0
    for harmonic in harmonics:
        target = base_hz * harmonic
        if target >= nyquist * 0.95:
            break
        # 频漂跟随：容差内取实测 PSD 峰。
        mask = np.abs(freqs - target) <= drift_hz
        if mask.any():
            target = float(freqs[mask][int(np.argmax(psd[mask]))])
        bw = target / max(1.0, q)
        low = max(1.0, target - bw / 2.0)
        high = min(nyquist * 0.99, target + bw / 2.0)
        if high <= low:
            continue
        sos = butter(2, [low, high], btype="bandstop", fs=sample_rate,
                     output="sos")
        try:
            out = sosfiltfilt(sos, out, axis=-1).astype(np.float32)
        except ValueError:
            # 信号短于滤波器填充需求：跳过该谐波（诊断由调用方负责）。
            continue
    k = 10.0 ** (float(np.clip(reduction_db, 0.0, 48.0)) / -20.0)
    wet = (1.0 - k) * out + k * pcm.astype(np.float32)
    if region is not None:
        lo = int(max(0, region[0]))
        hi = int(min(pcm.shape[-1], region[1]))
        result = pcm.astype(np.float32).copy()
        result[:, lo:hi] = wet[:, lo:hi]
        return np.ascontiguousarray(result, dtype=np.float32)
    return np.ascontiguousarray(wet, dtype=np.float32)


# 语义别名（detect_hum_base 的简短名）。
detect_hum = detect_hum_base


# ─────────────────────────────────────────────────────────────────────────────
# De-Click
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClickCandidate:
    """一个检测到的点击（样本位置 + 置信度）。"""

    position: int
    width: int                 # 建议修复窗（样本，双侧）
    confidence: float          # 0..1（跨声道同时超限提高）


def detect_clicks(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    hp_hz: float = 3000.0,
    threshold_sigma: float = 8.0,
    min_gap_ms: float = 1.0,
    max_width: int = 128,
) -> List[ClickCandidate]:
    """高通残差 + 滑窗 MAD：短离群候选；宽带瞬态 veto。

    - 残差 = 各声道高通（butter 一阶）；MAD 滑窗（200 ms）自适应
      本底；|residual| > k·MAD 的连通段为候选；
    - 宽带瞬态 veto：候选段在全带能量同时大幅超限时判为真实瞬态
      （鼓点/爆破音），不作为点击修复；
    - 跨声道置信度：多声道候选重叠比例。
    """
    if not _SCIPY or pcm is None or pcm.ndim != 2 or pcm.shape[-1] == 0:
        return []
    b, a = butter(1, min(hp_hz, sample_rate * 0.45), btype="highpass",
                  fs=sample_rate)
    from scipy.signal import lfilter
    residual = np.stack([
        lfilter(b, a, pcm[ch].astype(np.float64)) for ch in range(pcm.shape[0])])
    abs_res = np.abs(residual).max(axis=0)

    win = max(64, int(0.2 * sample_rate))
    # 滑窗 MAD（中位绝对差的滑窗近似：块中位数）。数字净音（合成
    # 斜坡等）MAD→0：加信号尺度下限，避免阈值塌到 0 把全文件连成
    # 一个超限区（FX-07 修复）。
    mad = np.empty_like(abs_res)
    for start in range(0, abs_res.shape[0], win):
        end = min(abs_res.shape[0], start + win)
        segment = abs_res[start:end]
        med = np.median(segment)
        mad[start:end] = np.median(np.abs(segment - med)) + 1e-12
    signal_scale = float(np.median(np.abs(pcm))) + 1e-9
    mad = np.maximum(mad, 1e-4 * signal_scale)

    exceed = abs_res > threshold_sigma * mad
    # 宽带包络（10 ms 滑动 |mono| 均值）：鼓点/爆破音的持续宽带事件
    # 显著抬高包络；6 样本点击几乎不动包络——以此 veto 保护辅音与鼓点。
    mono_abs = np.abs(pcm.mean(axis=0)).astype(np.float64)
    env_win = max(8, int(0.01 * sample_rate))
    env_kernel = np.ones(env_win, dtype=np.float64) / env_win
    broadband_env = np.convolve(mono_abs, env_kernel, mode="same")
    env_baseline = np.median(broadband_env) + 1e-12
    candidates: List[ClickCandidate] = []
    min_gap = int(min_gap_ms * sample_rate / 1000.0)
    position = 0
    total = exceed.shape[0]
    while position < total:
        if not exceed[position]:
            position += 1
            continue
        end = position
        while end < total and exceed[end]:
            end += 1
        width = min(max_width, max(8, (end - position) * 3))
        # veto 回望事件起始：段前 40 ms 到段后 10 ms 内的包络峰值
        # （鼓击尾段的候选回望 onset 峰；孤立点击无前后宽带抬升）。
        lo_env = max(0, position - int(0.040 * sample_rate))
        hi_env = min(total, end + int(0.010 * sample_rate))
        broadband_ratio = (float(np.max(broadband_env[lo_env:hi_env]))
                           / env_baseline if hi_env > lo_env else 0.0)
        if broadband_ratio < 3.0:
            # 跨声道置信度：各声道独立超限的占比。
            per_channel = [
                np.any(np.abs(residual[ch, position:end])
                       > threshold_sigma * mad[position:end].mean())
                for ch in range(pcm.shape[0])]
            confidence = 0.5 + 0.5 * (sum(per_channel) / pcm.shape[0])
            candidates.append(ClickCandidate(
                position=position, width=width, confidence=confidence))
        position = end + min_gap
    return candidates


def _interpolate_repair(
    pcm: np.ndarray, start: int, end: int,
) -> np.ndarray:
    """短区间双侧插值（边界钳制；不修改输入）。

    FX-07 坐标修复：左右上下文取自**严格区间外**（lo 左侧 / hi 右侧
    同一绝对样本轴），上下文不足时按可用量收缩（窗贴左/贴右合法）；
    可用上下文不足 4 点时区间置零（保守回退）。
    """
    out = pcm.copy()
    lo = max(0, start)
    hi = min(pcm.shape[-1], end)
    if hi <= lo:
        return out
    gap = hi - lo
    margin = max(4, gap)
    left = pcm[:, max(0, lo - margin):lo]
    right = pcm[:, hi:min(pcm.shape[-1], hi + margin)]
    for ch in range(pcm.shape[0]):
        # 绝对样本轴：左上下文 = [lo−L, lo)，右上下文 = [hi, hi+R)。
        src_x = np.concatenate([
            np.arange(left.shape[-1]),
            gap + np.arange(right.shape[-1])])
        src_y = np.concatenate([left[ch], right[ch]])
        if src_x.size < 2:
            out[ch, lo:hi] = 0.0
            continue
        dst_x = left.shape[-1] + np.arange(gap)
        out[ch, lo:hi] = np.interp(dst_x, src_x, src_y)
    return out


def remove_clicks(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    candidates: Optional[List[ClickCandidate]] = None,
    spectral_fallback_ms: float = 25.0,
) -> Tuple[np.ndarray, List[ClickCandidate]]:
    """移除点击：短区间双侧插值；长缺失转频谱修复。

    返回 (修复后 PCM, 实际处理的候选)。长缺失（候选宽 > 阈值）调用
    ``audio_repair.repair_spectral``（衰减模式填补），保持与频谱修复
    工具同一语义。
    """
    if candidates is None:
        candidates = detect_clicks(pcm, sample_rate)
    if not candidates or pcm is None:
        return pcm, []
    out = pcm
    handled: List[ClickCandidate] = []
    spectral_threshold = int(spectral_fallback_ms * sample_rate / 1000.0)
    for candidate in candidates:
        half = max(4, candidate.width)
        lo = candidate.position - half
        hi = candidate.position + half
        if hi - lo > spectral_threshold:
            from src.features.edit.core.audio_repair import repair_spectral
            try:
                out = repair_spectral(
                    out, sample_rate, [],
                    (max(0, lo), min(out.shape[-1], hi)),
                    amount_db=24.0, mode="attenuate")
                handled.append(candidate)
            except Exception:  # noqa: BLE001 - 频谱修复缺席保持原样
                continue
        else:
            out = _interpolate_repair(out, max(0, lo), hi)
            handled.append(candidate)
    return out, handled
