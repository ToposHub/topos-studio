"""
Decode Queue Module
解码队列管理，支持预读和优先级调度。

## 职责

- FrameRequest: 帧请求描述
- DecodeQueue: 解码队列管理器

## 设计原则

- 异步预读：后台线程预解码帧
- 优先级调度：当前帧优先，远离帧次之
- Seek flush：seek 时清空旧请求
- 线程安全：队列操作使用锁保护

## 使用示例

```python
from src.shared.media import MediaSourceFactory, DecodeQueue

# 创建媒体源
source = MediaSourceFactory.create('/path/to/video.mp4')
source.open()

# 创建解码队列
queue = DecodeQueue(source, window_size=12)
queue.start()

# 请求帧（自动预读窗口内的帧）
queue.request_frame(100, is_master=True)

# 获取已解码帧
frame = queue.get_frame(timeout=0.1)

# Seek 时清空旧请求
queue.seek(200)

# 停止
queue.stop()
source.close()
```
"""

from __future__ import annotations

import queue
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from .source import MediaSource
    from .frame import DecodedFrame

logger = logging.getLogger(__name__)


class FramePriority(Enum):
    """帧请求优先级"""
    CRITICAL = 0    # 当前帧，最高优先级
    HIGH = 1        # 邻近帧（+/- 3帧内）
    NORMAL = 2      # 窗口内帧
    LOW = 3         # 预读范围外


@dataclass(order=True)
class FrameRequest:
    """
    帧请求描述。

    使用 dataclass 的 order=True 实现优先级排序，
    priority 越小优先级越高。

    Attributes:
        frame_index: 帧号（从 0 开始）
        is_master: 是否为母版帧（高优先级）
        priority: 优先级（数值越小优先级越高）
        timestamp: 请求创建时间戳
        cancelled: 是否已取消
        request_id: 唯一请求 ID
    """
    frame_index: int
    is_master: bool = False
    priority: int = field(default=FramePriority.NORMAL.value)
    timestamp: float = field(default_factory=time.monotonic)
    cancelled: bool = field(default=False, compare=False)
    request_id: int = field(default=0, compare=False)

    def cancel(self) -> None:
        """取消请求"""
        self.cancelled = True

    def __repr__(self) -> str:
        status = "CANCELLED" if self.cancelled else "ACTIVE"
        return (
            f"FrameRequest(frame={self.frame_index}, "
            f"master={self.is_master}, "
            f"priority={self.priority}, "
            f"status={status})"
        )


@dataclass
class DecodedFrameEntry:
    """
    已解码帧条目。

    Attributes:
        frame: 解码后的帧数据
        frame_index: 帧号
        is_master: 是否为母版帧
        decode_time: 解码完成时间戳
        request_id: 对应的请求 ID
        decode_epoch: 解码代数（AUD-34500）——seek/stop 清队时自增；
            入队时记录请求所属代，消费侧据此丢弃过期代的帧
    """
    frame: DecodedFrame
    frame_index: int
    is_master: bool
    decode_time: float = field(default_factory=time.monotonic)
    request_id: int = 0
    decode_epoch: int = 0


