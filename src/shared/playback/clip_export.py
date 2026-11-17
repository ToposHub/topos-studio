"""E6：媒体片段截取/导出——预算、取消、有界输出与安全交付。

现状（FUP 调研确认）：``ToposPlayer._export_clip`` 与
``UltimatePlayer.execute_clip`` 用守护线程里的 ``subprocess.run(...,
capture_output=True)``：无超时预算（坏源可无限挂起）、不可取消、
``capture_output`` 无上限（异常源可吃掉任意内存）、UltimatePlayer 直接
写目标路径（半成品文件可见）。

本模块把截取统一为一个受约束的作业：

* **预算**：deadline（默认 180s）到点按取消路径收尾，报"超预算"；
* **取消**：协作式 ``cancel()``——terminate → 有界 wait → kill；
* **有界输出**：stderr 只保留最后 ``max_capture`` 字节（环形丢弃），
  stdout 丢弃；
* **安全交付**：命令写入 ``.part`` 临时文件（同目录），成功后
  ``os.replace`` 原子改名；失败/取消/超时删除临时文件——目标路径永远
  不会出现半成品。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_DEADLINE_SEC = 180.0
DEFAULT_MAX_CAPTURE = 64 * 1024
_TERMINATE_WAIT_SEC = 2.0


class ClipExportJob:
    """一次 ffmpeg 截取作业（命令的输出文件由作业追加为 ``tmp_path``）。"""

    def __init__(self, command: list[str], destination: str, *,
                 deadline_sec: float = DEFAULT_DEADLINE_SEC,
                 max_capture: int = DEFAULT_MAX_CAPTURE,
                 poll_interval: float = 0.2) -> None:
        self._command = [str(arg) for arg in command]
        self.destination = str(destination)
        self.deadline_sec = float(deadline_sec)
        self._max_capture = max(1024, int(max_capture))
        self._poll = max(0.05, float(poll_interval))
        self._cancelled = threading.Event()
        self._running = threading.Lock()   # 单作业串行（重复 start 拒绝）
        self._thread: Optional[threading.Thread] = None
        self.last_error = ""
        self.stderr_tail = ""

    # ------------------------------------------------------------ 生命周期
    @property
    def tmp_path(self) -> str:
        """同目录临时文件：保留目标扩展名（ffmpeg 按扩展名推断封装，
        ``.part`` 后缀会破坏推断），带唯一 token 防并发碰撞。"""
        base, ext = os.path.splitext(self.destination)
        return f"{base}.{id(self):x}.part{ext}"

    def start(self, on_done: Optional[Callable[[bool, str], None]] = None,
              *, thread_name: str = "clip-export") -> bool:
        """后台线程执行；``on_done(ok, message)`` 在工作线程触发。

        返回是否成功启动（同一作业只能跑一次）。
        """
        if not self._running.acquire(blocking=False):
            return False
        self._thread = threading.Thread(
            target=self._run, args=(on_done,), daemon=True,
            name=thread_name)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """协作取消：运行中的进程被终止，临时文件清理，按取消收尾。"""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def is_alive(self) -> bool:
        """作业是否仍在执行（含进程收尾阶段）。"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 作业体
    def _run(self, on_done: Optional[Callable[[bool, str], None]]) -> None:
        ok, message = self._execute()
        try:
            if on_done is not None:
                on_done(ok, message)
        except Exception:
            logger.exception("[CLIP-EXPORT] 完成回调异常")

    def _execute(self) -> tuple[bool, str]:
        tmp = self.tmp_path
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                self._command + [tmp], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False, f"启动失败: {self.last_error}"
        reader = _BoundedReader(proc.stderr, self._max_capture)
        reader.start()
        try:
            while True:
                try:
                    code = proc.wait(timeout=self._poll)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self._cancelled.is_set():
                    self._terminate(proc)
                    self.last_error = "已取消"
                    return False, "导出已取消"
                if time.monotonic() - started > self.deadline_sec:
                    self._terminate(proc)
                    self.last_error = "超预算"
                    return False, (f"导出超过 {int(self.deadline_sec)}s "
                                   f"预算上限，已终止")
            reader.join(timeout=2.0)
            self.stderr_tail = reader.tail()
            if code != 0:
                self.last_error = f"exit={code}"
                return False, "导出失败（ffmpeg 退出码非零）"
            if not os.path.exists(tmp):
                self.last_error = "no output"
                return False, "导出失败（没有产出文件）"
            os.replace(tmp, self.destination)   # 原子交付
            return True, self.destination
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass
            for path in (tmp,):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    logger.debug("[CLIP-EXPORT] 清理失败: %s", path,
                                 exc_info=True)

    def _terminate(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=_TERMINATE_WAIT_SEC)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=_TERMINATE_WAIT_SEC)
            except Exception:
                logger.warning("[CLIP-EXPORT] 进程未随 kill 退出（延迟回收）")


class _BoundedReader(threading.Thread):
    """持续读管道并只保留最后 N 字节（防异常源吃内存）。"""

    def __init__(self, pipe, max_bytes: int) -> None:
        super().__init__(daemon=True, name="clip-export-stderr")
        self._pipe = pipe
        self._max = max_bytes
        self._buffer = bytearray()

    def run(self) -> None:
        try:
            for chunk in iter(lambda: self._pipe.read(4096) or b"", b""):
                self._buffer.extend(chunk)
                if len(self._buffer) > self._max:
                    del self._buffer[:-self._max]
        except Exception:
            pass

    def tail(self) -> str:
        return bytes(self._buffer[-self._max:]).decode(errors="ignore")
