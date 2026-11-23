"""显式读回的专用任务队列（Metal 迁移计划 7.2 / 7.3）。

「显式读回」指用户动作或输出请求触发的一次性 GPU→CPU 搬运：截图、AI
抓帧、缩略图、监看输出。它与**播放读回**（播放主时钟驱动的 seek /
后台快照）必须分开记账，并且不得在 UI 线程或渲染线程上阻塞。

本模块提供：

- `ReadbackPurpose`：目的词表，取值沿用 ``frame_trace_schema.READBACK_KINDS``
  （``explicit_snapshot`` / ``monitor``）。`readback_breakdown` 把全局
  ``CPU_DOWNLOAD`` 审计拆成「显式 / 监看 / 播放」三份。
- `ReadbackImage`：读回结果的记录。``origin`` 把三类来源明确分开——
  ``gpu``（本进程刚从 GPU 读回）、``cpu_plane``（借用的 CPU 平面，零
  拷贝）、``disk_cache``（磁盘缓存记录，不是 GPU 结果）；并带格式、
  后端、行序和色彩四元组。
- `MetalTextureReader`：在**私有命令队列**上读回一张 Metal 纹理，不占
  渲染队列。
- `ExplicitReadbackQueue`：有界的专用工作线程队列。

线程契约：

- 任何线程都可以 `submit`（只入队）。读回与回调都在专用工作线程上执行；
  回调只应把数据转交出去（UI 侧经 Qt 信号回到主线程），不得触碰 UI、
  工程或渲染上下文。
- 被读的纹理必须已过 **GPU 完成事件**，并由请求方持有租约（``release``
  回调）。跨命令队列没有隐式排序：生产者未完成的纹理不能交给本队列。
- ``release`` 由队列保证恰好调用一次——完成、失败、取消、**被拒绝**各
  路径都会调用（拒绝时也由队列归还，调用方不要自己再还）。
- 队列有界：``max_pending`` 个在途（排队 + 执行中）请求。满时的行为由
  ``FullPolicy`` 决定，不会悄悄排队增长。
- 关停先 `close` 再退出进程：带着在途读回退出会撞上 Metal 完成回调与
  解释器收尾的竞争（见 ``cfa_metal_debayer.CfaMetalDebayer.drain`` 的
  实测记录）。`close` 会等执行中的读回结束并让每个用过的读取器收尾。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, Iterable, Optional, Tuple

import numpy as np

from src.shared.gpu.present_frame import (
    NATIVE_ORIGIN_BOTTOM_LEFT,
    NATIVE_ORIGIN_TOP_LEFT,
    PresentColorInfo,
)
from src.shared.video.pixel_copy_counters import (
    CopyKind,
    add_pixel_copy,
    snapshot_pixel_copies,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 目的与计数
# =============================================================================


class ReadbackPurpose:
    """读回目的词表（``frame_trace_schema.READBACK_KINDS`` 的子集）。"""

    EXPLICIT_SNAPSHOT: str = "explicit_snapshot"   # 截图 / 抓帧 / 缩略图 / 导出快照
    MONITOR: str = "monitor"                       # 监看输出


#: 经专用队列搬运、按目的分类记账的读回。
EXPLICIT_READBACK_PURPOSES = (ReadbackPurpose.EXPLICIT_SNAPSHOT, ReadbackPurpose.MONITOR)

_lock = threading.Lock()
_events: Dict[str, int] = {purpose: 0 for purpose in EXPLICIT_READBACK_PURPOSES}
_bytes: Dict[str, int] = {purpose: 0 for purpose in EXPLICIT_READBACK_PURPOSES}
_backends: Dict[str, Dict[str, int]] = {
    purpose: {} for purpose in EXPLICIT_READBACK_PURPOSES
}


def note_explicit_readback(*, purpose: str, backend: str, nbytes: int) -> None:
    """记一次显式读回的目的与后端（线程安全）。

    只记目的分类，**不**重复计入全局 ``CPU_DOWNLOAD``：全局审计由调用点
    经 ``add_pixel_copy`` 记一次，这里再记一次会让 `readback_breakdown`
    把它数两遍。``nbytes`` 为 0 不记事件。
    """
    if purpose not in EXPLICIT_READBACK_PURPOSES:
        raise ValueError(
            f"未知的读回目的 {purpose!r}（支持: {', '.join(EXPLICIT_READBACK_PURPOSES)}）"
        )
    nbytes = int(nbytes)
    if nbytes < 0:
        raise ValueError(f"读回字节数不可为负（purpose={purpose}, nbytes={nbytes}）")
    if nbytes == 0:
        return
    backend_name = str(backend or "unknown") or "unknown"
    with _lock:
        _events[purpose] += 1
        _bytes[purpose] += nbytes
        _backends[purpose][backend_name] = _backends[purpose].get(backend_name, 0) + 1


def reset_explicit_readbacks() -> None:
    """清零目的分类计数（不含全局 ``CPU_DOWNLOAD``，它由 ``reset_pixel_copies`` 清）。"""
    with _lock:
        for purpose in EXPLICIT_READBACK_PURPOSES:
            _events[purpose] = 0
            _bytes[purpose] = 0
            _backends[purpose] = {}


def readback_breakdown() -> Dict[str, Dict[str, int]]:
    """把 GPU→CPU 读回拆成「显式 / 监看 / 播放」三份。

    ``playback`` = 全局 ``CPU_DOWNLOAD`` 减去经本模块按目的记账的部分，
    即既有播放路径（seek / 后台快照 / CPU 回退等）直接记入的读回。只清了
    全局计数、没清目的计数时差值会是负数，按 0 计。
    """
    with _lock:
        explicit_events = _events[ReadbackPurpose.EXPLICIT_SNAPSHOT]
        explicit_bytes = _bytes[ReadbackPurpose.EXPLICIT_SNAPSHOT]
        monitor_events = _events[ReadbackPurpose.MONITOR]
        monitor_bytes = _bytes[ReadbackPurpose.MONITOR]
        backends = {key: dict(value) for key, value in _backends.items()}
    download = snapshot_pixel_copies()[CopyKind.CPU_DOWNLOAD]
    playback_events = download["events"] - explicit_events - monitor_events
    playback_bytes = download["bytes"] - explicit_bytes - monitor_bytes
    return {
        "explicit": {"events": explicit_events, "bytes": explicit_bytes,
                     "backends": backends[ReadbackPurpose.EXPLICIT_SNAPSHOT]},
        "monitor": {"events": monitor_events, "bytes": monitor_bytes,
                    "backends": backends[ReadbackPurpose.MONITOR]},
        "playback": {"events": max(0, playback_events), "bytes": max(0, playback_bytes)},
    }


# =============================================================================
# 结果记录
# =============================================================================


class ReadbackOrigin:
    """读回记录的来源——GPU 结果与磁盘缓存的分界。"""

    GPU: str = "gpu"                  # 本进程刚从 GPU 读回
    CPU_PLANE: str = "cpu_plane"      # 借用的 CPU 平面（零拷贝）
    DISK_CACHE: str = "disk_cache"    # 磁盘缓存记录（不是 GPU 结果）


@dataclass(frozen=True)
class ReadbackImage:
    """一次读回的结果记录（GPU 结果 / 借用平面 / 磁盘缓存记录）。

    Attributes:
        pixels: ``(H, W, C)`` 数组，行序见 ``row_order``。
        format: 像素格式词（``rgba8`` / ``bgra8`` / ``rgba16f`` / ``rgba32f``）。
        origin: `ReadbackOrigin` 之一。``disk_cache`` 表示这是缓存记录，
            ``pixels`` 为空、不得当作画面使用；``produced_by`` 记录当初
            产生该缓存的后端。
        backend: 产生这份像素的后端（``metal`` / ``opengl`` / ``cpu``）；
            缓存记录为 ``cache``。
        color: 像素的色彩四元组。
        row_order: 行 0 在图像哪一端（``top_left`` / ``bottom_left``）。
        frame_index / generation_id: 这份像素属于哪一帧、哪一代。
        source: 来源标识（如 ``native_viewport``、``program_viewer``）。
        cache_key: 磁盘缓存键（缓存命中或可入缓存时给出）。
    """

    pixels: Optional[np.ndarray]
    width: int
    height: int
    format: str = "rgba8"
    origin: str = ReadbackOrigin.GPU
    backend: str = "metal"
    color: PresentColorInfo = field(default_factory=PresentColorInfo)
    row_order: str = NATIVE_ORIGIN_TOP_LEFT
    frame_index: int = -1
    generation_id: int = 0
    source: str = ""
    cache_key: Optional[str] = None
    produced_by: Optional[str] = None

    def __post_init__(self) -> None:
        if self.origin not in (ReadbackOrigin.GPU, ReadbackOrigin.CPU_PLANE,
                               ReadbackOrigin.DISK_CACHE):
            raise ValueError(f"未知的读回来源 {self.origin!r}")
        if self.row_order not in (NATIVE_ORIGIN_TOP_LEFT, NATIVE_ORIGIN_BOTTOM_LEFT):
            raise ValueError(f"未知的行序 {self.row_order!r}")
        if int(self.width) < 0 or int(self.height) < 0:
            raise ValueError("ReadbackImage 尺寸不可为负")
        if self.origin == ReadbackOrigin.DISK_CACHE:
            if self.pixels is not None:
                raise ValueError("磁盘缓存记录不携带像素")
        elif self.pixels is None:
            raise ValueError(f"来源 {self.origin!r} 的读回记录必须携带像素")
        if not self.cache_key and self.origin == ReadbackOrigin.DISK_CACHE:
            raise ValueError("磁盘缓存记录必须带 cache_key")

    @property
    def nbytes(self) -> int:
        return 0 if self.pixels is None else int(self.pixels.nbytes)

    @property
    def from_disk_cache(self) -> bool:
        """是否为磁盘缓存记录（而非本进程的 GPU 结果）。"""
        return self.origin == ReadbackOrigin.DISK_CACHE

    def to_rgb8(self) -> np.ndarray:
        """转成 ``(H, W, 3)`` uint8（行序不变）；缓存记录没有像素，显式拒绝。"""
        if self.pixels is None:
            raise ValueError("磁盘缓存记录没有像素，不能转成 RGB8")
        pixels = self.pixels
        if pixels.dtype == np.uint8:
            rgb = pixels[..., :3]
        elif np.issubdtype(pixels.dtype, np.floating):
            rgb = np.clip(pixels[..., :3].astype(np.float32) * 255.0, 0.0, 255.0)
            rgb = np.rint(rgb).astype(np.uint8)
        else:
            raise ValueError(f"不支持的读回像素类型 {pixels.dtype}")
        if self.format.startswith("bgra"):
            rgb = rgb[..., ::-1]
        return np.ascontiguousarray(rgb)

    def as_cache_record(self, cache_key: str) -> "ReadbackImage":
        """这份 GPU 结果对应的磁盘缓存记录（同样的尺寸、格式与色彩语义）。"""
        return ReadbackImage(
            pixels=None,
            width=int(self.width),
            height=int(self.height),
            format=str(self.format),
            origin=ReadbackOrigin.DISK_CACHE,
            backend="cache",
            color=self.color,
            row_order=str(self.row_order),
            frame_index=int(self.frame_index),
            generation_id=int(self.generation_id),
            source=str(self.source),
            cache_key=str(cache_key),
            produced_by=str(self.backend),
        )


def borrow_cpu_plane(
    pixels: np.ndarray,
    *,
    format: str,
    color: Optional[PresentColorInfo] = None,
    row_order: str = NATIVE_ORIGIN_TOP_LEFT,
    frame_index: int = -1,
    generation_id: int = 0,
    source: str = "",
) -> ReadbackImage:
    """借用一张已在 CPU 上的平面（零拷贝；不算 GPU 读回，不记读回字节）。"""
    array = np.asarray(pixels)
    if array.ndim < 2:
        raise ValueError("借用的 CPU 平面至少是二维")
    height, width = int(array.shape[0]), int(array.shape[1])
    return ReadbackImage(
        pixels=array,
        width=width,
        height=height,
        format=str(format),
        origin=ReadbackOrigin.CPU_PLANE,
        backend="cpu",
        color=color or PresentColorInfo(),
        row_order=row_order,
        frame_index=int(frame_index),
        generation_id=int(generation_id),
        source=str(source),
    )


# =============================================================================
# Metal 纹理读回
# =============================================================================

#: 格式词 → (numpy dtype, 每像素字节数)。
METAL_READBACK_FORMATS: Dict[str, tuple] = {
    "rgba8": (np.uint8, 4),
    "bgra8": (np.uint8, 4),
    "rgba16f": (np.float16, 8),
    "rgba32f": (np.float32, 16),
}


class MetalTextureReader:
    """在私有命令队列上读回 Metal 纹理（不占渲染队列）。

    纹理必须已过生产者的 GPU 完成事件并由调用方持有租约——跨命令队列没有
    隐式排序。独显上 Managed 纹理先经 blit ``synchronizeResource`` 再
    ``getBytes``；统一内存上该同步是空操作。完成回调只置位事件。
    """

    def __init__(self, device: Any, *, backend_label: str = "metal") -> None:
        import Metal  # pyobjc-framework-Metal

        self._metal = Metal
        self._device = device
        self._backend_label = str(backend_label)
        self._queue: Any = None
        self._lock = threading.Lock()

    @property
    def device(self) -> Any:
        """读取器绑定的 MTLDevice。"""
        return self._device

    def _command_queue(self) -> Any:
        with self._lock:
            if self._queue is None:
                queue = self._device.newCommandQueue()
                if queue is None:
                    raise RuntimeError("MetalTextureReader: 无法创建读回命令队列")
                self._queue = queue
            return self._queue

    def read(
        self,
        texture: Any,
        *,
        width: int,
        height: int,
        pixel_format: str,
        color: Optional[PresentColorInfo] = None,
        frame_index: int = -1,
        generation_id: int = 0,
        source: str = "",
        purpose: str = ReadbackPurpose.EXPLICIT_SNAPSHOT,
        row_order: str = NATIVE_ORIGIN_TOP_LEFT,
    ) -> ReadbackImage:
        """同步读回一张纹理（在专用工作线程上调用；阻塞到该读回完成）。

        记一次全局 ``CPU_DOWNLOAD`` 并按 ``purpose`` 记目的分类。
        """
        if pixel_format not in METAL_READBACK_FORMATS:
            raise ValueError(
                f"不支持的 Metal 读回格式 {pixel_format!r}"
                f"（支持: {', '.join(sorted(METAL_READBACK_FORMATS))}）"
            )
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            raise ValueError(f"读回尺寸必须为正，得到 {width}×{height}")
        dtype, bytes_per_pixel = METAL_READBACK_FORMATS[pixel_format]
        bytes_per_row = width * bytes_per_pixel
        metal = self._metal
        command_buffer = self._command_queue().commandBuffer()
        blit = command_buffer.blitCommandEncoder()
        blit.synchronizeResource_(texture)
        blit.endEncoding()
        done = threading.Event()

        def completed(_command_buffer: Any) -> None:
            done.set()   # 驱动线程：只记录事实

        command_buffer.addCompletedHandler_(completed)
        command_buffer.commit()
        if not done.wait(5.0):
            raise RuntimeError("MetalTextureReader: 读回同步命令 5s 内未完成")
        error = command_buffer.error()
        if error is not None:
            raise RuntimeError(f"MetalTextureReader: 读回同步命令失败: {error}")
        raw = bytearray(bytes_per_row * height)
        region = metal.MTLRegionMake2D(0, 0, width, height)
        texture.getBytes_bytesPerRow_fromRegion_mipmapLevel_(raw, bytes_per_row, region, 0)
        pixels = np.frombuffer(raw, dtype=dtype).reshape(height, width, 4)
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, int(pixels.nbytes))
        note_explicit_readback(
            purpose=purpose, backend=self._backend_label, nbytes=int(pixels.nbytes),
        )
        return ReadbackImage(
            pixels=pixels,
            width=width,
            height=height,
            format=str(pixel_format),
            origin=ReadbackOrigin.GPU,
            backend=self._backend_label,
            color=color or PresentColorInfo(),
            row_order=row_order,
            frame_index=int(frame_index),
            generation_id=int(generation_id),
            source=str(source),
        )

    def drain(self, *, timeout_s: float = 5.0) -> bool:
        """关停收尾：在本读取器的队列上提交一个空命令缓冲并同步等待。

        ``True`` 表示此前排队的读回（含完成线程对回调 block 的释放）都已
        结束，进程可以退出；``False`` 表示超时或失败，不要退出解释器。
        """
        with self._lock:
            queue = self._queue
        if queue is None:
            return True   # 从未读过：不会有完成回调
        command_buffer = queue.commandBuffer()
        command_buffer.commit()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while command_buffer.status() < 4:   # MTLCommandBufferStatusCompleted
            if time.monotonic() >= deadline:
                logger.warning("MetalTextureReader.drain: 哨兵命令 %.1fs 内未完成", timeout_s)
                return False
            time.sleep(0.001)
        return command_buffer.error() is None


# =============================================================================
# 有界任务队列
# =============================================================================


class FullPolicy(Enum):
    """队列满时的行为。"""

    REJECT = "reject"            # 新请求被拒绝（票面 REJECTED），在途的不动
    LATEST_WINS = "latest_wins"  # 同 key 的在途请求被取代，让位给新请求


class TicketState(Enum):
    """一张读回票的状态；``PENDING`` / ``RUNNING`` 之外都是终态。"""

    PENDING = "pending"      # 已入队，未开始
    RUNNING = "running"      # 工作线程正在读
    DONE = "done"            # 读回成功，``image`` 可用
    FAILED = "failed"        # 读回抛错，``error`` 有原因
    CANCELLED = "cancelled"  # 取消或被取代（未开始，或读回完成后结果被丢弃）
    REJECTED = "rejected"    # 入队时被拒绝，``error`` 带原因


TERMINAL_STATES = frozenset({
    TicketState.DONE, TicketState.FAILED, TicketState.CANCELLED, TicketState.REJECTED,
})


@dataclass
class ReadbackRequest:
    """一次显式读回请求。

    Attributes:
        read: 在工作线程上执行、返回 `ReadbackImage` 的无参可调用（如
            ``MetalTextureReader.read`` 的偏函数）。它读的纹理必须已过
            GPU 完成事件，租约由 ``release`` 归还。
        key: 同 ``key`` 的请求在 ``LATEST_WINS`` 下互相取代（如 ``frame_grab``）。
        purpose: `ReadbackPurpose` 之一。
        on_done: 读回成功时在工作线程上调用（只转交数据，不触碰 UI / 工程）。
        on_cancelled: 被取消或取代时调用，带原因；未开始的票在调用
            ``submit`` / ``cancel_all`` / ``close`` 的线程上回调，执行中的票
            在工作线程上回调。
        on_rejected: 入队被拒绝时在提交线程上调用，带原因。
        on_failed: 读回抛错时在工作线程上调用，带异常。
        release: 归还被读纹理的租约。提交时调用方已持有租约（否则纹理在
            读回开始前就可能被回收）；**无论请求被接受还是被拒绝**，队列都
            恰好调用它一次（完成、失败、取消、被拒绝各路径都会调用），
            调用方不得再自行归还。注意这与 ``present_port.MetalPresentPort.
            submit``「未取走则调用方保留租约」的约定不同——这里拒绝时也由
            队列归还，忘记处理拒绝路径的调用方才不会把槽位漏在池外。
        reader: 产生 ``read`` 的读取器（可选）；若有 ``drain``，`close` 时收尾。
    """

    read: Callable[[], ReadbackImage]
    key: str = ""
    purpose: str = ReadbackPurpose.EXPLICIT_SNAPSHOT
    on_done: Optional[Callable[[ReadbackImage], None]] = None
    on_cancelled: Optional[Callable[[str], None]] = None
    on_rejected: Optional[Callable[[str], None]] = None
    on_failed: Optional[Callable[[BaseException], None]] = None
    release: Optional[Callable[[], None]] = None
    reader: Optional[Any] = None

    def __post_init__(self) -> None:
        if self.purpose not in EXPLICIT_READBACK_PURPOSES:
            raise ValueError(
                f"未知的读回目的 {self.purpose!r}（支持: {', '.join(EXPLICIT_READBACK_PURPOSES)}）"
            )
        if not callable(self.read):
            raise ValueError("ReadbackRequest.read 必须可调用")


@dataclass
class ExplicitReadbackTicket:
    """一张读回票：状态与结果。"""

    ticket_id: int
    key: str
    purpose: str
    state: TicketState = TicketState.PENDING
    image: Optional[ReadbackImage] = None
    error: Optional[BaseException] = None
    #: 票尚在排队或执行时被取消 / 取代的原因；工作线程据此丢弃结果。
    cancel_reason: Optional[str] = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _settled: bool = field(default=False, repr=False)

    def wait(self, timeout_s: Optional[float] = None) -> bool:
        """阻塞到这张票进入终态；``False`` 表示超时。"""
        return self._done.wait(timeout_s)


class ExplicitReadbackQueue:
    """有界的显式读回专用队列（至多一条工作线程）。

    状态迁移只发生在两处：入队时（``PENDING`` / ``REJECTED``）和
    ``_finalize``（其余全部终态——计数、回调、归还租约都收拢在那里，每张票
    恰好一次）。工作线程空闲超过 ``idle_timeout_s`` 自行退出，下个请求再起。
    """

    def __init__(
        self,
        *,
        max_pending: int = 4,
        full_policy: FullPolicy = FullPolicy.REJECT,
        name: str = "ExplicitReadback",
        idle_timeout_s: float = 2.0,
    ) -> None:
        if int(max_pending) < 1:
            raise ValueError("max_pending 必须 >= 1")
        if float(idle_timeout_s) <= 0.0:
            raise ValueError("idle_timeout_s 必须为正")
        self.max_pending = int(max_pending)
        self.full_policy = FullPolicy(full_policy)
        self._name = str(name)
        self._idle_timeout_s = float(idle_timeout_s)
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._queue: Deque[Tuple[ExplicitReadbackTicket, ReadbackRequest]] = deque()
        self._active: Optional[ExplicitReadbackTicket] = None
        self._thread: Optional[threading.Thread] = None
        self._next_id = 1
        self._closed = False
        self._readers: Dict[int, Any] = {}
        self._stats: Dict[str, int] = {
            "submitted": 0, "completed": 0, "cancelled": 0,
            "rejected": 0, "failed": 0, "coalesced": 0,
        }

    # -- 查询 --------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """队列是否已关闭（关闭后不再接受请求）。"""
        with self._lock:
            return self._closed

    @property
    def pending(self) -> int:
        """已入队、尚未开始的请求数。"""
        with self._lock:
            return len(self._queue)

    @property
    def running(self) -> int:
        """工作线程正在读的请求数（0 或 1）。"""
        with self._lock:
            return 0 if self._active is None else 1

    @property
    def in_flight(self) -> int:
        """在途请求数（排队 + 执行中），恒不超过 ``max_pending``。"""
        with self._lock:
            return len(self._queue) + (0 if self._active is None else 1)

    def stats(self) -> Dict[str, int]:
        """诊断快照。"""
        with self._lock:
            snapshot = dict(self._stats)
        snapshot.update({
            "max_pending": self.max_pending,
            "in_flight": self.in_flight,
            "pending": self.pending,
            "running": self.running,
        })
        return snapshot

    # -- 提交 --------------------------------------------------------------

    def submit(self, request: ReadbackRequest) -> ExplicitReadbackTicket:
        """入队一个读回请求（任意线程）。满时的行为见 ``FullPolicy``。

        ``LATEST_WINS`` 下，同 ``key`` 的排队请求被取消、执行中的请求被
        标记取代（读回完成后结果丢弃），然后按容量判断是否接受新请求。
        """
        with self._wake:
            if self._closed:
                ticket = self._reject_locked(request, "queue_closed")
                superseded: list[Tuple[ExplicitReadbackTicket, ReadbackRequest]] = []
            else:
                superseded = self._supersede_locked(request)
                busy = len(self._queue) + (0 if self._active is None else 1)
                if busy >= self.max_pending:
                    ticket = self._reject_locked(request, "queue_full")
                else:
                    ticket = ExplicitReadbackTicket(
                        ticket_id=self._next_id, key=str(request.key),
                        purpose=request.purpose,
                    )
                    self._next_id += 1
                    self._queue.append((ticket, request))
                    self._stats["submitted"] += 1
                    self._start_thread_locked()
                self._wake.notify_all()
        # 取代 / 拒绝的收尾在锁外进行：回调里可能再次提交请求。
        self._settle_superseded(superseded)
        if ticket.state is TicketState.REJECTED:
            self._finalize(ticket, request)
        return ticket

    def _reject_locked(
        self, request: ReadbackRequest, reason: str
    ) -> ExplicitReadbackTicket:
        ticket = ExplicitReadbackTicket(
            ticket_id=self._next_id, key=str(request.key), purpose=request.purpose,
            state=TicketState.REJECTED, error=RuntimeError(reason),
        )
        self._next_id += 1
        self._stats["rejected"] += 1
        return ticket

    def _supersede_locked(
        self, request: ReadbackRequest
    ) -> list[Tuple[ExplicitReadbackTicket, ReadbackRequest]]:
        """``LATEST_WINS``：撤下同 key 的排队请求，标记执行中的同 key 请求。"""
        if self.full_policy is not FullPolicy.LATEST_WINS or not request.key:
            return []
        withdrawn: list[Tuple[ExplicitReadbackTicket, ReadbackRequest]] = []
        kept: Deque[Tuple[ExplicitReadbackTicket, ReadbackRequest]] = deque()
        for item in self._queue:
            if item[0].key == request.key:
                item[0].cancel_reason = "superseded"
                withdrawn.append(item)
            else:
                kept.append(item)
        self._queue = kept
        active = self._active
        if active is not None and active.key == request.key and active.cancel_reason is None:
            active.cancel_reason = "superseded"
            self._stats["coalesced"] += 1
        self._stats["coalesced"] += len(withdrawn)
        return withdrawn

    def _settle_superseded(
        self, withdrawn: Iterable[Tuple[ExplicitReadbackTicket, ReadbackRequest]]
    ) -> None:
        """被撤下的排队请求：直接进入终态（不经工作线程）。"""
        for ticket, queued in withdrawn:
            self._finalize(ticket, queued, TicketState.CANCELLED)

    def _start_thread_locked(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True,
            )
            self._thread.start()

    # -- 工作线程 ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._closed:
                    if not self._wake.wait(timeout=self._idle_timeout_s):
                        if not self._queue and not self._closed:
                            self._thread = None   # 空闲退出；之后的 submit 会重起线程
                            return
                if not self._queue:
                    self._thread = None           # 已关闭且排空
                    return
                ticket, request = self._queue.popleft()
                ticket.state = TicketState.RUNNING
                self._active = ticket
            self._execute(ticket, request)
            with self._wake:
                self._active = None
                self._wake.notify_all()

    def _execute(self, ticket: ExplicitReadbackTicket, request: ReadbackRequest) -> None:
        reader = request.reader
        if reader is not None:
            with self._lock:
                self._readers[id(reader)] = reader
        try:
            image = request.read()
        except BaseException as exc:   # noqa: BLE001 - 记入票面并回调，原因不丢
            with self._lock:
                cancelled = ticket.cancel_reason
            if cancelled is not None:
                logger.debug(
                    "ExplicitReadbackQueue: 已取消的读回（票 %s）仍然失败: %s",
                    ticket.ticket_id, exc,
                )
                self._finalize(ticket, request, TicketState.CANCELLED)
            else:
                self._finalize(ticket, request, TicketState.FAILED, error=exc)
            return
        with self._lock:
            cancelled = ticket.cancel_reason
        if cancelled is not None:
            self._finalize(ticket, request, TicketState.CANCELLED)
        else:
            self._finalize(ticket, request, TicketState.DONE, image=image)

    # -- 终态（唯一入口） ------------------------------------------------------

    def _finalize(
        self,
        ticket: ExplicitReadbackTicket,
        request: ReadbackRequest,
        state: Optional[TicketState] = None,
        *,
        error: Optional[BaseException] = None,
        image: Optional[ReadbackImage] = None,
    ) -> bool:
        """把一张票推入终态：计数、回调、归还租约，各恰好一次。

        ``state`` 为 ``None`` 表示票已是终态（入队时被拒绝），只补回调与
        归还租约。返回 ``False`` 表示这张票已经收尾过。
        """
        with self._lock:
            if ticket._settled:
                return False
            ticket._settled = True
            if state is not None and ticket.state not in TERMINAL_STATES:
                ticket.state = state
            final = ticket.state
            if final is TicketState.DONE:
                ticket.image = image
            if ticket.error is None and error is not None:
                ticket.error = error
            if final is TicketState.CANCELLED and ticket.error is None:
                ticket.error = RuntimeError(ticket.cancel_reason or "cancelled")
            if final is not TicketState.REJECTED:
                counter = {
                    TicketState.DONE: "completed",
                    TicketState.FAILED: "failed",
                    TicketState.CANCELLED: "cancelled",
                }[final]
                self._stats[counter] += 1
        # 回调与归还租约在锁外进行：回调里可能再次提交请求。
        self._dispatch_callback(request, final, ticket)
        self._release_lease(request)
        # 最后才置位：``wait`` 返回时回调已跑完、租约已归还。
        ticket._done.set()
        return True

    def _dispatch_callback(self, request: ReadbackRequest, state: TicketState,
                           ticket: ExplicitReadbackTicket) -> None:
        if state is TicketState.DONE:
            self._invoke(request.on_done, ticket.image)
        elif state is TicketState.CANCELLED:
            self._invoke(request.on_cancelled, str(ticket.error or "cancelled"))
        elif state is TicketState.REJECTED:
            self._invoke(request.on_rejected, str(ticket.error or "rejected"))
        elif state is TicketState.FAILED:
            self._invoke(request.on_failed, ticket.error)

    @staticmethod
    def _invoke(callback: Optional[Callable[..., None]], *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("ExplicitReadbackQueue: 读回回调抛错")

    def _release_lease(self, request: ReadbackRequest) -> None:
        release, request.release = request.release, None
        if callable(release):
            try:
                release()
            except Exception:
                logger.exception("ExplicitReadbackQueue: 归还租约失败")

    # -- 取消与关停 ------------------------------------------------------------

    def cancel_all(self, reason: str = "cancelled") -> int:
        """取消全部在途请求，返回本次取消的票数。

        排队中的票立即进入终态（在调用线程上回调）；执行中的票被标记取消，
        读回完成后结果丢弃（在工作线程上回调）。
        """
        with self._lock:
            withdrawn = list(self._queue)
            self._queue.clear()
            for ticket, _request in withdrawn:
                ticket.cancel_reason = reason
            active = self._active
            if active is not None and active.cancel_reason is None:
                active.cancel_reason = reason
                running_cancelled = 1
            else:
                running_cancelled = 0
            self._wake.notify_all()
        for ticket, queued in withdrawn:
            self._finalize(ticket, queued, TicketState.CANCELLED)
        return len(withdrawn) + running_cancelled

    def close(self, *, timeout_s: float = 5.0) -> bool:
        """关停：取消在途请求，等执行中的读回结束，让用过的读取器收尾。

        ``True`` 表示在途请求为零、工作线程已退出、每个用过的读取器都已
        收尾——进程可以退出。``False`` 表示超时：不要退出解释器，保留资源。
        """
        self.cancel_all("queue_closed")
        with self._wake:
            self._closed = True
            thread = self._thread
            self._wake.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_s)))
            if thread.is_alive():
                logger.warning(
                    "ExplicitReadbackQueue.close: 工作线程 %.1fs 内未退出", timeout_s,
                )
                return False
        with self._lock:
            readers = list(self._readers.values())
            self._readers.clear()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        for reader in readers:
            drain = getattr(reader, "drain", None)
            if callable(drain) and not drain(timeout_s=max(0.0, deadline - time.monotonic())):
                return False
        return self.in_flight == 0


__all__ = [
    "EXPLICIT_READBACK_PURPOSES",
    "METAL_READBACK_FORMATS",
    "ExplicitReadbackQueue",
    "ExplicitReadbackTicket",
    "FullPolicy",
    "MetalTextureReader",
    "ReadbackImage",
    "ReadbackOrigin",
    "ReadbackPurpose",
    "ReadbackRequest",
    "TicketState",
    "borrow_cpu_plane",
    "note_explicit_readback",
    "readback_breakdown",
    "reset_explicit_readbacks",
]
