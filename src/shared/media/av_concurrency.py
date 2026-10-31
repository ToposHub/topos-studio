"""进程级 PyAV 解码并发计数（Q01/F1.2 根治设施）。

F1.1 五轮 faulthandler 转储把单进程挂起的根因收敛为 libav↔GIL 锁序反转：
av 容器 ``close()`` 持 GIL 等 libav 内部锁；并发的解码线程持 libav 锁等
GIL 回来做 PyAV 包装层簿记。楔死期间整个进程的 Python 线程都无法推进。

本模块提供进程级 in-flight 计数：所有解码侧 av 容器的**公共入口**（视频
源读取/seek/open/probe、音频解码读取、VT 源读取）进出各计一次；
``ffmpeg_source`` 的常驻关闭线程只在计数归零的空闲窗口执行容器关闭，
从而打断锁序反转。

边界：计数只覆盖经本设施包装的入口；未包装的 PyAV 直接使用（编码器、
bench 脚本）不参与窗口判定，如实登记（F1.2 ledger）。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

_IN_FLIGHT = 0
_LOCK = threading.Lock()


@contextmanager
def av_operation() -> Iterator[None]:
    """标记一段可能触及 libav 内部锁的解码侧 av 调用。

    进入 +1 / 退出 -1；嵌套安全。所有解码容器的公共读/seek/open 入口
    都应包在本上下文中，使容器关闭可以依赖计数判断全局空闲。
    """
    global _IN_FLIGHT
    with _LOCK:
        _IN_FLIGHT += 1
    try:
        yield
    finally:
        with _LOCK:
            _IN_FLIGHT -= 1


def in_flight_count() -> int:
    """当前进程内解码侧 av 操作的 in-flight 数。"""
    with _LOCK:
        return _IN_FLIGHT


def wait_for_idle(poll_interval: float = 0.05) -> None:
    """阻塞直到全局解码空闲（in-flight 归零）。

    供容器关闭线程使用：只在空闲窗口执行 close，保证 close 不会与
    持 libav 锁等 GIL 的解码线程形成锁序反转（Q01 根治）。持续播放下
    调用方会等待（容器停留队列，随进程退出兜底回收）。
    """
    while in_flight_count() > 0:
        time.sleep(poll_interval)


def av_operation_decorator(method):
    """类方法装饰器形态（便于给既有解码入口最小侵入地加计数）。"""

    def wrapper(*args, **kwargs):
        with av_operation():
            return method(*args, **kwargs)

    wrapper.__name__ = getattr(method, "__name__", "av_op")
    wrapper.__doc__ = getattr(method, "__doc__", None)
    wrapper.__wrapped__ = method
    return wrapper


def av_locked_operation(method):
    """解码实例方法装饰器：进程级计数 + 每实例 RLock 串行化。

    Q01/F1.2 run6 转储：空闲窗口关闭消除 close×decode 反转后，92% 楔点
    暴露为**同一 FFmpegMediaSource 实例被多线程并发访问**（libav 上下文
    非线程安全，内部楔死且不释放 GIL）。本装饰器在计数之外再取
    ``self._av_lock``（RLock，可重入），把同实例的 open/read/seek 串行
    化；正常播放/导出各用专属源、无争用，代价为零；并发滥用被安全阻塞。
    close() 需另行显式获取同一把锁（见 FFmpegMediaSource.close）。
    """

    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_av_lock", None)
        with av_operation():
            if lock is not None:
                with lock:
                    return method(self, *args, **kwargs)
            return method(self, *args, **kwargs)

    wrapper.__name__ = getattr(method, "__name__", "av_op")
    wrapper.__doc__ = getattr(method, "__doc__", None)
    wrapper.__wrapped__ = method
    return wrapper
