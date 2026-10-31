"""
Unified Resource Snapshot (P0-05)
统一资源快照：CPU RAM / VRAM / 线程 / 句柄 / 队列深度 / 对象池（性能总计划 §6 P0-05）。

## 职责

- 全局注册表：各子系统（播放、缓存、导出、缩略图、AI）注册只读
  stats provider，快照按类别聚合。
- take_snapshot()：一次全量快照（进程 RSS、线程数、fd 数、各类别 stats）。
- ResourceSnapshotSampler：后台线程每 1s 采样，保留 ring buffer 与峰值。

## 用法

```python
from src.shared.diagnostics.resource_snapshot import (
    get_resource_registry, ResourceSnapshotSampler,
)

get_resource_registry().register(
    "playback", "edit_present_pool", renderer.present_pool_stats)

sampler = ResourceSnapshotSampler(interval_s=1.0)
sampler.start()
...
sampler.stop()
report = sampler.export_json()   # 含 samples + peaks
```

## 约束

- provider 必须无锁快速返回（内部自持统计锁），快照线程不持有业务锁。
- provider 抛异常视为该子系统本次缺失，不影响其他类别（遥测失败不伤播放）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import types
import weakref
from collections import deque
from typing import TYPE_CHECKING, Callable, Deque, Dict, List, Optional

if TYPE_CHECKING:
    import psutil

logger = logging.getLogger(__name__)

#: 快照类别词表（P0-05：按播放、缓存、导出、缩略图、AI 分类）。
RESOURCE_CATEGORIES = (
    "playback",
    "cache",
    "export",
    "thumbnail",
    "ai",
    "system",
)

StatsProvider = Callable[[], dict]


# AUD-33020: psutil.Process.cpu_percent(interval=None) 基于实例内上次调用
# 做差分，首次调用恒返回 0.0。此前 _system_stats 每次采样都新建 Process
# 实例——等于每次都是"首次调用"，导出的 CPU% 永远是 0.0。缓存进程
# 单例（本进程 pid 稳定）后，第二次采样起返回真实差分值（首次仍为
# 0.0，psutil 语义无从规避）。
_PSUTIL_PROC_LOCK = threading.Lock()
_PSUTIL_PROC: Optional["psutil.Process"] = None


def _psutil_process() -> "psutil.Process":
    """返回当前进程的（缓存的）psutil.Process 单例。"""
    global _PSUTIL_PROC
    with _PSUTIL_PROC_LOCK:
        if _PSUTIL_PROC is None:
            import psutil

            _PSUTIL_PROC = psutil.Process()
        return _PSUTIL_PROC


class _ProviderSlot:
    """registry 内部的一个 provider 槽位（AUD-33021）。

    绑定方法（生产调用方注册的 ``renderer.stats`` 等）经 WeakMethod
    弱持有：渲染器/对象池被关闭回收后槽位自动失效，不再把整个对象图
    钉在全局单例 registry 上（泄漏），collect() 也不会再对已 shutdown
    的对象继续调用。其余可调用对象（模块级函数、测试中的 lambda——通常
    没有其他强引用，弱持有会立即死亡）保持强引用，行为与旧版一致。
    """

    __slots__ = ("_weak", "_strong")

    def __init__(self, provider: StatsProvider) -> None:
        self._strong: Optional[StatsProvider] = None
        self._weak: Optional[weakref.WeakMethod] = None
        if isinstance(provider, types.MethodType):
            self._weak = weakref.WeakMethod(provider)
        else:
            self._strong = provider

    def resolve(self) -> Optional[StatsProvider]:
        """取存活的 provider；弱引用已死亡返回 None。"""
        if self._strong is not None:
            return self._strong
        assert self._weak is not None
        return self._weak()


class ResourceSnapshotRegistry:
    """子系统 stats provider 注册表（线程安全）。

    register(category, name, provider) 后，每次快照会调用 provider()
    并把结果挂在 snapshot["categories"][category][name] 下。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: Dict[str, Dict[str, _ProviderSlot]] = {
            cat: {} for cat in RESOURCE_CATEGORIES
        }

    def register(
        self, category: str, name: str, provider: StatsProvider
    ) -> None:
        if category not in self._providers:
            raise ValueError(f"unknown resource category: {category!r}")
        with self._lock:
            self._providers[category][name] = _ProviderSlot(provider)

    def unregister(self, category: str, name: str) -> None:
        with self._lock:
            self._providers.get(category, {}).pop(name, None)

    def unregister_owner(self, name_prefix: str) -> int:
        """按 provider 名前缀批量注销（页面/工程关闭时清理）。"""
        removed = 0
        with self._lock:
            for providers in self._providers.values():
                stale = [n for n in providers if n.startswith(name_prefix)]
                for n in stale:
                    providers.pop(n, None)
                    removed += 1
        return removed

    def collect(self) -> Dict[str, Dict[str, dict]]:
        """收集所有类别当前 stats（provider 异常记为 {"error": str}）。"""
        with self._lock:
            providers = {
                cat: dict(provs) for cat, provs in self._providers.items()
            }
        out: Dict[str, Dict[str, dict]] = {}
        for cat, provs in providers.items():
            entries: Dict[str, dict] = {}
            for name, slot in provs.items():
                provider = slot.resolve()
                if provider is None:
                    # AUD-33021: 弱引用已死亡（对象已 teardown 且未注销）
                    # ——跳过而不是调用死对象。
                    logger.debug(
                        "resource provider %r in %r is gone; skipping "
                        "(unregister it in the owner's teardown)", name, cat,
                    )
                    continue
                try:
                    stats = provider()
                    entries[name] = stats if isinstance(stats, dict) else {"value": stats}
                except Exception as e:  # noqa: BLE001 遥测不伤业务
                    entries[name] = {"error": str(e)}
            out[cat] = entries
        return out


