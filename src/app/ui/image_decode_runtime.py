"""E1/E2（FUP-06）：跨预览/比较窗口共享的图片解码 runtime。

FUP-06 确认的三类调用链风险在此收口：

* ``PreviewDecoder`` / ``CompareDialog`` 各自持有带父对象的线程池——
  N 个窗口 = 2+2N 条解码线程且无总预算；窗口销毁时 ``tryTake`` 失败
  仍清空 job 引用，运行中的 ``QRunnable``（setAutoDelete(False)）只剩
  C++ 侧指针，Python 包装被 GC 后 run() 退化为抽象基类；
* shutdown 同步等 2 秒后忽略 ``waitForDone`` 结果清空引用——假装线程
  已结束；
* 完成的 job 压缩字节留到 shutdown 才释放。

本模块的口径：

* **单一共享线程池（无父对象）**：并发预算全局统一；窗口销毁不动池，
  也不把池绑到任何窗口生命周期上；
* **总字节/像素预算**覆盖排队+运行的全部在途任务（按提交时的头部
  估宽），超预算的新任务立即以可解释错误回绝——不无界积压内存；
* **取消分层**：排队任务 ``tryTake`` 即丢（字节/像素立即归还）；运行
  中任务只作废回包（不再送达），等它真实跑完由 runtime 回收引用与
  预算——绝不清引用假装线程结束；
* **完成即回收**：job 结束立刻释放压缩数据并移出台账（不留到关闭）；
* **关闭先作废回包**：closed 之后任何回调不再送达；关闭本身不做同步
  等待——引用保留到真实完成，线程自然收敛。

线程口径：``submit/cancel`` 在 GUI 线程调用；完成信号从 worker 线程
经队列连接回到 GUI 线程做台账与回包（与旧 PreviewDecoder 相同的
marshal 方式）。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

from PyQt6.QtCore import (
    QBuffer, QByteArray, QCoreApplication, QIODevice, QObject, QRunnable,
    QSize, Qt, QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import QImage, QImageReader

# 与预览/比较解码同源的边长上限：解码端超过就先等比缩小再解
MAX_DECODE_EDGE = 8192

DEFAULT_MAX_CONCURRENCY = 2
# 预览单文件字节上限 64MB（transport 侧）——256MB ≈ 4 张同时在途
DEFAULT_MAX_PENDING_BYTES = 256 * 1024 * 1024
# 一张 8192×8192 ≈ 67MP；128MP ≈ 两张 8K 图同时在途
DEFAULT_MAX_PENDING_PIXELS = 128 * 1024 * 1024


def _bytes_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size // (1024 * 1024)}MB"
    return f"{max(size // 1024, 1)}KB"


def _pixels_label(pixels: int) -> str:
    if pixels >= 1024 * 1024:
        return f"{pixels // (1024 * 1024)}MP"
    return f"{max(pixels // 1024, 1)}KP"


def estimate_pixels(data: bytes) -> int:
    """提交时解析图片头部估"将解码出的像素"（GUI 线程，只读头部不解码）。

    解码端超过 8192 边长会先等比缩小，所以估值以该上限封顶；
    头部不可读的格式按最坏情况保守估计（宁可回绝也不无界放行）。
    """
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return MAX_DECODE_EDGE * MAX_DECODE_EDGE
    try:
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            width = min(size.width(), MAX_DECODE_EDGE)
            height = min(size.height(), MAX_DECODE_EDGE)
            return max(1, width) * max(1, height)
        return MAX_DECODE_EDGE * MAX_DECODE_EDGE
    finally:
        buffer.close()


def decode_image_scaled(data: bytes, *, max_edge: int = MAX_DECODE_EDGE) -> tuple[QImage, str]:
    """把字节解成 ``QImage``；超过边长上限先等比缩小再解码。

    返回 ``(image, error)`` 二选一为空。原先分别内嵌在
    ``_PreviewDecodeJob`` / ``_CompareDecodeJob`` 里的两份相同逻辑
    合并到此（E4 的"共享 decoder"口径在图片路径上的落点）。
    """
    if not data:
        return QImage(), "文件没有可预览内容"
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return QImage(), "无法读取数据"
    try:
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > max_edge or size.height() > max_edge):
            reader.setScaledSize(size.scaled(
                QSize(max_edge, max_edge),
                Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return QImage(), reader.errorString() or "该格式无内置解码器"
        return image, ""
    finally:
        buffer.close()


class _JobSignals(QObject):
    finished = pyqtSignal(int, str, object, str)  # job_id, path, image, error


class _RuntimeDecodeJob(QRunnable):
    """一张图的解码任务：完成即释放压缩数据；异常不击穿线程池。"""

    def __init__(self, job_id: int, path: str, data: bytes,
                 decode: Callable[[bytes], tuple[QImage, str]]) -> None:
        super().__init__()
        self.job_id = job_id
        self.path = path
        self.data = data
        self.byte_weight = len(data)
        self._decode = decode
        self.invalidated = False  # 在途作废：跑完也不回包
        self.signals = _JobSignals()
        self.setAutoDelete(False)  # 引用由 runtime 持有到真实完成

    def run(self) -> None:
        try:
            image, error = self._decode(self.data)
        except Exception as exc:  # 解码器异常必须可解释送达，不能击穿线程
            image, error = QImage(), f"{exc.__class__.__name__}: {exc}"
        self.data = b""  # 完成即释放压缩数据
        self.signals.finished.emit(self.job_id, self.path, image, error)


class DecodeLease:
    """一个消费窗口的解码租约：命名归属 + 批量取消/关闭。

    runtime 按 lease 记账（``in_flight(lease)`` 可查），关窗只影响
    本窗口的任务，不动共享池里的其他窗口。
    """

    def __init__(self, runtime: "ImageDecodeRuntime", name: str) -> None:
        self._runtime = runtime
        self.name = name
        self._jobs: set[int] = set()
        self._closed = False

    # ------------------------------------------------------------ 提交/取消
    def submit(self, path: str, data: bytes,
               decode: Callable[[bytes], tuple[QImage, str]],
               on_done: Callable[[str, QImage, str], None]) -> Optional[int]:
        """提交一个解码任务；返回 job_id（回绝/已关闭时为 None）。

        回绝（超预算/已关闭）立即以错误回调 ``on_done``——GUI 可解释
        可重试，不静默丢弃。
        """
        return self._runtime._submit(self, path, data, decode, on_done)

    def cancel_all(self) -> int:
        """取消本租约全部在途任务；返回仍在运行的个数（已作废回包）。"""
        return self._runtime._cancel_jobs(tuple(self._jobs))

    def close(self) -> None:
        """关闭租约：先作废全部回包，之后的提交直接以错误回绝。"""
        if self._closed:
            return
        self._closed = True
        self._runtime._cancel_jobs(tuple(self._jobs))

    # ------------------------------------------------------------ 查询
    @property
    def closed(self) -> bool:
        return self._closed

    def in_flight(self) -> int:
        return len(self._jobs)


class ImageDecodeRuntime(QObject):
    """进程级共享的图片解码 runtime（唯一线程池 + 总预算 + 分层取消）。"""

    def __init__(self, parent: QObject | None = None, *,
                 max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
                 max_pending_bytes: int = DEFAULT_MAX_PENDING_BYTES,
                 max_pending_pixels: int = DEFAULT_MAX_PENDING_PIXELS) -> None:
        super().__init__(parent)
        # 无父对象：窗口销毁不牵连线程池；生命周期由 runtime 自己管理
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(1, int(max_concurrency)))
        self.max_pending_bytes = int(max_pending_bytes)
        self.max_pending_pixels = int(max_pending_pixels)
        self._next_id = 1
        self._jobs: dict[int, _RuntimeDecodeJob] = {}
        self._callbacks: dict[int, Callable[[str, QImage, str], None]] = {}
        self._leases: dict[int, DecodeLease] = {}
        self._pixel_weights: dict[int, int] = {}
        self._pending_bytes = 0
        self._pending_pixels = 0
        self._closed = False
        self._completed_total = 0
        self._rejected_total = 0

    # ------------------------------------------------------------ 租约
    def acquire(self, name: str = "") -> DecodeLease:
        """开一个命名租约；窗口关掉就 close，不影响其他租约。"""
        return DecodeLease(self, name or f"lease-{self._next_id}")

    # ------------------------------------------------------------ 提交
    def _submit(self, lease: DecodeLease, path: str, data: bytes,
                decode: Callable[[bytes], tuple[QImage, str]],
                on_done: Callable[[str, QImage, str], None]) -> Optional[int]:
        if self._closed:
            self._rejected_total += 1
            on_done(path, QImage(), "解码运行时已关闭")
            return None
        if lease.closed:
            self._rejected_total += 1
            on_done(path, QImage(), "解码会话已关闭")
            return None
        pixels = estimate_pixels(data)
        if len(data) > self.max_pending_bytes:
            self._rejected_total += 1
            on_done(path, QImage(),
                    f"文件超过单任务解码字节预算（{_bytes_label(self.max_pending_bytes)}）")
            return None
        if pixels > self.max_pending_pixels:
            self._rejected_total += 1
            on_done(path, QImage(),
                    f"图片超过单任务解码像素预算（{_pixels_label(self.max_pending_pixels)}）")
            return None
        if lease.in_flight() >= self._pool.maxThreadCount() * 8:
            # 单窗口在途上限：一个窗口也不许独占整个共享预算
            self._rejected_total += 1
            on_done(path, QImage(), "本窗口解码任务过多，请等待当前任务完成")
            return None
        if (self._pending_bytes + len(data) > self.max_pending_bytes
                or self._pending_pixels + pixels > self.max_pending_pixels):
            # 总预算回绝：立即、可解释、可重试——不无界积压内存
            self._rejected_total += 1
            on_done(path, QImage(),
                    f"解码队列已满（在途 {_bytes_label(self._pending_bytes)}"
                    f"/{_pixels_label(self._pending_pixels)}），请稍后再试")
            return None

        job_id = self._next_id
        self._next_id += 1
        job = _RuntimeDecodeJob(job_id, path, data, decode)
        job.signals.finished.connect(self._on_job_finished)
        self._jobs[job_id] = job
        self._callbacks[job_id] = on_done
        lease._jobs.add(job_id)
        self._leases[job_id] = lease
        self._pixel_weights[job_id] = pixels
        self._pending_bytes += len(data)
        self._pending_pixels += pixels
        # 并发由池的 maxThreadCount 统一管；超出部分在池队列排队
        self._pool.start(job)
        return job_id

    # ------------------------------------------------------------ 取消（分层）
    def _cancel_jobs(self, job_ids: tuple[int, ...]) -> int:
        """批量取消：排队即丢（预算立即归还）；在途只作废回包。

        返回仍在运行的个数——调用方不得把这当成"已结束"。
        """
        running = 0
        for job_id in job_ids:
            state = self._cancel_job(job_id)
            if state == "running":
                running += 1
        return running

    def _cancel_job(self, job_id: int) -> str:
        job = self._jobs.get(job_id)
        if job is None:
            return "missing"
        if self._pool.tryTake(job):
            # 还在队列里：直接回收，字节/像素立即归还
            self._release(job_id)
            return "dropped"
        # 已在运行：作废回包；引用保留到真实完成（见 _on_job_finished）
        job.invalidated = True
        return "running"

    # ------------------------------------------------------------ 完成/回收
    def _on_job_finished(self, job_id: int, path: str,
                         image: QImage, error: str) -> None:
        job = self._jobs.get(job_id)
        invalidated = self._closed or (job is not None and job.invalidated)
        callback = self._callbacks.get(job_id)  # 取在回收之前：_release 会清
        # 完成即回收：压缩数据 run() 内已清；这里归还预算并移出台账
        self._release(job_id)
        self._completed_total += 1
        if invalidated:
            return  # 先作废回包：取消/关闭后的迟到结果不送达
        if callback is not None:
            callback(path, image, error)

    def _release(self, job_id: int) -> None:
        job = self._jobs.pop(job_id, None)
        self._callbacks.pop(job_id, None)
        lease = self._leases.pop(job_id, None)
        if lease is not None:
            lease._jobs.discard(job_id)
        pixels = self._pixel_weights.pop(job_id, 0)
        self._pending_pixels = max(0, self._pending_pixels - pixels)
        if job is not None:
            self._pending_bytes = max(0, self._pending_bytes - job.byte_weight)

    # ------------------------------------------------------------ 关闭/查询
    def shutdown(self) -> None:
        """关闭 runtime：先作废全部回包，再清队列；运行中的任务等真实
        完成。**不做同步等待**——不出现"等两秒后清引用假装结束"。
        """
        self._closed = True
        self._cancel_jobs(tuple(self._jobs.keys()))

    def is_idle(self) -> bool:
        return not self._jobs

    def in_flight(self) -> int:
        return len(self._jobs)

    def stats(self) -> dict[str, Any]:
        return {
            "in_flight": len(self._jobs),
            "pending_bytes": self._pending_bytes,
            "pending_pixels": self._pending_pixels,
            "max_thread_count": self._pool.maxThreadCount(),
            "completed_total": self._completed_total,
            "rejected_total": self._rejected_total,
        }

    def wait_for_idle(self, timeout_ms: int) -> bool:
        """有界等待全部在途真实结束（测试/关停路径用；GUI 运行期勿用）。

        台账在 GUI 线程经队列连接更新，所以这里必须泵事件而不是干等
        线程池；返回是否真的空闲——调用方必须如实处理 False。
        """
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        while self._jobs:
            QCoreApplication.processEvents()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return True


# ---------------------------------------------------------------------------
# 进程级共享实例
# ---------------------------------------------------------------------------

_SHARED: ImageDecodeRuntime | None = None


def shared_runtime() -> ImageDecodeRuntime:
    """预览/比较窗口共用的进程级 runtime（跨窗口统一并发与总预算）。"""
    global _SHARED
    if _SHARED is None:
        _SHARED = ImageDecodeRuntime()
    return _SHARED


def shutdown_shared_runtime() -> bool:
    """应用关停：先作废回包再退出（不等待，不假装完成）。返回是否已空闲。"""
    global _SHARED
    if _SHARED is None:
        return True
    runtime, _SHARED = _SHARED, None
    runtime.shutdown()
    idle = runtime.is_idle()
    runtime.deleteLater()
    return idle
