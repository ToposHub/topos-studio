"""Clip-level PCM sampling, visualization and audition data for the Mixer.

P0-2 of the Clip Editor roadmap: one canonical, binding-aware sample range
per clip feeds the waveform, the spectrogram, the measurements and the
audition player, so what you see and what you hear share coordinates.

Design contract (docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §2):

- ``ClipAudioRenderSpec`` is pure data resolved from the stable timeline
  model ``(project, timeline, clip, asset)``.  Its cache key covers the
  source fingerprint, the working rate, the channel layout and the exact
  half-open sample range, so stale results never overwrite a newer window.
- Decoding resamples to the 48 kHz working rate as planar ``float32[C, N]``
  and runs cooperatively cancelable; results are published atomically and
  cached in a small LRU keyed by the spec.
- Waveform min/max/RMS, Hann-STFT spectrograms (log-frequency rows, as the
  Clip Editor canvas expects) and zero-crossing snapping all consume the
  same PCM array.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QMutex

from src.features.edit.core.audio_sync import binding_for_asset, bound_audio_frame, resolve_audio_source
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE, TimelineSampleClock

logger = logging.getLogger(__name__)

# 与 TakeAuditionPlayer 一致的护栏：单次取样的上限（内存与 UI 停留时间）。
MAX_CLIP_PCM_SECONDS = 300.0
MAX_CLIP_PCM_SAMPLES = int(AUDIO_REFERENCE_SAMPLE_RATE * MAX_CLIP_PCM_SECONDS)
MAX_CLIP_PCM_FILE_BYTES = 256 * 1024 * 1024
# LRU：同一时间段内打开的片段数很有限，4 份立体声 5 分钟 ≈ 460 MB 上限。
PCM_CACHE_CAPACITY = 4
# 过零吸附：两声道绝对幅值和低于该值的符号变化都视为干净过零，
# 干净候选之间按"离原游标最近"决胜（P0-2 §2.4）。
ZERO_CROSSING_AMPLITUDE = 0.05


@dataclass(frozen=True)
class ClipAudioRenderSpec:
    """一次片段音频渲染的全部输入（纯数据、可哈希、可持久化重建）。"""

    project_id: str
    timeline_id: str
    clip_id: str
    revision: int
    asset_id: str
    source_path: str
    source_fingerprint: str
    sample_rate: int
    channel_count: int
    channel_layout: Optional[str]
    # 源域半开区间 [start_sample, end_sample)，工作采样率。
    start_sample: int
    end_sample: int
    reverse: bool
    offset_seconds: float = 0.0
    display_duration: float = 0.0

    @property
    def sample_count(self) -> int:
        return max(0, self.end_sample - self.start_sample)

    def cache_key(self) -> tuple:
        """结果缓存的稳定标识：源指纹 + 精确范围 + 速率/声道。"""
        return (
            self.source_fingerprint, self.sample_rate, self.channel_count,
            self.start_sample, self.end_sample, self.reverse,
        )


@dataclass(frozen=True)
class ClipPcmResult:
    """解码完成的 PCM 与其来源标识（原子发布的不可变载荷）。"""

    spec_key: tuple
    pcm: np.ndarray  # planar float32 [channels, samples]
    sample_rate: int
    channel_count: int
    channel_layout: Optional[str] = None


def source_fingerprint(path: str) -> str:
    """路径 + mtime + size 的指纹（与波形缓存同一套身份假设）。"""
    try:
        stat = os.stat(path)
        raw = f"{os.path.abspath(path)}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        raw = f"{os.path.abspath(path)}::"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _audio_channel_count(asset) -> int:
    channels = getattr(asset, "audio_channels", None)
    if isinstance(channels, int) and channels > 0:
        return channels
    return 2


def _audio_channel_layout(asset) -> Optional[str]:
    layout = getattr(asset, "audio_channel_layout", None)
    return layout if isinstance(layout, str) and layout else None


def resolve_clip_audio_spec(
    project, timeline, clip, asset, *,
    working_rate: int = AUDIO_REFERENCE_SAMPLE_RATE,
) -> Optional[ClipAudioRenderSpec]:
    """把时间线片段解析为工作采样率下的取样范围。

    复用 ``resolve_audio_source`` / ``bound_audio_frame`` 的绑定与帧域换算
    语义（与 ProjectClipEditor 的波形映射同一条数学路径），保证波形、
    频谱、试听共用同一坐标系。
    """
    if project is None or timeline is None or clip is None or asset is None:
        return None
    path, offset = resolve_audio_source(project, asset)
    if not path:
        return None
    timeline_fps = float(timeline.fps)
    if not math.isfinite(timeline_fps) or timeline_fps <= 0.0:
        return None
    source_fps = asset.fps
    if not isinstance(source_fps, (int, float)) or not math.isfinite(source_fps) or source_fps <= 0.0:
        source_fps = timeline_fps
    start_frame = bound_audio_frame(clip.source_start, offset, source_fps)
    start_sec = start_frame / source_fps + (clip.source_sample_offset or 0) / AUDIO_REFERENCE_SAMPLE_RATE
    end_sec = (start_frame + max(0, clip.source_duration)) / source_fps
    start_sample = max(0, round(start_sec * working_rate))
    end_sample = max(start_sample, round(end_sec * working_rate))
    return ClipAudioRenderSpec(
        project_id=str(getattr(project, "project_id", "") or ""),
        timeline_id=str(timeline.timeline_id),
        clip_id=str(clip.clip_id),
        revision=int(getattr(timeline, "revision", 0) or 0),
        asset_id=str(asset.asset_id),
        source_path=path,
        source_fingerprint=source_fingerprint(path),
        sample_rate=working_rate,
        channel_count=_audio_channel_count(asset),
        channel_layout=_audio_channel_layout(asset),
        start_sample=start_sample,
        end_sample=end_sample,
        reverse=bool(clip.is_reverse_playback()),
        offset_seconds=offset,
        display_duration=clip.timeline_duration / timeline_fps,
    )


def resolve_asset_audio_spec(
    project, asset, *,
    working_rate: int = AUDIO_REFERENCE_SAMPLE_RATE,
) -> Optional[ClipAudioRenderSpec]:
    """源监视模式：整段音频素材（无时间线裁剪）。"""
    if asset is None:
        return None
    path, offset = resolve_audio_source(project, asset)
    if not path:
        return None
    duration = getattr(asset, "duration_seconds", None)
    total = (
        int(round(duration * working_rate))
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0
        else MAX_CLIP_PCM_SAMPLES
    )
    total = max(0, min(total, MAX_CLIP_PCM_SAMPLES))
    return ClipAudioRenderSpec(
        project_id=str(getattr(project, "project_id", "") or "") if project is not None else "",
        timeline_id="",
        clip_id="",
        revision=0,
        asset_id=str(asset.asset_id),
        source_path=path,
        source_fingerprint=source_fingerprint(path),
        sample_rate=working_rate,
        channel_count=_audio_channel_count(asset),
        channel_layout=_audio_channel_layout(asset),
        start_sample=max(0, round(offset * working_rate)) if offset < 0 else 0,
        end_sample=total,
        reverse=False,
        offset_seconds=offset,
        display_duration=total / working_rate,
    )


def decode_audio_range(
    path: str,
    start_sample: int,
    end_sample: int,
    sample_rate: int,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    channel_map: Optional[tuple] = None,
) -> Optional[np.ndarray]:
    """解码源域 ``[start, end)`` 为工作采样率平面 float32。

    P0 · 声道域合同（2026-11-19 计划）：解码保留源布局与通道名——
    >2ch 不再由 PyAV 重采样器 naive 混成立体声，Nch 解出后先做可选的
    源 patch 选道（``channel_map``，Nch 域——poly WAV 逐声道选择），
    再经标准 BS.775 矩阵折到 ≤2ch 工作域（LFE 默认排除）。Clip Editor
    取样、预渲染缓存与波形共用本路径（同一通道域）。

    与 WaveformExtractorWorker 相同的 PyAV 管线：seek 到入点，重采样到
    目标速率（fltp），按精确采样数裁剪 preroll/尾部。协作式取消——取消
    返回 None。容器句柄 try/finally 关闭。
    """
    import av

    if end_sample <= start_sample or sample_rate <= 0:
        return None
    wanted = end_sample - start_sample
    try:
        if os.path.getsize(path) > MAX_CLIP_PCM_FILE_BYTES:
            logger.warning("clip pcm rejected (file too large): %s", path)
            return None
    except OSError:
        return None

    container = None
    try:
        container = av.open(path)
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return None
        # 注：不做 container.seek——WAV 等容器的 seek 精度与 resampler 状态
        # 会引入样本漂移；片段取样受 300 s 上限约束，从头解码并精确跳过
        # preroll 更可靠（脉冲位置容差 1 sample 的验收依赖这一点）。
        resampler = None
        mono_resampler = None
        source_channels = 0
        blocks: list[np.ndarray] = []
        collected = 0
        # 先丢到入点前的 preroll，再收满所需采样。
        skip = start_sample
        for frame in container.decode(stream):
            if cancel_check is not None and cancel_check():
                return None
            if resampler is None and mono_resampler is None:
                source_channels = len(frame.layout.channels)
                # P0：保留源布局解码（>2ch 不在重采样阶段 naive 混声）；
                # 工作域折叠在解码完成后走我们的 BS.775 矩阵。无名多声道
                # 布局（无 channel mask 的 WAV）会让 PyAV 重采样器崩溃——
                # >2ch 一律逐声道以 mono 重采样（声道数无关、率精确）。
                if source_channels <= 2:
                    resampler = av.AudioResampler(
                        format="fltp",
                        layout="mono" if source_channels == 1 else "stereo",
                        rate=sample_rate)
                else:
                    mono_resampler = av.AudioResampler(
                        format="fltp", layout="mono", rate=sample_rate)
            if resampler is not None:
                try:
                    resampled = resampler.resample(frame)
                except Exception:  # noqa: BLE001 - 个别帧重采样失败时跳过
                    continue
                if resampled is None:
                    continue
                if not isinstance(resampled, (list, tuple)):
                    resampled = [resampled]
                chunks = [
                    out.to_ndarray() for out in resampled
                ]
            else:
                # >2ch：packed → (chs, n) float32，逐声道 mono 重采样。
                try:
                    raw = frame.to_ndarray()
                except Exception:  # noqa: BLE001
                    continue
                if raw.ndim == 1:
                    raw = raw.reshape(1, -1)
                if raw.dtype != np.float32:
                    raw = raw.astype(np.float32) / 32768.0
                if raw.shape[0] == 1:
                    raw = raw.reshape(-1, source_channels).T
                try:
                    per_channel = []
                    for ch in range(source_channels):
                        mono_row = np.ascontiguousarray(
                            raw[ch], dtype=np.float32).reshape(1, -1)
                        mono_frame = av.AudioFrame.from_ndarray(
                            mono_row, format="fltp", layout="mono")
                        mono_frame.sample_rate = frame.sample_rate or sample_rate
                        res_ch = mono_resampler.resample(mono_frame)
                        if res_ch is None:
                            continue
                        if isinstance(res_ch, (list, tuple)):
                            res_ch = res_ch[0]
                        per_channel.append(res_ch.to_ndarray()[0])
                    if not per_channel:
                        continue
                    chunks = [np.stack(per_channel)]
                except Exception:  # noqa: BLE001
                    continue
            for data in chunks:
                if cancel_check is not None and cancel_check():
                    return None
                if data.ndim == 1:
                    data = data.reshape(1, -1)
                if skip > 0:
                    if data.shape[1] <= skip:
                        skip -= data.shape[1]
                        continue
                    data = data[:, skip:]
                    skip = 0
                blocks.append(data.astype(np.float32, copy=False))
                collected += data.shape[1]
                if collected >= wanted:
                    break
            if collected >= wanted:
                break
        if not blocks:
            return None
        pcm = np.concatenate(blocks, axis=1)[:, :wanted]
        if pcm.shape[1] == 0:
            return None
        # P0 · 声道域：Nch 域源 patch 选道（选不出=直通）→ BS.775 矩阵
        # 折到 ≤2ch 工作域（LFE 默认排除）。
        if channel_map:
            from src.features.edit.core.source_patch import SourcePatchSpec
            pcm = SourcePatchSpec(
                audio_channel_map=tuple(channel_map)).select_channels(pcm)
        if pcm.shape[0] > 2:
            from src.features.edit.core.channel_layout import adapt_channels
            pcm = adapt_channels(pcm, 2)
        return np.ascontiguousarray(pcm, dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 - 解码失败按“无 PCM”上报
        logger.warning("clip pcm decode failed for %s: %s", path, exc)
        return None
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:  # noqa: BLE001
                logger.debug("clip pcm container close failed", exc_info=True)


def compute_waveform_stats(
    pcm: np.ndarray, bins: int,
) -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
    """每声道 min/max/RMS 包络（值域与画布约定一致：min≤0，max≥0）。"""
    channels = pcm.shape[0]
    bins = max(2, bins)
    mins: list[list[float]] = []
    maxs: list[list[float]] = []
    rmss: list[list[float]] = []
    for ch in range(channels):
        data = pcm[ch]
        if data.size == 0:
            mins.append([0.0] * bins)
            maxs.append([0.0] * bins)
            rmss.append([0.0] * bins)
            continue
        idx = np.linspace(0, data.size, bins + 1).astype(np.int64)
        # 空桶（超采样 bins > samples）退化为逐样本
        counts = np.diff(idx)
        valid = counts > 0
        segments = np.where(valid, counts, 1)
        low = np.full(bins, 0.0, dtype=np.float32)
        high = np.full(bins, 0.0, dtype=np.float32)
        rms = np.full(bins, 0.0, dtype=np.float32)
        starts = idx[:-1][valid]
        ends = idx[1:][valid]
        for i, (s, e) in enumerate(zip(starts, ends)):
            seg = data[s:e]
            low[i] = float(seg.min())
            high[i] = float(seg.max())
            rms[i] = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
        mins.append(low.tolist())
        maxs.append(high.tolist())
        rmss.append(rms.tolist())
    return mins, maxs, rmss


def compute_spectrogram(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    nfft: int = 2048,
    time_bins: int = 256,
    freq_bins: int = 96,
    db_floor: float = -96.0,
) -> list[list[float]]:
    """Hann STFT → 对数频率行、归一化 0-1 的 [time][freq] 矩阵。

    画布把行 0..f_bins-1 映射为 20 Hz..Nyquist 的对数频率，因此这里直接
    产出对数频率桶（每桶取覆盖 FFT bin 的最大能量），避免画布再做二次
    换算。单声道取该声道，立体声取双声道功率和（相位无关）。
    """
    nfft = max(256, 1 << int(max(8, math.log2(nfft))))
    hop = max(1, nfft // 4)
    window = np.hanning(nfft)
    data = pcm.astype(np.float64, copy=False)
    # 每声道独立 STFT 后功率相加——与声道间相位无关，反极性不抵消。
    frames = max(1, 1 + (data.shape[1] - nfft) // hop) if data.shape[1] >= nfft else 1
    power = np.zeros((frames, nfft // 2 + 1), dtype=np.float64)
    for ch in range(data.shape[0]):
        x = data[ch]
        if x.size < nfft:
            x = np.pad(x, (0, nfft - x.size))
        for i in range(frames):
            seg = x[i * hop:i * hop + nfft] * window
            power[i] += np.abs(np.fft.rfft(seg)) ** 2
    # 时间轴聚到 time_bins 桶
    if frames > time_bins:
        edges = np.linspace(0, frames, time_bins + 1).astype(np.int64)
        edges = np.unique(np.maximum.accumulate(edges))
        cols = np.maximum(1, len(edges) - 1)
        aggregated = np.empty((cols, power.shape[1]), dtype=np.float64)
        for i in range(cols):
            aggregated[i] = power[edges[i]:max(edges[i] + 1, edges[i + 1])].max(axis=0)
    else:
        aggregated = power
        cols = frames
    db = 10.0 * np.log10(np.maximum(aggregated, 1e-18))
    nyquist = sample_rate * 0.5
    f_min, f_max = 20.0, max(1000.0, nyquist)
    log_span = math.log10(f_max / f_min)
    fft_freqs = np.fft.rfftfreq(nfft, d=1.0 / max(1, sample_rate))
    matrix: list[list[float]] = []
    norm = 1.0 / (-db_floor)
    for col in range(cols):
        row = [0.0] * freq_bins
        for b in range(freq_bins):
            lo = f_min * (f_max / f_min) ** (b / freq_bins)
            hi = f_min * (f_max / f_min) ** ((b + 1) / freq_bins)
            mask = (fft_freqs >= lo) & (fft_freqs < hi) if b else (fft_freqs >= 0) & (fft_freqs < hi)
            if not mask.any():
                # 高于 Nyquist 的行（f_max 被钳到 1k 的极端情况）置 0
                row[b] = 0.0
                continue
            value = float(db[col][mask].max())
            row[b] = max(0.0, min(1.0, value * norm))
        matrix.append(row)
    return matrix


def find_zero_crossing(
    pcm: np.ndarray,
    candidate: int,
    *,
    tolerance_samples: int,
    bounds: tuple[int, int],
) -> Optional[int]:
    """在候选样本附近找最近的过零点；找不到返回 None。

    - 单声道：对应声道符号变化；
    - 立体声：任一声道符号变化即为候选，按“两声道绝对幅值和最小、
      距候选最近”打分——不使用 L+R 合成波形，反极性素材不会误判。
    """
    if pcm.ndim != 2 or pcm.shape[0] == 0 or pcm.shape[1] == 0:
        return None
    lo_bound, hi_bound = bounds
    lo = max(0, lo_bound, candidate - max(0, tolerance_samples))
    hi = min(min(pcm.shape[1] - 1, hi_bound - 1), candidate + max(0, tolerance_samples))
    if hi <= lo:
        return None
    window = pcm[:, lo:hi + 1]
    signs = np.sign(window)
    changed = np.zeros(window.shape[1], dtype=bool)
    changed[0] = False
    for ch in range(window.shape[0]):
        channel_change = signs[ch, 1:] != signs[ch, :-1]
        # 零样本本身也视为过零（sign 0 → 非零）
        zero_hit = (window[ch, 1:] == 0) | (window[ch, :-1] == 0)
        changed[1:] |= channel_change | zero_hit
    if not changed.any():
        return None
    indices = np.nonzero(changed)[0]
    abs_sum = np.abs(window[:, indices]).sum(axis=0)
    distances = np.abs(indices + lo - candidate)
    # 幅值先量化：低于阈值的符号变化都视为"干净过零"，此时距离优先；
    # 只有候选都不干净（如 DC 偏置下的斜穿）才按幅值比较。
    clean = np.where(abs_sum <= ZERO_CROSSING_AMPLITUDE, 0.0, abs_sum)
    order = np.lexsort((distances, np.round(clean, 6)))
    return int(indices[order[0]] + lo)


class _ClipPcmWorker(QThread):
    """后台解码一个 spec；同一 worker 串行处理最新请求（旧请求协作取消）。"""

    finished_pcm = pyqtSignal(object)  # ClipPcmResult | None（携带 spec_key）
    failed = pyqtSignal(tuple, str)    # spec_key, error

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mutex = QMutex()
        self._spec: Optional[ClipAudioRenderSpec] = None
        self._generation = 0
        self._canceled = False
        self._wakeup = threading.Event()

    def submit(self, spec: ClipAudioRenderSpec) -> None:
        self._mutex.lock()
        try:
            self._generation += 1
            self._canceled = True  # 掐掉在途任务
            self._spec = spec
        finally:
            self._mutex.unlock()
        self._wakeup.set()
        if not self.isRunning():
            self.start()

    def cancel_all(self) -> None:
        self._mutex.lock()
        try:
            self._generation += 1
            self._canceled = True
            self._spec = None
        finally:
            self._mutex.unlock()

    def run(self) -> None:  # noqa: D102 - QThread 入口
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            self._mutex.lock()
            generation = self._generation
            self._canceled = False
            spec = self._spec
            self._spec = None
            self._mutex.unlock()
            if spec is None:
                continue
            def canceled() -> bool:
                self._mutex.lock()
                try:
                    return self._canceled or self._generation != generation
                finally:
                    self._mutex.unlock()
            try:
                pcm = decode_audio_range(
                    spec.source_path, spec.start_sample, spec.end_sample,
                    spec.sample_rate, cancel_check=canceled,
                )
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(spec.cache_key(), str(exc))
                continue
            if canceled() or pcm is None:
                continue
            if spec.reverse:
                pcm = pcm[:, ::-1]
            self.finished_pcm.emit(ClipPcmResult(
                spec_key=spec.cache_key(),
                pcm=pcm,
                sample_rate=spec.sample_rate,
                channel_count=int(pcm.shape[0]),
                channel_layout=spec.channel_layout,
            ))


class ClipPcmProvider(QObject):
    """单例：spec → PCM 的请求/缓存层（原子发布 + LRU + 代际校验）。"""

    pcm_ready = pyqtSignal(object)     # ClipPcmResult
    pcm_failed = pyqtSignal(tuple, str)

    _instance: "ClipPcmProvider | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: dict[tuple, ClipPcmResult] = {}
        self._cache_order: list[tuple] = []
        self._worker = _ClipPcmWorker()
        self._worker.finished_pcm.connect(self._on_worker_pcm)
        self._worker.failed.connect(self.pcm_failed)
        self._pending: dict[tuple, list[Callable[[ClipPcmResult], None]]] = {}
        self._done_once: set[tuple] = set()

    @classmethod
    def instance(cls) -> "ClipPcmProvider":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ClipPcmProvider()
            return cls._instance

    def get_cached(self, spec: ClipAudioRenderSpec) -> Optional[ClipPcmResult]:
        return self._cache.get(spec.cache_key())

    def request(
        self, spec: ClipAudioRenderSpec,
        callback: Optional[Callable[[ClipPcmResult], None]] = None,
    ) -> bool:
        """请求一个 spec 的 PCM；命中缓存立即（同步）回调。"""
        key = spec.cache_key()
        cached = self._cache.get(key)
        if cached is not None:
            if callback is not None:
                callback(cached)
            return True
        if callback is not None:
            self._pending.setdefault(key, []).append(callback)
        self._worker.submit(spec)
        return False

    def shutdown(self) -> None:
        self._worker.cancel_all()
        self._worker.wait(200)

    # ------------------------------------------------------------------
    def _on_worker_pcm(self, result: ClipPcmResult) -> None:
        key = result.spec_key
        self._cache[key] = result
        self._cache_order.append(key)
        while len(self._cache_order) > PCM_CACHE_CAPACITY:
            self._cache.pop(self._cache_order.pop(0), None)
        callbacks = self._pending.pop(key, [])
        for callback in callbacks:
            try:
                callback(result)
            except Exception:  # noqa: BLE001
                logger.exception("clip pcm callback failed")
        self.pcm_ready.emit(result)
