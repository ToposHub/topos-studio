"""
Decode Worker Module
定义后台解码工作线程，用于异步解码 proxy 和 master 帧。

## 职责

- BaseDecodeWorker: 解码工作线程基类
- ProxyDecodeWorker: Proxy 帧解码工作线程
- MasterDecodeWorker: Master 帧解码工作线程
- DecodeWorkerStats: 解码统计信息

## 设计原则（P2.B）

- 独立线程运行，不阻塞 UI
- ProxyDecodeWorker: 顺序解码为主，预读 [current+1, current+N]
- MasterDecodeWorker: 精确帧处理，不允许错帧
- Worker 禁止直接接触 UI 控件和 ModernGL
- 输出 DecodedFrame 到 ReadyFrameQueue
- generation_id 用于 seek 后丢弃旧帧
- stop/unload 后线程能在 2 秒内退出

## 使用示例

```python
from src.shared.video.decode_worker import ProxyDecodeWorker, MasterDecodeWorker
from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

# 创建队列
queue = ReadyFrameQueue()

# 创建 proxy worker
proxy_worker = ProxyDecodeWorker(
    source_manager=source_manager,
    frame_queue=queue,
    prefetch_lookahead=8,
)

# 启动
proxy_worker.start()

# 请求帧
proxy_worker.request_frame(frame_index=100, generation_id=1)

# 停止
proxy_worker.stop(timeout=2.0)
```

## 线程安全

- 所有状态变更通过 threading.Lock 保护
- 使用 threading.Event 进行线程间通信
- stop() 设置 _stop_event，worker 循环检测并退出
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..media.frame import DecodedFrame
    from .playback_clock import SourceManager
    from .frame_queue import ReadyFrameQueue
    from .frame_request import VideoFrameRequest
    from .frame_provider_protocol import FrameProviderProtocol

# 导入用于运行时的类型（FrameRequestReason 用于默认参数）
from .frame_request import FrameRequestReason
from .frame_provider_protocol import FrameProviderProtocol


logger = logging.getLogger(__name__)


# =============================================================================
# 常量定义
# =============================================================================

# 默认预读帧数
DEFAULT_PREFETCH_LOOKAHEAD: int = 8

# 默认最大队列深度
DEFAULT_MAX_QUEUE_DEPTH: int = 12

# 停止超时（秒）
STOP_TIMEOUT_SECONDS: float = 2.0

# 空闲等待时间（秒）
IDLE_WAIT_SECONDS: float = 0.01

# 解码间隔（秒）- 避免过于频繁解码占用 CPU
DECODE_INTERVAL_SECONDS: float = 0.005


# =============================================================================
# 解码统计
# =============================================================================

@dataclass
class DecodeWorkerStats:
    """
    解码工作线程统计信息。

    Attributes:
        frames_decoded: 解码帧数
        frames_dropped: 丢弃帧数（generation 过期）
        seek_count: seek 操作次数
        decode_total_ms: 累计解码时间（毫秒）
        decode_avg_ms: 平均解码时间（毫秒）
        queue_full_count: 队列满次数
        error_count: 错误次数
        last_decode_ms: 最近一次解码时间（毫秒）
        last_frame_index: 最近解码的帧号
        requests_deduped: 因重复而被合并的请求数
    """

    frames_decoded: int = 0
    frames_dropped: int = 0
    seek_count: int = 0
    decode_total_ms: float = 0.0
    decode_avg_ms: float = 0.0
    queue_full_count: int = 0
    error_count: int = 0
    last_decode_ms: float = 0.0
    last_frame_index: int = -1
    requests_deduped: int = 0

    def __post_init__(self) -> None:
        """AUD-38803: 统计对象跨线程读写（解码线程更新、管理线程读取），
        所有变更与快照必须经过同一把锁，保证 += 读-改-写不丢更新。"""
        self._stats_lock = threading.Lock()

    def update_decode(self, decode_ms: float, frame_index: int) -> None:
        """
        更新解码统计。

        Args:
            decode_ms: 本次解码时间（毫秒）
            frame_index: 帧号
        """
        with self._stats_lock:
            self.frames_decoded += 1
            self.decode_total_ms += decode_ms
            self.decode_avg_ms = self.decode_total_ms / self.frames_decoded
            self.last_decode_ms = decode_ms
            self.last_frame_index = frame_index

    def update_drop(self, count: int = 1) -> None:
        """更新丢弃统计"""
        with self._stats_lock:
            self.frames_dropped += count

    def update_seek(self) -> None:
        """更新 seek 统计"""
        with self._stats_lock:
            self.seek_count += 1

    def update_queue_full(self) -> None:
        """更新队列满统计"""
        with self._stats_lock:
            self.queue_full_count += 1

    def update_error(self) -> None:
        """更新错误统计"""
        with self._stats_lock:
            self.error_count += 1

    def update_dedup(self, count: int = 1) -> None:
        """更新去重统计"""
        with self._stats_lock:
            self.requests_deduped += count

    def to_dict(self) -> dict[str, float | int]:
        """转换为字典（AUD-38803: 持锁快照，与更新互斥）"""
        with self._stats_lock:
            return self._to_dict_locked()

    def _to_dict_locked(self) -> dict[str, float | int]:
        """内部快照（调用方必须已持有 _stats_lock）"""
        return {
            'frames_decoded': self.frames_decoded,
            'frames_dropped': self.frames_dropped,
            'seek_count': self.seek_count,
            'decode_total_ms': self.decode_total_ms,
            'decode_avg_ms': self.decode_avg_ms,
            'queue_full_count': self.queue_full_count,
            'error_count': self.error_count,
            'last_decode_ms': self.last_decode_ms,
            'last_frame_index': self.last_frame_index,
            'requests_deduped': self.requests_deduped,
        }


# =============================================================================
# 工作线程状态
# =============================================================================

class WorkerState(Enum):
    """
    工作线程状态。

    Values:
        IDLE: 空闲，无任务
        RUNNING: 正常运行
        STOPPING: 正在停止
        STOPPED: 已停止
        ERROR: 错误状态
    """
    IDLE = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    ERROR = auto()


# =============================================================================
# 基类：BaseDecodeWorker
# =============================================================================

class BaseDecodeWorker:
    """
    解码工作线程基类。

    提供线程管理、状态控制和基本解码框架。
    子类实现具体的解码策略（proxy vs master）。

    Attributes:
        _frame_provider: 帧提供者（实现 FrameProviderProtocol）
        _frame_queue: 就绪帧队列
        _state: 工作线程状态
        _thread: 工作线程
        _stop_event: 停止事件
        _request_event: 请求事件
        _current_generation: 当前 generation_id
        _stats: 统计信息

    Note:
        禁止直接接触 UI 控件和 ModernGL
        所有状态变更通过锁保护
        _frame_provider 可以是 SourceManager 或 ColorFrameProviderAdapter
    """

    def __init__(
        self,
        frame_provider: FrameProviderProtocol,
        frame_queue: ReadyFrameQueue,
        name: str = "DecodeWorker",
    ) -> None:
        """
        初始化解码工作线程。

        Args:
            frame_provider: 帧提供者（实现 FrameProviderProtocol，可以是 SourceManager 或 ColorFrameProviderAdapter）
            frame_queue: 就绪帧队列（输出目标）
            name: 线程名称
        """
        self._frame_provider: FrameProviderProtocol = frame_provider
        self._frame_queue: ReadyFrameQueue = frame_queue
        self._name: str = name

        # 状态
        self._state: WorkerState = WorkerState.STOPPED
        self._state_lock: threading.Lock = threading.Lock()

        # 线程
        self._thread: Optional[threading.Thread] = None

        # 控制事件
        self._stop_event: threading.Event = threading.Event()
        self._request_event: threading.Event = threading.Event()

        # 当前 generation
        self._current_generation: int = 0
        self._generation_lock: threading.Lock = threading.Lock()

        # 统计
        self._stats: DecodeWorkerStats = DecodeWorkerStats()

        # 当前请求
        self._pending_requests: list[VideoFrameRequest] = []
        self._request_lock: threading.Lock = threading.Lock()

        # 请求去重计数（监控用）
        self._dedup_count: int = 0

        # 当前解码位置
        # AUD-38804: 由解码线程写、其他线程读，写入与读取经锁同步。
        self._current_decode_frame: int = -1
        self._decode_position_lock: threading.Lock = threading.Lock()

        # 正在解码中的 (generation_id, frame_index) 集合（用于避免重复解码同一帧）
        # V-P0-04: key 必须包含 generation。此前只存 frame_index，导致：
        #   帧 N 在 generation A 下正在解码 → seek 把 generation 升到 B →
        #   再次请求 N 时被当作"已解码"直接丢弃，新请求永远拿不到帧。
        #   现在存 (generation, frame)，旧 generation 的解码不会屏蔽新 generation
        #   对同一帧的请求。
        self._decoding_frames: set[tuple[int, int]] = set()
        # 注意：_decoding_frames 与 _pending_requests 共用 _request_lock 保护

    @property
    def name(self) -> str:
        """线程名称"""
        return self._name

    @property
    def state(self) -> WorkerState:
        """工作线程状态"""
        with self._state_lock:
            return self._state

    @property
    def stats(self) -> DecodeWorkerStats:
        """统计信息"""
        return self._stats

    @property
    def current_decode_frame(self) -> int:
        """当前解码位置（AUD-38804: 持锁读取，与解码线程写入互斥）"""
        with self._decode_position_lock:
            return self._current_decode_frame

    def _set_current_decode_frame(self, frame_index: int) -> None:
        """更新当前解码位置（AUD-38804: 持锁写入）"""
        with self._decode_position_lock:
            self._current_decode_frame = frame_index

    @property
    def current_generation(self) -> int:
        """当前 generation_id"""
        with self._generation_lock:
            return self._current_generation

    def start(self) -> bool:
        """
        启动工作线程。

        Returns:
            True 表示成功启动

        Note:
            AUD-38802：stop() 超时后状态为 ERROR 且旧线程可能仍在
            运行。此时绝不清除停止事件、绝不再启新线程（否则产生双
            工作线程并发消费同一 frame_provider）。只有旧线程确认
            已退出（is_alive() 为 False）才允许重启。
        """
        with self._state_lock:
            if self._state in (WorkerState.RUNNING, WorkerState.STOPPING):
                logger.warning(f"[{self._name}] Worker already running or stopping")
                return False

            # AUD-38802：上次 stop() 超时残留的线程仍活着 → 拒绝启动
            stale_thread = self._thread
            if stale_thread is not None and stale_thread.is_alive():
                logger.error(
                    f"[{self._name}] Refusing start: previous worker "
                    f"thread still alive after stop() timeout"
                )
                return False

            # 重置停止事件
            self._stop_event.clear()

            # 创建线程
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self._name,
                daemon=True,  # 主线程退出时自动结束
            )

            # 更新状态
            self._state = WorkerState.RUNNING

            # 启动线程
            self._thread.start()

        logger.info(f"[{self._name}] Worker started")
        return True

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> bool:
        """
        停止工作线程。

        Args:
            timeout: 等待超时（秒）

        Returns:
            True 表示成功停止，False 表示超时

        Note:
            设计要求：stop/unload 后线程能在 2 秒内退出
        """
        with self._state_lock:
            if self._state == WorkerState.STOPPED:
                return True

            if self._state == WorkerState.STOPPING:
                # 已经在停止，等待
                pass
            else:
                self._state = WorkerState.STOPPING

        # 设置停止事件
        self._stop_event.set()

        # 等待线程结束
        if self._thread is not None:
            self._thread.join(timeout=timeout)

            if self._thread.is_alive():
                logger.warning(f"[{self._name}] Worker stop timeout after {timeout}s")
                with self._state_lock:
                    self._state = WorkerState.ERROR
                return False

        with self._state_lock:
            self._state = WorkerState.STOPPED
            self._thread = None

        logger.info(f"[{self._name}] Worker stopped")
        return True

    def is_running(self) -> bool:
        """
        检查线程是否运行中。

        Returns:
            运行中返回 True
        """
        return self.state == WorkerState.RUNNING

    def is_stopped(self) -> bool:
        """
        检查线程是否已停止。

        Returns:
            已停止返回 True
        """
        return self.state == WorkerState.STOPPED

    def update_generation(self, generation_id: int) -> None:
        """
        更新 generation_id（seek 后调用）。

        V-P0-05: generation 提升时，同步把队列里所有旧 generation 的待处理请求
        与 in-flight 标记批量清掉。此前这些旧请求会逐个被取出、逐个解码、再逐个
        在 _process_request 里因 generation 过期被丢弃，等于让过期 Seek 继续占满
        decoder（GOP 前向解码最痛的点）。这里无法真正中断正在进行的单次
        read_frame，但能立即停止"继续消费旧队列"，让 worker 尽快回到新 generation。

        Args:
            generation_id: 新的 generation_id
        """
        bumped = False
        with self._generation_lock:
            if generation_id > self._current_generation:
                self._current_generation = generation_id
                bumped = True
                logger.debug(f"[{self._name}] Generation updated to {generation_id}")

        if bumped:
            # 在 _request_lock 下批量清除过期请求；不嵌套 _generation_lock 以防死锁。
            purged_pending = 0
            purged_decoding = 0
            with self._request_lock:
                kept = []
                for r in self._pending_requests:
                    if r.generation_id < generation_id:
                        purged_pending += 1
                    else:
                        kept.append(r)
                self._pending_requests = kept
                # 清除旧 generation 的 in-flight 标记（key 为 (gen, frame)）。
                if self._decoding_frames:
                    stale_decoding = {
                        k for k in self._decoding_frames if k[0] < generation_id
                    }
                    if stale_decoding:
                        self._decoding_frames -= stale_decoding
                        purged_decoding = len(stale_decoding)
            if purged_pending or purged_decoding:
                if purged_pending:
                    self._stats.update_drop(purged_pending)
                logger.debug(
                    f"[{self._name}] Purged {purged_pending} stale pending + "
                    f"{purged_decoding} stale in-flight for gen<{generation_id}"
                )

    def request_frame(
        self,
        frame_index: int,
        generation_id: int,
        reason: FrameRequestReason = FrameRequestReason.PLAYBACK,
    ) -> None:
        """
        请求解码指定帧。

        Args:
            frame_index: 目标帧号
            generation_id: generation ID
            reason: 请求原因
        """
        # 更新 generation
        self.update_generation(generation_id)

        # 创建请求并添加到队列
        from .frame_request import VideoFrameRequest, RequestPriority

        is_master = self._is_master_worker()
        priority = RequestPriority.get_default(reason)

        request = VideoFrameRequest(
            frame_index=frame_index,
            is_master=is_master,
            generation_id=generation_id,
            priority=priority,
            reason=reason,
        )

        with self._request_lock:
            # 检查帧是否正在解码中（避免重复解码）。
            # V-P0-04: 用 (generation, frame) 作 key，旧 generation 的在途解码
            # 不会屏蔽新 generation 对同一帧的合法请求。
            if (generation_id, frame_index) in self._decoding_frames:
                logger.debug(
                    f"[{self._name}] Skipping request for frame {frame_index}: "
                    f"already decoding (gen={generation_id})"
                )
                self._stats.update_dedup()
                return

            # 清除旧 generation 的请求
            self._pending_requests = [
                r for r in self._pending_requests
                if r.generation_id >= generation_id
            ]

            # 去重检查：如果相同 key (generation_id, frame_index, is_master) 已存在，
            # 只保留优先级更高的那个
            dedupe_key = (generation_id, frame_index, is_master)
            existing_idx = None
            for i, r in enumerate(self._pending_requests):
                if (r.generation_id, r.frame_index, r.is_master) == dedupe_key:
                    existing_idx = i
                    break

            if existing_idx is not None:
                # 已存在同 key 请求：如果新请求优先级更高则替换
                existing = self._pending_requests[existing_idx]
                if request.priority > existing.priority:
                    self._pending_requests[existing_idx] = request
                self._stats.update_dedup()
            else:
                # 新帧请求，添加到队列
                self._pending_requests.append(request)

            # 按优先级排序（高优先级先处理），同优先级按帧号升序保持顺序播放。
            self._sort_pending_requests_locked()

        # 通知有新请求
        self._request_event.set()

    def request_prefetch(
        self,
        current_frame: int,
        lookahead: int,
        generation_id: int,
    ) -> None:
        """
        请求预读帧。

        从 current_frame 开始预读 lookahead 帧。使用去重策略避免重复请求累积：
        - 保留非 playback（seek/master 等）的高优先级请求
        - 替换旧的 playback prefetch 窗口为新的窗口

        Args:
            current_frame: 当前帧号
            lookahead: 预读帧数
            generation_id: generation ID
        """
        total_frames = self._frame_provider.total_frames
        if total_frames is None:
            return

        from .frame_request import VideoFrameRequest, FrameRequestReason, RequestPriority

        end_frame = min(current_frame + lookahead, total_frames - 1)

        is_master = self._is_master_worker()

        with self._request_lock:
            # 清除旧 generation 的请求
            self._pending_requests = [
                r for r in self._pending_requests
                if r.generation_id >= generation_id
            ]

            # 计算新预读窗口的帧号集合
            new_prefetch_frames = set(range(current_frame + 1, end_frame + 1))

            # 排除正在解码中的帧（避免重复解码）。
            # V-P0-04: _decoding_frames 现存 (generation, frame)；只排除本
            # generation 下正在解码的帧，旧 generation 的在途解码不影响。
            decoding_this_gen = {
                f for (g, f) in self._decoding_frames if g == generation_id
            }
            frames_decoding = new_prefetch_frames & decoding_this_gen
            if frames_decoding:
                new_prefetch_frames -= frames_decoding
                self._stats.update_dedup(len(frames_decoding))
                logger.debug(
                    f"[{self._name}] Prefetch skipping {len(frames_decoding)} "
                    f"frames already decoding: {sorted(frames_decoding)[:5]}..."
                )

            # 分离：保留高优先级请求（非 PLAYBACK），清除旧 playback prefetch
            kept_requests: list[VideoFrameRequest] = []
            removed_count = 0
            for r in self._pending_requests:
                if r.reason != FrameRequestReason.PLAYBACK:
                    # 非 playback 请求（seek/scrub/composite/export）保留
                    kept_requests.append(r)
                elif r.frame_index in new_prefetch_frames:
                    # 新窗口内的 playback 请求保留（避免重复添加）
                    new_prefetch_frames.discard(r.frame_index)
                    kept_requests.append(r)
                else:
                    # 旧 playback prefetch 请求不在新窗口内，丢弃
                    removed_count += 1

            if removed_count > 0:
                self._stats.update_dedup(removed_count)

            # 添加新预读请求（仅窗口中尚未存在的帧）
            for frame_index in sorted(new_prefetch_frames):
                request = VideoFrameRequest(
                    frame_index=frame_index,
                    is_master=is_master,
                    generation_id=generation_id,
                    priority=RequestPriority.PLAYBACK,
                    reason=FrameRequestReason.PLAYBACK,
                )
                kept_requests.append(request)

            self._pending_requests = kept_requests
            self._sort_pending_requests_locked()

        self._request_event.set()

    def _sort_pending_requests_locked(self) -> None:
        """Sort pending requests while _request_lock is held."""
        self._pending_requests.sort(key=lambda req: (-req.priority, req.frame_index))

    def clear_requests(self) -> None:
        """清除所有待处理请求"""
        with self._request_lock:
            self._pending_requests.clear()

    def get_next_request(self) -> Optional[VideoFrameRequest]:
        """
        获取下一个待处理请求。

        AUD-38801：出队与在途标记（_decoding_frames）必须在同一
        _request_lock 临界区内完成。此前"先 pop 再标记"之间存在窗口：
        并发 request_frame 在该窗口内既看不到在途标记、pending 列表里
        也找不到同帧请求（已被 pop），于是入队重复请求，同一帧被解码
        两次（check-then-act 竞态）。

        Returns:
            VideoFrameRequest 或 None
        """
        with self._request_lock:
            if not self._pending_requests:
                return None
            request = self._pending_requests.pop(0)
            # V-P0-04: (generation, frame) 作 key
            self._decoding_frames.add(
                (request.generation_id, request.frame_index)
            )
            return request

    def _run_loop(self) -> None:
        """
        工作线程主循环。

        循环检测：
        1. _stop_event 是否设置（停止）
        2. 有请求则处理
        3. 无请求则等待

        帧解码去重（AUD-38801 原子化）：
        - get_next_request 出队请求时在同一临界区内置位 _decoding_frames
        - 处理完成（或 generation 过期跳过）后从 _decoding_frames 移除
        - 确保同一帧不会因 check-then-act 竞态被重复解码
        """
        logger.debug(f"[{self._name}] Worker loop started")

        while not self._stop_event.is_set():
            try:
                # 获取下一个请求
                request = self.get_next_request()

                if request is not None:
                    # AUD-38801：在途标记已由 get_next_request 在
                    # _request_lock 临界区内原子置位（出队即标记）。
                    frame_index = request.frame_index

                    # 检查 generation 是否过期
                    if request.generation_id < self.current_generation:
                        logger.debug(
                            f"[{self._name}] Skipping stale request "
                            f"gen={request.generation_id} < current={self.current_generation}"
                        )
                        self._stats.update_drop()
                        with self._request_lock:
                            self._decoding_frames.discard(
                                (request.generation_id, frame_index)
                            )
                        continue

                    try:
                        # 处理请求
                        self._process_request(request)
                    finally:
                        # 无论成功失败，都要移除正在解码的标记
                        with self._request_lock:
                            self._decoding_frames.discard(
                                (request.generation_id, frame_index)
                            )
                else:
                    # 无请求时的空闲处理
                    self._idle_process()

                    # 等待新请求或停止
                    self._request_event.wait(timeout=IDLE_WAIT_SECONDS)
                    self._request_event.clear()

            except Exception as e:
                logger.error(f"[{self._name}] Worker loop error: {e}")
                self._stats.update_error()

        logger.debug(f"[{self._name}] Worker loop ended")

    def _process_request(self, request: VideoFrameRequest) -> None:
        """
        处理解码请求。

        子类实现具体解码策略。

        Args:
            request: 帧请求
        """
        raise NotImplementedError("Subclass must implement _process_request")

    def _idle_process(self) -> None:
        """
        空闲时的处理。

        子类可重写实现预读等行为。
        """
        pass

    def _is_master_worker(self) -> bool:
        """
        是否为 master 工作线程。

        子类实现。

        Returns:
            True 表示 master worker
        """
        raise NotImplementedError("Subclass must implement _is_master_worker")

    def _enqueue_frame(
        self,
        frame: 'DecodedFrame',
        decode_ms: float,
        is_master: bool,
        generation_id: int,
    ) -> bool:
        """
        将解码后的帧入队。

        支持 DecodedFrame（CPU 路径）和 GpuDecodedFrame（GPU 零拷贝路径），
        自动选择 FrameSource: PROXY/MASTER vs GPU_PROXY/GPU_MASTER。

        Args:
            frame: 解码帧 (DecodedFrame 或 GpuDecodedFrame)
            decode_ms: 解码时间（毫秒）
            is_master: 是否为 master 帧
            generation_id: generation ID

        Returns:
            成功入队返回 True
        """
        from .frame_queue import QueueEntry, FrameSource
        from src.shared.media.gpu_frame import is_gpu_decoded_frame

        if is_master:
            source = FrameSource.GPU_MASTER if is_gpu_decoded_frame(frame) else FrameSource.MASTER
        else:
            source = FrameSource.GPU_PROXY if is_gpu_decoded_frame(frame) else FrameSource.PROXY
        can_drop = not is_master  # proxy 可丢弃，master 不可

        entry = QueueEntry(
            frame=frame,
            decode_ms=decode_ms,
            source=source,
            can_drop=can_drop,
            generation_id=generation_id,
        )

        result = self._frame_queue.enqueue(entry)

        if not result:
            self._stats.update_queue_full()

        return result


# =============================================================================
# ProxyDecodeWorker: Proxy 帧解码
# =============================================================================

class ProxyDecodeWorker(BaseDecodeWorker):
    """
    Proxy 帧解码工作线程。

    实现设计文档 P2.B 的 ProxyDecodeWorker 要求：
    - 独立线程运行
    - 顺序解码为主
    - 根据 clock 预读 [current+1, current+N]
    - 落后时跳到最新需要的 frame，允许丢旧 preview
    - 输出 DecodedFrame 到 ReadyFrameQueue

    Attributes:
        _prefetch_lookahead: 预读前瞻帧数
        _last_clock_frame: 上次 clock 帧号

    Note:
        连续播放请求时不重复 seek
        seek 后旧帧不会覆盖新 seek 结果
    """

    def __init__(
        self,
        frame_provider: Optional[FrameProviderProtocol] = None,
        frame_queue: Optional[ReadyFrameQueue] = None,
        prefetch_lookahead: int = DEFAULT_PREFETCH_LOOKAHEAD,
        source_manager: Optional[SourceManager] = None,  # 已废弃，兼容旧调用
    ) -> None:
        """
        初始化 Proxy 解码工作线程。

        Args:
            frame_provider: 帧提供者（实现 FrameProviderProtocol）
                            P7.A 推荐：ColorFrameProviderAdapter（共享模式）
            frame_queue: 就绪帧队列
            prefetch_lookahead: 预读前瞻帧数
            source_manager: 已废弃。兼容旧调用

        Note:
            P7.A 推荐调用方式：
            ProxyDecodeWorker(frame_provider=ColorFrameProviderAdapter, frame_queue=queue)

            兼容旧调用（已废弃）：
            ProxyDecodeWorker(source_manager=SourceManager, frame_queue=queue)
        """
        # P7.A: frame_provider 是主要参数
        if frame_provider is None:
            # 兼容旧调用：使用 source_manager 作为 frame_provider
            if source_manager is not None:
                frame_provider = source_manager
                logger.warning(
                    "ProxyDecodeWorker: source_manager parameter is deprecated, "
                    "use frame_provider instead"
                )
            else:
                raise ValueError("frame_provider must be provided")
        if frame_queue is None:
            raise ValueError("frame_queue must be provided")

        super().__init__(
            frame_provider=frame_provider,
            frame_queue=frame_queue,
            name="ProxyDecodeWorker",
        )

        # AUD-38804: _prefetch_lookahead / _last_clock_frame 由外部线程
        # （set_prefetch_lookahead / update_clock_position）写、解码线程读，
        # check-then-act 序列必须持锁，避免竞态丢失预读触发。
        self._clock_state_lock: threading.Lock = threading.Lock()
        self._prefetch_lookahead: int = prefetch_lookahead
        self._last_clock_frame: int = -1

    def _is_master_worker(self) -> bool:
        """Proxy worker 返回 False"""
        return False

    def _process_request(self, request: VideoFrameRequest) -> None:
        """
        处理 Proxy 解码请求。

        实现 P2.B 要求：
        - 顺序解码为主（避免重复 seek）
        - 落后时跳帧，允许丢旧 preview

        Args:
            request: 帧请求
        """
        frame_index = request.frame_index
        generation_id = request.generation_id

        # 检查 generation 是否过期（再次检查，防止处理过程中被更新）
        if generation_id < self.current_generation:
            logger.debug(
                f"[{self._name}] Dropping stale frame {frame_index}, "
                f"gen={generation_id} < current={self.current_generation}"
            )
            self._stats.update_drop()
            return

        start_time = time.perf_counter()

        try:
            # 使用 SourceManager.get_proxy_frame 获取帧
            # P1.A: 顺序播放优化 - get_proxy_frame 已经优化了顺序解码
            # 如果 frame_index == _proxy_current_frame + 1，不 seek
            # 落后时跳帧（设计要求：落后时跳到最新需要的 frame）
            frame = self._frame_provider.get_proxy_frame(frame_index)

            decode_ms = (time.perf_counter() - start_time) * 1000

            if frame is None:
                logger.warning(f"[{self._name}] Decode failed for frame {frame_index}")
                self._stats.update_error()
                return

            # 标记为 proxy 帧
            object.__setattr__(frame, 'is_proxy', True)

            # 入队
            if self._enqueue_frame(frame, decode_ms, is_master=False, generation_id=generation_id):
                self._stats.update_decode(decode_ms, frame_index)
                logger.debug(
                    f"[{self._name}] Decoded proxy frame {frame_index} "
                    f"in {decode_ms:.1f}ms, gen={generation_id}"
                )
            else:
                # 队列满或 generation 过期，丢弃
                logger.debug(
                    f"[{self._name}] Dropped proxy frame {frame_index} "
                    f"(queue full or stale)"
                )
                self._stats.update_drop()

            # 更新当前位置（AUD-38804: 持锁写入）
            self._set_current_decode_frame(frame_index)

        except Exception as e:
            logger.error(f"[{self._name}] Decode error for frame {frame_index}: {e}")
            self._stats.update_error()

    def _idle_process(self) -> None:
        """
        空闲时自动预读。

        根据 clock 预读 [current+1, current+N]
        """
        # 空闲时不做预读，由外部调用 request_prefetch 触发
        # 避免在无播放时钟时盲目预读
        pass

    def update_clock_position(
        self,
        current_frame: int,
        generation_id: int,
    ) -> None:
        """
        更新时钟位置，触发预读。

        Args:
            current_frame: 当前 clock 帧号
            generation_id: 当前 generation
        """
        # AUD-38804: check-then-set 持锁完成；request_prefetch 在锁外调用
        #（避免持锁执行解码请求路径）。
        with self._clock_state_lock:
            if current_frame == self._last_clock_frame:
                return
            self._last_clock_frame = current_frame
            lookahead = self._prefetch_lookahead
        self.request_prefetch(
            current_frame=current_frame,
            lookahead=lookahead,
            generation_id=generation_id,
        )

    def set_prefetch_lookahead(self, value: int) -> None:
        """
        设置预读帧数。

        Args:
            value: 新的预读帧数（最小为1）
        """
        with self._clock_state_lock:
            self._prefetch_lookahead = max(1, value)
        logger.debug(f"[{self._name}] Prefetch lookahead set to {self._prefetch_lookahead}")


# =============================================================================
# MasterDecodeWorker: Master 帧解码
# =============================================================================

class MasterDecodeWorker(BaseDecodeWorker):
    """
    Master 帧解码工作线程。

    实现设计文档 P2.B 的 MasterDecodeWorker 要求：
    - 独立线程运行
    - 处理 seek、暂停、逐帧、scrub 停下后的高质量帧
    - 不允许返回错误 frame_index
    - 保留高位深和 alpha

    Attributes:
        _last_seek_generation: 上次 seek 的 generation
        _pending_master_request: 待处理的 master 请求（优先）

    Note:
        seek 后旧帧不会覆盖新 seek 结果
        master 帧不降级，保留原始质量
    """

    def __init__(
        self,
        frame_provider: Optional[FrameProviderProtocol] = None,
        frame_queue: Optional[ReadyFrameQueue] = None,
        source_manager: Optional[SourceManager] = None,  # 已废弃，兼容旧调用
    ) -> None:
        """
        初始化 Master 解码工作线程。

        Args:
            frame_provider: 帧提供者（实现 FrameProviderProtocol）
                            P7.A 推荐：ColorFrameProviderAdapter（共享模式）
            frame_queue: 就绪帧队列
            source_manager: 已废弃。兼容旧调用

        Note:
            P7.A 推荐调用方式：
            MasterDecodeWorker(frame_provider=ColorFrameProviderAdapter, frame_queue=queue)

            兼容旧调用（已废弃）：
            MasterDecodeWorker(source_manager=SourceManager, frame_queue=queue)
        """
        # P7.A: frame_provider 是主要参数
        if frame_provider is None:
            # 兼容旧调用：使用 source_manager 作为 frame_provider
            if source_manager is not None:
                frame_provider = source_manager
                logger.warning(
                    "MasterDecodeWorker: source_manager parameter is deprecated, "
                    "use frame_provider instead"
                )
            else:
                raise ValueError("frame_provider must be provided")
        if frame_queue is None:
            raise ValueError("frame_queue must be provided")

        super().__init__(
            frame_provider=frame_provider,
            frame_queue=frame_queue,
            name="MasterDecodeWorker",
        )

        self._pending_master_request: Optional[VideoFrameRequest] = None
        self._master_request_lock: threading.Lock = threading.Lock()

    def _is_master_worker(self) -> bool:
        """Master worker 返回 True"""
        return True

    def request_master_frame(
        self,
        frame_index: int,
        generation_id: int,
        reason: FrameRequestReason,
    ) -> None:
        """
        请求高质量 master 帧。

        用于 seek、暂停、逐帧、scrub 停下后的高质量帧请求。

        Args:
            frame_index: 目标帧号
            generation_id: generation ID
            reason: 请求原因
        """
        from .frame_request import VideoFrameRequest, RequestPriority

        # 更新 generation
        self.update_generation(generation_id)

        # 检查帧是否正在解码中（避免重复解码）。
        # V-P0-04: 用 (generation, frame) 作 key。
        with self._request_lock:
            if (generation_id, frame_index) in self._decoding_frames:
                logger.debug(
                    f"[{self._name}] Skipping master request for frame {frame_index}: "
                    f"already decoding (gen={generation_id})"
                )
                self._stats.update_dedup()
                return

        # master 请求有最高优先级
        priority = RequestPriority.SEEK  # SEEK = 50, 高优先级

        request = VideoFrameRequest(
            frame_index=frame_index,
            is_master=True,
            generation_id=generation_id,
            priority=priority,
            reason=reason,
        )

        # 设置为优先请求
        with self._master_request_lock:
            # 清除旧的 master 请求
            if self._pending_master_request is not None:
                old_gen = self._pending_master_request.generation_id
                if old_gen < generation_id:
                    logger.debug(
                        f"[{self._name}] Clearing old master request "
                        f"gen={old_gen} < {generation_id}"
                    )
                    self._stats.update_drop()

            self._pending_master_request = request

        # 清除普通请求队列（master 优先）
        self.clear_requests()

        # 通知有新请求
        self._request_event.set()

    def get_next_request(self) -> Optional[VideoFrameRequest]:
        """
        获取下一个待处理请求。

        优先处理 master 请求。

        AUD-38801：master 请求的出队与在途标记（_decoding_frames）
        同样原子完成（_master_request_lock 内出队、_request_lock 内
        置位标记），关闭"检查时 pending 已被取走但标记未置位"的重复
        解码窗口。

        Returns:
            VideoFrameRequest 或 None
        """
        # 先检查 master 请求
        with self._master_request_lock:
            if self._pending_master_request is not None:
                request = self._pending_master_request
                self._pending_master_request = None
                with self._request_lock:
                    self._decoding_frames.add(
                        (request.generation_id, request.frame_index)
                    )
                return request

        # 然后检查普通请求
        return super().get_next_request()

    def _process_request(self, request: VideoFrameRequest) -> None:
        """
        处理 Master 解码请求。

        实现 P2.B 要求：
        - 不允许返回错误 frame_index
        - 保留高位深和 alpha

        Args:
            request: 帧请求
        """
        frame_index = request.frame_index
        generation_id = request.generation_id

        # 检查 generation 是否过期
        if generation_id < self.current_generation:
            logger.debug(
                f"[{self._name}] Dropping stale master frame {frame_index}, "
                f"gen={generation_id} < current={self.current_generation}"
            )
            self._stats.update_drop()
            return

        start_time = time.perf_counter()

        try:
            # 使用 SourceManager.get_master_frame 获取高质量帧
            # Master 源保持精确，不做降级
            frame = self._frame_provider.get_master_frame(frame_index)

            decode_ms = (time.perf_counter() - start_time) * 1000

            if frame is None:
                # Master 不允许返回错误 frame_index
                # 重试一次
                logger.warning(
                    f"[{self._name}] Master decode failed for frame {frame_index}, retrying..."
                )
                frame = self._frame_provider.get_master_frame(frame_index)
                decode_ms = (time.perf_counter() - start_time) * 1000

                if frame is None:
                    logger.error(
                        f"[{self._name}] Master decode failed twice for frame {frame_index}"
                    )
                    self._stats.update_error()
                    return

            # Master 帧不降级，保留原始质量
            object.__setattr__(frame, 'is_proxy', False)

            # 入队（master 帧不可丢弃）
            if self._enqueue_frame(frame, decode_ms, is_master=True, generation_id=generation_id):
                self._stats.update_decode(decode_ms, frame_index)
                logger.debug(
                    f"[{self._name}] Decoded master frame {frame_index} "
                    f"in {decode_ms:.1f}ms, gen={generation_id}"
                )
            else:
                # 队列满或 generation 过期
                logger.debug(
                    f"[{self._name}] Dropped master frame {frame_index} "
                    f"(queue full or stale)"
                )
                self._stats.update_drop()

            # 更新当前位置（AUD-38804: 持锁写入）
            self._set_current_decode_frame(frame_index)

        except Exception as e:
            logger.error(f"[{self._name}] Master decode error for frame {frame_index}: {e}")
            self._stats.update_error()


# =============================================================================
# 工作线程管理器
# =============================================================================

class DecodeWorkerManager:
    """
    解码工作线程管理器。

    管理 ProxyDecodeWorker 和 MasterDecodeWorker 的生命周期。

    Attributes:
        _proxy_worker: Proxy 解码工作线程
        _master_worker: Master 解码工作线程
        _frame_provider: 帧提供者（实现 FrameProviderProtocol）
        _frame_queue: 就绪帧队列
        _generation: 当前 generation_id

    Note:
        P7.A: _frame_provider 使用 FrameProviderProtocol 协议。
        可以是 ColorFrameProviderAdapter（共享模式）或 SourceManager（独立模式）。
        推荐使用 ColorFrameProviderAdapter 与 Composite 共享帧服务。
    """

    def __init__(
        self,
        frame_provider: Optional[FrameProviderProtocol] = None,
        frame_queue: Optional[ReadyFrameQueue] = None,
        prefetch_lookahead: int = DEFAULT_PREFETCH_LOOKAHEAD,
        source_manager: Optional[SourceManager] = None,  # 已废弃，兼容旧调用
    ) -> None:
        """
        初始化工作线程管理器。

        Args:
            frame_provider: 帧提供者（实现 FrameProviderProtocol）
                            P7.A 推荐：ColorFrameProviderAdapter（共享模式）
            frame_queue: 就绪帧队列
            prefetch_lookahead: Proxy 预读帧数
            source_manager: 已废弃。兼容旧调用，如果提供则使用它作为 frame_provider

        Note:
            P7.A 推荐调用方式：
            DecodeWorkerManager(frame_provider=ColorFrameProviderAdapter, frame_queue=queue)

            兼容旧调用（已废弃）：
            DecodeWorkerManager(source_manager=SourceManager, frame_queue=queue)
        """
        # P7.A: frame_provider 是主要参数
        if frame_provider is None:
            # 兼容旧调用：使用 source_manager 作为 frame_provider
            if source_manager is not None:
                frame_provider = source_manager
                logger.warning(
                    "DecodeWorkerManager: source_manager parameter is deprecated, "
                    "use frame_provider instead"
                )
            else:
                raise ValueError("frame_provider must be provided")

        if frame_queue is None:
            raise ValueError("frame_queue must be provided")

        self._frame_provider: FrameProviderProtocol = frame_provider
        self._frame_queue: ReadyFrameQueue = frame_queue
        # AUD-38804: 管理器侧镜像值与 setter 可能被外部线程并发调用，
        # 写入经锁同步（读取仅发生在创建 worker 的构造路径）。
        self._prefetch_lookahead_lock: threading.Lock = threading.Lock()
        self._prefetch_lookahead: int = prefetch_lookahead

        self._proxy_worker: Optional[ProxyDecodeWorker] = None
        self._master_worker: Optional[MasterDecodeWorker] = None

        self._generation: int = 0
        self._generation_lock: threading.Lock = threading.Lock()

    @property
    def generation(self) -> int:
        """当前 generation_id"""
        with self._generation_lock:
            return self._generation

    @property
    def proxy_worker(self) -> Optional[ProxyDecodeWorker]:
        """Proxy 工作线程"""
        return self._proxy_worker

    @property
    def master_worker(self) -> Optional[MasterDecodeWorker]:
        """Master 工作线程"""
        return self._master_worker

    def create_workers(self) -> bool:
        """
        创建工作线程。

        Returns:
            True 表示成功创建
        """
        try:
            self._proxy_worker = ProxyDecodeWorker(
                frame_provider=self._frame_provider,
                frame_queue=self._frame_queue,
                prefetch_lookahead=self._prefetch_lookahead,
            )

            self._master_worker = MasterDecodeWorker(
                frame_provider=self._frame_provider,
                frame_queue=self._frame_queue,
            )

            logger.info("Decode workers created")
            return True

        except Exception as e:
            logger.error(f"Failed to create decode workers: {e}")
            return False

    def start_workers(self) -> bool:
        """
        启动工作线程。

        Returns:
            True 表示成功启动
        """
        proxy_started = False
        master_started = False

        if self._proxy_worker is not None:
            proxy_started = self._proxy_worker.is_running() or self._proxy_worker.start()

        if self._master_worker is not None:
            master_started = self._master_worker.is_running() or self._master_worker.start()

        if proxy_started and master_started:
            logger.info("Decode workers started")
            return True
        else:
            logger.warning(
                f"Workers start partial: proxy={proxy_started}, master={master_started}"
            )
            return False

    def start_master_worker(self) -> bool:
        """Start only the exact-frame worker for a paused static preview.

        A Color clip switch needs its master frame without starting proxy
        prefetch.  Keeping that work single-lane prevents a shared FFmpeg
        decoder from receiving concurrent seek/read operations.
        """
        if self._master_worker is None:
            return False
        return self._master_worker.is_running() or self._master_worker.start()

    def stop_workers(self, timeout: float = STOP_TIMEOUT_SECONDS) -> bool:
        """
        停止工作线程。

        Args:
            timeout: 等待超时（秒）

        Returns:
            True 表示成功停止

        Note:
            设计要求：stop/unload 后线程能在 2 秒内退出
        """
        proxy_stopped = True
        master_stopped = True

        if self._proxy_worker is not None:
            proxy_stopped = self._proxy_worker.stop(timeout=timeout)

        if self._master_worker is not None:
            master_stopped = self._master_worker.stop(timeout=timeout)

        if proxy_stopped and master_stopped:
            logger.info("Decode workers stopped")
            return True
        else:
            logger.warning(
                f"Workers stop timeout: proxy={proxy_stopped}, master={master_stopped}"
            )
            return False

    def pause_workers(self) -> None:
        """V-P0-06: 暂停解码传输但保留工作线程。

        pause/seek/stop 在播放过程中极其高频；若每次都 stop_workers() 会反复
        销毁并重建线程，丢失热解码器/请求缓存，导致恢复延迟与线程抖动。这里只
        清空待处理请求并让 worker 进入空闲等待（线程仍存活），seek/恢复播放时
        立即可用。只有 unload/teardown 才调用 stop_workers() 彻底销毁线程。
        """
        if self._proxy_worker is not None:
            try:
                self._proxy_worker.clear_requests()
            except Exception:
                logger.debug("Failed to clear proxy worker requests", exc_info=True)
        if self._master_worker is not None:
            try:
                self._master_worker.clear_requests()
            except Exception:
                logger.debug("Failed to clear master worker requests", exc_info=True)
        logger.debug("Decode workers paused (threads kept alive)")

    def increment_generation(self) -> int:
        """
        递增 generation_id（seek 后调用）。

        Returns:
            新的 generation_id
        """
        with self._generation_lock:
            self._generation += 1

            # 同步到队列
            self._frame_queue.increment_generation()

            # 同步到 worker
            if self._proxy_worker is not None:
                self._proxy_worker.update_generation(self._generation)

            if self._master_worker is not None:
                self._master_worker.update_generation(self._generation)

            logger.debug(f"Generation incremented to {self._generation}")
            return self._generation

    def update_generation(self, generation_id: int) -> None:
        """
        更新 generation_id（由 VideoSession._increment_generation 调用）。

        Args:
            generation_id: 新的 generation_id
        """
        with self._generation_lock:
            self._generation = generation_id

            # 同步到 worker
            if self._proxy_worker is not None:
                self._proxy_worker.update_generation(generation_id)

            if self._master_worker is not None:
                self._master_worker.update_generation(generation_id)

            logger.debug(f"Generation updated to {generation_id}")

    def request_proxy_frame(
        self,
        frame_index: int,
        reason: FrameRequestReason = FrameRequestReason.PLAYBACK,
    ) -> None:
        """
        请求 Proxy 帧。

        Args:
            frame_index: 目标帧号
            reason: 请求原因
        """
        if self._proxy_worker is not None:
            self._proxy_worker.request_frame(
                frame_index=frame_index,
                generation_id=self.generation,
                reason=reason,
            )

    def request_master_frame(
        self,
        frame_index: int,
        reason: FrameRequestReason = FrameRequestReason.SEEK,
    ) -> None:
        """
        请求 Master 帧。

        Args:
            frame_index: 目标帧号
            reason: 请求原因
        """
        if self._master_worker is not None:
            self._master_worker.request_master_frame(
                frame_index=frame_index,
                generation_id=self.generation,
                reason=reason,
            )

    def update_clock_position(self, current_frame: int) -> None:
        """
        更新时钟位置，触发预读。

        Args:
            current_frame: 当前 clock 帧号
        """
        if self._proxy_worker is not None:
            self._proxy_worker.update_clock_position(
                current_frame=current_frame,
                generation_id=self.generation,
            )

    def set_prefetch_lookahead(self, value: int) -> None:
        """
        设置预读帧数。

        Args:
            value: 新的预读帧数（最小为1）
        """
        # AUD-38804: 镜像值持锁写入，转发给 proxy worker 保持锁外。
        with self._prefetch_lookahead_lock:
            self._prefetch_lookahead = max(1, value)
        # 通知 proxy worker
        if self._proxy_worker:
            self._proxy_worker.set_prefetch_lookahead(self._prefetch_lookahead)
        logger.debug(f"[DecodeWorkerManager] Prefetch lookahead set to {self._prefetch_lookahead}")

    def get_stats(self) -> dict[str, dict[str, float | int]]:
        """
        获取工作线程统计信息。

        Returns:
            统计信息字典
        """
        stats = {}

        if self._proxy_worker is not None:
            stats['proxy'] = self._proxy_worker.stats.to_dict()

        if self._master_worker is not None:
            stats['master'] = self._master_worker.stats.to_dict()

        stats['queue'] = self._frame_queue.stats
        stats['generation'] = self.generation

        return stats

    def is_running(self) -> bool:
        """
        检查所有工作线程是否运行中。

        Returns:
            全部运行返回 True
        """
        proxy_running = self._proxy_worker is not None and self._proxy_worker.is_running()
        master_running = self._master_worker is not None and self._master_worker.is_running()
        return proxy_running and master_running
