"""
Edit Page GPU Decode Manager.

管理编辑页面每个 clip 的解码源。支持四种解码路径：
1. 磁盘代理: DiskProxyService 生成的 proxy 文件 (时间线预览优先)
2. GPU 零拷贝: MacOSVtZeroCopySource → VT PixelBuffer → renderer GL import (H264/H265)
3. CPU Planar YUV: FFmpegMediaSource(read_frame_planar) → DecodedFrame (ProRes/DNxHR/VP8)
4. CPU Packed RGB: FFmpegMediaSource(read_frame) → DecodedFrame (其他格式回退)

对于 ProRes/DNxHR/VP8 等 codec，使用 planar 模式解码保留 YUV planes，
跳过 CPU 端 YUV→RGB 转换，后续通过 YUVGPUUploader 上传到 GPU shader 处理。

注意：当前 macOS VideoToolbox/FFmpeg 硬解路径不提供 VP8 的可用 IOSurface
输出，因此 VP8 的“全链路 GPU”边界是 CPU 解压缩、GPU 纹理上传、GPU
YUV→RGB/缩放/合成；不会把不支持的 VP8 硬解误报为硬件解码。
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, Tuple

from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.core.realtime_playback.decode_metrics import (
    DecodeMetrics,
)
from src.shared.media.hwaccel import HardwareCapability
from src.shared.media.seek import estimate_seek_cost, is_intra_codec

logger = logging.getLogger(__name__)
_SEQUENTIAL_CATCHUP_MAX_FRAMES = 48
_PREVIEW_TARGET_WIDTH_BUCKET = 64
_WARMUP_SOURCE_WAIT_SECONDS = 2.0
_INTERACTIVE_DECODE_MIN_DEADLINE_MS = 24.0
_INTERACTIVE_DECODE_MAX_DEADLINE_MS = 160.0
_INTERACTIVE_SOURCE_POOL_SIZE = 3
_INTERACTIVE_POOL_FORWARD_MAX_FRAMES = 120
_INTERACTIVE_PROGRESS_MAX_DISTANCE = 2

# P2-09 会话池上限：主解码会话（_sources）的最大存活数。覆盖典型工程
# （~5 条可见轨 × 播放/scrub 两种 vt_policy 会话）并留余量；超限时按 LRU
# 逐出空闲（租约为 0）的最旧会话。VT 会话的创建/销毁会短暂阻塞
# VideoToolbox 子系统，所以单次插入最多逐出 _MAX_EVICTIONS_PER_INSERT 个，
# 避免一次性关闭风暴把正在播放的解码流也卡住。
_MAX_LIVE_SOURCES = 12
_MAX_EVICTIONS_PER_INSERT = 2
# P2-09 关闭路径（close_source/close_all/invalidate_all_sources）的租约
# 排水超时：与 _join_playback_prefetches 的 join 预算对齐。超时后仍在用
# 的会话不立即 close，改为推迟到租约归零（_deferred_close_sources），
# 而不是冒着 C 层 use-after-free 的风险强行关闭。
_CLOSE_LEASE_DRAIN_TIMEOUT = 2.0
# 负缓存（创建失败 key）的规模上限：超限整体清空。它只用于避免播放期间
# 逐帧重试注定失败的 av.open；画质切换/整页关闭后文件可能已修复，必须
# 允许重新探测，否则一个坏 key 会把素材永久钉死在 CPU 回退上。
_MAX_FAILED_SOURCE_KEYS = 256

# W03（计划 W-I02）运行中驻留源失败的按素材降级：
# - 渲染 GL 线程 cuda 驻留导入失败 → note_resident_import_failure 按素材
#   记账并停用该素材的新驻留请求（不再创建 win_cuda_resident 源），在飞
#   租约归零后关闭旧驻留源；下一次 get_frame 自然重建下载 planar/CPU 源。
# - 与 B2.1 全局导入门闩（连续 3 次停所有新源路由）互补：这里是单素材
#   故障隔离——一个坏源持续黑帧/反复失败不应拖垮其它正常驻留源。
# - 限次重试：每次降级记一个 cycle；cycle 数在冷却窗口
#   （_RESIDENT_DEMOTE_COOLDOWN_S，到期后允许重新探测驻留）内累计，
#   超过 _RESIDENT_MAX_DEMOTE_CYCLES 后本会话内不再自动重试，只能经
#   reset_resident_demotions（诊断/显式恢复）或 close_all（新会话）清除。
_RESIDENT_DEMOTE_COOLDOWN_S = 300.0
_RESIDENT_MAX_DEMOTE_CYCLES = 2

# A 4K 10/12-bit intra frame costs considerably more to resample on the CPU
# than it does to sample down in the existing YUV -> RGB GPU pass.  Keep this
# intentionally narrow: it is a playback optimisation for the professional
# intra codecs whose high-bandwidth 4K masters are otherwise CPU-resized for
# every visible timeline layer.  Long-GOP camera codecs already have a native
# VideoToolbox path and must not be routed through this policy.
_NATIVE_GPU_SCALE_CODECS = frozenset({
    'prores', 'prores_ks', 'prores_aw',
    'dnxhr', 'dnxhd',
})
_NATIVE_GPU_SCALE_MIN_TARGET_PIXELS = 1920 * 1080
_NATIVE_GPU_SCALE_MIN_REDUCTION_RATIO = 2.0

_H264_H265_CODECS = frozenset({
    'h264', 'h265', 'hevc', 'avc', 'avc1', 'h264_mpeg',
    'libx264', 'libx265', 'mpeg4', 'xvid',
})

# Intra-frame / visually lossless codecs that benefit from planar decode
# + GPU YUV upload (skip CPU-side YUV→RGB, let GPU shader handle it).
#
# Categories:
#   ProRes family — Apple professional codec, all variants
#   DNxHR/DNxHD  — Avid professional codec
#   XAVC-Intra    — Sony professional intra codec
#   AVC-Intra     — Panasonic professional intra codec
#   CineForm      — GoPro professional codec (wavelet, high bit-depth)
#   JPEG 2000     — D-Cinema / IMF / broadcast standard
#   FFV1          — Lossless archival codec
#   Utvideo       — Lossless Matroska codec
#   HuffYUV / Lagarith — Lossless legacy codecs
#   V210 / V410   — uncompressed 10-bit YUV (broadcast)
#   r210 / r10k   — uncompressed 10-bit RGB (broadcast)
#   VP8           — WebM 长 GOP；macOS 无可用 VideoToolbox VP8 输出时，
#                   保留 yuv420p 平面，交给 GPU 做色彩转换/缩放/合成
_PLANAR_PREFERRED_CODECS = frozenset({
    # ProRes
    'prores', 'prores_ks', 'prores_aw',
    'prores_proxy', 'prores_lt', 'prores_standard', 'prores_hq',
    'prores422', 'prores4444', 'prores4444xq',
    # DNxHR / DNxHD
    'dnxhr', 'dnxhd', 'dnxhr_lb', 'dnxhr_sq', 'dnxhr_hq', 'dnxhr_hqx', 'dnxhr_444',
    # Sony XAVC (intra-only variants; XAVC-S is long-GOP, handled by GPU path)
    'xavc', 'xavc-intra', 'xavc-i',
    # Panasonic AVC-Intra
    'avc-intra', 'avcintra', 'avc_intra',
    # CineForm
    'cineform', 'cfhd',
    # JPEG 2000 (D-Cinema / IMF)
    'jpeg2000', 'j2k', 'libopenjpeg', 'jpeg2000_lib',
    # Lossless
    'ffv1', 'utvideo', 'huffyuv', 'huffuyv', 'lagarith',
    # Uncompressed broadcast
    'v210', 'v410', 'v308', 'r210', 'r10k', 'ayuv',
    # AV1 All-Intra (future)
    'av1', 'libaom-av1',
    # VP8 WebM: FFmpeg 解压缩后保留 yuv420p，避免 CPU packed RGB 转换。
    # VideoToolbox 当前没有可用的 VP8 zero-copy/IOSurface 路径。
    'vp8', 'libvpx',
})


_SourceKey = Tuple[str, str, str, Optional[Tuple[int, int]], str]


@dataclass(frozen=True)
class _DecodeSourceSpec:
    original_path: str
    decode_path: str
    target_size: Optional[Tuple[int, int]]
    source_kind: str
    # 'auto': scrub/interactive default — VT zero-copy whenever supported.
    # 'prefer_ffmpeg': sequential playback when the VT hardware-decode probe
    # says VideoToolbox would run software for this codec/bit-depth (FFmpeg
    # frame-threading + planar GPU upload is several times faster there).
    vt_policy: str = 'auto'
    # 预览跳过输出去块（用户偏好 video_preview_no_deblock）：只对 Topos native
    # 平面路径有效，且只有带输出特性位的流可见。进 _SourceKey → 切换开关自然
    # 换一套 source（不误用旧 flag 的 request）。
    no_deblock: bool = False


class EditGpuDecodeManager:
    """
    Decode source manager for edit page timeline preview.

    Decode path priority:
    1. Ready disk proxy for timeline preview
    2. GPU zero-copy deferred import (MacOSVtZeroCopySource) for H264/H265
    3. CPU planar YUV for ProRes/DNxHR (avoids CPU YUV→RGB)
    4. CPU packed fallback for other formats

    Thread safety: decode calls happen on the PreviewRenderWorker thread.
    """

    def __init__(self) -> None:
        self._no_deblock = False
        self._sources: Dict[_SourceKey, Any] = {}
        self._source_types: Dict[_SourceKey, str] = {}
        self._lock = threading.Lock()
        # M16.2：分域指标（锁等待/解码时长/缓存命中/重复开源——离屏
        # harness 与真实窗口复测同一口径，纯计数零行为影响）
        self._metrics = DecodeMetrics()
        # 创建失败过的 source key（负缓存）：避免播放期间每帧重试注定失败的
        # av.open（Topos/损坏文件曾以每帧一次的频率刷 "CPU source failed"）。
        self._failed_source_keys: set = set()
        # Cache for hot-path lookups (avoid lock on every get_frame call)
        # These are set once during source creation and never mutated.
        self._source_cache: Dict[_SourceKey, Tuple[Any, str]] = {}
        # (stream_key, original_path, decode_path, decode_target_size) -> (source, source_type)
        # Cached codec probe results: asset_path → codec_name.
        # Avoids redundant av.open() calls in _probe_codec.
        self._codec_cache: Dict[str, str] = {}
        # Container sniff result: asset_path → is Topos MOV. The bounded
        # signature read happens before generic codec probes on source setup.
        self._topos_movie_cache: Dict[str, bool] = {}
        # Source dimensions are needed when deciding whether non-H26x native
        # planar fallback would defeat preview downscaling.
        self._resolution_cache: Dict[str, Tuple[int, int]] = {}
        # Source pixel formats feed the playback bit-depth routing decision.
        self._pixfmt_cache: Dict[str, str] = {}
        # Assets already traced for the playback prefer-ffmpeg route (log once).
        self._playback_route_logged: set[str] = set()
        # Source warmup tasks are keyed exactly like decode sources. This avoids
        # duplicate VT session creation when timeline prewarm and first playback
        # request hit the same clip at the same time.
        self._warmup_events: Dict[_SourceKey, threading.Event] = {}
        self._warmup_generation: int = 0
        self._metadata_warmups: set[str] = set()
        self._interactive_source_pool: Dict[_SourceKey, list[Tuple[Any, str]]] = {}
        # 播放期间不构建交互（scrub）解码池：每个流 +2 个 VT 会话的创建
        # 会短暂阻塞 VideoToolbox 子系统（含正在播放的解码流），造成播放
        # 中段的解码尖峰。暂停后的下一次交互访问会重新触发预热。
        self._playback_active: bool = False
        self._playback_pool_warmup_deferred: set = set()
        self._interactive_pool_warmup_events: Dict[_SourceKey, threading.Event] = {}
        # P2-09 交互预约改为 id(source) → source 本体：旧实现是 set[int]，
        # source 关闭后 id() 可能被新分配的对象复用，导致健康的新会话被
        # 误判为 busy 而永远跳过。字典存本体后用身份比较判断。
        self._interactive_busy_sources: Dict[int, Any] = {}
        # P2-09 引用租约（source_key → 在用计数）：所有从缓存取出 source 并
        # 在 self._lock 之外解码的路径（显示/交互/预置位/预取线程）都必须在
        # 取出的同一把锁内 +1、finally 里 -1。逐出只回收租约为 0 的会话，
        # 杜绝"解码中途会话被关闭"的 C 层 use-after-free。
        self._source_refs: Dict[_SourceKey, int] = {}
        # P2-09 关闭路径排水超时仍在用的会话（source_key → [主源, *池克隆]）：
        # close_source/close_all/invalidate_all_sources 与逐出共用同一租约
        # 契约，未归零的单元登记在此，由最后一个 _release_source 在锁外补
        # 关闭——三条关闭路径同样绝不在解码中途关闭会话。
        self._deferred_close_sources: Dict[_SourceKey, list] = {}
        # P2-09 LRU 时钟（source_key → monotonic last_used）：超限时最旧优先。
        self._source_last_used: Dict[_SourceKey, float] = {}
        # P2-09 诊断计数：被逐出的会话总数（bench/§22.3 证据用）。
        self._eviction_count: int = 0
        self._interactive_prefetch_tokens: Dict[_SourceKey, int] = {}
        self._interactive_prefetch_events: set[threading.Event] = set()
        self._interactive_prefetch_generation: int = 0
        # Per-stream decode locks: serialize the display decode (get_frame)
        # against background stream prepositioning on the SAME clip stream.
        # Different clip streams keep independent locks, so parallel
        # multi-track decode is unaffected.
        self._stream_locks: Dict[_SourceKey, threading.Lock] = {}
        # source_key -> event set while a background preposition decode runs.
        self._preposition_inflight: Dict[_SourceKey, threading.Event] = {}
        # source_key set while a playback lookahead prefetch thread runs
        # (dedup: at most one prefetch per stream at any time).
        self._playback_prefetch_pending: set = set()
        # 播放预取线程注册表：close/invalidate 销毁 VT 会话前 join，
        # 防止 C 层解码 use-after-free（bench teardown SIGSEGV 复现）。
        self._playback_prefetch_threads: list = []
        self._playback_prefetch_lock = threading.Lock()
        # AUD-16501：交互预置位线程注册表（(asset_path, thread) 列表，与
        # 播放预取共用 _playback_prefetch_lock 保护）。旧实现只依赖
        # 0.12–0.3s 的协作式事件等待，预置位线程阻塞在单次 C 层解码内
        # （8K/长 GOP 单帧可超 0.3s）时，close 会销毁它正在使用的池克隆
        # 源——与播放预取的 2s join 保护不一致的 C 层 use-after-free。
        self._interactive_preposition_threads: list[Tuple[str, threading.Thread]] = []
        self._closing: bool = False
        # 最近一次交互读（scrub）时间：拖拽期间抑制预取，避免预取线程
        # 与交互读争抢同一 source_key 的 stream lock（实测拖拽掉 ~10-16%）。
        self._last_interactive_read_at: float = 0.0
        # W03 按素材驻留降级记账：asset_path →
        # {"cycles": 累计降级次数, "until": 冷却截止 monotonic 时间
        #  （cycles 超上限时为 math.inf）, "last_reason": 最近失败原因,
        #  "last_failed_at": 最近失败时间, "recoveries": 重建次数}
        self._resident_demoted: Dict[str, Dict[str, Any]] = {}
        # 正在执行降级关闭的素材（后台线程 dedup：同一素材的连续失败只
        # 排一次"关源"作业，避免重复 join/drain）。
        self._resident_demotion_pending: set[str] = set()
        # 交互优先级门控（P1.5）：交互读（scrub/seek 显示路径）在临界区
        # 期间递增 pending 计数；后台预取/预置位通过让位式获取流锁——
        # pending > 0 时放弃，长 GOP 前解中途也会经 is_stale 提前中止。
        # 这把"后台批量解码与交互解码争锁"从时间窗启发（0.5s 静默期）
        # 升级为显式优先级：显示请求永远先于预热。
        self._interactive_pending: int = 0

    def get_frame(
        self,
        asset_path: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
    ) -> Tuple[Optional[Any], str]:
        """
        Get a decoded frame for the given asset at the given frame index.

        Args:
            asset_path: Absolute path to the media file
            frame_index: Frame number to decode (0-based)

        Returns:
            (frame, frame_type) tuple:
            - frame: VtPixelBufferFrame or GpuDecodedFrame or DecodedFrame or None
            - frame_type: 'vt_pixel_buffer' | 'vt_planar' | 'gpu' | 'planar' | 'cpu' | ''
        """
        normalized_target_size = self._normalize_target_size(target_size)
        source_spec = self._resolve_preview_decode_source(
            asset_path, normalized_target_size, for_playback=True,
        )
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)

        # Hot path: use cached source info. The cache itself is protected
        # because multi-track preview can decode layer streams in parallel.
        # P2-09：取出即加租约，解码期间本会话不会被 LRU 逐出关闭。
        with self._lock:
            cached = self._source_cache.get(source_key)
            if cached is not None:
                self._lease_source(source_key)
        if cached is not None:
            source, source_type = cached
            start = time.perf_counter()
            self._metrics.record_cache("display", hit=True)
            # The stream lock makes a concurrent background preposition of
            # this very clip stream finish first; its decode-forward work is
            # exactly what this display read needs, so the wait is productive
            # and the boundary frame then decodes sequentially.
            try:
                with DecodeMetrics.timed_stream_lock(
                        self._stream_lock(source_key), "display",
                        self._metrics):
                    result = self._decode_frame(source, source_type, frame_index, source_spec.target_size, start)
                    self._metrics.record_decode(
                        "display",
                        time.perf_counter_ns() - int(start * 1e9))
            finally:
                self._release_source(source_key)
            self._kick_playback_prefetch(source, source_key, frame_index, result)
            return result

        self._metrics.record_cache("display", hit=False)
        # Slow path: create source (requires lock). P2-09：返回的 source 带一个
        # 租约，解码完成后归还。
        source = self._get_or_create_source(
            asset_path,
            normalized_target_size,
            decode_stream_key,
            source_spec=source_spec,
        )
        if source is None:
            playback_trace(
                "decode_manager.get_frame_no_source",
                frame=frame_index,
                asset=asset_path,
                decode_asset=source_spec.decode_path,
                source_kind=source_spec.source_kind,
                target_size=normalized_target_size,
            )
            return None, ''

        with self._lock:
            already = self._source_cache.get(source_key)
            if already is not None:
                # M16.2：慢路径竞态——另一线程已把源放入缓存（重复开源）
                self._metrics.record_duplicate_source_open()
            source_type = self._source_types.get(source_key, 'cpu')
            self._source_cache[source_key] = (source, source_type)
        start = time.perf_counter()
        try:
            with DecodeMetrics.timed_stream_lock(
                    self._stream_lock(source_key), "display",
                    self._metrics):
                result = self._decode_frame(source, source_type, frame_index, source_spec.target_size, start)
                self._metrics.record_decode(
                    "display",
                    time.perf_counter_ns() - int(start * 1e9))
        finally:
            self._release_source(source_key)
        self._kick_playback_prefetch(source, source_key, frame_index, result)
        return result

    def _kick_playback_prefetch(
        self,
        source: Any,
        source_key: _SourceKey,
        frame_index: int,
        result: Tuple[Optional[Any], str],
    ) -> None:
        """播放成功解码后，后台预取该流的下一顺序帧。

        预取帧进入 VT 源的 reorder 缓冲（见
        MacOSVtZeroCopySource.prefetch_sequential_frame），下一帧的解码波
        通过精确命中直接弹出，把按需解码从关键路径上移走。约束：
        - 仅 vt_pixel_buffer 精确命中帧才预取（planar/CPU 路径不适用）
        - 每条流同时最多一个预取线程（dedup）
        - 预取线程与显示解码共用同一把 stream lock，串行等待即合并
        - 速度≠1.0 的剪辑下一帧可能不是 S+1：预取帧不被命中时由
          reorder 淘汰机制自然释放，只浪费一次解码，无正确性影响
        """
        frame, frame_type = result
        if frame_type != 'vt_pixel_buffer' or frame is None:
            return
        if getattr(frame, 'frame_index', frame_index) != frame_index:
            return
        if not hasattr(source, 'prefetch_sequential_frame'):
            return
        # 拖拽刚结束的窗口内不预取：交互读与主源共用 stream lock，
        # 预取会与 scrub 解码争抢（实测拖拽 fps 掉 ~10-16%）。
        if time.perf_counter() - self._last_interactive_read_at < 0.5:
            return
        # AUD-16502：检查+登记在 self._lock 同一临界区内完成（并与
        # _lease_source 原子化）。旧的无锁 check-then-add 在多轨并行解码
        # 下可让两个线程同时通过 `in` 检查、为同一 source_key 各起一个
        # 预取线程，破坏"每条流同时最多一个预取线程"的 dedup 不变量。
        with self._lock:
            if source_key in self._playback_prefetch_pending:
                return
            if self._closing:
                return
            self._playback_prefetch_pending.add(source_key)
            # P2-09：预取线程持有租约直到退出，逐出不会关掉它正在用的会话。
            self._lease_source(source_key)

        def _run_prefetch() -> None:
            try:
                _pf_t0 = time.perf_counter_ns()
                if not self._acquire_stream_lock_background(source_key, timeout_s=0.2):
                    playback_trace(
                        "decode_manager.playback_prefetch_yield_interactive",
                        frame=frame_index + 1,
                    )
                    return
                try:
                    self._metrics.record_lock_wait(
                        "prefetch",
                        time.perf_counter_ns() - _pf_t0)
                    source.prefetch_sequential_frame(frame_index + 1)
                    self._metrics.record_decode(
                        "prefetch", time.perf_counter_ns() - _pf_t0)
                finally:
                    self._stream_lock(source_key).release()
            except Exception as exc:
                # AUD-16505：预取失败必须留痕——旧实现 except Exception: pass
                # 完全静默，违反"不得宽泛 except 掩盖领域错误"约束。预取是
                # 尽力而为的后台优化（失败只退化为下一帧按需解码），因此
                # 记录后不上抛、不重试。
                logger.warning(
                    "EditGpuDecodeManager: playback prefetch frame %d failed: %s",
                    frame_index + 1,
                    exc,
                )
                playback_trace(
                    "decode_manager.playback_prefetch_error",
                    frame=frame_index + 1,
                    error=exc,
                )
            finally:
                self._playback_prefetch_pending.discard(source_key)
                self._release_source(source_key)
                with self._playback_prefetch_lock:
                    try:
                        self._playback_prefetch_threads.remove(threading.current_thread())
                    except ValueError:
                        pass

        t = threading.Thread(
            target=_run_prefetch,
            name=f"PlaybackPrefetch-{frame_index + 1}",
            daemon=True,
        )
        # 注册线程：close_all/invalidate 在销毁 VT 会话前必须等它退出，
        # 否则 C 层解码使用已销毁的 session → SIGSEGV（bench teardown 复现）。
        with self._playback_prefetch_lock:
            self._playback_prefetch_threads.append(t)
        t.start()

    def _join_playback_prefetches(self, timeout: float = 2.0) -> None:
        """等待所有在飞的播放预取线程退出（防 close 后 C 层 use-after-free）。"""
        deadline = time.perf_counter() + float(timeout)
        while True:
            with self._playback_prefetch_lock:
                threads = list(self._playback_prefetch_threads)
            if not threads:
                return
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                logger.warning(
                    "EditGpuDecodeManager: playback prefetch join timeout "
                    f"({len(threads)} thread(s) still running)"
                )
                return
            for t in threads:
                if t is threading.current_thread():
                    continue
                t.join(timeout=max(0.01, remaining / max(1, len(threads))))

    def _join_interactive_prepositions(
        self,
        timeout: float = 2.0,
        *,
        asset_path: Optional[str] = None,
    ) -> None:
        """AUD-16501：等待在飞的交互预置位线程退出（防 close 后 C 层 use-after-free）。

        与播放预取的 2s join 同强度；``asset_path`` 给定时只 join 该素材
        的线程（close_source 用），None 表示全量（close_all/invalidate 用）。
        取消信号（generation bump / token 失效）已由调用方先行发出，线程
        会协作式尽快返回；join 只是给阻塞在单次 C 层解码内的线程留出
        有界收尾窗口。
        """
        deadline = time.perf_counter() + max(0.0, float(timeout))
        while True:
            with self._playback_prefetch_lock:
                # 防御性摘除已退出的线程（注册方异常路径未自清理时不能
                # 让 join 循环对着死条目空转到超时）。
                self._interactive_preposition_threads = [
                    (asset, t) for asset, t in self._interactive_preposition_threads
                    if t.is_alive() or t is threading.current_thread()
                ]
                threads = [
                    t for asset, t in self._interactive_preposition_threads
                    if asset_path is None or asset == asset_path
                ]
            if not threads:
                return
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                logger.warning(
                    "EditGpuDecodeManager: interactive preposition join "
                    f"timeout ({len(threads)} thread(s) for "
                    f"{asset_path or 'all sources'} still running)"
                )
                return
            for t in threads:
                if t is threading.current_thread():
                    continue
                t.join(timeout=max(0.01, remaining / max(1, len(threads))))

    # ------------------------------------------------------------------
    # Final-2 (FINAL-P0-02): policy-aware decode entrypoint.
    # 当 RealtimePlaybackEngine 已对该 layer 做出 SourceDecision 时，走这里：
    # - source_kind == PLACEHOLDER/NONE 仅记录 telemetry
    #   （decode_manager.realtime_no_proxy_fallback_decode），**不阻塞解码、
    #   不返回特殊 frame_type**（AUD-16504：Final-2 修正后拒绝行为已删除，
    #   本段注释同步订正——旧文描述的"拒绝并返回占位标记"契约与实现矛盾，
    #   依赖它会误判 realtime 阻塞语义）。
    # - source_kind == DISK_PROXY/RENDER_CACHE 时正常解码（已通过 spec 解析走代理）。
    # - source_kind == MASTER 时只有在非 realtime 才允许。
    # - "无代理 → original_downscaled" 的回退由 get_frame 内部的
    #   _resolve_preview_decode_source 处理，对 H264/H265 流畅。
    # ------------------------------------------------------------------

    def get_frame_with_policy(
        self,
        asset_path: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
        source_decision: Optional[Any] = None,
    ) -> Tuple[Optional[Any], str]:
        """
        Policy-aware decode: gate master decode using a SourceDecision.

        Args:
            asset_path: original media path
            frame_index: 0-based source frame
            target_size: decode resolution bucket
            stream_key: clip-identity key for cursor isolation
            source_decision: realtime_playback SourceDecision (may be None).

        Returns:
            (frame, frame_type). PLACEHOLDER/NONE 的 SourceDecision 只产生
            telemetry，**不改变返回值**——解码正常时 frame 非 None、
            frame_type 由底层 get_frame 决定；只有真正无法解码（asset 打不开）
            时 frame 才是 None（AUD-16504：旧的 "denied → frame_type ==
            'placeholder'" 契约已随 Final-2 拒绝路径一并删除）。

        Final-2 修正：原实现只要 SourceDecision 返回 PLACEHOLDER 就拒绝解码，
        但这会让"没有预生成代理的普通 H264/H265 素材"在 realtime 模式下
        每帧都被拒绝，导致画面完全不动。正确的行为是：
        - get_frame 内部的 _resolve_preview_decode_source 已经能处理
          "无代理 → 原始文件降采样 (original_downscaled)" 的回退路径，
          这条路径对 H264/H265 是流畅的（实测 100-800fps）。
        - SourceDecision 的 PLACEHOLDER 标记保留作为 telemetry，
          记录"此帧走了降采样 master 而非专业代理路径"，但不阻塞解码。
        - 只有真正无法解码的情况（asset 打不开）才会返回 None。
        """
        if source_decision is not None:
            sk = getattr(source_decision, "source_kind", None)
            sk_val = getattr(sk, "value", sk)
            # 仅记录 telemetry，不阻塞解码。真正的 master 阻止应该在
            # 更上层（proxy/cache 闭环）按格式/分辨率策略决定，而不是
            # 一刀切拒绝所有无代理素材。
            if sk_val in ("placeholder", "none"):
                playback_trace(
                    "decode_manager.realtime_no_proxy_fallback_decode",
                    frame=frame_index,
                    asset=asset_path,
                    source_kind=sk_val,
                    reason=getattr(source_decision, "reason", ""),
                )

        return self.get_frame(asset_path, frame_index, target_size=target_size, stream_key=stream_key)

    def get_frame_interactive(
        self,
        asset_path: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
        *,
        is_stale: Optional[Callable[[], bool]] = None,
        deadline_at: Optional[float] = None,
        coarse: bool = False,
    ) -> Tuple[Optional[Any], str]:
        """
        Decode a frame for interactive scrub, aborting work that is already stale.

        This is intentionally compatible with existing sources. If a source
        implements an interactive read method, it receives ``is_stale`` and
        ``deadline_at`` so long GOP decode-forward loops can yield early. Older
        sources still run through the normal read path, with stale/deadline
        checks before expensive source creation and direct seek.
        """
        # 记录交互读时间窗：拖拽期间 _kick_playback_prefetch 抑制预取，
        # 避免预取线程与交互读争抢同一 source_key 的 stream lock。
        self._last_interactive_read_at = time.perf_counter()
        normalized_target_size = self._normalize_target_size(target_size)
        source_spec = self._resolve_preview_decode_source(asset_path, normalized_target_size)
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)

        if self._interactive_decode_should_abort(is_stale, deadline_at):
            playback_trace(
                "decode_manager.interactive_abort_before_source",
                frame=frame_index,
                asset=asset_path,
            )
            return None, 'interactive_stale'

        with self._lock:
            cached = self._source_cache.get(source_key)
            source = self._sources.get(source_key)
            source_type = self._source_types.get(source_key, 'cpu')
            warmup_event = self._warmup_events.get(source_key)
            if cached is not None or source is not None:
                self._lease_source(source_key)

        if cached is not None:
            source, source_type = cached
        elif source is None and warmup_event is not None:
            timeout = self._interactive_warmup_wait_timeout(deadline_at)
            if timeout <= 0.0 or not warmup_event.wait(timeout):
                playback_trace(
                    "decode_manager.interactive_warmup_pending",
                    frame=frame_index,
                    asset=asset_path,
                    wait_ms=timeout * 1000.0,
                )
                return None, 'interactive_stale'
            with self._lock:
                cached = self._source_cache.get(source_key)
                source = self._sources.get(source_key)
                source_type = self._source_types.get(source_key, 'cpu')
                if cached is not None or source is not None:
                    self._lease_source(source_key)
            if cached is not None:
                source, source_type = cached

        if source is None:
            if self._interactive_decode_should_abort(is_stale, deadline_at):
                playback_trace(
                    "decode_manager.interactive_abort_before_create",
                    frame=frame_index,
                    asset=asset_path,
                )
                return None, 'interactive_stale'
            self.warmup_source(
                asset_path,
                target_size=normalized_target_size,
                stream_key=decode_stream_key,
            )
            with self._lock:
                warmup_event = self._warmup_events.get(source_key)
            timeout = self._interactive_warmup_wait_timeout(deadline_at)
            if warmup_event is not None and timeout > 0.0:
                warmup_event.wait(timeout)
            with self._lock:
                cached = self._source_cache.get(source_key)
                source = cached[0] if cached is not None else self._sources.get(source_key)
                source_type = (
                    cached[1]
                    if cached is not None
                    else self._source_types.get(source_key, 'cpu')
                )
                if cached is not None or source is not None:
                    self._lease_source(source_key)
            if source is None:
                playback_trace(
                    "decode_manager.interactive_source_warmup_pending",
                    frame=frame_index,
                    asset=asset_path,
                    decode_asset=source_spec.decode_path,
                    source_kind=source_spec.source_kind,
                    target_size=normalized_target_size,
                    wait_ms=timeout * 1000.0,
                )
                return None, 'interactive_stale'

        self._begin_interactive_decode()
        try:
            source, source_type = self._select_interactive_source(
                source_key,
                source,
                source_type,
                frame_index,
                reserve=True,
            )
            start = time.perf_counter()
            # 标记交互临界区：后台预取/预置位让位（P1.5）
            with self._stream_lock(source_key):
                return self._decode_frame_interactive(
                    source,
                    source_type,
                    frame_index,
                    source_spec.target_size,
                    start,
                    is_stale=is_stale,
                    deadline_at=deadline_at,
                    coarse=coarse,
                )
        finally:
            self._end_interactive_decode()
            self._release_interactive_source(source)
            self._release_source(source_key)

    def preposition_interactive_frame(
        self,
        asset_path: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
        *,
        deadline_ms: float = 48.0,
        sticky: bool = False,
    ) -> bool:
        """
        Move one spare VT decoder cursor toward the latest scrub target.

        This does not publish a frame.  It only spends a tiny deadline on an
        extra interactive source so the next worker render can pick a hotter
        cursor instead of starting a random seek from the previous displayed
        frame.  The primary source is intentionally left alone for the renderer
        worker.

        ``sticky=True``（拖拽起点预热用）：不递增预置位 token、不被后续
        预置位请求打断——普通预置位每次调用都会作废上一个在飞预置位，
        快速连续输入下非 sticky 预热的长走永远走不完。sticky 预置位只在
        generation 变化时失效。
        """
        normalized_target_size = self._normalize_target_size(target_size)
        source_spec = self._resolve_preview_decode_source(asset_path, normalized_target_size)
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)
        frame_index = int(frame_index)

        selected_source: Optional[Any] = None
        selected_type = ""
        should_start_pool = False
        with self._lock:
            source_info = self._source_cache.get(source_key)
            if source_info is None:
                primary = self._sources.get(source_key)
                primary_type = self._source_types.get(source_key, "")
            else:
                primary, primary_type = source_info
            generation = self._warmup_generation
            prefetch_generation = self._interactive_prefetch_generation
            if sticky:
                # 不递增 token：后续请求级预置位不会打断本次起点预热
                token = self._interactive_prefetch_tokens.get(source_key, 0)
            else:
                token = self._interactive_prefetch_tokens.get(source_key, 0) + 1
                self._interactive_prefetch_tokens[source_key] = token

            pool = list(self._interactive_source_pool.get(source_key, ()))
            if primary is None:
                should_warm = source_key not in self._warmup_events
            else:
                should_warm = False
                if primary_type not in ('vt_pixel_buffer', 'vt_planar'):
                    playback_trace(
                        "decode_manager.interactive_preposition_skip_non_vt",
                        frame=frame_index,
                        asset=asset_path,
                        source_type=primary_type,
                    )
                    return False
                should_start_pool = not pool

            best: Optional[Tuple[int, int, Any, str]] = None
            for index, (candidate, candidate_type) in enumerate(pool):
                if candidate_type not in ('vt_pixel_buffer', 'vt_planar'):
                    continue
                if self._interactive_busy_sources.get(id(candidate)) is candidate:
                    continue
                current_frame = getattr(candidate, "current_frame", None)
                gap = (
                    abs(frame_index - current_frame)
                    if isinstance(current_frame, int)
                    else 1_000_000
                )
                score = (gap, index, candidate, candidate_type)
                if best is None or score[:2] < best[:2]:
                    best = score
            if best is not None:
                _, _index, selected_source, selected_type = best
                self._interactive_busy_sources[id(selected_source)] = selected_source
                done_event = threading.Event()
                self._interactive_prefetch_events.add(done_event)
            else:
                done_event = None

        if should_warm:
            self.warmup_source(
                asset_path,
                target_size=normalized_target_size,
                stream_key=decode_stream_key,
            )
            return False
        if (
            primary is not None
            and primary_type in ('vt_pixel_buffer', 'vt_planar')
            and should_start_pool
        ):
            self._ensure_interactive_source_pool_warmup(
                source_key,
                source_spec,
                generation=generation,
            )
        if selected_source is None or done_event is None:
            playback_trace(
                "decode_manager.interactive_preposition_no_spare_source",
                frame=frame_index,
                asset=asset_path,
                pool_size=len(pool),
            )
            return False

        deadline_at = time.perf_counter() + max(1.0, float(deadline_ms)) / 1000.0

        def _is_stale() -> bool:
            # 注意：这里不做交互让位判定。池预置位使用私有克隆 VT 源
            # （_interactive_busy_sources 预约独占），不与显示解码共用
            # stream lock；它恰恰是让持续拖拽保持快的关键——预热线游标
            # 让后续解码走顺序命中。若在交互读在场时全部中止，池游标
            # 永远不前进，拖拽中每次解码都退化为关键帧锚定 seek（真实
            # App 复现：反复拖拽后明显变卡）。让位只作用于与显示流共锁
            # 的后台路径（_kick_playback_prefetch / preposition_stream_frame）。
            with self._lock:
                if self._interactive_prefetch_generation != prefetch_generation:
                    return True
                if sticky:
                    return False
                return self._interactive_prefetch_tokens.get(source_key) != token

        def _do_preposition() -> None:
            start = time.perf_counter()
            decoded = None
            frame_type = ""
            try:
                decoded, frame_type = self._decode_frame_interactive(
                    selected_source,
                    selected_type,
                    frame_index,
                    source_spec.target_size,
                    start,
                    is_stale=_is_stale,
                    deadline_at=deadline_at,
                )
                playback_trace(
                    "decode_manager.interactive_preposition_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    frame_type=frame_type,
                    source_type=selected_type,
                    stale=_is_stale(),
                    elapsed_ms=(time.perf_counter() - start) * 1000.0,
                )
            except Exception as e:
                playback_trace(
                    "decode_manager.interactive_preposition_error",
                    frame=frame_index,
                    source_type=selected_type,
                    elapsed_ms=(time.perf_counter() - start) * 1000.0,
                    error=e,
                )
            finally:
                self._release_interactive_source(selected_source)
                with self._lock:
                    self._interactive_prefetch_events.discard(done_event)
                # AUD-16501：从注册表移除自己（close 路径据此判断是否
                # 还需要等待）。
                with self._playback_prefetch_lock:
                    self._interactive_preposition_threads = [
                        entry for entry in self._interactive_preposition_threads
                        if entry[1] is not threading.current_thread()
                    ]
                done_event.set()

        # AUD-16503：close_all 之后不再启动新的后台解码线程。
        if self._closing:
            # AUD-16506：线程不会启动，锁内登记的 busy 预约与 done_event
            # 必须在此回收，否则永久泄漏（busy 槽位挤占池、未 set 的
            # done_event 卡住 close 等待）。
            self._release_interactive_source(selected_source)
            with self._lock:
                self._interactive_prefetch_events.discard(done_event)
            return False
        t = threading.Thread(
            target=_do_preposition,
            daemon=True,
            name="DecodeInteractivePreposition",
        )
        # AUD-16501：注册后再 start——close_source/close_all/invalidate 在
        # 销毁会话前按素材/全量 join（与播放预取同等保护强度）。
        with self._playback_prefetch_lock:
            self._interactive_preposition_threads.append((asset_path, t))
        try:
            t.start()
        except Exception as exc:
            # AUD-16506：start 失败（如系统线程耗尽 RuntimeError）时回收
            # 上面锁内登记的三项状态——busy 预约、done_event 与线程注册
            # 表项。否则三者永久泄漏：busy 槽位令该池游标再不可被选，
            # done_event 永不 set，注册表让 close 路径白等。
            logger.warning(
                "EditGpuDecodeManager: preposition thread start failed: %s", exc
            )
            self._release_interactive_source(selected_source)
            with self._lock:
                self._interactive_prefetch_events.discard(done_event)
            with self._playback_prefetch_lock:
                self._interactive_preposition_threads = [
                    entry for entry in self._interactive_preposition_threads
                    if entry[1] is not t
                ]
            return False
        return True

    def estimate_interactive_decode_deadline_ms(
        self,
        asset_path: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
    ) -> float:
        """
        Estimate the decode budget for a latest-only interactive scrub request.

        The budget is intentionally about *one visible frame attempt*, not a
        render queue SLA. Stale tokens still abort old scrub work; this method
        decides how long the current latest request may spend in the decoder
        before yielding back to the UI.
        """
        normalized_target_size = self._normalize_target_size(target_size)
        source_spec = self._resolve_preview_decode_source(asset_path, normalized_target_size)
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)

        with self._lock:
            cached = self._source_cache.get(source_key)
            source = cached[0] if cached is not None else self._sources.get(source_key)
            source_type = (
                cached[1]
                if cached is not None
                else self._source_types.get(source_key, 'cpu')
            )

        if source is not None:
            source, source_type = self._select_interactive_source(
                source_key,
                source,
                source_type,
                frame_index,
                trace_selection=False,
            )

        current_frame = getattr(source, "current_frame", None) if source is not None else None
        codec = self._probe_codec(source_spec.decode_path) or self._probe_codec(asset_path)
        width, height = self._probe_resolution(source_spec.decode_path)
        if (width <= 0 or height <= 0) and source_spec.target_size is not None:
            width, height = source_spec.target_size
        pixels = max(0, width) * max(0, height)

        seek_cost: Optional[int] = None
        gap: Optional[int] = None
        if isinstance(current_frame, int):
            gap = frame_index - current_frame
            seek_cost = estimate_seek_cost(
                current_frame,
                frame_index,
                codec or "",
                getattr(source, "keyframe_interval_hint", None),
            )

        deadline_ms = self._interactive_decode_deadline_from_cost(
            codec=codec,
            pixels=pixels,
            gap=gap,
            seek_cost=seek_cost,
            has_hot_source=source is not None,
        )
        playback_trace(
            "decode_manager.interactive_deadline_estimate",
            frame=frame_index,
            asset=asset_path,
            decode_asset=source_spec.decode_path,
            source_kind=source_spec.source_kind,
            stream_key=decode_stream_key,
            codec=codec,
            current_frame=current_frame,
            gap=gap,
            seek_cost=seek_cost,
            pixels=pixels,
            deadline_ms=deadline_ms,
        )
        return deadline_ms

    # ------------------------------------------------------------------
    # Source warmup (non-blocking pre-creation)
    # ------------------------------------------------------------------

    def warmup_source(
        self,
        asset_path: str,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
        *,
        for_playback: bool = False,
        warm_interactive_pool: bool = True,
    ) -> None:
        """
        Pre-create the decode source in a background thread (non-blocking).

        Call this from the main thread (e.g. request_render) so that the
        expensive av.open() + codec probe runs in parallel while the render
        task is being dispatched to the worker.  By the time the worker calls
        get_frame(), the source is likely already cached.

        Args:
            for_playback: resolve with the playback VT policy so the warm
                session matches the one the playback hot path will use.
            warm_interactive_pool: False skips the extra scrub-pool sessions.
                Playback start passes False — the pool only serves random
                scrub access, and creating 2 extra VT decoder sessions per
                asset right at play start competes with the first frames for
                hardware decoder instances (visible as startup stutter).

        Thread-safe: checks existing caches and deduplicates warmup requests.
        """
        # AUD-16503：close_all 之后不再创建新会话（否则无人再调 close_all
        # 收尾，VT/FFmpeg 会话泄漏直到进程退出）。
        if self._closing:
            return
        normalized_target_size = self._normalize_target_size(target_size)
        source_spec = self._resolve_preview_decode_source(
            asset_path, normalized_target_size, for_playback=for_playback,
        )
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)

        existing_source_type: Optional[str] = None
        existing_source_available = False
        # Fast path: source already available or already warming.
        with self._lock:
            generation = self._warmup_generation
            if source_key in self._source_cache or source_key in self._sources:
                existing_source_available = True
                existing_source_type = self._source_types.get(source_key)
                if existing_source_type is None and source_key in self._source_cache:
                    existing_source_type = self._source_cache[source_key][1]
            if source_key in self._source_cache or source_key in self._sources:
                should_start_pool = (
                    warm_interactive_pool
                    and existing_source_type in ('vt_pixel_buffer', 'vt_planar')
                )
                warmup_event = None
            elif source_key in self._warmup_events:
                return
            else:
                should_start_pool = False
                warmup_event = threading.Event()
                self._warmup_events[source_key] = warmup_event

        if should_start_pool:
            self._ensure_interactive_source_pool_warmup(
                source_key,
                source_spec,
                generation=generation,
            )
            return
        if existing_source_available:
            return

        def _do_warmup() -> None:
            # IMPORTANT: Call _create_source directly, NOT _get_or_create_source.
            # _get_or_create_source would check _warmup_events and wait for
            # itself → self-deadlock for 10 seconds.
            source = None
            try:
                source, source_type = self._create_source(
                    source_spec.decode_path,
                    source_spec.target_size,
                    vt_policy=source_spec.vt_policy,
                )
                if source is None:
                    return
                should_close_source = False
                victims: list = []
                with self._lock:
                    # Only store if no one else beat us to it
                    if generation == self._warmup_generation and source_key not in self._sources:
                        self._sources[source_key] = source
                        self._source_types[source_key] = source_type
                        self._source_cache[source_key] = (source, source_type)
                        self._source_last_used[source_key] = time.perf_counter()
                        victims = self._evict_sources_if_over_cap()
                    else:
                        should_close_source = True
                self._close_eviction_victims(victims)
                if should_close_source:
                    self._close_source_object(source, source_key)
                    return
                playback_trace(
                    "decode_manager.warmup_done",
                    source_type=source_type,
                    asset=asset_path,
                    decode_asset=source_spec.decode_path,
                    source_kind=source_spec.source_kind,
                    target_size=normalized_target_size,
                )
                if warm_interactive_pool and source_type in ('vt_pixel_buffer', 'vt_planar'):
                    self._ensure_interactive_source_pool_warmup(
                        source_key,
                        source_spec,
                        generation=generation,
                    )
            except Exception as e:
                logger.debug(f"Decode warmup failed for {asset_path}: {e}")
            finally:
                with self._lock:
                    current_event = self._warmup_events.pop(source_key, None)
                if current_event is not None:
                    current_event.set()

        t = threading.Thread(
            target=_do_warmup,
            daemon=True,
            name="DecodeWarmup",
        )
        t.start()

    def set_no_deblock(self, enabled: bool) -> None:
        """预览禁用输出去块（提速开关）。切换后旧 source（request 里带着
        旧 flag）不能被复用，故整体失效——下一次渲染重建 source。"""
        enabled = bool(enabled)
        if enabled == self._no_deblock:
            return
        self._no_deblock = enabled
        try:
            self.invalidate_all_sources()
        except Exception as exc:  # noqa: BLE001 —— 失效失败不阻断开关生效
            logger.warning("EditGpuDecodeManager: set_no_deblock 失效source失败: %s", exc)

    def set_playback_active(self, active: bool) -> None:
        """标记实时播放状态：播放中延迟交互解码池的预热。"""
        was = self._playback_active
        self._playback_active = bool(active)
        if was and not self._playback_active and self._playback_pool_warmup_deferred:
            # 播放结束：补齐播放期间被延迟的池预热请求。
            deferred = list(self._playback_pool_warmup_deferred)
            self._playback_pool_warmup_deferred.clear()
            for source_key, source_spec in deferred:
                self._ensure_interactive_source_pool_warmup(
                    source_key, source_spec, generation=self._warmup_generation)

    def _ensure_interactive_source_pool_warmup(
        self,
        source_key: _SourceKey,
        source_spec: _DecodeSourceSpec,
        *,
        generation: int,
    ) -> None:
        """Warm extra VT sources so random scrub can preserve multiple cursors."""
        # AUD-16503：close_all 之后不再预热新会话。
        if self._closing:
            return
        if self._playback_active:
            self._playback_pool_warmup_deferred.add((source_key, source_spec))
            return
        desired_extra_sources = max(0, _INTERACTIVE_SOURCE_POOL_SIZE - 1)
        if desired_extra_sources <= 0:
            return
        with self._lock:
            existing = len(self._interactive_source_pool.get(source_key, ()))
            if existing >= desired_extra_sources:
                return
            if source_key in self._interactive_pool_warmup_events:
                return
            event = threading.Event()
            self._interactive_pool_warmup_events[source_key] = event

        def _do_pool_warmup() -> None:
            created: list[Tuple[Any, str]] = []
            try:
                remaining = desired_extra_sources - existing
                for _ in range(max(0, remaining)):
                    source, source_type = self._create_source(
                        source_spec.decode_path,
                        source_spec.target_size,
                    )
                    if source is None:
                        continue
                    if source_type not in ('vt_pixel_buffer', 'vt_planar'):
                        self._close_source_object(source, source_key)
                        continue
                    created.append((source, source_type))

                if not created:
                    return

                to_close: list[Any] = []
                with self._lock:
                    pool = self._interactive_source_pool.setdefault(source_key, [])
                    if generation != self._warmup_generation:
                        to_close = [source for source, _type in created]
                    else:
                        available = max(0, desired_extra_sources - len(pool))
                        pool.extend(created[:available])
                        to_close = [source for source, _type in created[available:]]
                for source in to_close:
                    self._close_source_object(source, source_key)
                playback_trace(
                    "decode_manager.interactive_pool_warmup_done",
                    decode_asset=source_spec.decode_path,
                    source_kind=source_spec.source_kind,
                    pool_size=len(self._interactive_source_pool.get(source_key, ())),
                )
            except Exception as e:
                logger.debug(f"Decode interactive source pool warmup failed: {e}")
                for source, _type in created:
                    self._close_source_object(source, source_key)
            finally:
                with self._lock:
                    current_event = self._interactive_pool_warmup_events.pop(source_key, None)
                if current_event is not None:
                    current_event.set()

        t = threading.Thread(
            target=_do_pool_warmup,
            daemon=True,
            name="DecodeInteractivePoolWarmup",
        )
        t.start()

    def warmup_asset_capability(self, asset_path: str) -> None:
        """
        Pre-warm cheap per-asset decode metadata and global VT capability.

        This is intended for media-pool import: it avoids the expensive
        capability probe and codec metadata probe landing on the first timeline
        play, without keeping an unused per-clip VT source open.
        """
        if not asset_path:
            return
        with self._lock:
            if asset_path in self._codec_cache or asset_path in self._metadata_warmups:
                return
            self._metadata_warmups.add(asset_path)

        def _do_warmup() -> None:
            try:
                codec = self._probe_codec(asset_path)
                if codec and sys.platform == 'darwin':
                    try:
                        from src.shared.media.macos_vt.capability import prewarm_zero_copy_capability
                        prewarm_zero_copy_capability(blocking=True)
                    except Exception as e:
                        logger.debug(f"VT capability warmup failed for {asset_path}: {e}")
                playback_trace(
                    "decode_manager.asset_capability_warmup_done",
                    asset=asset_path,
                    codec=codec,
                )
            except Exception as e:
                logger.debug(f"Asset capability warmup failed for {asset_path}: {e}")
            finally:
                with self._lock:
                    self._metadata_warmups.discard(asset_path)

        t = threading.Thread(
            target=_do_warmup,
            daemon=True,
            name="DecodeMetadataWarmup",
        )
        t.start()

    # ------------------------------------------------------------------
    # Playback stream prepositioning (cut-boundary prefetch)
    # ------------------------------------------------------------------

    def preposition_stream_frame(
        self,
        asset_path: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
        *,
        is_stale: Optional[Callable[[], bool]] = None,
        deadline_at: Optional[float] = None,
    ) -> bool:
        """
        Move the clip's OWN display decode stream so that the next display
        read of ``frame_index`` becomes a single sequential decode.

        Background prepositioning for playback cut boundaries.  The display
        stream (same source object the render worker will use) is advanced to
        ``frame_index - 1``; VT cursors are forward-only, so the boundary
        frame then decodes sequentially instead of paying a cold seek +
        decode-forward cost on the playhead thread.

        Concurrency: the per-stream lock serializes this against a concurrent
        display decode on the SAME stream only.  If the display read wins the
        lock, its decode-forward work is exactly what this preposition wanted,
        and the re-check under the lock turns this call into a cheap no-op.
        Different clip streams keep independent locks.

        Returns True when the stream is positioned for a sequential read of
        ``frame_index`` (already was, or preposition decode succeeded).
        """
        if frame_index <= 0:
            return False
        normalized_target_size = self._normalize_target_size(target_size)
        source_spec = self._resolve_preview_decode_source(asset_path, normalized_target_size)
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)

        # Deduplicate concurrent prepositions on the same stream.
        with self._lock:
            if source_key in self._preposition_inflight:
                return False
            inflight = threading.Event()
            self._preposition_inflight[source_key] = inflight
        leased = False
        try:
            source = self._get_or_create_source(
                asset_path,
                normalized_target_size,
                decode_stream_key,
                source_spec=source_spec,
            )
            if source is None:
                return False
            leased = True
            with self._lock:
                source_type = self._source_types.get(source_key, 'cpu')
            target = int(frame_index) - 1

            current_frame = getattr(source, "current_frame", None)
            if isinstance(current_frame, int) and current_frame >= target:
                return True

            # 后台让位式获取（P1.5）：交互显示读在场则放弃本批预置位
            if not self._acquire_stream_lock_background(source_key, timeout_s=1.0):
                playback_trace(
                    "decode_manager.preposition_yield_interactive",
                    frame=frame_index,
                    asset=asset_path,
                )
                return False
            try:
                # Re-check under the lock: a display decode that owned the
                # stream before us may already have advanced the cursor.
                current_frame = getattr(source, "current_frame", None)
                if isinstance(current_frame, int) and current_frame >= target:
                    return True

                def _background_stale() -> bool:
                    # 交互读到达 → 中止长 GOP 前解，立即让出
                    if self._interactive_decode_pending():
                        return True
                    return bool(is_stale()) if is_stale is not None else False

                decoded = self._preposition_decode(
                    source,
                    source_type,
                    target,
                    is_stale=_background_stale,
                    deadline_at=deadline_at,
                )
            finally:
                self._stream_lock(source_key).release()

            if decoded is None:
                return False
            if source_type == 'vt_pixel_buffer':
                release = getattr(decoded, "release", None)
                if callable(release):
                    try:
                        release()
                    except Exception as e:
                        logger.debug(f"EditGpuDecodeManager: preposition release failed: {e}")
            playback_trace(
                "decode_manager.preposition_done",
                frame=frame_index,
                target=target,
                asset=asset_path,
                decode_asset=source_spec.decode_path,
                source_type=source_type,
            )
            return True
        except Exception as e:
            logger.debug(
                f"EditGpuDecodeManager: preposition failed for "
                f"{asset_path}@{frame_index}: {e}"
            )
            return False
        finally:
            if leased:
                self._release_source(source_key)
            with self._lock:
                if self._preposition_inflight.get(source_key) is inflight:
                    self._preposition_inflight.pop(source_key, None)
            inflight.set()

    def _preposition_decode(
        self,
        source: Any,
        source_type: str,
        target: int,
        *,
        is_stale: Optional[Callable[[], bool]],
        deadline_at: Optional[float],
    ) -> Optional[Any]:
        """Decode ``target`` on a display stream to advance its cursor."""
        if source_type == 'vt_pixel_buffer':
            read_interactive = getattr(source, "read_frame_pixel_buffer_interactive", None)
            if callable(read_interactive):
                try:
                    return read_interactive(
                        target,
                        is_stale=is_stale,
                        deadline_at=deadline_at,
                    )
                except TypeError:
                    return read_interactive(target)
            read_pixel_buffer = getattr(source, "read_frame_pixel_buffer", None)
            if callable(read_pixel_buffer):
                return read_pixel_buffer(target)
            return None
        return self._read_source_frame(source, source_type, target)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_stream_key(self, asset_path: str, stream_key: Optional[str]) -> str:
        """Return the decode stream identity used for source reuse."""
        if stream_key:
            return str(stream_key)
        return asset_path

    def _stream_lock(self, source_key: _SourceKey) -> threading.Lock:
        """Return the per-stream decode serialization lock."""
        with self._lock:
            lock = self._stream_locks.get(source_key)
            if lock is None:
                lock = threading.Lock()
                self._stream_locks[source_key] = lock
            return lock

    # ------------------------------------------------------------------
    # 交互优先级门控（P1.5）：后台解码让位交互解码
    # ------------------------------------------------------------------

    def _begin_interactive_decode(self) -> None:
        """交互显示读进入临界区：后台批量解码此后让位。"""
        with self._lock:
            self._interactive_pending += 1

    def _end_interactive_decode(self) -> None:
        """交互显示读离开临界区。"""
        with self._lock:
            self._interactive_pending = max(0, self._interactive_pending - 1)

    def _interactive_decode_pending(self) -> bool:
        """是否有交互显示读正在等待或持有流锁。"""
        with self._lock:
            return self._interactive_pending > 0

    def _acquire_stream_lock_background(
        self,
        source_key: _SourceKey,
        *,
        timeout_s: float = 1.0,
    ) -> bool:
        """后台调用方的流锁获取：交互读在场则让位。

        返回 True 表示已获得锁（调用方必须通过 ``_stream_lock(key).release()``
        释放）；False 表示让位或超时，调用方应放弃本批预取（稍后由常规
        调度自然重试，不标记完成）。
        """
        lock = self._stream_lock(source_key)
        deadline = time.perf_counter() + max(0.0, timeout_s)
        while True:
            if self._interactive_decode_pending():
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                return False
            if lock.acquire(timeout=min(0.05, remaining)):
                if self._interactive_decode_pending():
                    # 获取到锁的瞬间交互读已到达：让出，交互读将立即获得锁
                    lock.release()
                    return False
                return True

    # ------------------------------------------------------------------
    # P2-09 会话池：租约 + LRU 逐出
    # ------------------------------------------------------------------

    def _lease_source(self, source_key: _SourceKey) -> None:
        """取出缓存的 source 前加租约并刷新 LRU 时钟（调用方须持 _lock）。"""
        self._source_refs[source_key] = self._source_refs.get(source_key, 0) + 1
        self._source_last_used[source_key] = time.perf_counter()

    def _release_source(self, source_key: _SourceKey) -> None:
        """解码用完归还租约（自带锁，供 finally 调用）。

        P2-09：若该 key 已被关闭路径标记为推迟关闭（排水超时仍在用），
        最后一个租约归还时在锁外补 close——关闭路径与逐出路径共用同一
        租约契约，绝不在解码中途关闭会话。
        """
        deferred: list[Tuple[_SourceKey, Any]] = []
        with self._lock:
            count = max(0, self._source_refs.get(source_key, 0) - 1)
            if count > 0:
                self._source_refs[source_key] = count
            else:
                self._source_refs.pop(source_key, None)
                pending = self._deferred_close_sources.pop(source_key, None)
                if pending:
                    deferred = [(source_key, source) for source in pending]
        for key, source in deferred:
            self._close_source_object(source, key)

    def _wait_for_source_leases(
        self,
        keys: list,
        timeout: Optional[float] = None,
    ) -> None:
        """P2-09：关闭路径排水——等待一组 source key 的在飞租约归零。

        租约 > 0 表示有显示/交互/预取线程正在 C 层使用该会话；关闭前先
        等待（与 LRU 逐出的租约检查同一契约）。超时未归零的由调用方
        推迟关闭（_deferred_close_sources），而不是强行 close。
        timeout 缺省取 _CLOSE_LEASE_DRAIN_TIMEOUT（调用时解析，便于
        测试注入短超时）。
        """
        if not keys:
            return
        if timeout is None:
            timeout = _CLOSE_LEASE_DRAIN_TIMEOUT
        deadline = time.perf_counter() + max(0.0, float(timeout))
        while True:
            with self._lock:
                leased = [
                    key for key in keys
                    if self._source_refs.get(key, 0) > 0
                ]
            if not leased:
                return
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                logger.warning(
                    "EditGpuDecodeManager: lease drain timeout, deferring "
                    "close of %d in-use source(s) (first asset: %s)",
                    len(leased), leased[0][1],
                )
                return
            time.sleep(min(0.02, remaining))

    def _partition_close_units_locked(
        self,
        units: list[Tuple[_SourceKey, list]],
    ) -> list[Tuple[_SourceKey, Any]]:
        """按 P2-09 租约拆分待关闭会话单元（调用方须持 _lock）。

        每个单元是 (source_key, [主源, *池克隆])。租约为 0 的单元展开为
        (key, source) 返回（调用方在锁外 close）；仍在用的登记到
        _deferred_close_sources，由最后一个 _release_source 归零时在锁外
        补关闭。两条路径都不再"解码中途关闭"。
        """
        close_now: list[Tuple[_SourceKey, Any]] = []
        for key, unit in units:
            if self._source_refs.get(key, 0) > 0:
                self._deferred_close_sources.setdefault(key, []).extend(
                    source for source in unit if source is not None
                )
            else:
                self._source_refs.pop(key, None)
                close_now.extend(
                    (key, source) for source in unit if source is not None
                )
        return close_now

    def _sweep_deferred_close_locked(
        self,
        match_asset: Optional[str] = None,
    ) -> list[Tuple[_SourceKey, Any]]:
        """收走租约已归零的推迟关闭单元（调用方须持 _lock）。

        match_asset 为 None 时收走全部（close_all/invalidate）；否则只收
        key[1] == match_asset 的（close_source 按素材重置）。仍未归零的
        继续留在 _deferred_close_sources 等待租约释放。
        """
        close_now: list[Tuple[_SourceKey, Any]] = []
        for key in list(self._deferred_close_sources.keys()):
            if match_asset is not None and key[1] != match_asset:
                continue
            if self._source_refs.get(key, 0) > 0:
                continue
            self._source_refs.pop(key, None)
            close_now.extend(
                (key, source) for source in self._deferred_close_sources.pop(key)
            )
        return close_now

    def _evict_sources_if_over_cap(self) -> list[Tuple[_SourceKey, Any, list[Any]]]:
        """超过会话上限时按 LRU 逐出空闲会话（调用方须持 _lock）。

        返回待关闭对象列表 [(key, source, pool_clones)]，调用方必须在
        释放 _lock 之后逐个 close——close VT 会话会短暂阻塞 VideoToolbox，
        绝不能拿着管理器锁做。跳过：
        - 租约 > 0（正在解码/预取/预置位）
        - warmup / 池预热 / 在飞预置位 / 播放预取进行中
        - 主源或任一池克隆处于交互预约（busy）
        单次最多 _MAX_EVICTIONS_PER_INSERT 个，剩余超额由下一次插入继续收敛。
        """
        victims: list[Tuple[_SourceKey, Any, list[Any]]] = []
        if self._closing or len(self._sources) <= _MAX_LIVE_SOURCES:
            return victims
        candidates: list[Tuple[float, _SourceKey]] = []
        for key, source in self._sources.items():
            if self._source_refs.get(key, 0) > 0:
                continue
            if key in self._warmup_events or key in self._interactive_pool_warmup_events:
                continue
            if key in self._preposition_inflight or key in self._playback_prefetch_pending:
                continue
            if self._interactive_busy_sources.get(id(source)) is source:
                continue
            pool = self._interactive_source_pool.get(key, ())
            if any(self._interactive_busy_sources.get(id(s)) is s for s, _t in pool):
                continue
            candidates.append((self._source_last_used.get(key, 0.0), key))
        candidates.sort()
        for _lru, key in candidates:
            if len(self._sources) - len(victims) <= _MAX_LIVE_SOURCES:
                break
            if len(victims) >= _MAX_EVICTIONS_PER_INSERT:
                break
            source = self._sources.pop(key)
            self._source_types.pop(key, None)
            self._source_cache.pop(key, None)
            self._source_refs.pop(key, None)
            self._source_last_used.pop(key, None)
            self._stream_locks.pop(key, None)
            pool = self._interactive_source_pool.pop(key, [])
            self._eviction_count += 1
            victims.append((key, source, [clone for clone, _t in pool]))
        if victims:
            playback_trace(
                "decode_manager.session_evicted",
                count=len(victims),
                live=len(self._sources) + len(victims),
                evicted_total=self._eviction_count,
            )
        return victims

    def _close_eviction_victims(
        self,
        victims: list[Tuple[_SourceKey, Any, list[Any]]],
    ) -> None:
        """在 _lock 之外关闭逐出对象。

        不需要额外等待在飞工作：所有在用路径都持有租约或交互预约，
        _evict_sources_if_over_cap 已据此跳过。
        """
        for key, source, pool_clones in victims:
            self._close_source_object(source, key)
            for clone in pool_clones:
                self._close_source_object(clone, key)

    def session_pool_stats(self) -> dict:
        """P2-09 诊断：会话池规模/租约/负缓存/逐出计数（测试与 bench 用）。"""
        with self._lock:
            return {
                "primary_sources": len(self._sources),
                "pool_clones": sum(
                    len(pool) for pool in self._interactive_source_pool.values()
                ),
                "leased": sum(1 for v in self._source_refs.values() if v > 0),
                "negative_keys": len(self._failed_source_keys),
                "evictions": self._eviction_count,
                "max_live_sources": _MAX_LIVE_SOURCES,
            }

    # ------------------------------------------------------------------
    # W03（计划 W-I02）：运行中驻留源失败的真实恢复
    # ------------------------------------------------------------------

    def _resident_route_allowed_for(self, asset_path: str) -> Tuple[bool, str]:
        """该素材当前是否允许新建 cuda 驻留源（调用方无须持锁）。

        返回 (allowed, reason)：allowed=False 时 reason 为
        'demoted_cooldown'（冷却期内，到期自动允许重新探测）或
        'demoted_max_cycles'（降级次数超上限，需显式恢复/新会话）。
        """
        with self._lock:
            return self._resident_route_allowed_locked(asset_path)

    def _resident_route_allowed_locked(self, asset_path: str) -> Tuple[bool, str]:
        entry = self._resident_demoted.get(asset_path)
        if entry is None:
            return True, ''
        if entry["cycles"] > _RESIDENT_MAX_DEMOTE_CYCLES:
            return False, 'demoted_max_cycles'
        if time.perf_counter() < entry["until"]:
            return False, 'demoted_cooldown'
        return True, ''

    def note_resident_import_failure(self, asset_path: str, reason: str) -> bool:
        """渲染线程报告某素材的 cuda 驻留帧导入失败（W03 恢复入口）。

        同步部分（本方法，任意线程可调）：按素材记账并立即拉起降级门——
        之后 _create_source 不再为该素材创建 win_cuda_resident 源（新驻留
        请求停用），与全局门闩互补（故障隔离到单素材）。

        异步部分（后台线程）：等该素材驻留源的在飞租约归零后关闭它们
        （复用 close/逐出的租约契约，绝不在解码中途关闭）；下一次
        get_frame 自然走 3.5 下载 planar / CPU 路径重建源——当前片段
        下一帧自动恢复，后续 seek 正确。

        限次重试：每次失败记一个 cycle；冷却窗口（默认 300s）内不重试
        驻留，窗口过期允许重新探测；cycle 超过上限后本会话不再自动重试
        （防单坏源反复抖动），只能 reset_resident_demotions / close_all
        显式恢复。

        Returns:
            True 表示本次调用新拉起了一次降级（记账+关闭作业）；
            False 表示重复报告（已在处理/已永久降级）或管理器已关闭。
        """
        if not asset_path:
            return False
        schedule_close = False
        with self._lock:
            if self._closing:
                return False
            entry = self._resident_demoted.get(asset_path)
            if entry is None:
                entry = {
                    "cycles": 0, "until": 0.0, "last_reason": "",
                    "last_failed_at": 0.0, "recoveries": 0,
                }
                self._resident_demoted[asset_path] = entry
            entry["cycles"] += 1
            entry["last_reason"] = str(reason)
            entry["last_failed_at"] = time.perf_counter()
            if entry["cycles"] > _RESIDENT_MAX_DEMOTE_CYCLES:
                entry["until"] = math.inf
            else:
                entry["until"] = time.perf_counter() + _RESIDENT_DEMOTE_COOLDOWN_S
            if asset_path not in self._resident_demotion_pending:
                self._resident_demotion_pending.add(asset_path)
                schedule_close = True
        playback_trace(
            "decode_manager.resident_import_failure",
            asset=asset_path,
            reason=str(reason),
            cycles=entry["cycles"],
            cooldown_remaining_s=(
                math.inf
                if math.isinf(entry["until"])
                else max(0.0, entry["until"] - time.perf_counter())
            ),
            close_scheduled=schedule_close,
        )
        if schedule_close:
            t = threading.Thread(
                target=self._apply_resident_demotion,
                args=(asset_path,),
                daemon=True,
                name=f"ResidentDemote-{Path(asset_path).name}" if len(asset_path) < 200
                else "ResidentDemote",
            )
            t.start()
        return schedule_close

    def _apply_resident_demotion(self, asset_path: str) -> None:
        """后台执行按素材驻留降级：租约归零 → 只关该素材的驻留源。

        只关 source_type == 'win_cuda_resident' 的 key——同素材已重建的
        下载 planar/CPU 源（其它 track 的正常源）不受影响。复用
        close_source 的租约排水 + 推迟关闭契约：在飞租约超时未归零的登记
        到 _deferred_close_sources，由最后一个 _release_source 补关闭，
        绝不在解码中途 close。
        """
        try:
            if self._closing:
                return
            # 在飞 warmup 会为该素材重建驻留源（race：记账后 warmup 线程
            # 可能已过 _create_source 检查）。bump warmup generation 让
            # 它们丢弃结果；demotion 标志保证后续 _create_source 返回
            # 下载/CPU 源，warmup 插入的也只会是回退源（无害且正确）。
            with self._lock:
                warmup_events = self._invalidate_warmups_locked()
                drain_keys = [
                    key for key, source_type in self._source_types.items()
                    if key[1] == asset_path and source_type == 'win_cuda_resident'
                ]
            for event in warmup_events:
                event.set()
            if not drain_keys:
                return
            self._wait_for_source_leases(drain_keys)

            close_now: list[Tuple[_SourceKey, Any]] = []
            with self._lock:
                units: list[Tuple[_SourceKey, list]] = []
                for key in drain_keys:
                    if self._source_types.get(key) != 'win_cuda_resident':
                        continue  # 期间已被其它路径换掉/关闭
                    source = self._sources.pop(key, None)
                    if source is None:
                        continue
                    self._source_types.pop(key, None)
                    self._source_cache.pop(key, None)
                    self._source_last_used.pop(key, None)
                    self._stream_locks.pop(key, None)
                    pool_clones = [
                        clone
                        for clone, _t in self._interactive_source_pool.pop(key, [])
                    ]
                    units.append((key, [source, *pool_clones]))
                close_now = self._partition_close_units_locked(units)
            seen: set[int] = set()
            for key, source in close_now:
                if source is None:
                    continue
                source_id = id(source)
                if source_id in seen:
                    continue
                seen.add(source_id)
                self._close_source_object(source, key)
            playback_trace(
                "decode_manager.resident_demotion_closed",
                asset=asset_path,
                closed=len(seen),
            )
        except Exception as exc:  # noqa: BLE001 —— 后台线程兜底，失败留痕不外抛
            logger.warning(
                "EditGpuDecodeManager: resident demotion for %s failed: %s",
                asset_path, exc,
            )
        finally:
            with self._lock:
                self._resident_demotion_pending.discard(asset_path)

    def resident_demotion_stats(self) -> Dict[str, Dict[str, Any]]:
        """W03 诊断：按素材驻留降级状态（原因/次数/冷却剩余可见）。"""
        with self._lock:
            now = time.perf_counter()
            return {
                asset: {
                    "cycles": entry["cycles"],
                    "last_reason": entry["last_reason"],
                    "cooldown_remaining_s": (
                        math.inf if math.isinf(entry["until"])
                        else max(0.0, entry["until"] - now)
                    ),
                    "barred_permanently": entry["cycles"] > _RESIDENT_MAX_DEMOTE_CYCLES,
                    "close_pending": asset in self._resident_demotion_pending,
                }
                for asset, entry in self._resident_demoted.items()
            }

    def reset_resident_demotions(self, asset_path: Optional[str] = None) -> None:
        """显式恢复：清除按素材驻留降级（诊断/用户操作入口）。

        只清降级记账，不触碰在用源——清掉后下一次 source 重建（画质切换/
        素材重开）即可重新探测驻留路径。asset_path 为 None 时清全部。
        """
        with self._lock:
            if asset_path is None:
                self._resident_demoted.clear()
            else:
                self._resident_demoted.pop(asset_path, None)

    def _make_source_key(
        self,
        decode_stream_key: str,
        source_spec: _DecodeSourceSpec,
    ) -> _SourceKey:
        return (
            decode_stream_key,
            source_spec.original_path,
            source_spec.decode_path,
            source_spec.target_size,
            source_spec.vt_policy,
            source_spec.no_deblock,
        )

    def _resolve_preview_decode_source(
        self,
        asset_path: str,
        target_size: Optional[Tuple[int, int]],
        *,
        for_playback: bool = False,
    ) -> _DecodeSourceSpec:
        """
        Resolve the file that timeline preview should decode.

        Preview requests prefer ready disk proxies. The original media is still
        used for full-quality/source-style requests where no target_size is
        provided.

        ``for_playback`` marks the sequential-playback hot path. When the
        VideoToolbox hardware-decode probe says this codec/bit-depth has no
        hardware decoder (VT would silently run software), the spec is marked
        ``vt_policy='prefer_ffmpeg'`` so playback decodes through the
        frame-threaded FFmpeg planar path instead. Scrub/interactive requests
        keep the VT zero-copy policy regardless.
        """
        if target_size is None:
            if self._is_topos_movie_path(asset_path):
                return _DecodeSourceSpec(
                    original_path=asset_path,
                    decode_path=asset_path,
                    target_size=None,
                    source_kind="original_topos",
                    vt_policy='auto',
                    no_deblock=self._no_deblock,
                )
            return _DecodeSourceSpec(
                original_path=asset_path,
                decode_path=asset_path,
                target_size=None,
                source_kind="original",
                vt_policy=self._playback_vt_policy(asset_path) if for_playback else 'auto',
            )

        try:
            info = self._get_disk_proxy_info(asset_path)
        except Exception as e:
            logger.debug(f"EditGpuDecodeManager: disk proxy query failed for {asset_path}: {e}")
            info = None

        if info is not None:
            proxy_path = getattr(info, "proxy_path", None)
            if getattr(info, "is_ready", False) and proxy_path and os.path.isfile(proxy_path):
                return _DecodeSourceSpec(
                    original_path=asset_path,
                    decode_path=proxy_path,
                    target_size=None,
                    source_kind="disk_proxy",
                )

            # Source selection runs once per rendered layer and must stay
            # read-only. Starting an ffmpeg proxy transcode here competes with
            # the realtime decoder for CPU, hardware decode and disk bandwidth,
            # which turns an otherwise smooth master preview into dropped
            # frames. Proxy creation remains an explicit media-pool/cache job.

        # Topos MOV is a private TPIC stream inside a QuickTime container. It
        # must keep the requested box for the native reduced decoder and must
        # never enter the FFmpeg/VideoToolbox codec-probing path.
        if self._is_topos_movie_path(asset_path):
            return _DecodeSourceSpec(
                original_path=asset_path,
                decode_path=asset_path,
                target_size=target_size,
                source_kind="original_topos_reduced",
                no_deblock=self._no_deblock,
                vt_policy='auto',
            )

        vt_policy = self._playback_vt_policy(asset_path) if for_playback else 'auto'
        prefer_ffmpeg_planar = vt_policy in ('prefer_ffmpeg', 'hw_or_ffmpeg')

        if self._should_preserve_native_planar_size(
            asset_path, target_size, include_h26x=prefer_ffmpeg_planar,
        ):
            # Do not make FFmpeg/swscale resize a 4K ProRes/DNxHR frame on the
            # decode threads just to feed a 1080p program monitor.  The edit
            # GPU renderer already converts YUV directly into its 1080p track
            # FBO, so texture sampling supplies the resize without reducing
            # the timeline output resolution or the source precision. The
            # same economics apply to H.264/H.265 streams that lost the VT
            # hardware race: decode native planar, let the GPU scale.
            return _DecodeSourceSpec(
                original_path=asset_path,
                decode_path=asset_path,
                target_size=None,
                source_kind="original_native_planar",
                vt_policy=vt_policy,
            )

        return _DecodeSourceSpec(
            original_path=asset_path,
            decode_path=asset_path,
            target_size=target_size,
            source_kind="original_downscaled",
            vt_policy=vt_policy,
        )

    def _playback_vt_policy(self, asset_path: str) -> str:
        """Playback-only decoder routing.

        Returns one of:
        - 'auto': unchanged VT zero-copy preference (8-bit camera codecs,
          ProRes, scrub-shaped requests).
        - 'prefer_ffmpeg': the hardware probe says VideoToolbox has NO
          hardware decoder for this codec/bit-depth — sequential playback
          belongs on the frame-threaded FFmpeg planar path.
        - 'hw_or_ffmpeg': hardware exists, but the session must be created
          with a hardware requirement. VideoToolbox otherwise silently hands
          out software sessions when the decoder-instance pool is busy, and
          that session stays software forever (visible as randomly slow
          playback). If the required session cannot be created, fall through
          to FFmpeg planar instead of accepting software VT.

        The first call for an asset may pay a one-time probe (~tens of ms,
        persisted per codec/bit-depth/machine); callers on the playback path
        run inside the worker/warmup threads, never the UI thread.
        """
        if sys.platform != 'darwin':
            return 'auto'
        try:
            codec = self._probe_codec(asset_path)
            if not any(c in codec for c in _H264_H265_CODECS):
                return 'auto'
            pix_fmt = self._probe_pix_fmt(asset_path)
            if not pix_fmt:
                return 'auto'
            from src.shared.media.ffmpeg_source import _infer_bit_depth
            if _infer_bit_depth(pix_fmt) <= 8:
                # 8-bit H.264/H.265 hardware decode is effectively always
                # granted; skip probing and keep the auto session policy.
                return 'auto'
            from src.shared.media.macos_vt.hardware_capability import (
                playback_should_prefer_ffmpeg,
                vt_hardware_decode_available,
            )
            if playback_should_prefer_ffmpeg(
                codec, pix_fmt, asset_path,
                width=self._probe_resolution(asset_path)[0],
                height=self._probe_resolution(asset_path)[1],
            ):
                self._trace_playback_route_once(
                    asset_path, codec, pix_fmt, 'prefer_ffmpeg',
                    reason='vt_software_only',
                )
                return 'prefer_ffmpeg'
            verdict = vt_hardware_decode_available(
                asset_path, codec, _infer_bit_depth(pix_fmt),
                width=self._probe_resolution(asset_path)[0],
                height=self._probe_resolution(asset_path)[1],
            )
            if verdict is True:
                self._trace_playback_route_once(
                    asset_path, codec, pix_fmt, 'hw_or_ffmpeg',
                    reason='hw_session_strict',
                )
                return 'hw_or_ffmpeg'
        except Exception as e:
            logger.debug(f"EditGpuDecodeManager: playback VT policy failed for {asset_path}: {e}")
        return 'auto'

    def _trace_playback_route_once(
        self, asset_path: str, codec: str, pix_fmt: str, policy: str, *, reason: str,
    ) -> None:
        if asset_path in self._playback_route_logged:
            return
        self._playback_route_logged.add(asset_path)
        playback_trace(
            "decode_manager.playback_route",
            asset=asset_path,
            codec=codec,
            pix_fmt=pix_fmt,
            policy=policy,
            reason=reason,
        )

    def _should_preserve_native_planar_size(
        self,
        asset_path: str,
        target_size: Optional[Tuple[int, int]],
        *,
        include_h26x: bool = False,
    ) -> bool:
        """Return whether an intra master should be scaled by the GPU.

        This is deliberately not a lower-quality preview mode.  It applies
        only when the requested program target is already 1080p-or-larger and
        the source has at least a 2x pixel reduction to make.  Smaller targets
        (for example CPU-only scrub previews) retain the existing resize
        policy to avoid uploading a 4K frame for a thumbnail-sized request.

        ``include_h26x`` extends the policy to long-GOP camera codecs that
        were routed off VideoToolbox by the playback hardware probe — the
        FFmpeg planar decode benefits from the same native-size + GPU-scale
        economics as the intra masters.
        """
        if target_size is None:
            return False
        target_w, target_h = target_size
        target_pixels = max(0, int(target_w)) * max(0, int(target_h))
        if target_pixels < _NATIVE_GPU_SCALE_MIN_TARGET_PIXELS:
            return False

        codec = self._probe_codec(asset_path).lower()
        if codec not in _NATIVE_GPU_SCALE_CODECS:
            if not (include_h26x and any(c in codec for c in _H264_H265_CODECS)):
                return False

        source_w, source_h = self._probe_resolution(asset_path)
        source_pixels = max(0, int(source_w)) * max(0, int(source_h))
        if source_pixels <= target_pixels:
            return False
        return (
            source_pixels / max(1, target_pixels)
            >= _NATIVE_GPU_SCALE_MIN_REDUCTION_RATIO
        )

    def _get_disk_proxy_info(self, asset_path: str) -> Optional[Any]:
        from src.shared.media.disk_proxy_service import get_disk_proxy_service

        return get_disk_proxy_service().get_disk_proxy_info(asset_path)

    @staticmethod
    def _interactive_warmup_wait_timeout(deadline_at: Optional[float]) -> float:
        """Return a short wait budget for interactive source warmup."""
        if deadline_at is None:
            return 0.03
        return max(0.0, min(0.03, float(deadline_at) - time.perf_counter()))

    @staticmethod
    def _interactive_decode_should_abort(
        is_stale: Optional[Callable[[], bool]],
        deadline_at: Optional[float],
    ) -> bool:
        if is_stale is not None:
            try:
                if is_stale():
                    return True
            except Exception:
                return True
        if deadline_at is not None and time.perf_counter() >= float(deadline_at):
            return True
        return False

    def _decode_frame(
        self,
        source: Any,
        source_type: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]],
        start: float,
    ) -> Tuple[Optional[Any], str]:
        """
        Decode a frame from the given source (hot path, no lock needed).

        This method is called after source lookup and does not modify any
        shared state, so no lock is required.
        """

        try:
            if source_type == 'gpu':
                from src.shared.media.gpu_frame import is_gpu_decoded_frame
                decoded = source.read_frame(frame_index)
                if decoded is None:
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, ''
                frame_type = 'gpu' if is_gpu_decoded_frame(decoded) else 'cpu'
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type=frame_type,
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, frame_type

            elif source_type == 'win_cuda_resident':
                # B2.1：cuda 驻留帧（设备指针，无 GL 资源）——GL 导入在
                # 渲染线程完成，此处仅透传 + trace。
                read_resident = getattr(source, "read_frame_resident", None)
                if not callable(read_resident):
                    return None, ''
                decoded = read_resident(frame_index)
                if decoded is None:
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, ''
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type="cuda_resident",
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, 'cuda_resident'

            elif source_type == 'vt_pixel_buffer':
                read_pixel_buffer = getattr(source, "read_frame_pixel_buffer", None)
                if not callable(read_pixel_buffer):
                    return None, ''
                decoded = read_pixel_buffer(frame_index)
                if decoded is None:
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, ''
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type="vt_pixel_buffer",
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, 'vt_pixel_buffer'

            elif source_type in ('planar', 'vt_planar'):
                decoded = self._read_source_frame(source, source_type, frame_index)
                if decoded is None:
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, ''
                frame_type = 'vt_planar' if source_type == 'vt_planar' else 'planar'
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type=frame_type,
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, frame_type

            else:
                decoded = self._read_source_frame(source, source_type, frame_index)
                if decoded is None:
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, ''
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type="cpu",
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, 'cpu'

        except StopIteration:
            playback_trace(
                "decode_manager.get_frame_stop_iteration",
                frame=frame_index,
                source_type=source_type,
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
            return None, ''
        except Exception as e:
            playback_trace(
                "decode_manager.get_frame_error",
                frame=frame_index,
                source_type=source_type,
                elapsed_ms=(time.perf_counter() - start) * 1000,
                error=e,
            )
            return None, ''

    def _decode_frame_interactive(
        self,
        source: Any,
        source_type: str,
        frame_index: int,
        target_size: Optional[Tuple[int, int]],
        start: float,
        *,
        is_stale: Optional[Callable[[], bool]],
        deadline_at: Optional[float],
        coarse: bool = False,
    ) -> Tuple[Optional[Any], str]:
        """Decode with cooperative stale/deadline checks for scrub."""
        if self._interactive_decode_should_abort(is_stale, deadline_at):
            playback_trace(
                "decode_manager.interactive_abort_before_decode",
                frame=frame_index,
                source_type=source_type,
            )
            return None, 'interactive_stale'

        try:
            if source_type == 'vt_pixel_buffer':
                read_interactive = getattr(source, "read_frame_pixel_buffer_interactive", None)
                read_pixel_buffer = getattr(source, "read_frame_pixel_buffer", None)
                if callable(read_interactive):
                    kwargs = {
                        "is_stale": is_stale,
                        "deadline_at": deadline_at,
                    }
                    if coarse:
                        # Fast-shuttle: keyframe-anchored single-frame decode.
                        kwargs["coarse"] = True
                    if self._interactive_should_prefer_seek(source, source_type, frame_index):
                        kwargs["prefer_seek"] = True
                    try:
                        decoded = read_interactive(frame_index, **kwargs)
                    except TypeError:
                        kwargs.pop("prefer_seek", None)
                        kwargs.pop("coarse", None)
                        decoded = read_interactive(frame_index, **kwargs)
                elif callable(read_pixel_buffer):
                    decoded = read_pixel_buffer(frame_index)
                else:
                    return None, ''
                if decoded is None:
                    frame_type = (
                        'interactive_stale'
                        if self._interactive_decode_should_abort(is_stale, deadline_at)
                        else ''
                    )
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        frame_type=frame_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, frame_type
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type="vt_pixel_buffer",
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, 'vt_pixel_buffer'

            if source_type in ('planar', 'vt_planar'):
                decoded = self._read_source_frame_interactive(
                    source,
                    source_type,
                    frame_index,
                    is_stale=is_stale,
                    deadline_at=deadline_at,
                )
                if decoded is None:
                    frame_type = (
                        'interactive_stale'
                        if self._interactive_decode_should_abort(is_stale, deadline_at)
                        else ''
                    )
                    playback_trace(
                        "decode_manager.get_frame_failed",
                        frame=frame_index,
                        source_type=source_type,
                        frame_type=frame_type,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                    return None, frame_type
                frame_type = 'vt_planar' if source_type == 'vt_planar' else 'planar'
                playback_trace(
                    "decode_manager.get_frame_done",
                    frame=frame_index,
                    decoded_frame=getattr(decoded, "frame_index", None),
                    source_type=source_type,
                    frame_type=frame_type,
                    target_size=target_size,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
                return decoded, frame_type

            return self._decode_frame(source, source_type, frame_index, target_size, start)
        except Exception as e:
            if self._interactive_decode_should_abort(is_stale, deadline_at):
                playback_trace(
                    "decode_manager.interactive_decode_aborted",
                    frame=frame_index,
                    source_type=source_type,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                    error=e,
                )
                return None, 'interactive_stale'
            playback_trace(
                "decode_manager.get_frame_error",
                frame=frame_index,
                source_type=source_type,
                elapsed_ms=(time.perf_counter() - start) * 1000,
                error=e,
            )
            return None, ''

    def _interactive_decode_deadline_from_cost(
        self,
        *,
        codec: str,
        pixels: int,
        gap: Optional[int],
        seek_cost: Optional[int],
        has_hot_source: bool,
    ) -> float:
        """Map codec/seek cost into a bounded scrub decode budget."""
        codec_name = (codec or "").lower()
        if is_intra_codec(codec_name):
            deadline_ms = 88.0
        elif not has_hot_source:
            deadline_ms = 56.0
        elif gap == 0:
            deadline_ms = 40.0
        elif gap is not None and 0 < gap <= 8:
            deadline_ms = 80.0
        elif gap is not None and 0 < gap <= _SEQUENTIAL_CATCHUP_MAX_FRAMES:
            deadline_ms = 96.0
        elif gap is not None and 0 < gap <= _INTERACTIVE_POOL_FORWARD_MAX_FRAMES:
            deadline_ms = 96.0
        else:
            cost = seek_cost if seek_cost is not None else 30
            if cost <= 4:
                deadline_ms = 64.0
            elif cost <= 16:
                deadline_ms = 64.0
            elif cost <= 48:
                deadline_ms = 48.0
            else:
                deadline_ms = 40.0

        # 2K/4K H264/H265 on modern Macs should stay in the native hardware
        # path long enough to return a clear frame.  8K+ remains aggressive so
        # dragging cannot get trapped behind an expensive random seek.
        if pixels > 0 and pixels <= 2560 * 1440 and any(
            name in codec_name for name in _H264_H265_CODECS
        ):
            # A 1080p/2K random seek is cheap enough on the target machines to
            # wait for the exact frame. The stale callback still aborts this
            # budget immediately when the mouse moves again.
            deadline_ms = max(deadline_ms, 160.0)
        elif pixels >= 7680 * 4320:
            deadline_ms = min(deadline_ms, 40.0)
        elif pixels >= 3840 * 2160:
            if any(name in codec_name for name in _H264_H265_CODECS):
                deadline_ms += 48.0
            else:
                deadline_ms += 8.0

        return max(
            _INTERACTIVE_DECODE_MIN_DEADLINE_MS,
            min(_INTERACTIVE_DECODE_MAX_DEADLINE_MS, deadline_ms),
        )

    def _select_interactive_source(
        self,
        source_key: _SourceKey,
        primary_source: Any,
        primary_source_type: str,
        frame_index: int,
        *,
        trace_selection: bool = True,
        reserve: bool = False,
    ) -> Tuple[Any, str]:
        """Choose the warm VT source best positioned for this scrub target."""
        if primary_source_type not in ('vt_pixel_buffer', 'vt_planar'):
            return primary_source, primary_source_type

        with self._lock:
            candidates: list[Tuple[Any, str]] = [(primary_source, primary_source_type)]
            candidates.extend(self._interactive_source_pool.get(source_key, ()))
            best_forward: Optional[Tuple[int, int, Any, str]] = None
            best_any: Optional[Tuple[int, int, Any, str]] = None
            for index, (source, source_type) in enumerate(candidates):
                if reserve and self._interactive_busy_sources.get(id(source)) is source:
                    continue
                current_frame = getattr(source, "current_frame", None)
                if not isinstance(current_frame, int):
                    continue
                gap = int(frame_index) - current_frame
                if 0 <= gap <= _INTERACTIVE_POOL_FORWARD_MAX_FRAMES:
                    score = (gap, index, source, source_type)
                    if best_forward is None or score[:2] < best_forward[:2]:
                        best_forward = score
                score_any = (abs(gap), index, source, source_type)
                if best_any is None or score_any[:2] < best_any[:2]:
                    best_any = score_any

            selected = best_forward or best_any
            if selected is None:
                selected_index = 0
                selected_source = primary_source
                selected_type = primary_source_type
            else:
                _, selected_index, selected_source, selected_type = selected
            if reserve:
                self._interactive_busy_sources[id(selected_source)] = selected_source
        if selected_index and trace_selection:
            playback_trace(
                "decode_manager.interactive_pool_source_selected",
                frame=frame_index,
                source_index=selected_index,
                current_frame=getattr(selected_source, "current_frame", None),
                source_type=selected_type,
            )
        return selected_source, selected_type

    def _release_interactive_source(self, source: Any) -> None:
        if source is None:
            return
        with self._lock:
            # 身份匹配才释放：id() 可能被新对象复用，误删会漏掉真正的预约
            if self._interactive_busy_sources.get(id(source)) is source:
                self._interactive_busy_sources.pop(id(source), None)

    def _normalize_target_size(
        self,
        target_size: Optional[Tuple[int, int]],
    ) -> Optional[Tuple[int, int]]:
        if target_size is None:
            return None
        try:
            width, height = int(target_size[0]), int(target_size[1])
        except Exception:
            return None
        if width <= 0 or height <= 0:
            return None
        if width >= _PREVIEW_TARGET_WIDTH_BUCKET * 2:
            aspect = width / max(1, height)
            bucketed_width = max(
                _PREVIEW_TARGET_WIDTH_BUCKET,
                int(round(width / _PREVIEW_TARGET_WIDTH_BUCKET)) * _PREVIEW_TARGET_WIDTH_BUCKET,
            )
            bucketed_height = max(1, int(round(bucketed_width / aspect)))
            if bucketed_height % 2:
                bucketed_height += 1
            width, height = bucketed_width, bucketed_height
        return width, height

    def _read_source_frame(
        self,
        source: Any,
        source_type: str,
        frame_index: int,
    ) -> Optional[Any]:
        current_frame = getattr(source, "current_frame", None)
        if isinstance(current_frame, int):
            gap = frame_index - current_frame
            if 0 <= gap <= _SEQUENTIAL_CATCHUP_MAX_FRAMES:
                decoded = None
                playback_trace(
                    "decode_manager.sequential_catchup_start",
                    frame=frame_index,
                    source_type=source_type,
                    current_frame=current_frame,
                    gap=gap,
                )
                for _ in range(gap + 1):
                    decoded = source.read_next_frame()
                    decoded_index = getattr(decoded, "frame_index", None)
                    if decoded_index == frame_index:
                        playback_trace(
                            "decode_manager.sequential_catchup_done",
                            frame=frame_index,
                            source_type=source_type,
                            decoded_frame=decoded_index,
                        )
                        return decoded
                    if isinstance(decoded_index, int) and decoded_index > frame_index:
                        playback_trace(
                            "decode_manager.sequential_catchup_overshoot",
                            frame=frame_index,
                            source_type=source_type,
                            decoded_frame=decoded_index,
                        )
                        # Overshoot: fall through to direct seek instead of
                        # returning None.  Direct seek repositions the decoder
                        # to the correct frame and avoids the decode failure
                        # that would otherwise cause gpu_prepare_failed.
                        break

        if source_type in ('planar', 'vt_planar'):
            read_planar = getattr(source, 'read_frame_planar', None)
            if callable(read_planar):
                return read_planar(frame_index)
        return source.read_frame(frame_index)

    def _read_source_frame_interactive(
        self,
        source: Any,
        source_type: str,
        frame_index: int,
        *,
        is_stale: Optional[Callable[[], bool]],
        deadline_at: Optional[float],
    ) -> Optional[Any]:
        """Read a source frame while allowing scrub to abort between steps."""
        prefer_seek = self._interactive_should_prefer_seek(source, source_type, frame_index)
        current_frame = getattr(source, "current_frame", None)
        if isinstance(current_frame, int):
            gap = frame_index - current_frame
            if not prefer_seek and 0 <= gap <= _SEQUENTIAL_CATCHUP_MAX_FRAMES:
                decoded = None
                progress = None
                playback_trace(
                    "decode_manager.interactive_sequential_catchup_start",
                    frame=frame_index,
                    source_type=source_type,
                    current_frame=current_frame,
                    gap=gap,
                )
                for _ in range(gap + 1):
                    if self._interactive_decode_should_abort(is_stale, deadline_at):
                        progress_index = getattr(progress, "frame_index", None)
                        if (
                            isinstance(progress_index, int)
                            and 0 <= frame_index - progress_index <= _INTERACTIVE_PROGRESS_MAX_DISTANCE
                        ):
                            playback_trace(
                                "decode_manager.interactive_progress_frame",
                                frame=frame_index,
                                decoded_frame=progress_index,
                                source_type=source_type,
                                reason="abort",
                            )
                            return progress
                        return None
                    read_next_interactive = getattr(source, 'read_next_frame_interactive', None)
                    if callable(read_next_interactive):
                        decoded = read_next_interactive(
                            is_stale=is_stale,
                            deadline_at=deadline_at,
                        )
                    else:
                        decoded = source.read_next_frame()
                    if decoded is None:
                        if progress is not None:
                            progress_index = getattr(progress, "frame_index", None)
                            playback_trace(
                                "decode_manager.interactive_progress_frame",
                                frame=frame_index,
                                decoded_frame=progress_index,
                                source_type=source_type,
                                reason="read_none",
                            )
                            return progress
                        return None
                    decoded_index = getattr(decoded, "frame_index", None)
                    if decoded_index == frame_index:
                        playback_trace(
                            "decode_manager.interactive_sequential_catchup_done",
                            frame=frame_index,
                            source_type=source_type,
                            decoded_frame=decoded_index,
                        )
                        return decoded
                    if (
                        isinstance(decoded_index, int)
                        and decoded_index < frame_index
                        and frame_index - decoded_index <= _INTERACTIVE_PROGRESS_MAX_DISTANCE
                    ):
                        progress = decoded
                    if isinstance(decoded_index, int) and decoded_index > frame_index:
                        break

        if self._interactive_decode_should_abort(is_stale, deadline_at):
            return None

        if source_type in ('planar', 'vt_planar'):
            read_interactive = getattr(source, 'read_frame_planar_interactive', None)
            if callable(read_interactive):
                kwargs = {
                    "is_stale": is_stale,
                    "deadline_at": deadline_at,
                }
                if prefer_seek:
                    kwargs["prefer_seek"] = True
                try:
                    return read_interactive(frame_index, **kwargs)
                except TypeError:
                    kwargs.pop("prefer_seek", None)
                    return read_interactive(frame_index, **kwargs)
            read_planar = getattr(source, 'read_frame_planar', None)
            if callable(read_planar):
                return read_planar(frame_index)

        read_interactive = getattr(source, 'read_frame_interactive', None)
        if callable(read_interactive):
            kwargs = {
                "is_stale": is_stale,
                "deadline_at": deadline_at,
            }
            if prefer_seek:
                kwargs["prefer_seek"] = True
            try:
                return read_interactive(frame_index, **kwargs)
            except TypeError:
                kwargs.pop("prefer_seek", None)
                return read_interactive(frame_index, **kwargs)
        return source.read_frame(frame_index)

    def _interactive_should_prefer_seek(
        self,
        source: Any,
        source_type: str,
        frame_index: int,
    ) -> bool:
        """Prefer keyframe seek when it is cheaper than VT forward catch-up."""
        current_frame = getattr(source, "current_frame", None)
        if not isinstance(current_frame, int):
            return False
        gap = int(frame_index) - current_frame

        if source_type == 'planar':
            # FFmpeg intra codecs (DNxHR/ProRes on the CPU planar path): every
            # frame is a keyframe and seek is exact + cheap (~5ms with slice
            # threading). Sequential catch-up costs gap x per-frame and is
            # always the worse option, so jump straight to the target.
            metadata = getattr(source, "metadata", None)
            codec = getattr(metadata, "codec_name", "") if metadata is not None else ""
            if is_intra_codec(str(codec or "").lower()):
                return gap != 0
            return False

        if source_type not in ('vt_pixel_buffer', 'vt_planar'):
            return False
        if gap <= _SEQUENTIAL_CATCHUP_MAX_FRAMES:
            return False
        if gap > _INTERACTIVE_POOL_FORWARD_MAX_FRAMES:
            return True
        metadata = getattr(source, "metadata", None)
        codec = getattr(metadata, "codec_name", "") if metadata is not None else ""
        seek_cost = estimate_seek_cost(
            current_frame,
            int(frame_index),
            codec or "",
            getattr(source, "keyframe_interval_hint", None),
        )
        return seek_cost + 8 < gap

    def _get_or_create_source(
        self,
        asset_path: str,
        target_size: Optional[Tuple[int, int]] = None,
        stream_key: Optional[str] = None,
        *,
        source_spec: Optional[_DecodeSourceSpec] = None,
        for_playback: bool = False,
    ) -> Optional[Any]:
        """Get cached source or create a new one.

        P2-09 租约契约：返回非 None 的 source 时自带一个租约（避免调用方
        在取出与解码之间被 LRU 逐出关闭），调用方用完必须
        ``_release_source(source_key)`` 归还。
        """
        # AUD-16503：close_all 之后管理器已"关闭"（_closing=True 且永不
        # 复位，逐出也被禁用）——迟到解码在这里重建会话并写入 _sources
        # 将永久泄漏 VT/FFmpeg 会话，直接放行 None（调用方走 CPU 回退/
        # 持帧）。
        if self._closing:
            return None
        normalized_target_size = self._normalize_target_size(target_size)
        if source_spec is None:
            source_spec = self._resolve_preview_decode_source(
                asset_path, normalized_target_size, for_playback=for_playback,
            )
        decode_stream_key = self._normalize_stream_key(asset_path, stream_key)
        source_key = self._make_source_key(decode_stream_key, source_spec)
        with self._lock:
            if source_key in self._sources:
                self._lease_source(source_key)
                return self._sources[source_key]
            if source_key in self._failed_source_keys:
                # 负缓存：该 key 曾创建失败，本次直接放行 None（调用方走
                # CPU 回退/持帧），避免播放期间逐帧重试失败的 av.open。
                return None
            warmup_event = self._warmup_events.get(source_key)

        if warmup_event is not None:
            playback_trace(
                "decode_manager.wait_for_warmup",
                asset=asset_path,
                decode_asset=source_spec.decode_path,
                source_kind=source_spec.source_kind,
                wait_ms=int(_WARMUP_SOURCE_WAIT_SECONDS * 1000),
            )
            if not warmup_event.wait(_WARMUP_SOURCE_WAIT_SECONDS):
                # A primary source is already being created for this exact
                # clip stream. Starting another VT session here creates the
                # startup contention that makes the first timeline playback
                # stutter. Hold this frame; the next tick will consume the
                # warmed source once the single owner finishes.
                playback_trace(
                    "decode_manager.warmup_still_pending_hold",
                    asset=asset_path,
                    decode_asset=source_spec.decode_path,
                    source_kind=source_spec.source_kind,
                )
                return None
            with self._lock:
                if source_key in self._sources:
                    self._lease_source(source_key)
                    return self._sources[source_key]

        # Create source outside lock (can be slow)
        source, source_type = self._create_source(
            source_spec.decode_path,
            source_spec.target_size,
            vt_policy=source_spec.vt_policy,
        )
        if source is None:
            with self._lock:
                if len(self._failed_source_keys) >= _MAX_FAILED_SOURCE_KEYS:
                    # 规模护栏：负缓存只防"每帧重试失败 open"，超限整体
                    # 清空，让后续请求重新探测（文件可能已修复/补装）。
                    self._failed_source_keys.clear()
                self._failed_source_keys.add(source_key)
            return None

        loser: Optional[Any] = None
        victims: list = []
        won_existing = False
        existing: Optional[Any] = None
        with self._lock:
            # 创建成功即清除可能的失败标记（如库补装/文件修复后）
            self._failed_source_keys.discard(source_key)
            # Double-check after acquiring lock
            if source_key in self._sources:
                # 竞速输家：复用已有会话；输家对象在锁外关闭（close VT
                # 会话可能阻塞，别占管理器锁）
                loser = source
                existing = self._sources[source_key]
                self._lease_source(source_key)
                won_existing = True
            else:
                self._sources[source_key] = source
                self._source_types[source_key] = source_type
                self._source_last_used[source_key] = time.perf_counter()
                # 新插入同样带租约返回（调用方解码期间受逐出保护）
                self._lease_source(source_key)
                victims = self._evict_sources_if_over_cap()
        self._close_source_object(loser, source_key)
        self._close_eviction_victims(victims)
        if won_existing:
            return existing
        return source

    def wait_for_warmups(self, timeout: float = 1.0) -> bool:
        """Wait for active source warmups to finish; used by tests/diagnostics."""
        deadline = time.perf_counter() + max(0.0, float(timeout))
        while True:
            with self._lock:
                events = (
                    list(self._warmup_events.values())
                    + list(self._interactive_pool_warmup_events.values())
                )
            if not events:
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return False
            for event in events:
                event.wait(max(0.0, min(remaining, 0.05)))

    def _invalidate_warmups_locked(self) -> list[threading.Event]:
        self._warmup_generation += 1
        events = list(self._warmup_events.values())
        self._warmup_events.clear()
        events.extend(self._interactive_pool_warmup_events.values())
        self._interactive_pool_warmup_events.clear()
        return events

    def _cancel_interactive_prefetches_locked(self) -> list[threading.Event]:
        self._interactive_prefetch_generation += 1
        self._interactive_prefetch_tokens.clear()
        return list(self._interactive_prefetch_events)

    @staticmethod
    def _wait_for_events(events: list[threading.Event], timeout: float = 0.12) -> None:
        if not events:
            return
        deadline = time.perf_counter() + max(0.0, timeout)
        for event in events:
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                return
            event.wait(min(remaining, 0.03))

    def _close_source_object(self, source: Any, key: Any) -> None:
        if source is None:
            return
        try:
            source.close()
        except Exception as e:
            logger.warning(f"EditGpuDecodeManager: source close error for {key}: {e}")

    def _create_source(
        self,
        asset_path: str,
        target_size: Optional[Tuple[int, int]] = None,
        *,
        vt_policy: str = 'auto',
    ) -> Tuple[Optional[Any], str]:
        """
        Create the best available decode source for the given asset.

        Decode path priority:
        1. GPU zero-copy (MacOSVtZeroCopySource) for H264/H265
        2. CPU planar YUV for ProRes/DNxHR/VP8 (avoids CPU YUV→RGB)
        3. CPU planar for H264/H265 (GPU upload via YUV shader)
        4. CPU packed fallback for other formats

        Args:
            vt_policy: 'prefer_ffmpeg' skips the VideoToolbox branches — used
                by sequential playback when the hardware probe says VT would
                run a software decoder for this format.

        Returns:
            (source, source_type): source_type is 'gpu', 'planar', or 'cpu'.
        """
        # 虚拟素材（topos:// scheme）没有磁盘文件，任何解码路径都会失败 ——
        # 提前返回 (None, 'virtual')，避免触发 factory.create → av.open 抛
        # ProtocolNotFoundError 并刷 "CPU source failed" 日志。这些素材的画面
        # 由渲染层程序化生成（adjustment = 后处理 pass，solid = 填色）。
        if isinstance(asset_path, str) and asset_path.startswith("topos://"):
            return None, 'virtual'
        # Route by container signature before codec probing or hardware setup.
        # PyAV sees TPIC as an unknown QuickTime codec and cannot open it.
        if self._is_topos_movie_path(asset_path):
            return self._create_topos_source(asset_path, target_size,
                                             self._no_deblock)
        codec = self._probe_codec(asset_path)
        native_vt_preview = (
            sys.platform != 'darwin'
            or (
                vt_policy != 'prefer_ffmpeg'
                and self._should_use_vt_planar_source(asset_path, target_size)
            )
        )

        # H.264/H.265 2K/4K edit playback should prefer native VideoToolbox
        # decode even when the viewer requests a lower preview size. The GPU
        # YUV pass already renders into the preview FBO, so scaling belongs on
        # the GPU instead of forcing FFmpeg to downscale on the CPU first.
        if native_vt_preview and vt_policy == 'auto':
            # 1. Try MediaSourceFactory for VT zero-copy source.
            #    (hw_or_ffmpeg skips this branch: the factory cannot enforce
            #    the hardware requirement, and an auto session may silently
            #    come back software.)
            try:
                from src.shared.media.factory import MediaSourceFactory
                from src.shared.media.decode_policy import DecodeTarget

                factory = MediaSourceFactory()
                source, status = factory.create_with_hwaccel(
                    path=asset_path,
                    decode_target=DecodeTarget.PREVIEW,
                    backend='ffmpeg',
                    auto_open=False,
                )

                if source is not None:
                    hw_type = getattr(status, 'decoder_type', 'none')
                    hw_enabled = getattr(status, 'hwaccel_enabled', False)

                    # VT zero-copy source already opened by factory.  Do not bind
                    # IOSurface to GL textures here: PreviewRenderWorker has no
                    # current GL context.  Return unbound CVPixelBuffer frames and
                    # let EditGpuPreviewRenderer import them on its GL thread.
                    if hw_enabled and 'zero_copy' in hw_type:
                        playback_trace(
                            "decode_manager.create_source",
                            source_type="vt_pixel_buffer",
                            decoder=hw_type,
                            target_size=None,
                            asset=asset_path,
                        )
                        logger.info(
                            f"EditGpuDecodeManager: GPU zero-copy source for {asset_path}, "
                            f"decoder={hw_type}"
                        )
                        return source, 'vt_pixel_buffer'

                    # Packed hardware downloads still make a GPU -> CPU -> GPU
                    # round trip, so the planar path below is preferred.
                    source.close()
            except Exception as e:
                logger.debug(f"EditGpuDecodeManager: hwaccel source failed: {e}")

        # 2. Try MacOSVtZeroCopySource directly
        if sys.platform == 'darwin' and native_vt_preview:
            try:
                from src.shared.media.macos_vt.source import MacOSVtZeroCopySource
                from src.shared.media.macos_vt.capability import is_zero_copy_available

                if is_zero_copy_available(codec=codec):
                    vt_source = MacOSVtZeroCopySource()
                    if vt_source.open(
                        asset_path,
                        require_hardware=(vt_policy == 'hw_or_ffmpeg'),
                    ):
                        playback_trace(
                            "decode_manager.create_source",
                            source_type="vt_pixel_buffer",
                            decoder="macos_vt_zero_copy",
                            codec=codec,
                            target_size=None,
                            asset=asset_path,
                            hw_required=(vt_policy == 'hw_or_ffmpeg'),
                        )
                        logger.info(
                            f"EditGpuDecodeManager: VT zero-copy source for {asset_path}"
                        )
                        return vt_source, 'vt_pixel_buffer'
                    else:
                        vt_source.close()
                        if vt_policy == 'hw_or_ffmpeg':
                            playback_trace(
                                "decode_manager.hw_session_denied_ffmpeg_fallback",
                                codec=codec,
                                asset=asset_path,
                                reason=getattr(vt_source, "_fallback_reason", ""),
                            )
            except Exception as e:
                logger.debug(f"EditGpuDecodeManager: VT zero-copy attempt failed: {e}")

        # 3. macOS safe hardware fallback: VideoToolbox decode, then copy only
        # native NV12/P010 planes. This avoids the broken CGL bridge as well as
        # the far more expensive GPU -> packed RGB -> GPU round trip.
        # (hw_or_ffmpeg skips this branch — a software VT planar download is
        # exactly the decode speed this policy exists to avoid.)
        if (
            sys.platform == 'darwin'
            and codec in {'h264', 'h265', 'hevc'}
            and native_vt_preview
            and vt_policy == 'auto'
        ):
            try:
                from src.shared.media.macos_vt.source import MacOSVtPlanarSource

                vt_planar_source = MacOSVtPlanarSource()
                if vt_planar_source.open(asset_path):
                    playback_trace(
                        "decode_manager.create_source",
                        source_type="vt_planar",
                        decoder="videotoolbox",
                        codec=codec,
                        target_size=target_size,
                        asset=asset_path,
                    )
                    logger.info(
                        "EditGpuDecodeManager: VideoToolbox planar source for %s "
                        "(safe CVPixelBuffer YUV download)",
                        asset_path,
                    )
                    return vt_planar_source, 'vt_planar'
                vt_planar_source.close()
            except Exception as e:
                logger.debug(f"EditGpuDecodeManager: VT planar source failed: {e}")

        # 3.4b Windows cuda 驻留零拷贝（计划表 B B2.1，opt-in 默认关闭）。
        # B1.2 证据（docs/poc_interop_b12_windows_2026-10.md）：4K H264
        # 241fps / H265-10bit 306fps（vs 下载路径 102/80fps），0 CPU 字节。
        # 门禁：显式 opt-in（TOPOS_WIN_HW_RESIDENT_DECODE）+ cuda 绑定
        # 层在位；失败/不支持按 fail-closed 回落 3.5 下载 planar（不冒充
        # 驻留）。帧以 CudaResidentFrame 形态（设备指针，无 GL 资源）经
        # 管线传递，渲染 GL 线程导入——解码线程不触 CUDA API。
        # W03：按素材降级门（note_resident_import_failure 记账）在全局
        # 门闩之前——单素材导入失败只停该素材的驻留路由，其他素材不受
        # 影响；被降级的素材直接落到 3.5 下载 planar 重建。
        if (
            sys.platform == 'win32'
            and codec in {'h264', 'h265', 'hevc'}
            and vt_policy == 'auto'
            and not os.environ.get('TOPOS_DISABLE_WIN_HW_DECODE')
            and (
                os.environ.get('TOPOS_FORCE_WIN_HW_DECODE')
                or HardwareCapability.has_nvidia_gpu()
            )
        ):
            from src.shared.media.windows_hw.cuda_resident import (
                probe_cuda_resident_capability,
                resident_route_available,
            )

            demote_allowed, demote_reason = self._resident_route_allowed_for(
                asset_path)
            if not demote_allowed:
                playback_trace(
                    "decode_manager.resident_route_demoted",
                    asset=asset_path,
                    reason=demote_reason,
                )
                logger.info(
                    "EditGpuDecodeManager: cuda resident route demoted for "
                    "%s (%s), using download/CPU path",
                    asset_path, demote_reason,
                )
            elif resident_route_available():
                available, cap_reason = probe_cuda_resident_capability()
                if available:
                    try:
                        from src.shared.media.windows_hw.cuda_resident import (
                            WindowsCudaResidentSource,
                        )

                        resident_source = WindowsCudaResidentSource(asset_path)
                        resident_source.open()  # 验证首帧驻留为 cuda 格式
                        playback_trace(
                            "decode_manager.create_source",
                            source_type="win_cuda_resident",
                            decoder="nvdec_is_hw_owned",
                            contract_format=resident_source.resident_contract_format,
                            hw_output_format=resident_source.hw_output_format,
                            codec=codec,
                            target_size=None,
                            asset=asset_path,
                        )
                        logger.info(
                            "EditGpuDecodeManager: Windows cuda resident "
                            "source for %s (contract=%s)",
                            asset_path,
                            resident_source.resident_contract_format,
                        )
                        return resident_source, 'win_cuda_resident'
                    except Exception as e:
                        logger.info(
                            "EditGpuDecodeManager: cuda resident unavailable "
                            "for %s (%s), falling back to hw download planar",
                            asset_path, e,
                        )
                else:
                    logger.info(
                        "EditGpuDecodeManager: cuda resident capability "
                        "gate failed (%s), using hw download planar",
                        cap_reason,
                    )

        # 3.5 Windows hardware decode planar (NVDEC/D3D11 → NV12/P010 download).
        # Mirrors the macOS VT planar branch above: decode on the GPU ASIC and
        # download tight semi-planar YUV, keeping scaling on the GPU. This moves
        # the H.264/H.265 decode cost off the CPU cores (key for multi-track
        # timelines and for Windows machines without a high-end CPU).
        # Streams the hardware cannot decode (e.g. H.264 4:2:2 10-bit) raise
        # MediaOpenError inside open() and fall through to the CPU path below.
        # Kill switch: TOPOS_DISABLE_WIN_HW_DECODE=1
        #
        # 硬解优先仅限 NVIDIA GPU（或 TOPOS_FORCE_WIN_HW_DECODE=1 显式开启）。
        # 基准依据（scripts/bench_hw_decode_windows.py，AMD RX 6800 +
        # 16C/24T CPU，真实素材）：PyAV HWAccel 对每帧做同步 GPU→CPU 下载
        # （is_hw_owned=False 是固定行为，且无公开 API 可流水线化），D3D11VA
        # 解码 4K H.264 仅 ~47fps（21ms/帧）、4K HEVC 10bit ~43fps；同机
        # CPU frame-threaded planar 解码为 4K H.264 ~486fps、4K HEVC
        # ~172fps。非 NVIDIA（AMD/Intel GPU）上硬解默认劣于 CPU 多线程，
        # 故默认走下方 CPU planar 路径。
        if (
            sys.platform == 'win32'
            and codec in {'h264', 'h265', 'hevc'}
            and vt_policy == 'auto'
            and not os.environ.get('TOPOS_DISABLE_WIN_HW_DECODE')
            and (
                os.environ.get('TOPOS_FORCE_WIN_HW_DECODE')
                or HardwareCapability.has_nvidia_gpu()
            )
        ):
            try:
                from src.shared.media.windows_hw.hw_planar_source import (
                    WindowsHwPlanarSource,
                )

                hw_source = WindowsHwPlanarSource(asset_path)
                hw_source.open()  # verifies hw output is NV12/P010-family
                playback_trace(
                    "decode_manager.create_source",
                    source_type="win_hw_planar",
                    decoder=hw_source.active_decoder.value
                    if hw_source.active_decoder else "auto",
                    hw_output_format=hw_source.hw_output_format,
                    codec=codec,
                    target_size=None,  # native decode; GPU scales
                    asset=asset_path,
                )
                logger.info(
                    "EditGpuDecodeManager: Windows hw planar source for %s "
                    "(decoder=%s, output=%s)",
                    asset_path,
                    hw_source.active_decoder.value if hw_source.active_decoder else "auto",
                    hw_source.hw_output_format,
                )
                return hw_source, 'vt_planar'
            except Exception as e:
                logger.info(
                    "EditGpuDecodeManager: Windows hw planar unavailable for %s "
                    "(%s), falling back to CPU planar",
                    asset_path, e,
                )

        # 3.6 Topos 私有编码（TPIC）：FFmpeg/PyAV 没有该解码器，硬编码的
        # CPU packed 路径对它必然失败（曾造成播放期间逐帧 "CPU source
        # failed"）。Topos 与其它 planar intra codec 不同：V7-B 可在
        # container 层只读取独立 <=2K base packet。预览目标必须传入 native
        # source，不能沿用“完整解码再 GPU 缩放”的 ProRes 路由，否则 6K/8K/
        # 12K 会重新落回全分辨率 entropy/residual 工作量。旧流仍由 source
        # 的 reduced fallback 兼容处理。
        if codec == 'topos':
            return self._create_topos_source(asset_path, target_size,
                                             self._no_deblock)

        # 4. CPU path: use planar mode for ProRes/DNxHR AND H.264/H.265
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
            prefer_planar = (
                any(c in codec for c in _PLANAR_PREFERRED_CODECS)
                or any(c in codec for c in _H264_H265_CODECS)
            )
            source = FFmpegMediaSource(
                asset_path,
                target_size=target_size,
                preserve_planes=prefer_planar,
                output_mode="planar_yuv" if prefer_planar else "packed_rgb",
            )
            source.open()

            if any(c in codec for c in _PLANAR_PREFERRED_CODECS):
                playback_trace(
                    "decode_manager.create_source",
                    source_type="planar",
                    codec=codec,
                    target_size=target_size,
                    asset=asset_path,
                )
                logger.info(
                    f"EditGpuDecodeManager: CPU planar source for {asset_path} "
                    f"(codec={codec})"
                )
                return source, 'planar'

            # H.264/H.265: try planar decode + GPU upload before packed RGB fallback.
            # This avoids the CPU YUV→RGB conversion by uploading planar data to GPU shaders.
            if any(c in codec for c in _H264_H265_CODECS):
                # Check if planar read is supported
                if hasattr(source, 'read_frame_planar'):
                    playback_trace(
                        "decode_manager.create_source",
                        source_type="planar",
                        codec=codec,
                        target_size=target_size,
                        asset=asset_path,
                    )
                    logger.info(
                        f"EditGpuDecodeManager: H.264/H.265 planar source for {asset_path} "
                        f"(codec={codec}, will use GPU YUV upload)"
                    )
                    return source, 'planar'

            playback_trace(
                "decode_manager.create_source",
                source_type="cpu",
                codec=codec,
                target_size=target_size,
                asset=asset_path,
            )
            logger.info(f"EditGpuDecodeManager: CPU source for {asset_path}")
            return source, 'cpu'

        except Exception as e:
            logger.error(f"EditGpuDecodeManager: CPU source failed for {asset_path}: {e}")
            return None, 'cpu'

    def _is_topos_movie_path(self, asset_path: str) -> bool:
        """Check the Topos MOV container signature without invoking FFmpeg."""
        if not asset_path or asset_path.startswith("topos://"):
            return False
        cached = self._topos_movie_cache.get(asset_path)
        if cached is not None:
            return cached
        try:
            from src.shared.media.topos_source import is_topos_movie
            result = is_topos_movie(asset_path)
        except ImportError:
            result = False
        self._topos_movie_cache[asset_path] = result
        return result

    @staticmethod
    def _create_topos_source(
        asset_path: str,
        target_size: Optional[Tuple[int, int]],
        no_deblock: bool = False,
    ) -> Tuple[Optional[Any], str]:
        """Open the native Topos planar decoder for a TPIC movie."""
        source: Optional[Any] = None
        try:
            from src.shared.media.factory import MediaSourceFactory

            source = MediaSourceFactory.create(
                asset_path, backend='topos', target_size=target_size,
                no_deblock=no_deblock,
            )
            source.open()
            playback_trace(
                "decode_manager.create_source",
                source_type="topos_planar",
                codec="topos",
                target_size=target_size,
                no_deblock=no_deblock,
                asset=asset_path,
            )
            logger.info(
                "EditGpuDecodeManager: Topos planar source for %s", asset_path,
            )
            return source, 'planar'
        except Exception as exc:
            if source is not None:
                try:
                    source.close()
                except Exception as close_exc:
                    logger.warning(
                        "EditGpuDecodeManager: failed to close unusable Topos "
                        "source for %s: %s",
                        asset_path, close_exc,
                    )
            logger.error(
                "EditGpuDecodeManager: Topos source failed for %s: %s",
                asset_path, exc,
            )
            return None, 'planar'

    def _probe_codec(self, asset_path: str) -> str:
        """Probe codec name for an asset (cached)."""
        # 虚拟素材（topos:// scheme）没有可探测的 codec —— 提前返回，避免
        # 触发 factory 探测链刷 ProtocolNotFoundError 日志。
        if isinstance(asset_path, str) and asset_path.startswith("topos://"):
            return ''
        cached = self._codec_cache.get(asset_path)
        if cached is not None and cached.lower() != 'unknown':
            return cached
        if self._is_topos_movie_path(asset_path):
            self._codec_cache[asset_path] = 'topos'
            return 'topos'
        if cached is not None:
            return cached
        try:
            from src.shared.media.factory import MediaSourceFactory
            metadata = MediaSourceFactory.probe_video_metadata(asset_path)
            codec = metadata.codec_name.lower() if metadata else ''
            if codec:
                self._codec_cache[asset_path] = codec
            return codec
        except Exception:
            return ''

    def _probe_resolution(self, asset_path: str) -> Tuple[int, int]:
        """Probe video resolution for an asset."""
        cached = self._resolution_cache.get(asset_path)
        if cached is not None:
            return cached
        try:
            from src.shared.media.factory import MediaSourceFactory
            metadata = MediaSourceFactory.probe_video_metadata(asset_path)
            if metadata:
                resolution = (metadata.width or 0, metadata.height or 0)
                self._resolution_cache[asset_path] = resolution
                return resolution
            return 0, 0
        except Exception:
            return 0, 0

    def _probe_pix_fmt(self, asset_path: str) -> str:
        """Probe the source pixel format (cached; feeds bit-depth routing)."""
        cached = self._pixfmt_cache.get(asset_path)
        if cached is not None:
            return cached
        try:
            from src.shared.media.factory import MediaSourceFactory
            metadata = MediaSourceFactory.probe_video_metadata(asset_path)
            pix_fmt = str(getattr(metadata, 'pixel_format', '') or '') if metadata else ''
            if pix_fmt and pix_fmt.lower() != 'unknown':
                self._pixfmt_cache[asset_path] = pix_fmt
            return pix_fmt
        except Exception:
            return ''

    def _should_use_vt_planar_source(
        self,
        asset_path: str,
        target_size: Optional[Tuple[int, int]],
    ) -> bool:
        """Return whether native VT should be attempted for preview decode.

        H.264/H.265 are the normal camera/editing codecs modern Macs decode
        efficiently in VideoToolbox. Even when the viewer asks for a smaller
        preview, the preferred path is native decode at coded dimensions plus
        GPU YUV conversion/scale into the preview FBO.

        ProRes joins the native-VT group: VideoToolbox's own ProRes decoder
        (hardware on T2/Apple Silicon, optimized software elsewhere) beats the
        FFmpeg software decoder, and the zero-copy CVPixelBuffer output skips
        the per-frame planar CPU copies entirely. GPU scaling handles the
        smaller preview target.

        For other non-H26x sources, keep the older guard:
        ``MacOSVtPlanarSource`` is a lossless CVPixelBuffer download and does
        not satisfy a smaller preview target. Those codecs should use FFmpeg
        planar downscale unless the requested target is at least native size.
        """
        codec = self._probe_codec(asset_path)
        if any(c in codec for c in _H264_H265_CODECS):
            return True
        if 'prores' in codec:
            # 调优开关：Intel 核显上 VT 的 ProRes 是软件解码，多轨播放
            # 吞吐可能不如 FFmpeg slice 线程（1acacd1c 实测 DNxHR 提速
            # ~6 倍的同机制）。TOPOS_PRORES_DECODE=ffmpeg 强制走 FFmpeg
            # planar 降采样路径（A/B 对比与用户调参用）。
            if os.environ.get('TOPOS_PRORES_DECODE', '').lower() == 'ffmpeg':
                return False
            return True
        if target_size is None:
            return True
        source_w, source_h = self._probe_resolution(asset_path)
        if source_w <= 0 or source_h <= 0:
            # Keep the hardware path when metadata is unavailable. It remains
            # the most reliable fallback for unknown sources.
            return True
        target_w, target_h = target_size
        return target_w >= source_w and target_h >= source_h

    def close_source(self, asset_path: str) -> None:
        """Close and remove a specific source."""
        # Clear cache
        with self._lock:
            prefetch_events = self._cancel_interactive_prefetches_locked()
            preposition_events = [
                event
                for key, event in list(self._preposition_inflight.items())
                if key[1] == asset_path
            ]
            for key in [k for k in self._preposition_inflight if k[1] == asset_path]:
                self._preposition_inflight.pop(key, None)
            for key in [k for k in self._stream_locks if k[1] == asset_path]:
                self._stream_locks.pop(key, None)
        # AUD-16501：取消信号已发（generation bump），给预置位线程 2s 的
        # 有界收尾窗口（旧实现只靠 0.3s 协作事件等待）。
        self._join_interactive_prepositions(asset_path=asset_path)
        self._wait_for_events(prefetch_events)
        self._wait_for_events(preposition_events, timeout=0.3)

        # P2-09：关闭前排水——等该素材在飞租约归零（含此前推迟关闭的
        # 单元）；超时未归零的推迟关闭，绝不在解码中途 close 会话。
        with self._lock:
            drain_keys = [
                key for key in self._sources if key[1] == asset_path
            ] + [
                key for key in self._deferred_close_sources
                if key[1] == asset_path
            ]
        self._wait_for_source_leases(drain_keys)

        close_now: list[Tuple[_SourceKey, Any]] = []
        with self._lock:
            warmup_events = self._invalidate_warmups_locked()
            for key in list(self._source_cache.keys()):
                if key[1] == asset_path:
                    self._source_cache.pop(key, None)
            keys = [key for key in self._sources if key[1] == asset_path]
            units: list[Tuple[_SourceKey, list]] = []
            for key in keys:
                source = self._sources.pop(key)
                pool_clones = [
                    clone
                    for clone, _source_type in self._interactive_source_pool.pop(key, [])
                ]
                self._source_types.pop(key, None)
                self._source_last_used.pop(key, None)
                units.append((key, [source, *pool_clones]))
            close_now = self._partition_close_units_locked(units)
            close_now.extend(self._sweep_deferred_close_locked(asset_path))
            # P2-09：随素材清除失败负缓存与交互预约身份（重装/重链后可重新探测）
            for key in [k for k in self._failed_source_keys if k[1] == asset_path]:
                self._failed_source_keys.discard(key)
            for _key, source in close_now:
                if source is not None and self._interactive_busy_sources.get(id(source)) is source:
                    self._interactive_busy_sources.pop(id(source), None)
        for event in warmup_events:
            event.set()

        seen: set[int] = set()
        for _key, source in close_now:
            if source is None:
                continue
            source_id = id(source)
            if source_id in seen:
                continue
            seen.add(source_id)
            try:
                source.close()
            except Exception as e:
                logger.warning(f"EditGpuDecodeManager: close error for {asset_path}: {e}")

    def close_all(self) -> None:
        """Close all sources."""
        self._closing = True
        self._join_playback_prefetches()
        # Clear cache
        with self._lock:
            prefetch_events = self._cancel_interactive_prefetches_locked()
            preposition_events = list(self._preposition_inflight.values())
            self._preposition_inflight.clear()
            self._stream_locks.clear()
        # AUD-16501：取消信号（generation bump）已发，交互预置位线程与
        # 播放预取同等待遇（2s join），不再只靠 0.3s 协作事件等待。
        self._join_interactive_prepositions()
        self._wait_for_events(prefetch_events)
        self._wait_for_events(preposition_events, timeout=0.3)

        # P2-09：关闭前排水——等在飞租约归零（含此前推迟关闭的单元）；
        # 超时未归零的推迟关闭，绝不在解码中途 close 会话。
        with self._lock:
            drain_keys = (
                list(self._sources.keys())
                + list(self._deferred_close_sources.keys())
            )
        self._wait_for_source_leases(drain_keys)

        with self._lock:
            warmup_events = self._invalidate_warmups_locked()
            self._source_cache.clear()
            sources = dict(self._sources)
            self._sources.clear()
            self._source_types.clear()
            units = [
                (
                    key,
                    [source] + [
                        clone
                        for clone, _source_type in self._interactive_source_pool.pop(key, [])
                    ],
                )
                for key, source in sources.items()
            ]
            self._interactive_source_pool.clear()
            self._metadata_warmups.clear()
            # P2-09：LRU/负缓存随整页关闭一并清空；租约计数交由
            # _partition_close_units_locked 按归零/推迟分别处理。
            self._source_last_used.clear()
            self._failed_source_keys.clear()
            self._interactive_busy_sources.clear()
            # W03：整页关闭=新会话，按素材驻留降级记账一并重置（正在执行
            # 的降级关闭线程持有对象引用、幂等收尾，不受清理影响）。
            self._resident_demoted.clear()
            close_now = self._partition_close_units_locked(units)
            close_now.extend(self._sweep_deferred_close_locked())
        for event in warmup_events:
            event.set()

        seen: set[int] = set()
        for key, source in close_now:
            if source is None:
                continue
            source_id = id(source)
            if source_id in seen:
                continue
            seen.add(source_id)
            try:
                source.close()
            except Exception as e:
                logger.warning(f"EditGpuDecodeManager: close error for {key}: {e}")

    def invalidate_all_sources(self) -> None:
        """Close and clear all cached decode sources (e.g. when preview quality changes)."""
        self._join_playback_prefetches()
        with self._lock:
            prefetch_events = self._cancel_interactive_prefetches_locked()
            preposition_events = list(self._preposition_inflight.values())
            self._preposition_inflight.clear()
            self._stream_locks.clear()
        # AUD-16501：取消信号（generation bump）已发，交互预置位线程与
        # 播放预取同等待遇（2s join）。
        self._join_interactive_prepositions()
        self._wait_for_events(prefetch_events)
        self._wait_for_events(preposition_events, timeout=0.3)

        # P2-09：关闭前排水——等在飞租约归零（含此前推迟关闭的单元）；
        # 超时未归零的推迟关闭，绝不在解码中途 close 会话。
        with self._lock:
            drain_keys = (
                list(self._sources.keys())
                + list(self._deferred_close_sources.keys())
            )
        self._wait_for_source_leases(drain_keys)

        with self._lock:
            warmup_events = self._invalidate_warmups_locked()
            self._source_cache.clear()
            sources = dict(self._sources)
            self._sources.clear()
            self._source_types.clear()
            units = [
                (
                    key,
                    [source] + [
                        clone
                        for clone, _source_type in self._interactive_source_pool.pop(key, [])
                    ],
                )
                for key, source in sources.items()
            ]
            self._interactive_source_pool.clear()
            self._codec_cache.clear()
            self._topos_movie_cache.clear()
            # P2-09：画质切换后文件环境可能变化，LRU/负缓存一并清空，让
            # 所有素材重新探测（负缓存不清会永久钉死修复过的素材）；租约
            # 计数交由 _partition_close_units_locked 按归零/推迟分别处理。
            self._source_last_used.clear()
            self._failed_source_keys.clear()
            self._interactive_busy_sources.clear()
            close_now = self._partition_close_units_locked(units)
            close_now.extend(self._sweep_deferred_close_locked())
        for event in warmup_events:
            event.set()

        seen: set[int] = set()
        for key, source in close_now:
            if source is None:
                continue
            source_id = id(source)
            if source_id in seen:
                continue
            seen.add(source_id)
            try:
                source.close()
            except Exception as e:
                logger.warning(f"EditGpuDecodeManager: invalidate close error for {key}: {e}")

        logger.debug(f"EditGpuDecodeManager: invalidated all sources ({len(seen)} closed)")

    def is_gpu_source(self, asset_path: str) -> bool:
        """Check if the source for a given asset is GPU zero-copy."""
        with self._lock:
            return any(
                key[1] == asset_path and source_type in ('gpu', 'vt_pixel_buffer')
                for key, source_type in self._source_types.items()
            )