class DecodeQueue:
    """
    解码队列管理器。

    管理帧请求的预读、调度和解码。支持：
    - 预读窗口：当前帧 +/- window_size 帧
    - 优先级调度：当前帧最高优先级
    - Seek flush：seek 时清空旧请求
    - 线程安全：队列操作使用锁保护

    Usage:
        source = MediaSourceFactory.create('/path/to/video.mp4')
        source.open()
        queue = DecodeQueue(source, window_size=12)
        queue.start()
        queue.request_frame(100, is_master=True)
        frame = queue.get_frame(timeout=0.1)
        queue.stop()
    """

    def __init__(
        self,
        source: MediaSource,
        window_size: int = 12,
        max_queue_size: int = 64
    ) -> None:
        """
        初始化解码队列。

        Args:
            source: 媒体源（必须已打开）
            window_size: 预读窗口大小（当前帧前后各多少帧）
            max_queue_size: 已解码帧队列最大长度
        """
        self._source = source
        self._window_size = window_size
        self._max_queue_size = max_queue_size

        # 请求队列（按优先级排序）
        self._requests: list[FrameRequest] = []
        self._request_lock = threading.Lock()

        # 已解码帧队列（线程安全）
        self._decoded_queue: queue.Queue[Optional[DecodedFrameEntry]] = queue.Queue(
            maxsize=max_queue_size
        )

        # 请求 ID 计数器
        self._request_counter = 0

        # 线程控制
        self._stop_flag = threading.Event()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._current_frame_index: int = 0

        # AUD-34500：解码代数（epoch）。seek()/stop() 清空已解码队列时
        # 自增；预读线程把在途请求的解码结果入队前校验代数，过期帧
        # 直接丢弃——否则正在解码的旧帧会在清队后重新入队。
        self._decode_epoch: int = 0

        # 统计信息
        self._stats = {
            'requests_made': 0,
            'requests_cancelled': 0,
            'frames_decoded': 0,
            'frames_dropped': 0,
            'seek_count': 0,
        }

        logger.info(
            f"DecodeQueue: 初始化完成, "
            f"window_size={window_size}, "
            f"max_queue_size={max_queue_size}"
        )

    def start(self) -> None:
        """
        启动预读线程。

        如果线程已运行，不做任何操作。
        """
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            logger.warning("DecodeQueue: 预读线程已在运行")
            return

        self._stop_flag.clear()
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_loop,
            name="DecodeQueue-Prefetch",
            daemon=True
        )
        self._prefetch_thread.start()
        logger.info("DecodeQueue: 预读线程已启动")

    def stop(self, timeout: float = 2.0) -> None:
        """
        停止预读线程。

        Args:
            timeout: 等待线程结束的超时时间（秒）
        """
        self._stop_flag.set()

        if self._prefetch_thread is not None:
            self._prefetch_thread.join(timeout=timeout)
            if self._prefetch_thread.is_alive():
                logger.warning("DecodeQueue: 预读线程未能在超时时间内停止")
            else:
                logger.info("DecodeQueue: 预读线程已停止")

        self._prefetch_thread = None

        # 清空队列
        self._clear_queues()

    def request_frame(
        self,
        frame_index: int,
        is_master: bool = False,
        priority: Optional[int] = None
    ) -> FrameRequest:
        """
        请求解码指定帧。

        自动触发预读窗口内的帧请求。

        Args:
            frame_index: 目标帧号（从 0 开始）
            is_master: 是否为母版帧（高优先级）
            priority: 手动指定优先级（None 则自动计算）

        Returns:
            帧请求对象。同一帧号重复请求时返回队列中**既有**的请求对象
            （并提升其优先级）——调用方拿到的句柄即队列中真实条目，
            cancel_request/Request.cancel() 对其有效（AUD-34501）。
        """
        with self._request_lock:
            # 分配请求 ID
            self._request_counter += 1
            request_id = self._request_counter

            # 计算优先级
            if priority is None:
                priority = self._calculate_priority(frame_index, is_master)

            # AUD-34501：同帧号去重——返回队列中既有句柄（更新优先级），
            # 此前新建的 FrameRequest 被静默丢弃，调用方 cancel 的是孤儿
            # 对象，取消静默失效。
            handle: Optional[FrameRequest] = None
            for existing in self._requests:
                if existing.frame_index == frame_index and not existing.cancelled:
                    existing.priority = min(existing.priority, priority)
                    handle = existing
                    break

            if handle is None:
                handle = FrameRequest(
                    frame_index=frame_index,
                    is_master=is_master,
                    priority=priority,
                    request_id=request_id
                )
                # 添加到队列（按优先级插入）
                self._insert_request_sorted(handle)

            self._stats['requests_made'] += 1

            # 更新当前帧位置
            self._current_frame_index = frame_index

        # 触发预读
        self._trigger_prefetch(frame_index, is_master)

        logger.debug(f"DecodeQueue: 请求帧 {frame_index}, master={is_master}, priority={priority}")
        return handle

    def seek(self, frame_index: int) -> None:
        """
        Seek 到指定帧。

        清空所有旧请求和已解码帧，然后请求新位置的帧。

        Args:
            frame_index: 目标帧号
        """
        logger.info(f"DecodeQueue: Seek 到帧 {frame_index}")

        with self._request_lock:
            # 取消所有未完成的请求
            for request in self._requests:
                if not request.cancelled:
                    request.cancel()
                    self._stats['requests_cancelled'] += 1

            # 清空请求队列
            self._requests.clear()

            # 清空已解码帧队列
            self._clear_decoded_queue()

            # AUD-34500：代数自增——已弹出、正在解码的在途请求解码完成
            # 后不得重新入队（清队后入队即过期帧）
            self._decode_epoch += 1

            # 更新当前帧位置
            self._current_frame_index = frame_index
            self._stats['seek_count'] += 1

        # 请求新位置的帧
        self.request_frame(frame_index, is_master=True)

    def get_frame(
        self,
        frame_index: Optional[int] = None,
        timeout: float = 0.1
    ) -> Optional[DecodedFrame]:
        """
        获取已解码的帧。

        Args:
            frame_index: 期望的帧号（None 表示任意帧）
            timeout: 等待超时时间（秒）

        Returns:
            DecodedFrame 或 None（队列为空、超时、取到过期代条目，
            或超时预算内未出现期望帧号——AUD-34502：不匹配条目会
            被放回队列并在剩余预算内继续等待目标帧）
        """
        entry = self._take_entry(frame_index, timeout)
        if entry is None:
            return None
        return entry.frame

    def get_frame_entry(
        self,
        frame_index: Optional[int] = None,
        timeout: float = 0.1
    ) -> Optional[DecodedFrameEntry]:
        """
        获取已解码帧条目（包含元数据）。

        Args:
            frame_index: 期望的帧号（None 表示任意帧）
            timeout: 等待超时时间（秒）

        Returns:
            DecodedFrameEntry 或 None（语义同 get_frame）
        """
        return self._take_entry(frame_index, timeout)

    def _take_entry(
        self,
        frame_index: Optional[int],
        timeout: float,
    ) -> Optional[DecodedFrameEntry]:
        """
        取出一个已解码条目（get_frame / get_frame_entry 公共实现）。

        AUD-34502：期望帧已在队列中时经 `_scan_decoded_queue_for`
        原地摘除，不打乱其余条目次序；仅当弹出条目与期望帧号不匹配
        时才放回队尾，并在剩余超时预算内继续等待目标帧。

        Args:
            frame_index: 期望帧号（None 表示任意帧）
            timeout: 等待超时（秒）

        Returns:
            匹配的 DecodedFrameEntry；超时/队空/无匹配返回 None。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            # 原地扫描：目标帧已在队列时直接摘除（不打乱其余条目）
            if frame_index is not None:
                scanned = self._scan_decoded_queue_for(frame_index)
                if scanned is not None:
                    return scanned

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                entry = self._decoded_queue.get(timeout=remaining)
            except queue.Empty:
                return None

            if entry is None:
                return None

            # AUD-34500：过期代条目（seek/stop 清队前入队的帧）直接丢弃
            if entry.decode_epoch != self._decode_epoch:
                with self._request_lock:
                    self._stats['frames_dropped'] += 1
                continue

            if frame_index is None or entry.frame_index == frame_index:
                return entry

            # 不匹配：放回队列尾，剩余预算内继续等目标帧；
            # 队列满时丢弃（AUD-34502 文档化语义）
            try:
                self._decoded_queue.put(entry, block=False)
            except queue.Full:
                with self._request_lock:
                    self._stats['frames_dropped'] += 1

    def _scan_decoded_queue_for(
        self,
        frame_index: int,
    ) -> Optional[DecodedFrameEntry]:
        """
        原地扫描已解码队列，摘除目标帧条目（AUD-34502）。

        在 Queue 内部互斥锁下重建内部 deque：命中条目被摘除返回，
        过期代条目（AUD-34500）被顺手丢弃，其余条目保持原相对次序。

        Args:
            frame_index: 期望帧号

        Returns:
            匹配的 DecodedFrameEntry；未命中返回 None。
        """
        found: Optional[DecodedFrameEntry] = None
        stale_count = 0
        with self._decoded_queue.mutex:
            internal = self._decoded_queue.queue
            kept: list[Optional[DecodedFrameEntry]] = []
            for entry in internal:
                if entry is None:
                    continue
                if entry.decode_epoch != self._decode_epoch:
                    stale_count += 1
                    continue
                if found is None and entry.frame_index == frame_index:
                    found = entry
                    continue
                kept.append(entry)
            if found is not None or stale_count:
                internal.clear()
                internal.extend(entry for entry in kept if entry is not None)
        if stale_count:
            with self._request_lock:
                self._stats['frames_dropped'] += stale_count
        return found

    def cancel_request(self, request: FrameRequest) -> None:
        """
        取消指定请求。

        Args:
            request: 要取消的请求
        """
        with self._request_lock:
            if not request.cancelled:
                request.cancel()
                self._stats['requests_cancelled'] += 1
                logger.debug(f"DecodeQueue: 取消请求 {request.request_id}")

    def get_stats(self) -> dict:
        """
        获取统计信息。

        Returns:
            包含统计数据的字典
        """
        with self._request_lock:
            return {
                **self._stats,
                'pending_requests': len([r for r in self._requests if not r.cancelled]),
                'decoded_queue_size': self._decoded_queue.qsize(),
                'current_frame': self._current_frame_index,
            }

    # ================================================================
    # 内部方法
    # ================================================================

    def _calculate_priority(self, frame_index: int, is_master: bool) -> int:
        """
        计算帧请求优先级。

        Args:
            frame_index: 帧号
            is_master: 是否为母版帧

        Returns:
            优先级数值（越小优先级越高）
        """
        if is_master:
            return FramePriority.CRITICAL.value

        # 计算与当前帧的距离
        distance = abs(frame_index - self._current_frame_index)

        if distance <= 3:
            return FramePriority.HIGH.value
        elif distance <= self._window_size:
            return FramePriority.NORMAL.value
        else:
            return FramePriority.LOW.value

    def _insert_request_sorted(self, request: FrameRequest) -> None:
        """
        按优先级插入请求到队列。

        Args:
            request: 要插入的请求
        """
        # 检查是否已有相同帧号的请求
        for existing in self._requests:
            if existing.frame_index == request.frame_index and not existing.cancelled:
                # 已存在，更新优先级
                existing.priority = min(existing.priority, request.priority)
                return

        # 插入到正确位置（按优先级排序）
        inserted = False
        for i, existing in enumerate(self._requests):
            if request.priority < existing.priority:
                self._requests.insert(i, request)
                inserted = True
                break

        if not inserted:
            self._requests.append(request)

    def _trigger_prefetch(self, frame_index: int, is_master: bool) -> None:
        """
        触发预读窗口内的帧请求。

        Args:
            frame_index: 当前帧号
            is_master: 当前帧是否为母版帧
        """
        # 获取视频总帧数
        try:
            metadata = self._source.probe()
            total_frames = metadata.total_frames or 0
        except Exception:
            total_frames = 0

        with self._request_lock:
            # 预读窗口：current - window_size 到 current + window_size
            start_frame = max(0, frame_index - self._window_size)
            end_frame = frame_index + self._window_size
            if total_frames > 0:
                end_frame = min(end_frame, total_frames - 1)

            # 当前帧已经在 request_frame 中添加
            # 这里添加窗口内的其他帧
            for i in range(start_frame, end_frame + 1):
                if i == frame_index:
                    continue  # 跳过当前帧

                # 检查是否已有请求
                has_request = any(
                    r.frame_index == i and not r.cancelled
                    for r in self._requests
                )

                if not has_request:
                    # 创建预读请求
                    self._request_counter += 1
                    priority = self._calculate_priority(i, False)
                    request = FrameRequest(
                        frame_index=i,
                        is_master=False,
                        priority=priority,
                        request_id=self._request_counter
                    )
                    self._insert_request_sorted(request)
                    self._stats['requests_made'] += 1

    def _prefetch_loop(self) -> None:
        """
        预读循环。

        后台线程持续处理帧请求，直到收到停止信号。
        """
        logger.debug("DecodeQueue: 预读线程开始运行")

        while not self._stop_flag.is_set():
            # 获取下一个请求（携带弹出时的解码代数，AUD-34500）
            request, epoch = self._pop_next_request()

            if request is None:
                # 没有请求，等待一段时间
                time.sleep(0.01)
                continue

            # 检查是否已取消
            if request.cancelled:
                continue

            # 检查队列是否已满
            if self._decoded_queue.full():
                # 队列满，等待
                time.sleep(0.01)
                # 重新放回请求（仅在代数未变化时；seek/stop 后直接丢弃）
                with self._request_lock:
                    if epoch == self._decode_epoch:
                        self._insert_request_sorted(request)
                continue

            # 执行解码
            try:
                frame = self._source.read_frame(request.frame_index)

                # AUD-34500：seek/stop 清队后，在途旧帧不得重新入队
                if epoch != self._decode_epoch:
                    logger.debug(
                        f"DecodeQueue: 丢弃过期代帧 frame={request.frame_index}"
                    )
                    continue

                # 创建解码条目
                entry = DecodedFrameEntry(
                    frame=frame,
                    frame_index=request.frame_index,
                    is_master=request.is_master,
                    request_id=request.request_id,
                    decode_epoch=epoch
                )

                # 放入队列
                try:
                    self._decoded_queue.put(entry, timeout=0.1)
                    # AUD-34505：计数器写入统一置于 _request_lock 内，
                    # 与 get_stats 的加锁读取配对（RLock 可重入）
                    with self._request_lock:
                        self._stats['frames_decoded'] += 1
                    logger.debug(
                        f"DecodeQueue: 解码完成 frame={request.frame_index}, "
                        f"master={request.is_master}"
                    )
                except queue.Full:
                    # 队列满，丢弃
                    with self._request_lock:
                        self._stats['frames_dropped'] += 1
                    logger.warning(f"DecodeQueue: 队列满，丢弃帧 {request.frame_index}")

            except Exception as e:
                logger.error(
                    f"DecodeQueue: 解码失败 frame={request.frame_index}, "
                    f"error={e}"
                )
                # 解码失败，放入 None 表示错误（同样尊重代数）
                if epoch == self._decode_epoch:
                    try:
                        self._decoded_queue.put(None, timeout=0.1)
                    except queue.Full:
                        pass

        logger.debug("DecodeQueue: 预读线程结束")

    def _pop_next_request(self) -> tuple[Optional[FrameRequest], int]:
        """
        弹出下一个有效请求。

        弹出与代数读取在同一临界区内完成，保证返回的 epoch 即该请求
        所属的解码代（AUD-34500）。

        Returns:
            (请求或 None, 弹出时的解码代数)
        """
        with self._request_lock:
            while self._requests:
                request = self._requests.pop(0)
                if not request.cancelled:
                    return request, self._decode_epoch
            return None, self._decode_epoch

    def _clear_queues(self) -> None:
        """清空所有队列"""
        with self._request_lock:
            self._requests.clear()
            # AUD-34500：stop() 清队同样自增代数，阻止仍在解码的线程
            # 在清队后写入残留帧
            self._decode_epoch += 1

        self._clear_decoded_queue()

    def _clear_decoded_queue(self) -> None:
        """清空已解码帧队列"""
        while not self._decoded_queue.empty():
            try:
                self._decoded_queue.get_nowait()
            except queue.Empty:
                break

    def __repr__(self) -> str:
        """字符串表示"""
        stats = self.get_stats()
        return (
            f"DecodeQueue("
            f"pending={stats['pending_requests']}, "
            f"decoded={stats['decoded_queue_size']}, "
            f"current={stats['current_frame']})"
        )