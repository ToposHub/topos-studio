"""P1-01 多机位波形自动同步 — 音频互相关角度对齐。

把组内每个角度素材开头的音频段做互相关（参考角度 vs 其他角度），
求出各角度相对参考的滞后量，换算为源帧偏移写入
``MulticamAngle.sync_offset``（换角度命令按 ``base_source + sync_offset``
读源，正偏移 = 该角度开录更早、内容落后）。

分层：
- 纯 DSP（numpy/scipy）：``downmix_to_mono`` / ``decimate_mean`` /
  ``normalize_signal`` / ``best_lag_samples`` / ``compute_sync_alignments``。
- Qt worker：``MulticamSyncWorker`` 用 ``AudioDecoder`` 在后台线程解码取
  PCM（AudioDecoder 类内零 Qt 调用，可在 QThread 中直接使用）。

局限（诚实边界）：只对齐各角度素材的起始段（默认前 60s）；已放置在
时间线上的片段不会追溯平移，偏移作用于后续的角度切换。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from scipy.signal import correlate, correlation_lags

logger = logging.getLogger(__name__)

__all__ = [
    "SyncAlignment",
    "MulticamSyncJob",
    "MulticamSyncWorker",
    "downmix_to_mono",
    "decimate_mean",
    "normalize_signal",
    "best_lag_samples",
    "compute_sync_alignments",
]


# ------------------------------------------------------------------
# 纯 DSP
# ------------------------------------------------------------------


@dataclass(frozen=True)
class SyncAlignment:
    """一个角度相对参考角度的对齐结果。"""

    asset_id: str
    #: 参考内容在该角度素材中出现的位置滞后（秒）。>0 = 该角度开录更早。
    lag_seconds: float = 0.0
    #: 写入 MulticamAngle.sync_offset 的源帧偏移（round(lag_seconds * fps)）。
    offset_frames: int = 0
    #: 峰值 Pearson 相关（[-1, 1]），越接近 1 越可信。
    confidence: float = 1.0
    #: 非空 = 该角度分析失败（无音轨/时长过短等）。
    error: str = ""
    #: F2.6 次峰/主峰相关比（0..1，高=周期/重复信号歧义）。
    ambiguity: float = 0.0
    #: F2.6 非空 = 拒绝写回的原因（低置信/反相/歧义/出界/速率漂移）。
    #: 区别于 error（分析失败）：rejection = 分析完成但结果不可信。
    rejection: str = ""


def downmix_to_mono(pcm: np.ndarray) -> np.ndarray:
    """(channels, samples) float32 → 单声道均值。"""
    if pcm.ndim == 1:
        return pcm.astype(np.float32, copy=False)
    if pcm.shape[0] == 1:
        return pcm[0]
    return pcm.mean(axis=0)


def decimate_mean(x: np.ndarray, factor: int) -> np.ndarray:
    """块均值降采样（48k→8k 用 factor=6）。factor<2 原样返回。"""
    if factor < 2 or x.size == 0:
        return x
    usable = (x.size // factor) * factor
    if usable == 0:
        return x[:0]
    return x[:usable].reshape(-1, factor).mean(axis=1)


def normalize_signal(x: np.ndarray) -> np.ndarray:
    """去直流 + 单位方差（静音/近静音返回原信号，置信度自然很低）。"""
    if x.size == 0:
        return x
    centered = x - x.mean()
    std = centered.std()
    if std < 1e-9:
        return centered
    return centered / std


def best_lag_samples(
    ref: np.ndarray,
    other: np.ndarray,
    max_lag_samples: int,
) -> tuple[int, float]:
    """互相关找 other 相对 ref 的滞后样本数。

    返回 ``(lag, confidence)``。lag = k* 满足 ``other[t + k*] ≈ ref[t]``：
    同一事件在 other 中出现得更晚（k* > 0）时，读取 other 需要加正偏移。
    仅在 ``|lag| <= max_lag_samples`` 窗口内搜索；confidence 为峰值处的
    Pearson 相关近似（按重叠长度归一）。
    """
    n_ref = ref.size
    n_other = other.size
    if n_ref < 2 or n_other < 2:
        return 0, 0.0

    n = min(n_ref, n_other)
    ref_n = normalize_signal(ref[:n])
    other_n = normalize_signal(other[:n])
    corr = correlate(other_n, ref_n, mode="full", method="fft")
    lags = correlation_lags(n, n, mode="full")

    max_lag = max(1, int(max_lag_samples))
    window = np.abs(lags) <= max_lag
    masked = np.where(window, corr, -np.inf)
    peak_idx = int(np.argmax(masked))
    lag = int(lags[peak_idx])

    overlap = max(1, n - abs(lag))
    confidence = float(np.clip(corr[peak_idx] / overlap, -1.0, 1.0))
    return lag, confidence


#: F2.6 防错匹配门槛：|confidence| 低于此值（静音/不相关）拒绝写回。
MIN_SYNC_CONFIDENCE = 0.35
#: F2.6 次峰/主峰比高于此值（周期信号/重复音乐多峰歧义）拒绝写回。
MAX_SYNC_AMBIGUITY = 0.85
#: F2.6 多段采样：各段偏移超过此秒数视为不一致（固定 offset 假设破坏）。
SEGMENT_TOLERANCE_SECONDS = 0.05


def best_lag_diagnostics(
    ref: np.ndarray,
    other: np.ndarray,
    max_lag_samples: int,
) -> tuple[int, float, float]:
    """F2.6：互相关对齐 + 歧义诊断。

    返回 ``(lag, confidence, ambiguity)``；ambiguity = 窗口内次峰（距主
    峰 ≥ max_lag/10）与主峰的相关比（0..1）。周期信号（重复音乐床）在
    周期整数倍处产生近等高峰——ambiguity→1，单峰唯一匹配→→0。
    """
    n_ref = ref.size
    n_other = other.size
    if n_ref < 2 or n_other < 2:
        return 0, 0.0, 0.0
    n = min(n_ref, n_other)
    ref_n = normalize_signal(ref[:n])
    other_n = normalize_signal(other[:n])
    corr = correlate(other_n, ref_n, mode="full", method="fft")
    lags = correlation_lags(n, n, mode="full")
    max_lag = max(1, int(max_lag_samples))
    window = np.abs(lags) <= max_lag
    masked = np.where(window, corr, -np.inf)
    peak_idx = int(np.argmax(masked))
    # F2.6：反相接错线时真峰为负（argmax 只见正峰）——同时查负谷，
    # 负谷显著强于正峰时按反相处理（confidence 取负值作标记）。
    masked_for_min = np.where(window, corr, np.inf)
    trough_idx = int(np.argmin(masked_for_min))
    overlap_peak = max(1, n - abs(int(lags[peak_idx])))
    overlap_trough = max(1, n - abs(int(lags[trough_idx])))
    peak_score = float(masked[peak_idx]) / overlap_peak
    trough_score = float(masked_for_min[trough_idx]) / overlap_trough
    if -trough_score > peak_score * 1.2 and -trough_score >= MIN_SYNC_CONFIDENCE:
        peak_idx = trough_idx
    lag = int(lags[peak_idx])
    overlap = max(1, n - abs(lag))
    confidence = float(np.clip(corr[peak_idx] / overlap, -1.0, 1.0))

    peak_value = abs(float(corr[peak_idx]))
    min_sep = max(1, max_lag // 10)
    away = window & (np.abs(lags - lag) >= min_sep)
    if away.any():
        away_values = np.abs(corr[away])
        second_value = float(np.max(away_values))
    else:
        second_value = 0.0
    if peak_value > 0 and second_value > 0:
        ambiguity = float(np.clip(second_value / peak_value, 0.0, 1.0))
    else:
        ambiguity = 0.0
    return lag, confidence, ambiguity


def alignment_rejection(
    alignment: "SyncAlignment",
    *,
    out_of_range: bool = False,
) -> str:
    """F2.6 防错匹配门槛——返回拒绝写回的原因（空 = 可写回）。

    - ``inverted_polarity``：峰值相关为负且显著（音频反相接错线）。
    - ``low_confidence``：静音/不相关（|conf| < MIN_SYNC_CONFIDENCE）。
    - ``ambiguous_peak``：周期/重复信号多峰歧义。
    - ``offset_out_of_range``：|offset| 超出素材时长（错时码级错误）。
    """
    if alignment.error:
        return alignment.error
    if alignment.confidence < 0 and abs(alignment.confidence) >= MIN_SYNC_CONFIDENCE:
        return "inverted_polarity"
    if abs(alignment.confidence) < MIN_SYNC_CONFIDENCE:
        return "low_confidence"
    if alignment.ambiguity > MAX_SYNC_AMBIGUITY:
        return "ambiguous_peak"
    if out_of_range:
        return "offset_out_of_range"
    return ""


def compute_sync_alignments(
    signals: dict[str, np.ndarray],
    sample_rate: int,
    reference_id: str,
    fps_by_asset: dict[str, float],
    max_shift_seconds: float = 5.0,
) -> dict[str, SyncAlignment]:
    """对一组同采样率单声道信号求各角度相对参考的同步偏移。

    Args:
        signals: asset_id → 单声道波形（假定已统一采样率）。
        sample_rate: 波形采样率（Hz）。
        reference_id: 参考角度的 asset_id（其偏移恒为 0）。
        fps_by_asset: asset_id → 源帧率（帧偏移换算用）。
        max_shift_seconds: 最大搜索窗口（秒）。

    Returns:
        asset_id → SyncAlignment。参考角度 confidence=1.0；缺参考/过短
        时该角度结果带 error。
    """
    results: dict[str, SyncAlignment] = {}
    ref = signals.get(reference_id)
    if ref is None or ref.size < sample_rate // 2:
        for asset_id in signals:
            results[asset_id] = SyncAlignment(
                asset_id=asset_id, error="missing_or_short_reference")
        return results

    max_lag = int(sample_rate * max_shift_seconds)
    n = min(s.size for s in signals.values())
    for asset_id, wave in signals.items():
        if asset_id == reference_id:
            results[asset_id] = SyncAlignment(
                asset_id=asset_id, lag_seconds=0.0,
                offset_frames=0, confidence=1.0)
            continue
        if wave.size < sample_rate // 2:
            results[asset_id] = SyncAlignment(
                asset_id=asset_id, error="segment_too_short")
            continue
        lag, confidence, ambiguity = best_lag_diagnostics(
            ref[:n], wave[:n], max_lag)
        lag_seconds = lag / float(sample_rate)
        fps = float(fps_by_asset.get(asset_id) or 0.0)
        offset_frames = int(round(lag_seconds * fps)) if fps > 0 else 0
        segment_seconds = n / float(sample_rate)
        out_of_range = abs(lag_seconds) >= segment_seconds
        alignment = SyncAlignment(
            asset_id=asset_id,
            lag_seconds=round(lag_seconds, 6),
            offset_frames=offset_frames,
            confidence=round(confidence, 4),
            ambiguity=round(ambiguity, 4),
        )
        results[asset_id] = replace(
            alignment,
            rejection=alignment_rejection(
                alignment, out_of_range=out_of_range),
        )
    return results


def compute_timecode_alignments(
    metadata_by_asset: dict,
    reference_id: str,
    fps_by_asset: dict,
) -> dict[str, SyncAlignment]:
    """F2.6：时间码同步——无开头共同音频时的替代方式。

    各角度素材 metadata 的 ``timecode``（探测自流/容器/tmcd 轨）相对
    参考角度求差：正 lag = 该角度开录更早（与波形语义一致）。
    缺 TC/解析失败 → error（不写）；结果无置信度概念（确定性元数据），
    confidence=1.0，预览对话框仍可整体取消。
    """
    from src.features.edit.core.interchange import parse_timecode

    def _tc_seconds(meta: dict) -> Optional[float]:
        raw = (meta or {}).get("timecode") if isinstance(meta, dict) else None
        if not raw:
            return None
        try:
            hh, mm, ss, ff = parse_timecode(str(raw))
        except ValueError:
            return None
        # 帧字段按素材自身帧率折算（未持久化 TC fps 时的最优近似）
        fps = 25.0
        return hh * 3600.0 + mm * 60.0 + ss + ff / fps

    results: dict[str, SyncAlignment] = {}
    ref_seconds = _tc_seconds(metadata_by_asset.get(reference_id))
    for asset_id in metadata_by_asset:
        meta = metadata_by_asset.get(asset_id) or {}
        if asset_id == reference_id:
            results[asset_id] = SyncAlignment(
                asset_id=asset_id, confidence=1.0)
            continue
        if ref_seconds is None:
            results[asset_id] = SyncAlignment(
                asset_id=asset_id, error="reference_no_timecode")
            continue
        own = _tc_seconds(meta)
        if own is None:
            results[asset_id] = SyncAlignment(
                asset_id=asset_id,
                error="no_timecode" if not (meta or {}).get("timecode")
                else "bad_timecode")
            continue
        lag_seconds = own - ref_seconds
        fps = float(fps_by_asset.get(asset_id) or 0.0)
        offset_frames = int(round(lag_seconds * fps)) if fps > 0 else 0
        results[asset_id] = SyncAlignment(
            asset_id=asset_id,
            lag_seconds=round(lag_seconds, 6),
            offset_frames=offset_frames,
            confidence=1.0,
        )
    return results


def combine_segment_alignments(
    segments: list[SyncAlignment],
    fps: float = 0.0,
) -> SyncAlignment:
    """F2.6：多段（首/中/末）对齐合成 + 漂移判别。

    - 各段可靠且偏移一致（≤ SEGMENT_TOLERANCE_SECONDS）→ 固定 offset：
      取中位 lag，confidence 取各段最小，可写回。
    - 偏移随段位置显著发散（max-min > 1.0s 且单调）→ ``rate_drift_detected``
      拒绝写回（未支持漂移矫正时诊断并保留原片，不伪称已同步）。
    - 其余不一致（非单调/部分拒绝）→ ``inconsistent_segments`` 拒绝。
    """
    asset_id = segments[0].asset_id if segments else ""
    reliable = [s for s in segments if not s.error and not s.rejection]
    if not reliable:
        worst = next(
            (s for s in segments if s.error or s.rejection), None)
        return SyncAlignment(
            asset_id=asset_id,
            error=worst.error if worst else "no_reliable_segment",
            rejection="" if worst and worst.error else (
                worst.rejection if worst else ""),
        )
    lags = sorted(s.lag_seconds for s in reliable)
    median_lag = lags[len(lags) // 2]
    spread = lags[-1] - lags[0]
    if spread <= SEGMENT_TOLERANCE_SECONDS:
        offset_frames = (
            int(round(median_lag * fps)) if fps > 0
            else reliable[0].offset_frames)
        return SyncAlignment(
            asset_id=asset_id,
            lag_seconds=round(median_lag, 6),
            offset_frames=offset_frames,
            confidence=round(min(abs(s.confidence) for s in reliable), 4),
        )
    return SyncAlignment(
        asset_id=asset_id,
        lag_seconds=round(median_lag, 6),
        confidence=round(min(abs(s.confidence) for s in reliable), 4),
        rejection="rate_drift_detected" if len(reliable) >= 3
        else "inconsistent_segments",
    )


def filter_writable_offsets(payload: dict) -> tuple[dict, list[str]]:
    """F2.6：写回过滤——(可写 offsets, 拒绝原因列表)。

    控制器 Apply 路径共用：error / rejection 的角度不进 offsets（对话框
    把 rejection 呈现为原因行，用户可见为何没写）。
    """
    offsets: dict[str, int] = {}
    skipped: list[str] = []
    for asset_id, item in payload.items():
        reason = item.get("error") or item.get("rejection")
        if reason:
            skipped.append(f"{asset_id}: {reason}")
            continue
        offsets[asset_id] = int(item.get("offset_frames", 0))
    return offsets, skipped


# ------------------------------------------------------------------
# Qt worker（后台解码 + 对齐）
# ------------------------------------------------------------------


@dataclass(frozen=True)
class MulticamSyncJob:
    """一个角度的解码任务描述。"""

    asset_id: str
    path: str
    fps: float = 0.0


def _decode_mono_segment(
    path: str,
    seconds: float,
    output_rate: int = 48000,
    block_samples: int = 48000,
    start_seconds: float = 0.0,
) -> tuple[Optional[np.ndarray], str]:
    """用 AudioDecoder 从 ``start_seconds`` 起解码 ``seconds`` 秒单声道。

    F2.6：多段（首/中/末）采样评估长录制漂移——段起点任意秒定位。
    段尾不足（素材先结束）返回已解到的部分（>=0.5s 视为有效短段）。

    Returns:
        (mono_waveform, error) — 失败时 waveform 为 None。
    """
    from src.features.edit.core.audio_playback import AudioDecoder

    decoder = AudioDecoder()
    try:
        if not decoder.open(Path(path), output_sample_rate=output_rate):
            return None, "no_audio_stream"
        if not decoder.seek_to_time(float(start_seconds)):
            return None, "seek_failed"
        need = int(seconds * output_rate)
        chunks: list[np.ndarray] = []
        got = 0
        while got < need:
            block = decoder.decode_next_samples(min(block_samples, need - got))
            if block is None or block.size == 0:
                break
            chunks.append(block)
            got += block.shape[1]
        if not chunks:
            return None, "no_samples"
        pcm = np.concatenate(chunks, axis=1)
        mono = downmix_to_mono(pcm)
        # F2.10：decode_next_samples 不重采样（重采样只发生在
        # decode_samples_for_frame）——源率 ≠ 目标率时显式归一，否则
        # 44.1k 源被按 48k 计数，lag 秒数差 ~8.8%、跨率互相关直接失配。
        source_rate = int(getattr(decoder, "_sample_rate", 0) or output_rate)
        if source_rate and source_rate != output_rate:
            from src.features.edit.core.audio_playback import _resample_audio
            resampled = _resample_audio(
                mono, source_rate, output_rate,
                instance_id=abs(hash(path)) % 100000)
            if resampled is None or resampled.size == 0:
                return None, "sample_rate_mismatch"
            mono = np.asarray(resampled).reshape(-1)
        return mono, ""
    except Exception as exc:  # noqa: BLE001 — 后台线程兜底上报
        logger.warning("multicam sync decode failed for %s: %s", path, exc)
        return None, f"decode_error: {exc}"
    finally:
        try:
            decoder.close()
        except Exception:  # noqa: BLE001 — 释放失败不掩盖结果
            pass


class MulticamSyncWorker(QThread):
    """后台波形同步 worker：解码各角度音频段 → 互相关 → 偏移结果。

    信号：
    - ``sync_finished(dict)``: {asset_id: {"offset_frames", "lag_seconds",
      "confidence", "error"}}，含参考角度（全 0）。
    - ``sync_failed(str)``: 参考角度无法解码等致命失败。
    """

    sync_finished = pyqtSignal(dict)
    sync_failed = pyqtSignal(str)

    DECIMATE_FACTOR = 6  # 48k → 8k，相关计算量降 36 倍

    def __init__(
        self,
        jobs: list[MulticamSyncJob],
        reference_asset_id: str,
        analysis_seconds: float = 60.0,
        max_shift_seconds: float = 5.0,
        segment_starts: tuple = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._jobs = list(jobs)
        self._reference_id = reference_asset_id
        self._analysis_seconds = float(analysis_seconds)
        self._max_shift_seconds = float(max_shift_seconds)
        # F2.6：多段采样起点（秒）。空 = 仅头部单段（历史行为）。
        self._segment_starts = tuple(
            float(s) for s in segment_starts if float(s) >= 0.0)

    def run(self) -> None:
        rate = 48000 // self.DECIMATE_FACTOR
        starts = self._segment_starts or (0.0,)
        segment_signals: dict[str, dict[float, np.ndarray]] = {}
        errors: dict[str, str] = {}
        for job in self._jobs:
            if self.isInterruptionRequested():
                # F2.6：分析中途取消——上报取消（零写入；控制器清引用）
                self.sync_failed.emit("cancelled")
                return
            per_start: dict[float, np.ndarray] = {}
            for start in starts:
                mono, err = _decode_mono_segment(
                    job.path, self._analysis_seconds,
                    start_seconds=start)
                if mono is None:
                    if err == "seek_failed" and per_start:
                        break  # 素材先结束：末段自然缺席，保留已有段
                    errors[job.asset_id] = err
                    per_start = {}
                    break
                if mono.size < 48000 // 2:
                    continue  # 段尾不足 0.5s：丢弃该段
                per_start[start] = decimate_mean(mono, self.DECIMATE_FACTOR)
            if per_start:
                segment_signals[job.asset_id] = per_start
        if self._reference_id not in segment_signals:
            self.sync_failed.emit(
                errors.get(self._reference_id, "reference_unavailable"))
            return

        fps_by_asset = {j.asset_id: j.fps for j in self._jobs}
        payload: dict = {}
        ref_segments = segment_signals[self._reference_id]
        for job in self._jobs:
            if job.asset_id in errors:
                payload[job.asset_id] = {
                    "offset_frames": 0, "lag_seconds": 0.0,
                    "confidence": 0.0, "error": errors[job.asset_id],
                }
                continue
            if job.asset_id == self._reference_id:
                payload[job.asset_id] = {
                    "offset_frames": 0, "lag_seconds": 0.0,
                    "confidence": 1.0, "error": "",
                }
                continue
            own = segment_signals.get(job.asset_id, {})
            common = sorted(set(own) & set(ref_segments))
            if not common:
                payload[job.asset_id] = {
                    "offset_frames": 0, "lag_seconds": 0.0,
                    "confidence": 0.0, "error": "no_common_segment",
                }
                continue
            segment_alignments = [
                compute_sync_alignments(
                    {self._reference_id: ref_segments[start],
                     job.asset_id: own[start]},
                    sample_rate=rate,
                    reference_id=self._reference_id,
                    fps_by_asset=fps_by_asset,
                    max_shift_seconds=self._max_shift_seconds,
                )[job.asset_id]
                for start in common
            ]
            if len(segment_alignments) == 1:
                align = segment_alignments[0]
            else:
                align = combine_segment_alignments(
                    segment_alignments, fps=float(job.fps or 0.0))
            payload[job.asset_id] = {
                "offset_frames": align.offset_frames,
                "lag_seconds": align.lag_seconds,
                "confidence": align.confidence,
                "error": align.error,
                "rejection": align.rejection,
            }
        self.sync_finished.emit(payload)


def compute_audio_binding_alignment(
    video_path: str,
    wav_path: str,
    *,
    analysis_seconds: float = 30.0,
    max_shift_seconds: float = 300.0,
    segment_starts: tuple = (0.0,),
) -> SyncAlignment:
    """F2.10：视频机内声 vs 外录 WAV 的源级对齐（纯函数，段解码内置）。

    参考侧 = 视频内嵌音轨；各段经采样率归一解码后互相关，多段一致取
    中位；低置信/反相/歧义/出界/采样率不可归一 → rejection（不误绑，
    调用方按拒绝呈现且不得提交绑定）。大搜索窗默认 300s（外录提前
    开录常见量级，区别于多机位的 ±5s）。
    """
    rate = 48000 // MulticamSyncWorker.DECIMATE_FACTOR
    ref_segments: dict[float, np.ndarray] = {}
    own_segments: dict[float, np.ndarray] = {}
    for start in segment_starts:
        video_mono, err = _decode_mono_segment(
            video_path, analysis_seconds, start_seconds=float(start))
        if video_mono is None:
            return SyncAlignment(
                "wav", error=err or "video_decode_failed")
        wav_mono, err = _decode_mono_segment(
            wav_path, analysis_seconds, start_seconds=float(start))
        if wav_mono is None:
            return SyncAlignment("wav", error=err or "wav_decode_failed")
        ref_segments[float(start)] = decimate_mean(
            video_mono, MulticamSyncWorker.DECIMATE_FACTOR)
        own_segments[float(start)] = decimate_mean(
            wav_mono, MulticamSyncWorker.DECIMATE_FACTOR)
    segment_alignments = [
        compute_sync_alignments(
            {"video": ref_segments[start], "wav": own_segments[start]},
            sample_rate=rate,
            reference_id="video",
            fps_by_asset={"wav": 1.0},
            max_shift_seconds=max_shift_seconds,
        )["wav"]
        for start in sorted(ref_segments)
    ]
    if len(segment_alignments) == 1:
        return segment_alignments[0]
    return combine_segment_alignments(segment_alignments, fps=1.0)
