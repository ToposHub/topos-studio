"""E6：进程级音频输出仲裁——共享设备上的切源与播放互斥。

现状（FUP 调研确认）：每个播放器各开一个 ``QAudioSink``（默认输出设备），
跨引擎互斥只靠宿主约定（``stop_other_players``）；``VideoProEngine`` 与
``NativePlayerHost`` 互相独立，两个活跃播放器会在同一设备上各占一个
sink、双时钟并行。

本模块是**音频层的互斥兜底**（宿主级停其他播放器仍是第一道）：

* 管线 ``start`` 时登记（``acquire``）；新拥有者接管时，旧拥有者经
  ``on_takeover`` 回调被要求**退役**（不 join、不阻塞接管方——管线线程
  自行收尾）；
* ``release`` 只在拥有者匹配时生效（陈旧释放不得误伤新拥有者）；
* 纯 Python（无 Qt 依赖），回调线程安全（管线的 retire 只设标志 + 唤醒）。

时钟口径的诚实边界：libav（墙钟主时钟）与 topos（音频位置主时钟）两套
时基各自语义正确，本模块统一的是**设备占用与互斥**，不做时钟合并。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AudioOutputArbiter:
    """同一进程内音频输出的唯一仲裁者（默认输出设备一次一个拥有者）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: Optional[str] = None
        self._on_takeover: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------ 登记/释放
    def acquire(self, owner: str,
                on_takeover: Callable[[], None]) -> bool:
        """登记 ``owner`` 为当前唯一音频拥有者。

        返回是否发生了接管（旧拥有者存在且不是自己）。接管回调在本线程
        同步触发——必须快且线程安全（各管线的 ``retire`` 只设标志）。
        """
        previous_cb: Optional[Callable[[], None]] = None
        previous_owner: Optional[str] = None
        with self._lock:
            if self._owner != owner:
                previous_owner = self._owner
                previous_cb = self._on_takeover
                self._owner = owner
                self._on_takeover = on_takeover
        if previous_owner is not None and previous_cb is not None:
            logger.info("[AUDIO-ARBITER] %s 接管音频输出（退役 %s）",
                        owner, previous_owner)
            try:
                previous_cb()
            except Exception:
                logger.exception("[AUDIO-ARBITER] 旧拥有者退役回调失败")
        return previous_owner is not None

    def release(self, owner: str) -> None:
        """释放拥有权；只有当前拥有者匹配才生效（陈旧释放忽略）。"""
        with self._lock:
            if self._owner == owner:
                self._owner = None
                self._on_takeover = None

    # ------------------------------------------------------------ 查询
    def current_owner(self) -> Optional[str]:
        with self._lock:
            return self._owner


_SHARED: Optional[AudioOutputArbiter] = None


def shared_arbiter() -> AudioOutputArbiter:
    """进程级共享仲裁者（所有播放引擎的音频管线共用）。"""
    global _SHARED
    if _SHARED is None:
        _SHARED = AudioOutputArbiter()
    return _SHARED