class ResourceSnapshotSampler:
    """后台周期资源快照（1s 间隔 + ring buffer + 峰值）。

    线程规则：start/stop 可在任意线程；采样在独立 daemon 线程执行。
    """

    def __init__(
        self,
        registry: Optional[ResourceSnapshotRegistry] = None,
        interval_s: float = 1.0,
        max_samples: int = 3600,
    ) -> None:
        self._registry = registry or get_resource_registry()
        self._interval_s = max(0.1, float(interval_s))
        self._samples: Deque[dict] = deque(maxlen=max(10, int(max_samples)))
        self._lock = threading.Lock()
        # AUD-33022: 生命周期（start/stop 与句柄发布）由独立锁串行化；
        # 停止事件改为每次 start 新建并按线程绑定——复用单个 Event 的
        # clear() 会把仍在退出的旧线程"复活"，产生双采样线程。
        self._lifecycle_lock = threading.Lock()
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._join_timeout = 2.0
        self._peaks: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run, args=(stop,),
                name="ResourceSnapshotSampler", daemon=True,
            )
            self._stop = stop
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            stop = self._stop
        if stop is not None:
            stop.set()
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=self._join_timeout)
        if thread.is_alive():
            # AUD-33022: join 超时绝不丢掉存活线程的句柄——此前无条件
            # 置空 _thread，随后 start() 会另起第二个采样线程，且新 start
            # 清掉共享停止事件会让旧线程永远采样（僵尸线程 + 双倍采样）。
            # 现在保留句柄并告警：start() 在旧线程退出前拒绝重启，按线程
            # 绑定的停止事件保持 set，provider 一返回线程即退出；可再调
            # stop() 等它真正结束。
            logger.warning(
                "ResourceSnapshotSampler thread still alive after %.1fs "
                "join (a provider is blocking); keeping the handle — call "
                "stop() again once the provider returns",
                self._join_timeout,
            )
            return
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None
                self._stop = None

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval_s):
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 采样失败不退出
                logger.debug("resource snapshot failed", exc_info=True)

    # ------------------------------------------------------------------
    # sampling
    # ------------------------------------------------------------------

    def sample_once(self) -> dict:
        """立即采一次快照（含进程级 system 指标 + 各类别 stats）。"""
        sample: Dict[str, object] = {"ts": time.time()}
        sample["system"] = self._system_stats()
        sample["categories"] = self._registry.collect()
        self._update_peaks(sample)
        with self._lock:
            self._samples.append(sample)
        return sample

    @staticmethod
    def _system_stats() -> dict:
        """进程级指标：RSS、线程数、fd 数、CPU%（psutil 可用时）。"""
        stats: Dict[str, object] = {}
        try:
            import os

            stats["thread_count"] = threading.active_count()
            try:
                stats["os_thread_count"] = len(os.listdir("/proc/self/task"))
            except OSError:
                pass
            try:
                import psutil  # noqa: F401 —— 可用性探测

                # AUD-33020: 复用缓存的 Process 单例——cpu_percent 的差分
                # 状态保存在实例内，每次新建实例永远返回首次值 0.0。
                proc = _psutil_process()
                mem = proc.memory_info()
                stats["rss_bytes"] = int(mem.rss)
                stats["cpu_percent"] = proc.cpu_percent(None)
                try:
                    stats["fd_count"] = proc.num_fds()
                except (AttributeError, psutil.AccessDenied):
                    pass
            except ImportError:
                pass
        except Exception as e:  # noqa: BLE001
            stats["error"] = str(e)
        return stats

    def _update_peaks(self, sample: dict) -> None:
        system = sample.get("system") or {}
        for key in ("rss_bytes", "thread_count", "fd_count"):
            value = system.get(key)
            if isinstance(value, (int, float)):
                with self._lock:
                    prev = self._peaks.get(f"system.{key}", 0.0)
                    if value > prev:
                        self._peaks[f"system.{key}"] = float(value)
        for cat, entries in (sample.get("categories") or {}).items():
            for name, stats in entries.items():
                if not isinstance(stats, dict):
                    continue
                for key, value in stats.items():
                    if key.endswith(("_depth", "_size", "resident", "peak_in_flight")) \
                            and isinstance(value, (int, float)):
                        with self._lock:
                            pkey = f"{cat}.{name}.{key}"
                            if value > self._peaks.get(pkey, 0.0):
                                self._peaks[pkey] = float(value)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def get_samples(self, last_n: Optional[int] = None) -> List[dict]:
        with self._lock:
            samples = list(self._samples)
        return samples[-last_n:] if last_n else samples

    def get_peaks(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._peaks)

    def summary(self) -> dict:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {"sample_count": 0}
        rss = [s["system"].get("rss_bytes", 0) for s in samples
               if isinstance(s.get("system"), dict)]
        rss = [v for v in rss if isinstance(v, (int, float))]
        # last 必须在排序前取，否则拿到的是 max。
        rss_last = rss[-1] if rss else 0
        rss_sorted = sorted(rss)
        return {
            "sample_count": len(samples),
            "rss_bytes_last": rss_last,
            "rss_bytes_max": rss_sorted[-1] if rss_sorted else 0,
            "peaks": self.get_peaks(),
        }

    def export_json(self) -> str:
        return json.dumps(
            {
                "summary": self.summary(),
                "samples": self.get_samples(),
            },
            ensure_ascii=False,
            default=str,
        )


# ---------------------------------------------------------------------------
# 全局实例
# ---------------------------------------------------------------------------

_global_registry: Optional[ResourceSnapshotRegistry] = None
_registry_lock = threading.Lock()


def get_resource_registry() -> ResourceSnapshotRegistry:
    global _global_registry
    if _global_registry is None:
        with _registry_lock:
            if _global_registry is None:
                _global_registry = ResourceSnapshotRegistry()
    return _global_registry
