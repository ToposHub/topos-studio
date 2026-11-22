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
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from itertools import chain
from typing import TYPE_CHECKING, Callable, Iterator, Optional

import numpy as np
from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSignal

from src.features.edit.core.audio_sync import binding_for_asset, bound_audio_frame, resolve_audio_source
from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE, TimelineSampleClock

if TYPE_CHECKING:
    import av
    from src.features.edit.core.project import EditProject, MediaAsset

logger = logging.getLogger(__name__)

# 整段编辑/试听限制与各层字节上限；窗口显示保留完整逻辑范围。
MAX_CLIP_PCM_SECONDS = 300.0
MAX_DECODED_PCM_BYTES = 256 * 1024 * 1024
PCM_CACHE_CAPACITY = 4
PCM_CACHE_MAX_BYTES = 128 * 1024 * 1024
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
    # P0-2 · 生效的源选道（绑定优先，其次工程 patch；()=直通）。
    # 进入 cache_key：patch/绑定切换后旧结果不得复用。
    source_channel_map: tuple = ()
    source_asset_id: str = ""

    @property
    def sample_count(self) -> int:
        return max(0, self.end_sample - self.start_sample)

    @property
    def processing_channel_count(self) -> int:
        channel_map = self.source_channel_map
        if channel_map and all(0 <= src < self.channel_count for src, _dst in channel_map):
            return min(2, len(channel_map))
        return min(2, self.channel_count)

    def cache_key(self) -> tuple:
        """结果缓存的稳定标识：源指纹 + 精确范围 + 速率/声道/选道。"""
        return (
            self.source_fingerprint, self.sample_rate, self.channel_count,
            self.start_sample, self.end_sample, self.reverse,
            self.source_channel_map, self.channel_layout, self.source_asset_id,
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
    """区分同路径上的文件替换和原地修改。"""
    try:
        stat = os.stat(path)
        raw = (
            f"{os.path.abspath(path)}:{stat.st_dev}:{stat.st_ino}:"
            f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}"
        )
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


def _bound_source_asset(project: EditProject | None, asset: MediaAsset | None) -> MediaAsset | None:
    binding = binding_for_asset(
        getattr(project, "audio_sync_bindings", None) or {}, getattr(asset, "asset_id", ""))
    if binding is None:
        return None
    source = (getattr(project, "assets", None) or {}).get(binding.wav_asset_id)
    return source if getattr(source, "path", None) else None


def resolve_source_channel_map(project: EditProject | None, asset: MediaAsset | None) -> tuple:
    """P0-2 · 素材生效的源选道：绑定优先，其次工程 patch 覆盖。

    绑定的 ``audio_channel_map`` 只在绑定实际切换音频源（外录 WAV）时
    生效；否则回退 ``source_patch_overrides`` 里该素材的 patch（要求
    ``audio_enabled`` 且显式选道）。与预渲染计划（pcm_render_cache）同
    一解析顺序——片段取样与渲染基底共享同一通道域。返回
    ``((src, dst), ...)``；无选道 = ``()``（直通）。
    """
    from src.features.edit.core.source_patch import patch_for_asset

    asset_id = str(getattr(asset, "asset_id", ""))
    binding = binding_for_asset(
        getattr(project, "audio_sync_bindings", None) or {}, asset_id)
    if (binding is not None and binding.audio_channel_map
            and _bound_source_asset(project, asset) is not None):
        return tuple(binding.audio_channel_map)
    patch = patch_for_asset(getattr(project, "source_patch_overrides", None) or {}, asset_id)
    if patch.audio_enabled and patch.audio_channel_map:
        return tuple(patch.audio_channel_map)
    return ()


