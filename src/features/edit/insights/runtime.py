"""T20 · 工具自有运行时：有界 worker / 取消 / capture 守卫 / 缩略图闸门 /
临时产物生命周期。

纪律（计划 T20）：

- 阈值集中在 :class:`InsightsConfig`（开工冻结，代码即配置说明）；
- 后台作业只接 DTO 纯函数（搜索/报表/统计），不复制原始图像/音频/
  GPU 缓存；结果带 capture_id，消费端经 :class:`CaptureGuard` 拒收过时
  结果（后台结果绑定捕获 ID，过时拒收是 T03 的验收项）；
- 缩略图在途上限默认 2（计划 T13 建议值），支持播放暂停（pause 后
  不再派发新请求，已在途的不打断）；
- 临时产物只写在本次任务目录，发布走"临时写全→os.replace 原子换名"，
  失败/取消不覆盖旧产物；cleanup 只删自己目录。
"""

from __future__ import annotations

import os
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class InsightsConfig:
    """T20 阈值（第三机=Mac Intel 64GB 开工冻结值；超限走截断标志）。"""

    max_workers: int = 1                 # 单后台线程：纯数据作业足够
    search_hit_limit: int = 2000         # 搜索结果上限（truncated 标志）
    index_cache_capacity: int = 4        # 快照索引缓存条数（按捕获失效）
    thumbnail_inflight_limit: int = 2    # 在途缩略图上限（T13 建议值）
    thumbnail_queue_limit: int = 64      # 排队上限（溢出拒绝并报告）


class CancelledError(RuntimeError):
    """协作式取消：作业在检查点抛出。"""


class CancelToken:
    """线程安全取消令牌（一次取消，不可复位；新作业用新令牌）。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError("job cancelled")


class CaptureGuard:
    """capture-ID 结果守卫：refresh 发布新捕获后，旧捕获的结果全部过时。

    线程安全；消费端（UI 回调）在应用结果前必须 ``is_current`` 检查。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: str | None = None

    def publish(self, capture_id: str) -> None:
        with self._lock:
            self._current = capture_id

    @property
    def current(self) -> str | None:
        with self._lock:
            return self._current

    def is_current(self, capture_id: str) -> bool:
        with self._lock:
            return self._current == capture_id


@dataclass(frozen=True)
class JobResult:
    job_id: str
    capture_id: str
    ok: bool
    value: Any = None
    error: str | None = None
    cancelled: bool = False


class BoundedWorker:
    """有界后台 worker（默认单线程 + 有界队列）。

    作业签名为 ``fn(token: CancelToken) -> Any``；作业内部应在循环处调
    ``token.raise_if_cancelled()``。shutdown 取消队列中未开始的作业并
    尽快结束线程（不打断不可中断的纯计算，靠检查点退出）。
    """

    def __init__(self, config: InsightsConfig | None = None) -> None:
        self._config = config or InsightsConfig()
        self._queue: queue.Queue = queue.Queue(maxsize=256)
        self._threads: list[threading.Thread] = [
            threading.Thread(target=self._run, name=f"insights-worker-{i}", daemon=True)
            for i in range(max(1, self._config.max_workers))
        ]
        self._pending: dict[str, CancelToken] = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        for thread in self._threads:
            thread.start()

    def submit(
        self,
        fn: Callable[[CancelToken], Any],
        *,
        capture_id: str,
        job_id: str | None = None,
        on_done: Callable[[JobResult], None] | None = None,
    ) -> str:
        """提交作业；回调在工作线程执行（Qt 侧须经信号回到 UI 线程）。"""
        if self._closed:
            raise RuntimeError("worker already shut down")
        job_id = job_id or str(uuid.uuid4())
        token = CancelToken()
        with self._pending_lock:
            self._pending[job_id] = token

        def _wrap() -> None:
            result: JobResult
            try:
                value = fn(token)
                result = JobResult(
                    job_id=job_id, capture_id=capture_id, ok=True, value=value
                )
            except CancelledError:
                result = JobResult(
                    job_id=job_id, capture_id=capture_id, ok=False,
                    error="cancelled", cancelled=True,
                )
            except Exception as exc:  # 作业异常不杀 worker
                result = JobResult(
                    job_id=job_id, capture_id=capture_id, ok=False, error=str(exc)
                )
            finally:
                with self._pending_lock:
                    self._pending.pop(job_id, None)
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:
                    pass

        self._queue.put(_wrap)
        return job_id

    def cancel(self, job_id: str) -> None:
        with self._pending_lock:
            token = self._pending.get(job_id)
        if token is not None:
            token.cancel()

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            item()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """取消未开始作业、等待线程退出（有界等待，不永久阻塞）。"""
        self._closed = True
        with self._pending_lock:
            tokens = list(self._pending.values())
        for token in tokens:
            token.cancel()
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        for thread in self._threads:
            thread.join(timeout=timeout)


