"""
Timeline Decode Scheduler
多轨解码调度器（规格书 §9 / Stage E3）。

职责：
- 根据 timeline frame 和 render plan 计算各 layer source frame。
- 管理每个 clip 的 ClipDecodeStream。
- 预取未来帧，控制并发/背压/seek flush。
- 提供 ready LayerFrameBundle 给 playback engine。
- deadline 策略：不 ready 时返回 miss，由 engine hold/rebuffer。

线程模型（规格书 §9.2）：
- UI thread: PlaybackController / TimelinePreviewRenderer / ProgramViewer
- Scheduler 线程: 本调度器（source decisions, stream coordination）
- Decode worker pool: ClipDecodeStream decode jobs
- GL/render thread: GpuFramePool / TimelineGpuCompositor（不在本模块）

规则：
- 不在 scheduler 外部直接调用 frame_provider.request_frame()。
- 不让 prebuffer 移动 display stream cursor。
- pending 队列不堆积旧帧，播放中只保留当前窗口。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from typing import Dict, List, Optional, Tuple

from .clip_decode_stream import ClipDecodeStream, StreamKey
from .types import (
    FrameHandle,
    LayerFrameBundle,
    PlaybackMode,
    SourceKind,
)

logger = logging.getLogger(__name__)


# =============================================================================
# LayerSpec：scheduler 需要的 layer 描述（解耦于具体 timeline 模型）
# =============================================================================


class LayerSpec:
    """
    单个可见 layer 的调度描述。

    由 TimelinePreviewRenderer 从 render plan 转换而来。
    clip_id 必须唯一（同素材多 clip 各自独立 stream）。
    """

    def __init__(
        self,
        *,
        clip_id: str,
        asset_id: str,
        timeline_id: str,
        source_path: str,
        source_kind: SourceKind,
        target_width: int = 1920,
        target_height: int = 1080,
        visible: bool = True,
        decoder_stream_key: str = "",
    ) -> None:
        self.clip_id = clip_id
        self.asset_id = asset_id
        self.timeline_id = timeline_id
        self.source_path = source_path
        self.source_kind = source_kind
        self.target_width = target_width
        self.target_height = target_height
        self.visible = visible
        # Optional cursor-sharing identity used by EditGpuDecodeManager. The
        # scheduler still keeps one ring per editorial clip, while contiguous
        # cuts may reuse the manager's already-open sequential source.
        self.decoder_stream_key = decoder_stream_key or clip_id

    def stream_key(self) -> StreamKey:
        return StreamKey(
            timeline_id=self.timeline_id,
            clip_id=self.clip_id,
            asset_id=self.asset_id,
            source_kind=self.source_kind,
            source_path=self.source_path,
            target_width=self.target_width,
            target_height=self.target_height,
            decoder_stream_key=self.decoder_stream_key,
        )


# =============================================================================
# TimelineDecodeScheduler
# =============================================================================


class TimelineDecodeScheduler:
    """
    多轨解码调度器（非 QObject，纯逻辑 + 线程安全）。

    用法：
        scheduler = TimelineDecodeScheduler()
        scheduler.configure(layers=[...], default_capacity=48)
        scheduler.prepare_window(center_frame=100, direction=1,
                                  radius_before=2, radius_after=16, mode=REALTIME)
        bundle = scheduler.request_bundle(timeline_frame=100, generation_id=1,
                                           deadline_ns=..., mode=REALTIME)
    """

    def __init__(self, default_capacity: int = 48) -> None:
        self._default_capacity = max(8, int(default_capacity))
        self._layers: List[LayerSpec] = []
        self._streams: Dict[str, ClipDecodeStream] = {}  # clip_id -> stream
        self._lock = threading.RLock()
        self._generation = 0
        self._configured = False
        # 统计
        self._bundle_hits = 0
        self._bundle_misses = 0
        self._seek_flushes = 0
        # Latest-only asynchronous prefetch.  A playback tick must never
        # enqueue one future per frame: at most one request runs and one newer
        # request waits behind it.  Superseded pending requests complete with
        # False so callers never retain an unresolved Future.
        self._prefetch_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="TimelineDecodePrefetch",
        )
        self._prefetch_lock = threading.RLock()
        self._prefetch_active = False
        self._prefetch_active_request: dict | None = None
        self._prefetch_active_future: Future[bool] | None = None
        self._prefetch_active_cancel: threading.Event | None = None
        self._prefetch_pending: tuple[dict, Future[bool], threading.Event] | None = None
        self._prefetch_submitted = 0
        self._prefetch_completed = 0
        self._prefetch_dropped = 0
        # Optional timeline->source mapper supplied by the render-plan owner.
        # Without it, the scheduler keeps the simple 1:1 frame mapping used
        # by legacy callers; with it, speed/reverse/freeze clips are prefetched
        # using their actual source frames.
        self._source_frame_provider: Optional[callable] = None

    # ------------------------------------------------------------------
    # configure
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        layers: List[LayerSpec],
        capacity_per_stream: Optional[int] = None,
        media_source_factory: Optional[callable] = None,
        source_frame_provider: Optional[callable] = None,
    ) -> None:
        """
        配置 layer 集合。

        为每个 layer 创建（或复用）ClipDecodeStream。media_source_factory
        可选：传入一个 (StreamKey) -> media_source 的工厂，用于惰性打开。

        AUD-17419：工厂回调（打开媒体源）与 stream.close() 可能执行重
        I/O，全部移到锁外执行；锁内只做计划与提交。self._lock 同时保护
        request_bundle/prepare_window 等播放 tick 路径，锁内开源/关源会
        在多 layer 重建期间卡住渲染线程。
        """
        cap = capacity_per_stream or self._default_capacity
        with self._lock:
            previous_layers = tuple(layer.stream_key() for layer in self._layers)
            previous_provider = self._source_frame_provider
            self._layers = [l for l in layers if l.visible]
            self._source_frame_provider = source_frame_provider
            config_changed = (
                previous_layers != tuple(layer.stream_key() for layer in self._layers)
                or previous_provider is not source_frame_provider
            )
            # 计划阶段（持锁）：确定要关闭的流与要新建的 key。
            existing_keys = set(self._streams.keys())
            new_clip_ids = {l.clip_id for l in self._layers}
            # 移除不再存在的：先从表中摘除（后续读请求立即看不到），
            # 关闭动作延后到锁外。
            to_close: List[ClipDecodeStream] = []
            for cid in list(existing_keys - new_clip_ids):
                stream = self._streams.pop(cid, None)
                if stream is not None:
                    to_close.append(stream)
            # Rebuild a stream when the selected proxy/cache path, source kind,
            # geometry or decoder cursor identity changes.  Keeping the old
            # stream in that case would silently prefetch from a stale proxy
            # after SourceResolver switches paths.
            to_create: List[StreamKey] = []
            for layer in self._layers:
                desired_key = layer.stream_key()
                existing = self._streams.get(layer.clip_id)
                if existing is not None and existing.key != desired_key:
                    self._streams.pop(layer.clip_id, None)
                    to_close.append(existing)
                if layer.clip_id not in self._streams:
                    to_create.append(desired_key)
            self._configured = True

        # 执行阶段（锁外）：close 与工厂调用都可能阻塞（关解码器/开文件）。
        for stream in to_close:
            try:
                stream.close()
            except Exception:
                logger.debug("scheduler stream close failed", exc_info=True)
        created: List[tuple] = []
        for desired_key in to_create:
            media_source = None
            if media_source_factory is not None:
                try:
                    media_source = media_source_factory(desired_key)
                except Exception as e:
                    logger.debug(f"media_source_factory failed: {e}")
            created.append((
                desired_key.clip_id,
                ClipDecodeStream(
                    key=desired_key,
                    media_source=media_source,
                    capacity=cap,
                ),
            ))

        # 提交阶段（持锁）：期间可能又有一次 configure 并发执行，按最新
        # layer 集合与现有流做冲突复查，避免提交过期计划或覆盖新流。
        stale_created: List[ClipDecodeStream] = []
        if created:
            with self._lock:
                current_clip_ids = {l.clip_id for l in self._layers}
                for cid, stream in created:
                    if cid not in current_clip_ids:
                        # 计划已过期（并发 configure 已改变 layer 集合）
                        stale_created.append(stream)
                        continue
                    current = self._streams.get(cid)
                    if current is not None and current.key == stream.key:
                        # 并发 configure 已建同 key 流：丢弃重复新流。
                        stale_created.append(stream)
                        continue
                    self._streams[cid] = stream
        for stream in stale_created:
            try:
                stream.close()
            except Exception:
                logger.debug("scheduler stale stream close failed", exc_info=True)

        if config_changed:
            # A new render plan/source selection must not leave an old window
            # queued behind the current decode.  Closing/replacing streams
            # invalidates in-flight reads; the cancellation event also stops
            # the old window between frames.
            self._cancel_prefetch_requests(include_active=True)

    def attach_stream(self, clip_id: str, media_source: object) -> None:
        """注入已打开的 media source（proxy ready 热切换）。"""
        with self._lock:
            stream = self._streams.get(clip_id)
        if stream is not None:
            stream.set_source(media_source)

    # ------------------------------------------------------------------
    # prepare_window（预取）
    # ------------------------------------------------------------------

    def prepare_window(
        self,
        *,
        center_frame: int,
        direction: int,
        radius_before: int,
        radius_after: int,
        mode: PlaybackMode,
        generation_id: Optional[int] = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """
        预取 [center - radius_before, center + radius_after] 窗口的帧。

        规格书 §9.3：正向 lookahead 8-30，lookbehind 2-4。
        限流并发，避免多轨 CPU 雪崩。
        """
        with self._lock:
            if generation_id is not None and generation_id != self._generation:
                return
            layers = list(self._layers)
            streams = dict(self._streams)
            stream_generations = {
                layer.clip_id: stream.generation
                for layer in layers
                if (stream := streams.get(layer.clip_id)) is not None
            }

        if direction >= 0:
            start = center_frame - radius_before
            end = center_frame + radius_after
        else:
            start = center_frame - radius_after
            end = center_frame + radius_before
        start = max(0, start)

        source_frame_provider = self._source_frame_provider
        if source_frame_provider is not None:
            # A clip's source frame is not generally equal to its timeline
            # frame (speed, reverse, freeze and rational FPS all change it).
            # Resolve every timeline position in the bounded window before
            # asking the per-clip stream to decode.
            timeline_frames = (
                range(start, end + 1)
                if direction >= 0 else range(end, start - 1, -1)
            )
            for timeline_frame in timeline_frames:
                if not self._window_request_is_current(generation_id, cancel_event):
                    return
                try:
                    source_frames = dict(
                        source_frame_provider(int(timeline_frame)) or {}
                    )
                except Exception:
                    logger.debug(
                        "TimelineDecodeScheduler source frame mapping failed",
                        exc_info=True,
                    )
                    source_frames = {}
                for layer in layers:
                    if not self._window_request_is_current(generation_id, cancel_event):
                        return
                    stream = streams.get(layer.clip_id)
                    if stream is None or not stream.is_ready:
                        continue
                    source_frame = int(
                        source_frames.get(layer.clip_id, timeline_frame)
                    )
                    if not stream.ring.contains(source_frame):
                        stream.request_frame(
                            source_frame,
                            exact=True,
                            priority=10,
                            expected_generation=stream_generations.get(layer.clip_id),
                            cancel_event=cancel_event,
                        )
            return

        for layer in layers:
            if not self._window_request_is_current(generation_id, cancel_event):
                return
            stream = streams.get(layer.clip_id)
            if stream is None or not stream.is_ready:
                continue
            # 每个 stream 的 prefetch 由独立的、串行的 decode 访问守卫保护。
            stream.prefetch_range(
                start,
                end,
                direction=direction,
                priority=10,
                expected_generation=stream_generations.get(layer.clip_id),
                cancel_event=cancel_event,
            )

    def _window_request_is_current(
        self,
        generation_id: Optional[int],
        cancel_event: threading.Event | None,
    ) -> bool:
        """Return whether a prefetch may issue its next decode request."""
        if cancel_event is not None and cancel_event.is_set():
            return False
        if generation_id is None:
            return True
        with self._lock:
            return generation_id == self._generation

    def prepare_window_async(
        self,
        *,
        center_frame: int,
        direction: int,
        radius_before: int,
        radius_after: int,
        mode: PlaybackMode,
        generation_id: Optional[int] = None,
    ) -> Future[bool]:
        """异步预取播放窗口，并采用 latest-only 背压。

        ``prepare_window`` 本身保留为测试、暂停态和显式预热使用；实时
        播放应调用本方法，避免在 UI/tick 线程同步解码。窗口请求带有
        generation，seek 或工程变更后旧请求会被丢弃。

        Returns:
            Future，结果为 True 表示该窗口执行完成，False 表示请求被
            generation 变化或更近的请求淘汰。
        """
        request = {
            "center_frame": int(center_frame),
            "direction": -1 if int(direction) < 0 else 1,
            "radius_before": max(0, int(radius_before)),
            "radius_after": max(0, int(radius_after)),
            "mode": mode,
            "generation_id": (
                int(generation_id)
                if generation_id is not None else self.generation
            ),
        }
        future: Future[bool] = Future()
        cancel_event = threading.Event()
        with self._prefetch_lock:
            if self._prefetch_active:
                if request == self._prefetch_active_request:
                    active_cancel = self._prefetch_active_cancel
                    if active_cancel is None or not active_cancel.is_set():
                        active_future = self._prefetch_active_future
                        if active_future is not None:
                            return active_future
                    # 同参数的活跃请求已被更近的 pending 取代（cancel 已
                    # 置位）：不能早返回那个注定 False 的 future——那会
                    # 静默丢弃本最新意图，让 worker 去执行更旧的 pending。
                    # 落到下方替换逻辑，让 pending 重新指向本请求。
                previous = self._prefetch_pending
                if previous is not None:
                    previous_future = previous[1]
                    previous[2].set()
                    if not previous_future.done():
                        previous_future.set_result(False)
                    self._prefetch_dropped += 1
                # Direction/frame changes should stop the current window at
                # its next frame boundary; the playhead never waits for it.
                if self._prefetch_active_cancel is not None:
                    self._prefetch_active_cancel.set()
                self._prefetch_pending = (request, future, cancel_event)
                return future
            self._prefetch_active = True
            self._prefetch_active_request = request
            self._prefetch_active_future = future
            self._prefetch_active_cancel = cancel_event
            self._prefetch_submitted += 1
        try:
            self._prefetch_executor.submit(
                self._run_prefetch, request, future, cancel_event
            )
        except RuntimeError:
            with self._prefetch_lock:
                self._prefetch_active = False
                self._prefetch_active_request = None
                self._prefetch_active_future = None
                self._prefetch_active_cancel = None
                self._prefetch_dropped += 1
            future.set_result(False)
        return future

    def _run_prefetch(
        self,
        request: dict,
        future: Future[bool],
        cancel_event: threading.Event,
    ) -> None:
        """Run one prefetch, then consume at most the latest pending request."""
        current_request = request
        current_future = future
        while True:
            generation_id = current_request["generation_id"]
            with self._lock:
                generation_matches = generation_id == self._generation
            completed = False
            if generation_matches and not cancel_event.is_set():
                try:
                    self.prepare_window(
                        center_frame=current_request["center_frame"],
                        direction=current_request["direction"],
                        radius_before=current_request["radius_before"],
                        radius_after=current_request["radius_after"],
                        mode=current_request["mode"],
                        generation_id=generation_id,
                        cancel_event=cancel_event,
                    )
                    with self._lock:
                        completed = (
                            generation_id == self._generation
                            and not cancel_event.is_set()
                        )
                except Exception:
                    logger.exception("TimelineDecodeScheduler async prefetch failed")
            if not current_future.done():
                current_future.set_result(completed)
            with self._prefetch_lock:
                self._prefetch_completed += int(completed)
                pending = self._prefetch_pending
                self._prefetch_pending = None
                if pending is None:
                    self._prefetch_active = False
                    self._prefetch_active_request = None
                    self._prefetch_active_future = None
                    self._prefetch_active_cancel = None
                    return
                self._prefetch_submitted += 1
                self._prefetch_active_request = pending[0]
                self._prefetch_active_future = pending[1]
                self._prefetch_active_cancel = pending[2]
            current_request, current_future, cancel_event = pending

    @property
    def generation(self) -> int:
        """当前 scheduler generation（供 engine 做 seek 对齐）。"""
        with self._lock:
            return self._generation

    @property
    def layer_clip_ids(self) -> Tuple[str, ...]:
        """Configured visible clip ids, in scheduler/render-plan order."""
        with self._lock:
            return tuple(layer.clip_id for layer in self._layers)

    # ------------------------------------------------------------------
    # request_bundle
    # ------------------------------------------------------------------

    def request_bundle(
        self,
        *,
        timeline_frame: int,
        generation_id: int,
        deadline_ns: int,
        mode: PlaybackMode,
        layer_source_frames: Optional[Dict[str, int]] = None,
        consume: bool = False,
    ) -> Optional[LayerFrameBundle]:
        """
        请求一个 timeline frame 的多 layer bundle。

        Args:
            timeline_frame: 时间线帧号
            generation_id: 调度 generation（seek 后递增）
            deadline_ns: 截止时间戳（realtime 模式不阻塞等待，miss 即返回）
            mode: 播放模式（决定 exact 容忍度）
            layer_source_frames: {clip_id -> source_frame} 映射；
                若 None 则用 timeline_frame 作为各 layer source frame。
            consume: True 时从 ring 中取出帧并将所有权转移给调用方。
                仅在 bundle 完整时消费，避免多层帧被部分转移。

        Returns:
            LayerFrameBundle。complete=True 时所有 layer ready；
            complete=False 时 miss_reason 说明原因。
        """
        with self._lock:
            if generation_id != self._generation:
                return None  # stale
            layers = list(self._layers)
            streams = dict(self._streams)

        if not layers:
            # AUD-17420：计数器自增与 stats()/reset_stats() 同锁。
            with self._lock:
                self._bundle_misses += 1
            return LayerFrameBundle(
                timeline_frame=timeline_frame,
                generation_id=generation_id,
                layers=(),
                deadline_ns=deadline_ns,
                complete=False,
                miss_reason="no layers configured",
            )

        ready_handles: List[FrameHandle] = []
        miss_reasons: List[str] = []
        resolved_frames: Dict[str, int] = {}
        for layer in layers:
            resolved_frames[layer.clip_id] = int(
                layer_source_frames.get(layer.clip_id, timeline_frame)
                if layer_source_frames is not None else timeline_frame
            )

        # Check the whole bundle before taking anything.  This preserves the
        # all-layers-or-nothing contract when the last visible stream misses.
        can_consume = consume and all(
            (streams.get(layer.clip_id) is not None
             and streams[layer.clip_id].is_ready
             and streams[layer.clip_id].ring.contains(resolved_frames[layer.clip_id]))
            for layer in layers
        )

        for layer in layers:
            stream = streams.get(layer.clip_id)
            source_frame = resolved_frames[layer.clip_id]
            if stream is None or not stream.is_ready:
                miss_reasons.append(f"{layer.clip_id}: stream not ready")
                continue
            handle = (
                stream.ring.take_exact(source_frame)
                if can_consume else stream.get_ready_frame(source_frame)
            )
            if handle is None:
                # realtime 不阻塞等待：返回 miss（engine hold/rebuffer）
                # paused/step/export：触发一次 exact 解码（受 deadline 限制）
                if mode.allows_blocking and self._within_deadline(deadline_ns):
                    stream.request_frame(source_frame, exact=True)
                    handle = stream.get_ready_frame(source_frame)
                if handle is None:
                    miss_reasons.append(f"{layer.clip_id}: source_frame {source_frame} not ready")
                    continue
            ready_handles.append(handle)

        complete = len(ready_handles) == len(layers)
        # AUD-17420：_bundle_hits/_bundle_misses 由渲染线程自增、telemetry
        # 线程在 stats()/reset_stats() 读写——自增必须与读侧同一把锁。
        with self._lock:
            if complete:
                self._bundle_hits += 1
            else:
                self._bundle_misses += 1

        return LayerFrameBundle(
            timeline_frame=timeline_frame,
            generation_id=generation_id,
            layers=tuple(ready_handles),
            deadline_ns=deadline_ns,
            complete=complete,
            miss_reason="; ".join(miss_reasons) if miss_reasons else None,
        )

    def _within_deadline(self, deadline_ns: int) -> bool:
        """是否还在 deadline 内（允许阻塞解码时用）。"""
        if deadline_ns <= 0:
            return True
        return time.time_ns() < deadline_ns

    # ------------------------------------------------------------------
    # seek flush
    # ------------------------------------------------------------------

    def flush_seek(self, timeline_frame: int) -> int:
        """
        大跳变时 flush 所有 stream。

        递增 generation，旧 generation 的 bundle 结果会被 request_bundle 丢弃。
        Returns: 清理的总帧数。
        """
        with self._lock:
            self._generation += 1
            self._seek_flushes += 1
            streams = dict(self._streams)
        total = 0
        for stream in streams.values():
            total += stream.flush(f"seek to {timeline_frame}")
        self._cancel_prefetch_requests(include_active=True)
        return total

    def reset_generation(self, generation_id: int) -> None:
        """显式设置 generation（engine seek 时）。"""
        with self._lock:
            changed = int(generation_id) != self._generation
            self._generation = int(generation_id)
            streams = list(self._streams.values()) if changed else []
        if changed:
            # This public handoff API may be used without a timeline seek
            # frame.  Clear old ring contents so they cannot be returned under
            # the new generation.
            for stream in streams:
                stream.flush("scheduler generation reset")
        # pending 预取不应在新 generation 后继续占用窗口；正在运行的
        # request 会在完成前后检查 generation 并返回 False。
        with self._prefetch_lock:
            self._cancel_prefetch_requests_locked(include_active=True)

    def _cancel_prefetch_requests(self, *, include_active: bool) -> None:
        with self._prefetch_lock:
            self._cancel_prefetch_requests_locked(include_active=include_active)

    def _cancel_prefetch_requests_locked(self, *, include_active: bool) -> None:
        """Cancel queued work without marking the worker itself inactive."""
        if include_active and self._prefetch_active_cancel is not None:
            self._prefetch_active_cancel.set()
        pending = self._prefetch_pending
        self._prefetch_pending = None
        if pending is not None:
            pending[2].set()
            if not pending[1].done():
                pending[1].set_result(False)
            self._prefetch_dropped += 1

    # ------------------------------------------------------------------
    # stream access / stats
    # ------------------------------------------------------------------

    def get_stream(self, clip_id: str) -> Optional[ClipDecodeStream]:
        with self._lock:
            return self._streams.get(clip_id)

    def evict_outside_window(self, start: int, end: int) -> int:
        """淘汰所有 stream 窗口外的帧（播放窗口推进时）。"""
        with self._lock:
            streams = dict(self._streams)
        total = 0
        for stream in streams.values():
            total += stream.evict_outside_window(start, end)
        return total

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """关闭所有 stream。

        AUD-17423：预取 executor 用有界等待——``cancel_futures`` 取消全部
        排队任务后，只等在飞预取最多 ``timeout_s`` 秒。在飞预取内是同步
        解码、无 deadline，可能长时间阻塞；旧实现 ``wait=True`` 会让关闭
        流程被一个挂起的解码无限拖住。超时后放弃等待（executor 线程随后
        在进程退出时回收），继续关闭 stream——关闭语义优先于预取收尾。
        """
        with self._prefetch_lock:
            self._cancel_prefetch_requests_locked(include_active=True)
            self._prefetch_active = False
        self._prefetch_executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            with self._prefetch_lock:
                active_future = self._prefetch_active_future
            if active_future is None or active_future.done():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "TimelineDecodeScheduler.shutdown: active prefetch still "
                    "running after %.1fs; giving up bounded wait",
                    float(timeout_s),
                )
                break
            try:
                active_future.result(timeout=min(0.05, remaining))
            except TimeoutError:
                # 分片等待未到 deadline：继续等。
                continue
            except Exception:
                # cancelled/异常完成——无需继续等待
                break
        with self._lock:
            streams = dict(self._streams)
            self._streams.clear()
            self._layers.clear()
            self._configured = False
        for stream in streams.values():
            stream.close()

    def stats(self) -> Dict[str, object]:
        with self._lock:
            layer_count = len(self._layers)
            stream_count = len(self._streams)
            ring_stats: Dict[str, Dict[str, object]] = {}
            for cid, stream in self._streams.items():
                ring_stats[cid] = stream.ring.stats()
            total = self._bundle_hits + self._bundle_misses
            bundle_hit_rate = (self._bundle_hits / total) if total > 0 else 0.0
            bundle = {
                "bundle_hits": self._bundle_hits,
                "bundle_misses": self._bundle_misses,
                "bundle_hit_rate": round(bundle_hit_rate, 4),
                "seek_flushes": self._seek_flushes,
            }
        # AUD-17420：prefetch 计数器归属 _prefetch_lock（worker 线程在
        # _prefetch_lock 下写）——锁不对称读会撕裂同一次快照。
        with self._prefetch_lock:
            prefetch = {
                "prefetch_submitted": self._prefetch_submitted,
                "prefetch_completed": self._prefetch_completed,
                "prefetch_dropped": self._prefetch_dropped,
                "prefetch_active": self._prefetch_active,
            }
        return {
            "layers": layer_count,
            "streams": stream_count,
            "generation": self._generation,
            **bundle,
            **prefetch,
            "rings": ring_stats,
        }

    def reset_stats(self) -> None:
        with self._lock:
            self._bundle_hits = 0
            self._bundle_misses = 0
            self._seek_flushes = 0
            for stream in self._streams.values():
                stream.ring.reset_stats()
        # AUD-17420：reset 也要清 prefetch 计数（旧实现只清 bundle/seek，
        # benchmark 复跑时 prefetch_* 携带上轮脏值）。
        with self._prefetch_lock:
            self._prefetch_submitted = 0
            self._prefetch_completed = 0
            self._prefetch_dropped = 0