def resolve_clip_audio_spec(
    project, timeline, clip, asset, *,
    working_rate: int = AUDIO_REFERENCE_SAMPLE_RATE,
) -> Optional[ClipAudioRenderSpec]:
    """把时间线片段解析为工作采样率下的取样范围。

    复用 ``resolve_audio_source`` / ``bound_audio_frame`` 的绑定与帧域换算
    语义（与 ProjectClipEditor 的波形映射同一条数学路径），保证波形、
    频谱、试听共用同一坐标系。
    """
    if project is None or timeline is None or clip is None or asset is None or working_rate <= 0:
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
    clock = TimelineSampleClock(Fraction(str(source_fps)).limit_denominator(1_000_000), working_rate)
    source_offset = Fraction((clip.source_sample_offset or 0) * working_rate,
                             AUDIO_REFERENCE_SAMPLE_RATE)
    start_sample = max(0, round(clock.frame_start_sample(start_frame) + source_offset))
    end_sample = max(start_sample, round(
        clock.frame_start_sample(start_frame + max(0, clip.source_duration))))
    source_asset = _bound_source_asset(project, asset) or asset
    return ClipAudioRenderSpec(
        project_id=str(getattr(project, "project_id", "") or ""),
        timeline_id=str(timeline.timeline_id),
        clip_id=str(clip.clip_id),
        revision=int(getattr(timeline, "revision", 0) or 0),
        asset_id=str(asset.asset_id),
        source_path=path,
        source_fingerprint=source_fingerprint(path),
        sample_rate=working_rate,
        channel_count=_audio_channel_count(source_asset),
        channel_layout=_audio_channel_layout(source_asset),
        start_sample=start_sample,
        end_sample=end_sample,
        reverse=bool(clip.is_reverse_playback()),
        offset_seconds=offset,
        display_duration=clip.timeline_duration / timeline_fps,
        source_channel_map=resolve_source_channel_map(project, asset),
        source_asset_id=str(source_asset.asset_id),
    )


def resolve_asset_audio_spec(
    project, asset, *,
    working_rate: int = AUDIO_REFERENCE_SAMPLE_RATE,
    duration_seconds: Optional[float] = None,
) -> Optional[ClipAudioRenderSpec]:
    """Resolve the complete logical source extent; callers choose bounded windows."""
    if asset is None or working_rate <= 0:
        return None
    path, offset = resolve_audio_source(project, asset)
    if not path:
        return None
    source_asset = _bound_source_asset(project, asset) or asset
    duration = duration_seconds if duration_seconds is not None else getattr(source_asset, "duration_seconds", None)
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        # Unknown length is not permission to invent a five-minute asset.
        # The asynchronous waveform probe may provide a complete extent later.
        return None
    total = round(duration * working_rate)
    return ClipAudioRenderSpec(
        project_id=str(getattr(project, "project_id", "") or "") if project is not None else "",
        timeline_id="",
        clip_id="",
        revision=0,
        asset_id=str(asset.asset_id),
        source_path=path,
        source_fingerprint=source_fingerprint(path),
        sample_rate=working_rate,
        channel_count=_audio_channel_count(source_asset),
        channel_layout=_audio_channel_layout(source_asset),
        start_sample=0,
        end_sample=total,
        reverse=False,
        offset_seconds=offset,
        display_duration=total / working_rate,
        source_channel_map=resolve_source_channel_map(project, asset),
        source_asset_id=str(source_asset.asset_id),
    )



@dataclass(frozen=True)
class ClipWaveformWindow:
    """A bounded source-only display window, in full-clip local sample coordinates.

    This payload must never be attached as a full audio-edit/audition base.
    """

    spec_key: tuple
    start_sample: int
    end_sample: int
    pcm: np.ndarray


WAVEFORM_WINDOW_MAX_BYTES = 8 * 1024 * 1024


def full_clip_pcm_available(spec: ClipAudioRenderSpec) -> bool:
    """Keep full-stack preview limits explicit until streaming processors exist."""
    duration = max(spec.sample_count / max(1, spec.sample_rate), spec.display_duration)
    return (math.isfinite(duration) and duration <= MAX_CLIP_PCM_SECONDS
            and max(spec.sample_count, round(duration * spec.sample_rate))
            * spec.processing_channel_count * 4 <= MAX_DECODED_PCM_BYTES)