class ThumbnailGate:
    """缩略图在途闸门：默认最多 2 个在途；pause 期间不派发新请求。

    请求方（T13）调用 :meth:`acquire` 得到入场资格（排队上限内阻塞等
    待；溢出直接拒绝，调用方报告"忙"）；host 回调完成后 :meth:`release`。
    播放中 UI 调 :meth:`pause`，恢复调 :meth:`resume`——已在途的不打断。
    """

    def __init__(self, inflight_limit: int = 2, queue_limit: int = 64) -> None:
        self._cond = threading.Condition()
        self._limit = max(1, inflight_limit)
        self._queue_limit = max(1, queue_limit)
        self._inflight = 0
        self._waiting = 0
        self._paused = False

    def pause(self) -> None:
        with self._cond:
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    @property
    def paused(self) -> bool:
        with self._cond:
            return self._paused

    @property
    def inflight(self) -> int:
        with self._cond:
            return self._inflight

    def acquire(self, timeout: float | None = None) -> bool:
        import time as _time

        deadline = None if timeout is None else _time.monotonic() + timeout
        with self._cond:
            if self._waiting >= self._queue_limit:
                return False  # 排队溢出：立即拒绝，调用方报告忙
            self._waiting += 1
            try:
                while self._paused or self._inflight >= self._limit:
                    remaining = (
                        None if deadline is None else deadline - _time.monotonic()
                    )
                    if remaining is not None and remaining <= 0:
                        return False
                    self._cond.wait(min(remaining, 0.5) if remaining is not None else 0.5)
                self._inflight += 1
                return True
            finally:
                self._waiting -= 1

    def release(self) -> None:
        with self._cond:
            self._inflight = max(0, self._inflight - 1)
            self._cond.notify()


class TempArtifactManager:
    """输出临时产物生命周期：写临时 → 原子发布；cleanup 只删自己目录。"""

    def __init__(self, base_dir: str | Path, task_id: str | None = None) -> None:
        self._task_id = task_id or str(uuid.uuid4())
        self._dir = Path(base_dir) / f"insights-{self._task_id}"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._finalized = False

    @property
    def task_dir(self) -> Path:
        return self._dir

    def temp_path(self, name: str) -> Path:
        if self._finalized:
            raise RuntimeError("temp artifacts already finalized")
        return self._dir / f".tmp-{name}"

    def publish(self, name: str, final_path: str | Path) -> None:
        """临时文件写全后原子换名到目标（失败不覆盖已存在的旧产物）。"""
        src = self.temp_path(name)
        if not src.exists():
            raise FileNotFoundError(f"temp artifact not written: {src}")
        dst = Path(final_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)

    def cleanup(self) -> None:
        """删除本次任务目录（只清自己；已发布产物不受影响）。"""
        import shutil

        self._finalized = True
        shutil.rmtree(self._dir, ignore_errors=True)
