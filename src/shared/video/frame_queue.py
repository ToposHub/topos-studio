"""
Video Frame Queue Module

定义就绪帧队列的数据结构，用于播放、seek、scrub 等场景的帧缓存管理。

## 职责

- QueueEntry: 队列元素，封装 DecodedFrame + 元数据
- ReadyFrameQueue: 就绪帧队列管理

## 设计原则

- proxy queue 满时丢旧帧，保留最新帧
- master queue 不丢目标帧
- seek 后 generation_id 递增，旧 generation 的帧自动丢弃
- 线程安全（threading.Lock）
- 完整类型注解

## 队列策略

Proxy Queue:
- 用于实时播放预览
- 满时丢旧帧，保留最新帧（消费者能拿到最新状态）
- 可丢弃帧，不阻塞解码

Master Queue:
- 用于高质量显示、导出、Composite
- 不丢目标帧（精确性优先）
- generation 过期才丢弃；消费方落后时按 FIFO 淘汰最旧帧使容量上限
  真实生效（AUD-39200，防无界增长与 GPU 帧泄漏）
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from ..media.frame import DecodedFrame

from ..media.gpu_frame import GpuDecodedFrame, safe_release_gpu_frame, is_gpu_decoded_frame


logger = logging.getLogger(__name__)


# ================================================================
# AUD-39201: GPU 帧释放的渲染线程契约
#
# gpu_frame.py 的铁律：``release.release()`` 不由 UI tick 调用，必须由
# 渲染线程在安全时机调用。此前队列的所有淘汰路径（seek 翻代数、容量
# 淘汰、同帧号替换、clear）都在"恰好调用队列 API 的线程"上同步触发
# release 回调——UI 线程 seek / unload 时 release 回调（内含 GL /
# CVPixelBuffer 操作）就在 UI 线程执行，违反契约。
#
# 机制：队列淘汰 GPU 帧时改经 ``_defer_release_gpu_frame`` 派发——
# - 未注册渲染线程（测试 / 无 GL 环境）→ 立即释放（保持既有行为，
#   GpuFrameRelease.release 幂等且线程安全）；
# - 注册了渲染线程且当前线程即渲染线程 → 立即释放；
# - 其余线程 → 挂入模块级 pending 列表，由渲染线程在命令循环里
#   ``drain_pending_gpu_frame_releases()`` 排水释放。
# ================================================================

_gpu_release_lock: threading.Lock = threading.Lock()
_pending_gpu_releases: list[GpuDecodedFrame] = []
_gpu_release_thread_id: Optional[int] = None

#: pending 列表容量上限：防止渲染线程死亡时无界累积（超限即在调用
#: 线程同步释放兜底——宁可偶发同步释放也不许无界内存增长）。
_PENDING_GPU_RELEASE_CAP: int = 512


def set_gpu_frame_release_thread(thread_id: Optional[int]) -> None:
    """注册/注销 GPU 帧释放的所属渲染线程（按线程 ident）。

    Args:
        thread_id: 渲染线程 ``threading.get_ident()``；None 表示注销，
            注销后排水的 pending 会被立即清空释放，后续释放回到
            "调用线程立即释放"的默认行为。
    """
    global _gpu_release_thread_id
    with _gpu_release_lock:
        _gpu_release_thread_id = thread_id
        if thread_id is None:
            pending = list(_pending_gpu_releases)
            _pending_gpu_releases.clear()
        else:
            pending = []
    for frame in pending:
        safe_release_gpu_frame(frame)


def pending_gpu_release_count() -> int:
    """当前挂起待渲染线程释放的 GPU 帧数量（诊断/测试用）。"""
    with _gpu_release_lock:
        return len(_pending_gpu_releases)


def gpu_frame_release_thread_id() -> Optional[int]:
    """返回已注册的渲染线程 ident；未注册返回 None（AUD-39811 辅助）。

    供其他 GL 触碰点（如 YUVGPUUploader.enable_pbo）做线程契约守卫：
    注册了渲染线程而当前线程不是它时，禁止在该线程创建/释放 GL 对象。
    """
    with _gpu_release_lock:
        return _gpu_release_thread_id


def drain_pending_gpu_frame_releases(limit: int = 256) -> int:
    """由渲染线程排水释放挂起的 GPU 帧（AUD-39201）。

    Args:
        limit: 单次最多释放数量（防止长时间占用渲染线程）

    Returns:
        本次实际释放的帧数
    """
    released: list[GpuDecodedFrame] = []
    with _gpu_release_lock:
        take = min(max(0, limit), len(_pending_gpu_releases))
        if take:
            released = _pending_gpu_releases[:take]
            del _pending_gpu_releases[:take]
    for frame in released:
        safe_release_gpu_frame(frame)
    return len(released)


def _defer_release_gpu_frame(frame: Any) -> None:
    """按渲染线程契约释放 GPU 帧（队列内部淘汰路径统一入口）。

    Args:
        frame: 被淘汰的帧对象（非 GpuDecodedFrame 时无操作）
    """
    if not is_gpu_decoded_frame(frame):
        return
    global _gpu_release_thread_id
    with _gpu_release_lock:
        owner = _gpu_release_thread_id
        if owner is None or owner == threading.get_ident():
            # 未注册渲染线程，或当前就是渲染线程 → 立即释放
            immediate = True
        elif len(_pending_gpu_releases) >= _PENDING_GPU_RELEASE_CAP:
            # 渲染线程不排水导致 pending 堆满：调用线程同步释放兜底
            immediate = True
            logger.warning(
                "Pending GPU release backlog hit cap (%d); releasing "
                "on caller thread as a safety valve",
                _PENDING_GPU_RELEASE_CAP,
            )
        else:
            immediate = False
            _pending_gpu_releases.append(frame)
    if immediate:
        safe_release_gpu_frame(frame)


class FrameSource(Enum):
    """
    帧来源类型。

    用于追踪帧的解码来源和质量等级。
    """

    PROXY = "proxy"       # 低分辨率代理帧
    MASTER = "master"     # 高质量原始帧
    DISK_PROXY = "disk_proxy"  # 磁盘代理文件
    GPU_PROXY = "gpu_proxy"    # GPU 零拷贝代理帧（VT/D3D11 zero-copy）
    GPU_MASTER = "gpu_master"  # GPU 零拷贝母版帧


@dataclass
class LatestFrameResult:
    """
    latest-before-or-at 操作的返回结果。

    用于 dequeue_latest_before_or_at() 和 get_latest_before_or_at() 方法，
    提供清晰的类型定义。

    Attributes:
        entry: 找到的 QueueEntry（如果成功）
        actual_frame_index: 实际帧号（<= target_frame_index）
        found: 是否找到有效帧
        older_frames_discarded: 丢弃的更旧帧数量

    Examples:
        >>> result = queue.dequeue_latest_before_or_at(frame_index=100)
        >>> if result.found:
        ...     frame = result.entry.frame
        ...     print(f"Got frame {result.actual_frame_index}")
    """

    entry: Optional[QueueEntry] = None
    actual_frame_index: int = -1
    found: bool = False
    older_frames_discarded: int = 0

    def __bool__(self) -> bool:
        """支持 if result: 语法"""
        return self.found


@dataclass
class QueueEntry:
    """
    队列元素。

    封装 DecodedFrame 或 GpuDecodedFrame 及相关元数据，用于队列管理和统计。

    Attributes:
        frame: 解码后的帧数据（DecodedFrame 或 GpuDecodedFrame）
        decode_ms: 解码耗时（毫秒）
        source: 帧来源类型
        can_drop: 是否可丢弃（用于 proxy vs master 区分）
        generation_id: 生成版本号
        enqueue_time: 入队时间戳（用于计算队列等待时间）
        request_reason: 请求原因（用于调试和统计）

    Examples:
        >>> entry = QueueEntry(
        ...     frame=decoded_frame,
        ...     decode_ms=15.3,
        ...     source=FrameSource.PROXY,
        ...     can_drop=True,
        ...     generation_id=1,
        ... )
    """

    frame: Union['DecodedFrame', GpuDecodedFrame]
    decode_ms: float
    source: FrameSource
    can_drop: bool
    generation_id: int
    enqueue_time: float = field(default_factory=time.time)
    request_reason: Optional[str] = None

    @property
    def frame_index(self) -> int:
        """帧号"""
        return self.frame.frame_index

    @property
    def is_stale(self) -> bool:
        """
        是否为过期帧（基于 generation_id）。

        过期判断由队列管理，此处仅作为标记。
        """
        return False  # 实际判断在队列层面

    @property
    def is_gpu(self) -> bool:
        """是否为 GPU 零拷贝帧"""
        return is_gpu_decoded_frame(self.frame)

    @property
    def queue_wait_ms(self) -> float:
        """
        队列等待时间（毫秒）。

        Returns:
            从入队到现在的毫秒数
        """
        return (time.time() - self.enqueue_time) * 1000.0

    def snapshot(self) -> "QueueEntry":
        """
        返回 entry 的浅拷贝快照（AUD-39202）。

        队列的 peek/get_latest/get_latest_before_or_at 返回本方法产出的
        快照副本而不是队列内部 entry 引用：调用方对返回值的字段修改
        不会串改队列状态。注意快照**不转移帧所有权**——底层 frame 对象
        仍归队列所有，队列淘汰该帧时可能释放其 GPU 资源；需要持有
        帧所有权的场景必须使用 dequeue* 系列方法。

        Returns:
            新的 QueueEntry（字段浅拷贝，frame 引用共享）
        """
        return replace(self)

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"QueueEntry(frame={self.frame_index}, "
            f"source={self.source.value}, "
            f"decode_ms={self.decode_ms:.1f}, "
            f"gen={self.generation_id}, "
            f"drop={self.can_drop})"
        )


class ReadyFrameQueue:
    """
    就绪帧队列。

    管理 proxy 和 master 帧的缓存，支持：
    - proxy queue 满时丢旧帧，保留最新帧
    - master queue 不丢目标帧
    - generation 过期帧自动丢弃
    - 线程安全操作

    Attributes:
        max_proxy_size: proxy 队列最大长度
        max_master_size: master 队列最大长度
        current_generation: 当前 generation_id
        stats: 队列统计信息

    Examples:
        >>> queue = ReadyFrameQueue(max_proxy_size=12, max_master_size=3)
        >>> queue.enqueue(entry)
        >>> entry = queue.dequeue(is_master=False)
        >>> queue.increment_generation()  # seek 后递增
    """

    def __init__(
        self,
        max_proxy_size: int = 12,
        max_master_size: int = 3,
    ) -> None:
        """
        初始化队列。

        Args:
            max_proxy_size: proxy 队列最大长度（默认 12）
            max_master_size: master 队列最大长度（默认 3）
        """
        self._max_proxy_size: int = max_proxy_size
        self._max_master_size: int = max_master_size
        self._current_generation: int = 0

        # 队列存储（使用 list 作为 FIFO）
        self._proxy_queue: list[QueueEntry] = []
        self._master_queue: list[QueueEntry] = []

        # 线程安全锁
        self._lock: threading.Lock = threading.Lock()

        # 统计信息
        self._stats: dict[str, int] = {
            "proxy_enqueued": 0,
            "proxy_dequeued": 0,
            "proxy_dropped": 0,
            "master_enqueued": 0,
            "master_dequeued": 0,
            "master_dropped": 0,
            "generation_changes": 0,
        }

    @property
    def max_proxy_size(self) -> int:
        """proxy 队列最大长度"""
        return self._max_proxy_size

    @property
    def max_master_size(self) -> int:
        """master 队列最大长度"""
        return self._max_master_size

    @property
    def current_generation(self) -> int:
        """当前 generation_id"""
        return self._current_generation

    @property
    def stats(self) -> dict[str, int]:
        """队列统计信息（只读副本）"""
        with self._lock:
            return dict(self._stats)

    def increment_generation(self) -> int:
        """
        递增 generation_id（seek 后调用）。

        Returns:
            新的 generation_id
        """
        with self._lock:
            self._current_generation += 1
            self._stats["generation_changes"] += 1

            # 丢弃旧 generation 帧
            dropped_proxy = self._drop_stale_frames(self._proxy_queue)
            dropped_master = self._drop_stale_frames(self._master_queue)

            self._stats["proxy_dropped"] += dropped_proxy
            self._stats["master_dropped"] += dropped_master

            logger.debug(
                f"Generation incremented to {self._current_generation}, "
                f"dropped {dropped_proxy} proxy + {dropped_master} master frames"
            )

            return self._current_generation

    def _drop_stale_frames(self, queue: list[QueueEntry]) -> int:
        """
        丢弃过期帧（内部方法，不加锁）。

        对于 GpuDecodedFrame，丢弃前调用 safe_release_gpu_frame。

        Args:
            queue: 目标队列

        Returns:
            丢弃的帧数
        """
        current_gen = self._current_generation
        # AUD-39204: 单次遍历重建列表（原 list.remove 每次线性扫描，
        # O(n²)）。被剔除的 GPU 帧仍走渲染线程延迟释放契约。
        kept: list[QueueEntry] = []
        stale_entries: list[QueueEntry] = []
        for entry in queue:
            if entry.generation_id < current_gen:
                stale_entries.append(entry)
            else:
                kept.append(entry)
        queue[:] = kept

        for entry in stale_entries:
            _defer_release_gpu_frame(entry.frame)
            if is_gpu_decoded_frame(entry.frame):
                logger.debug(
                    f"Deferred release of stale GPU frame {entry.frame_index} "
                    f"(gen={entry.generation_id} < current={current_gen})"
                )

        return len(stale_entries)

    def enqueue(self, entry: QueueEntry) -> bool:
        """
        将帧入队。

        根据 entry.can_drop 决定放入 proxy 或 master 队列。
        过期 generation 的帧直接丢弃。

        Args:
            entry: 队列元素

        Returns:
            成功入队返回 True，过期丢弃返回 False
        """
        with self._lock:
            # 检查 generation 是否过期
            if entry.generation_id < self._current_generation:
                logger.debug(
                    f"Rejected stale frame {entry.frame_index}, "
                    f"gen={entry.generation_id} < current={self._current_generation}"
                )
                # AUD-39201: 经渲染线程契约释放
                _defer_release_gpu_frame(entry.frame)
                return False

            # 根据可丢弃性选择队列
            if entry.can_drop:
                return self._enqueue_proxy(entry)
            else:
                return self._enqueue_master(entry)

    def _enqueue_proxy(self, entry: QueueEntry) -> bool:
        """
        入队 proxy 帧（内部方法，不加锁）。

        proxy queue 满时丢旧帧，保留最新帧。

        Args:
            entry: 队列元素

        Returns:
            成功入队返回 True
        """
        # 检查是否已有同帧号的帧，避免重复
        existing_idx = None
        for i, e in enumerate(self._proxy_queue):
            if e.frame_index == entry.frame_index:
                existing_idx = i
                break

        if existing_idx is not None:
            # 替换旧的同帧号帧
            old_entry = self._proxy_queue[existing_idx]
            # 释放旧 GPU frame（如有；AUD-39201: 经渲染线程契约）
            _defer_release_gpu_frame(old_entry.frame)
            if is_gpu_decoded_frame(old_entry.frame):
                logger.debug(
                    f"Deferred release of replaced proxy GPU frame {old_entry.frame_index}"
                )
            self._proxy_queue[existing_idx] = entry
            logger.debug(
                f"Replaced proxy frame {entry.frame_index} "
                f"(old decode_ms={old_entry.decode_ms:.1f} -> new={entry.decode_ms:.1f})"
            )
        else:
            # 新帧入队
            self._proxy_queue.append(entry)

            # 检查队列长度，丢旧帧保留最新
            while len(self._proxy_queue) > self._max_proxy_size:
                # 移除最旧的帧（列表头）
                dropped = self._proxy_queue.pop(0)
                # 释放 GPU frame（如有；AUD-39201: 经渲染线程契约）
                _defer_release_gpu_frame(dropped.frame)
                if is_gpu_decoded_frame(dropped.frame):
                    logger.debug(
                        f"Deferred release of dropped proxy GPU frame {dropped.frame_index}"
                    )
                self._stats["proxy_dropped"] += 1
                logger.debug(
                    f"Dropped old proxy frame {dropped.frame_index} "
                    f"(queue size {len(self._proxy_queue)}/{self._max_proxy_size})"
                )

        self._stats["proxy_enqueued"] += 1
        return True

    def _enqueue_master(self, entry: QueueEntry) -> bool:
        """
        入队 master 帧（内部方法，不加锁）。

        master queue 不丢目标帧，只丢弃过期 generation。

        Args:
            entry: 队列元素

        Returns:
            成功入队返回 True
        """
        # 检查是否已有同帧号的帧
        existing_idx = None
        for i, e in enumerate(self._master_queue):
            if e.frame_index == entry.frame_index:
                existing_idx = i
                break

        if existing_idx is not None:
            # 替换旧的同帧号帧
            old_entry = self._master_queue[existing_idx]
            # 释放旧 GPU frame（如有；AUD-39201: 经渲染线程契约）
            _defer_release_gpu_frame(old_entry.frame)
            if is_gpu_decoded_frame(old_entry.frame):
                logger.debug(
                    f"Deferred release of replaced master GPU frame {old_entry.frame_index}"
                )
            self._master_queue[existing_idx] = entry
            logger.debug(
                f"Replaced master frame {entry.frame_index} "
                f"(old decode_ms={old_entry.decode_ms:.1f} -> new={entry.decode_ms:.1f})"
            )
        else:
            # 新帧入队
            self._master_queue.append(entry)

            # master queue 不丢"目标帧"（精确帧请求的语义），但容量上限
            # 必须真实生效（AUD-39200）。此前只丢 can_drop 帧，而 enqueue
            # 按 can_drop 路由、master 队列成员恒为 can_drop=False——
            # 上限永远触发不了，队列可无界增长（GPU 帧随之泄漏）。消费方
            # 落后超过 max_master_size 时最旧帧早已无人认领，按 FIFO
            # 淘汰队首（最旧）帧并释放 GPU 资源。
            while len(self._master_queue) > self._max_master_size:
                dropped = self._master_queue.pop(0)
                # 释放 GPU frame（如有；AUD-39201: 经渲染线程契约）
                _defer_release_gpu_frame(dropped.frame)
                if is_gpu_decoded_frame(dropped.frame):
                    logger.debug(
                        f"Deferred release of dropped master GPU frame {dropped.frame_index}"
                    )
                self._stats["master_dropped"] += 1
                logger.debug(
                    f"Dropped overflowed master frame {dropped.frame_index} "
                    f"(queue size {len(self._master_queue)}/{self._max_master_size})"
                )

        self._stats["master_enqueued"] += 1
        return True

    def dequeue(
        self,
        is_master: bool = False,
        frame_index: Optional[int] = None,
    ) -> Optional[QueueEntry]:
        """
        从队列取出帧。

        Args:
            is_master: 是否取 master 帧
            frame_index: 指定帧号（如果提供，精确匹配）

        Returns:
            QueueEntry 或 None（队列空或无匹配帧）
        """
        with self._lock:
            if is_master:
                return self._dequeue_master(frame_index)
            else:
                return self._dequeue_proxy(frame_index)

    def _dequeue_proxy(self, frame_index: Optional[int]) -> Optional[QueueEntry]:
        """
        取 proxy 帧（内部方法，不加锁）。

        Args:
            frame_index: 指定帧号或 None

        Returns:
            QueueEntry 或 None
        """
        if not self._proxy_queue:
            return None

        if frame_index is not None:
            # 精确匹配帧号
            for i, entry in enumerate(self._proxy_queue):
                if entry.frame_index == frame_index:
                    result = self._proxy_queue.pop(i)
                    self._stats["proxy_dequeued"] += 1
                    return result
            return None
        else:
            # 取最旧帧（FIFO）
            result = self._proxy_queue.pop(0)
            self._stats["proxy_dequeued"] += 1
            return result

    def _dequeue_master(self, frame_index: Optional[int]) -> Optional[QueueEntry]:
        """
        取 master 帧（内部方法，不加锁）。

        Args:
            frame_index: 指定帧号或 None

        Returns:
            QueueEntry 或 None
        """
        if not self._master_queue:
            return None

        if frame_index is not None:
            # 精确匹配帧号
            for i, entry in enumerate(self._master_queue):
                if entry.frame_index == frame_index:
                    result = self._master_queue.pop(i)
                    self._stats["master_dequeued"] += 1
                    return result
            return None
        else:
            # 取最旧帧（FIFO）
            result = self._master_queue.pop(0)
            self._stats["master_dequeued"] += 1
            return result

    def peek(
        self,
        is_master: bool = False,
        frame_index: Optional[int] = None,
    ) -> Optional[QueueEntry]:
        """
        查看队首帧（不出队）。

        Args:
            is_master: 是否查看 master 队列
            frame_index: 指定帧号（如果提供，精确匹配）

        Returns:
            QueueEntry 的**快照副本**或 None（AUD-39202：调用方不获得
            帧所有权——底层 frame 仍归队列所有，队列淘汰该帧时可能释放
            其 GPU 资源；需要所有权请用 dequeue/dequeue_latest_before_or_at）
        """
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue

            if not queue:
                return None

            if frame_index is not None:
                for entry in queue:
                    if entry.frame_index == frame_index:
                        return entry.snapshot()
                return None
            else:
                return queue[0].snapshot()

    def get_latest(self, is_master: bool = False) -> Optional[QueueEntry]:
        """
        获取最新帧（队尾帧，不出队）。

        用于播放时快速获取最新可用帧。

        Args:
            is_master: 是否查看 master 队列

        Returns:
            QueueEntry 的**快照副本**或 None（AUD-39202：调用方不获得
            帧所有权，语义同 peek）
        """
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue

            if not queue:
                return None

            return queue[-1].snapshot()

    def dequeue_latest_before_or_at(
        self,
        frame_index: int,
        is_master: bool = False,
    ) -> LatestFrameResult:
        """
        获取不超过目标帧号的最新帧，并出队。

        P2-2 实现：实时预览的帧调度核心逻辑。

        规则：
        - 精确目标帧 ready：返回该帧
        - 目标帧没 ready：返回 <= target 的最新帧
        - 更旧帧全部丢弃（出队时）
        - 未来帧（> target）不返回，保留在队列中
        - 队列空时返回 LatestFrameResult(found=False)

        Args:
            frame_index: 目标帧号上限
            is_master: 是否操作 master 队列

        Returns:
            LatestFrameResult:
                - found=True: 成功找到帧
                - entry: QueueEntry（已出队）
                - actual_frame_index: 实际帧号（<= frame_index）
                - older_frames_discarded: 丢弃的更旧帧数量
        """
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue

            if not queue:
                return LatestFrameResult(found=False)

            # 找到 <= frame_index 的最新帧
            best_idx: int = -1
            best_frame: int = -1
            for i, entry in enumerate(queue):
                if entry.frame_index <= frame_index and entry.frame_index >= best_frame:
                    best_idx = i
                    best_frame = entry.frame_index

            if best_idx == -1:
                # 所有帧都是未来帧（> frame_index），无可出队帧
                return LatestFrameResult(found=False)

            # 取出目标帧
            result_entry = queue.pop(best_idx)

            # 丢弃所有比目标帧更旧的帧（frame_index < best_frame）
            # AUD-39204: 单次遍历重建列表替代逐个 list.remove（O(n²)→O(n)），
            # 释放仍经渲染线程延迟释放契约（AUD-39201）。
            older_entries = [e for e in queue if e.frame_index < best_frame]
            older_count = len(older_entries)
            if older_count > 0:
                queue[:] = [e for e in queue if e.frame_index >= best_frame]
            for e in older_entries:
                _defer_release_gpu_frame(e.frame)

            # 更新统计
            if is_master:
                self._stats["master_dequeued"] += 1
                self._stats["master_dropped"] += older_count
            else:
                self._stats["proxy_dequeued"] += 1
                self._stats["proxy_dropped"] += older_count

            if older_count > 0:
                logger.debug(
                    f"dequeue_latest_before_or_at({frame_index}, "
                    f"master={is_master}): selected frame {best_frame}, "
                    f"discarded {older_count} older frames"
                )

            return LatestFrameResult(
                entry=result_entry,
                actual_frame_index=best_frame,
                found=True,
                older_frames_discarded=older_count,
            )

    def get_latest_before_or_at(
        self,
        frame_index: int,
        is_master: bool = False,
    ) -> LatestFrameResult:
        """
        获取不超过目标帧号的最新帧（不出队，仅查看）。

        用于 peek 操作，不改变队列状态。

        规则：
        - 找到 <= frame_index 的最新帧
        - 不出队、不丢弃更旧帧
        - 队列空时返回 LatestFrameResult(found=False)

        Args:
            frame_index: 目标帧号上限
            is_master: 是否查看 master 队列

        Returns:
            LatestFrameResult:
                - found=True: 找到可用帧
                - entry: QueueEntry 的**快照副本**（仍在队列中；AUD-39202：
                  调用方不获得帧所有权，队列淘汰该帧时可能释放其 GPU 资源）
                - actual_frame_index: 实际帧号
                - older_frames_discarded: 0（不丢弃）
        """
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue

            if not queue:
                return LatestFrameResult(found=False)

            # 找到 <= frame_index 的最新帧
            best_entry: Optional[QueueEntry] = None
            best_frame: int = -1
            for entry in queue:
                if entry.frame_index <= frame_index and entry.frame_index >= best_frame:
                    best_entry = entry
                    best_frame = entry.frame_index

            if best_entry is None:
                # 所有帧都是未来帧
                return LatestFrameResult(found=False)

            return LatestFrameResult(
                entry=best_entry.snapshot(),
                actual_frame_index=best_frame,
                found=True,
                older_frames_discarded=0,
            )

    def get_size(self, is_master: bool = False) -> int:
        """
        获取队列长度。

        Args:
            is_master: 是否查看 master 队列

        Returns:
            队列长度
        """
        with self._lock:
            return len(self._master_queue if is_master else self._proxy_queue)

    def contains(
        self,
        frame_index: int,
        is_master: bool = False,
    ) -> bool:
        """
        检查队列是否包含指定帧。

        Args:
            frame_index: 帧号
            is_master: 是否检查 master 队列

        Returns:
            包含返回 True
        """
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue
            return any(e.frame_index == frame_index for e in queue)

    def clear(self, is_master: bool = False) -> int:
        """
        清空队列。

        Args:
            is_master: 是否清空 master 队列（False = 清空 proxy）

        Returns:
            清空的帧数
        """
        with self._lock:
            if is_master:
                count = len(self._master_queue)
                for entry in self._master_queue:
                    # AUD-39201: 经渲染线程契约释放
                    _defer_release_gpu_frame(entry.frame)
                self._master_queue.clear()
                return count
            else:
                count = len(self._proxy_queue)
                for entry in self._proxy_queue:
                    # AUD-39201: 经渲染线程契约释放
                    _defer_release_gpu_frame(entry.frame)
                self._proxy_queue.clear()
                return count

    def clear_all(self) -> tuple[int, int]:
        """
        清空所有队列。

        Returns:
            (proxy 清空帧数, master 清空帧数)
        """
        with self._lock:
            proxy_count = len(self._proxy_queue)
            master_count = len(self._master_queue)
            for entry in self._proxy_queue:
                # AUD-39201: 经渲染线程契约释放
                _defer_release_gpu_frame(entry.frame)
            for entry in self._master_queue:
                _defer_release_gpu_frame(entry.frame)
            self._proxy_queue.clear()
            self._master_queue.clear()
            return (proxy_count, master_count)

    def get_frame_indices(
        self,
        is_master: bool = False,
    ) -> list[int]:
        """
        获取队列中所有帧号列表。

        Args:
            is_master: 是否查看 master 队列

        Returns:
            帧号列表（按入队顺序）
        """
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue
            return [e.frame_index for e in queue]

    def __repr__(self) -> str:
        """字符串表示"""
        with self._lock:
            return (
                f"ReadyFrameQueue(gen={self._current_generation}, "
                f"proxy={len(self._proxy_queue)}/{self._max_proxy_size}, "
                f"master={len(self._master_queue)}/{self._max_master_size})"
            )