def decode_clip_waveform_window(
    spec: ClipAudioRenderSpec, start_sample: int, end_sample: int, *,
    max_bytes: int = WAVEFORM_WINDOW_MAX_BYTES,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[ClipWaveformWindow]:
    """Read a complete viewport with bounded prefetch, preserving absolute position.

    A viewport too wide for the budget returns no detail: the existing full
    source envelope stays visible. Incomplete files never stretch a partial
    window across the requested interval. Reverse maps before the source read.
    """
    from src.features.edit.core.waveform_pyramid import plan_decode_window

    if source_fingerprint(spec.source_path) != spec.source_fingerprint:
        return None
    plan = plan_decode_window(
        0, spec.sample_count, start_sample, end_sample,
        spec.processing_channel_count, max_bytes=max_bytes,
        prefetch_samples=spec.sample_rate // 2)
    if plan.window_start != max(0, start_sample) or plan.window_end < min(spec.sample_count, end_sample):
        return None
    local_start, local_end = plan.prefetch_start, plan.prefetch_end
    if spec.reverse:
        source_start, source_end = spec.end_sample - local_end, spec.end_sample - local_start
    else:
        source_start, source_end = spec.start_sample + local_start, spec.start_sample + local_end
    pcm = decode_audio_range(
        spec.source_path, source_start, source_end, spec.sample_rate,
        channel_map=spec.source_channel_map or None, cancel_check=cancel_check)
    if (pcm is None or pcm.shape[1] != local_end - local_start
            or source_fingerprint(spec.source_path) != spec.source_fingerprint):
        return None
    if spec.reverse:
        pcm = np.ascontiguousarray(pcm[:, ::-1])
    pcm.setflags(write=False)
    return ClipWaveformWindow(spec.cache_key(), local_start, local_end, pcm)

def _decode_window_frames(
    container: av.container.InputContainer, stream: av.AudioStream,
    start_sample: int, sample_rate: int,
) -> tuple[Iterator[av.AudioFrame], int]:
    """Seek only for sample-addressable PCM WAV, preserving resampler phase.

    Start far enough before the window to prime the filter. Align the seek
    offset to the smallest source period corresponding to an integer number
    of output samples, so separately requested adjacent windows join exactly.
    """
    import av

    source_rate = stream.codec_context.sample_rate or 0
    if (container.format.name == "wav" and (stream.codec_context.name or "").startswith("pcm_")
            and source_rate > 0 and stream.time_base == Fraction(1, source_rate)
            and not stream.start_time):
        phase_period = source_rate // math.gcd(source_rate, sample_rate)
        target_in = start_sample * source_rate // sample_rate
        # swresample's interpolation history is in input samples.
        preroll = 4096 * max(1, (source_rate + sample_rate - 1) // sample_rate)
        seek_in = max(0, target_in - preroll) // phase_period * phase_period
        if seek_in > 0:
            try:
                container.seek(seek_in, stream=stream)
                frames = container.decode(stream)
                first = next(frames)
                if first.pts == seek_in and first.time_base == stream.time_base:
                    return chain((first,), frames), seek_in * sample_rate // source_rate
            except (av.error.FFmpegError, StopIteration):
                pass
            container.seek(0, stream=stream)
    return container.decode(stream), 0


def _pcm_frame_to_float(frame: av.AudioFrame) -> np.ndarray:
    """Unpack a decoded frame, including left-aligned 24-bit samples in s32."""
    data = frame.to_ndarray()
    channels = len(frame.layout.channels)
    if not frame.format.is_planar:
        data = data.reshape(-1, channels).T
    if np.issubdtype(data.dtype, np.integer):
        info = np.iinfo(data.dtype)
        midpoint = np.float32(1 << (info.bits - 1))
        return (data.astype(np.float32) - (midpoint if info.min == 0 else 0)) / midpoint
    return data.astype(np.float32, copy=False)


def decode_audio_range(
    path: str,
    start_sample: int,
    end_sample: int,
    sample_rate: int,
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    channel_map: Optional[tuple] = None,
    stream_index: int | None = None,
) -> Optional[np.ndarray]:
    """Decode a half-open range as planar float32, clipped to available audio.

    Source maps operate in the original channel domain before downmixing.
    Individual mono resamplers preserve independent channels for multichannel
    sources, including WAV files without a declared channel mask. The budget
    applies to the requested decoded PCM, independent of container size.
    """
    import av

    from src.features.edit.core.channel_layout import adapt_channels
    from src.features.edit.core.source_patch import SourcePatchSpec

    start_sample = max(0, start_sample)
    wanted = end_sample - start_sample
    if wanted <= 0 or sample_rate <= 0 or (cancel_check is not None and cancel_check()):
        return None

    try:
        with av.open(path) as container:
            stream = next((item for item in container.streams.audio
                           if stream_index is None or item.index == stream_index), None)
            if stream is None:
                return None
            frames, decoded_start = _decode_window_frames(container, stream, start_sample, sample_rate)
            skip = start_sample - decoded_start
            patch = SourcePatchSpec(audio_channel_map=tuple(channel_map)) if channel_map else None
            direct_resampler: av.AudioResampler | None = None
            mono_resamplers: list[av.AudioResampler] = []
            pcm: np.ndarray | None = None
            collected = 0

            def feed(data: np.ndarray) -> bool:
                nonlocal skip, collected, pcm
                dropped = min(skip, data.shape[1])
                skip -= dropped
                data = data[:, dropped:dropped + wanted - collected]
                if data.shape[1] == 0:
                    return False
                if patch is not None:
                    data = patch.select_channels(data)
                if data.shape[0] > 2:
                    data = adapt_channels(data, 2)
                channels, length = data.shape
                if pcm is None:
                    if wanted * channels * np.dtype(np.float32).itemsize > MAX_DECODED_PCM_BYTES:
                        raise ValueError("Requested audio window exceeds decoded PCM byte budget")
                    pcm = np.empty((channels, wanted), dtype=np.float32)
                pcm[:, collected:collected + length] = data
                collected += length
                return collected == wanted

            def resample_mono(
                resampler: av.AudioResampler, row: np.ndarray | None, rate: int,
            ) -> np.ndarray:
                mono = None
                if row is not None:
                    mono = av.AudioFrame.from_ndarray(
                        np.ascontiguousarray(row).reshape(1, -1), format="fltp", layout="mono")
                    mono.sample_rate = rate
                parts = resampler.resample(mono)
                if not parts:
                    return np.empty(0, dtype=np.float32)
                return np.concatenate([part.to_ndarray()[0] for part in parts])

            def feed_multichannel(raw: np.ndarray | None, rate: int) -> bool:
                rows = [
                    resample_mono(resampler, raw[index] if raw is not None else None, rate)
                    for index, resampler in enumerate(mono_resamplers)
                ]
                # Independent instances see the same number of samples. A
                # discrepancy is a decode failure, not permission to shift channels.
                return feed(np.stack(rows))

            for frame in frames:
                if cancel_check is not None and cancel_check():
                    return None
                if direct_resampler is None and not mono_resamplers:
                    channels = len(frame.layout.channels)
                    if channels <= 2:
                        direct_resampler = av.AudioResampler(
                            format="fltp", layout="mono" if channels == 1 else "stereo",
                            rate=sample_rate)
                    else:
                        mono_resamplers = [
                            av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
                            for _ in range(channels)
                        ]
                if direct_resampler is not None:
                    if any(feed(out.to_ndarray()) for out in direct_resampler.resample(frame)):
                        break
                elif feed_multichannel(_pcm_frame_to_float(frame), frame.sample_rate or sample_rate):
                    break
            else:
                if direct_resampler is not None:
                    for out in direct_resampler.resample(None):
                        if feed(out.to_ndarray()):
                            break
                elif mono_resamplers:
                    feed_multichannel(None, sample_rate)
            if pcm is None or (cancel_check is not None and cancel_check()):
                return None
            return pcm if collected == wanted else pcm[:, :collected].copy()
    except (av.error.FFmpegError, OSError, ValueError) as exc:
        logger.warning("clip pcm decode failed for %s: %s", path, exc)
        return None


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
    cancel_check: Callable[[], bool] | None = None,
) -> list[list[float]]:
    """Hann STFT → 对数频率行、归一化 0-1 的 [time][freq] 矩阵。

    画布把行 0..f_bins-1 映射为 20 Hz..Nyquist 的对数频率，因此这里直接
    产出对数频率桶（每桶取覆盖 FFT bin 的最大能量），避免画布再做二次
    换算。单声道取该声道，立体声取双声道功率和（相位无关）。
    """
    nfft = max(256, 1 << int(max(8, math.log2(nfft))))
    hop = max(1, nfft // 4)
    window = np.hanning(nfft)
    if pcm.shape[1] < nfft:
        pcm = np.pad(pcm, ((0, 0), (0, nfft - pcm.shape[1])))
    windows = np.lib.stride_tricks.sliding_window_view(pcm, nfft, axis=1)[:, ::hop]
    frames = windows.shape[1]
    cols = min(frames, max(1, time_bins))
    edges = np.linspace(0, frames, cols + 1).astype(np.int64)
    aggregated = np.zeros((cols, nfft // 2 + 1), dtype=np.float64)
    # Only materialize a small batch of windowed samples at a time. Summing
    # power before each bin's peak reduction prevents antiphase cancellation.
    batch_size = 64
    for col, (start, end) in enumerate(zip(edges[:-1], edges[1:])):
        for position in range(start, end, batch_size):
            if cancel_check is not None and cancel_check():
                return []
            batch = windows[:, position:min(position + batch_size, end)] * window
            energy = np.square(np.abs(np.fft.rfft(batch, axis=-1))).sum(axis=0)
            np.maximum(aggregated[col], energy.max(axis=0), out=aggregated[col])
    db = 10.0 * np.log10(np.maximum(aggregated, 1e-18))
    nyquist = sample_rate * 0.5
    f_min, f_max = 20.0, max(1000.0, nyquist)
    fft_freqs = np.fft.rfftfreq(nfft, d=1.0 / max(1, sample_rate))
    norm = 1.0 / (-db_floor)
    matrix = np.zeros((cols, freq_bins), dtype=np.float64)
    for band in range(freq_bins):
        lo = f_min * (f_max / f_min) ** (band / freq_bins) if band else 0.0
        hi = f_min * (f_max / f_min) ** ((band + 1) / freq_bins)
        mask = (fft_freqs >= lo) & (fft_freqs < hi)
        if mask.any():
            matrix[:, band] = np.clip(db[:, mask].max(axis=1) * norm, 0.0, 1.0)
    return matrix.tolist()


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
    """Decode the latest request and cooperatively cancel superseded work."""

    finished_pcm = pyqtSignal(int, object)
    failed = pyqtSignal(int, tuple, str)

    def __init__(self) -> None:
        super().__init__()
        self._condition = threading.Condition()
        self._job: tuple[int, ClipAudioRenderSpec] | None = None
        self._request_id: int | None = None
        self._stopping = False

    def submit(self, request_id: int, spec: ClipAudioRenderSpec) -> None:
        with self._condition:
            self._request_id = request_id
            self._job = (request_id, spec)
            self._condition.notify()
        if not self.isRunning():
            self.start()

    def cancel_all(self) -> None:
        with self._condition:
            self._request_id = None
            self._job = None

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._request_id = None
            self._job = None
            self._condition.notify()

    def run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._job is not None)
                if self._stopping:
                    return
                request_id, spec = self._job
                self._job = None

            def canceled() -> bool:
                with self._condition:
                    return self._request_id != request_id

            try:
                pcm = decode_audio_range(
                    spec.source_path, spec.start_sample, spec.end_sample,
                    spec.sample_rate, cancel_check=canceled,
                    channel_map=spec.source_channel_map or None,
                )
            except Exception as exc:  # noqa: BLE001 - worker boundary, report the failure
                if not canceled():
                    self.failed.emit(request_id, spec.cache_key(), str(exc))
                continue
            if canceled():
                continue
            if pcm is None:
                self.failed.emit(request_id, spec.cache_key(), "")
                continue
            if spec.reverse:
                pcm = np.ascontiguousarray(pcm[:, ::-1])
            channels = int(pcm.shape[0])
            self.finished_pcm.emit(request_id, ClipPcmResult(
                spec_key=spec.cache_key(),
                pcm=pcm,
                sample_rate=spec.sample_rate,
                channel_count=channels,
                channel_layout="mono" if channels == 1 else "stereo",
            ))


class ClipPcmProvider(QObject):
    """Deliver the newest PCM request and cache by both count and byte size."""

    pcm_ready = pyqtSignal(object)
    pcm_failed = pyqtSignal(tuple, str)

    _instance: "ClipPcmProvider | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: OrderedDict[tuple, ClipPcmResult] = OrderedDict()
        self._cache_bytes = 0
        self._worker = _ClipPcmWorker()
        self._worker.finished_pcm.connect(self._on_worker_pcm)
        self._worker.failed.connect(self._on_worker_failed)
        self._pending: dict[tuple, list[Callable[[ClipPcmResult], None]]] = {}
        self._request_id = 0
        self._active_key: tuple | None = None
        self._closed = False
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    @classmethod
    def instance(cls) -> "ClipPcmProvider":
        with cls._instance_lock:
            if cls._instance is None or cls._instance._closed:
                cls._instance = ClipPcmProvider()
            return cls._instance

    def get_cached(self, spec: ClipAudioRenderSpec) -> Optional[ClipPcmResult]:
        key = spec.cache_key()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
        return cached

    def request(
        self, spec: ClipAudioRenderSpec,
        callback: Optional[Callable[[ClipPcmResult], None]] = None,
    ) -> bool:
        """Start a request, delivering cache hits inline as before."""
        if self._closed:
            raise RuntimeError("Clip PCM provider is shut down")
        key = spec.cache_key()
        cached = self.get_cached(spec)
        if cached is not None:
            self.cancel_request(self._active_key)
            if callback is not None:
                callback(cached)
            return True
        if key != self._active_key:
            self._pending.clear()
            self._request_id += 1
            self._active_key = key
            self._worker.submit(self._request_id, spec)
        if callback is not None:
            self._pending.setdefault(key, []).append(callback)
        return False

    def cancel_request(
        self, key: tuple | None,
        callback: Callable[[ClipPcmResult], None] | None = None,
    ) -> None:
        """Release one callback, or cancel the entire current request by key."""
        if key is None or self._active_key != key:
            return
        if callback is not None:
            callbacks = self._pending.get(key, ())
            if callback not in callbacks:
                return
            remaining = [registered for registered in callbacks if registered != callback]
            if remaining:
                self._pending[key] = remaining
                return
        self._active_key = None
        self._pending.pop(key, None)
        self._worker.cancel_all()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._active_key = None
        self._cache.clear()
        self._cache_bytes = 0
        self._worker.stop()
        if not self._worker.wait(1000):
            logger.warning("clip PCM decode worker has not stopped")

    def _on_worker_failed(self, request_id: int, key: tuple, error: str) -> None:
        if request_id != self._request_id or key != self._active_key:
            return
        self._active_key = None
        self._pending.pop(key, None)
        self.pcm_failed.emit(key, error)

    def _on_worker_pcm(self, request_id: int, result: ClipPcmResult) -> None:
        key = result.spec_key
        if request_id != self._request_id or key != self._active_key:
            return
        self._active_key = None
        nbytes = result.pcm.nbytes
        if nbytes <= PCM_CACHE_MAX_BYTES:
            while self._cache and (len(self._cache) >= PCM_CACHE_CAPACITY
                                   or self._cache_bytes + nbytes > PCM_CACHE_MAX_BYTES):
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= evicted.pcm.nbytes
            self._cache[key] = result
            self._cache_bytes += nbytes
        callbacks = self._pending.pop(key, ())
        for callback in callbacks:
            try:
                callback(result)
            except Exception:  # noqa: BLE001 - callback failures must not suppress other clients
                logger.exception("clip PCM callback failed")
        self.pcm_ready.emit(result)
