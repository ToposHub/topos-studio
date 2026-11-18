"""Rebuildable pre-rendered PCM cache for the clip editor stack (§3.3).

STFT-class processors (spectral denoise, later the repair brush) cannot run
per playback block without breaking the whole-array semantics they were
authored with, so the roadmap keeps the "audition == playback == export"
parity contract by **pre-rendering** their result once per spec and sharing
it across the three render paths:

- The cache is keyed by a ``PreRenderSpec`` covering the working sample rate,
  the source fingerprint, the exact half-open sample range, the channel count
  and the ordered signature of the enabled pre-render steps.  Any parameter
  change produces a different key — stale results are never reused.
- It lives in the platform cache directory (rebuildable by definition, never
  inside the project package).  Writes go to a temp file next to the target
  and are validated before an atomic ``os.replace``; a cancelled or failed
  build leaves no temp files behind.
- ``build_async`` deduplicates in-flight builds across threads; listeners are
  notified (from the building thread) when an entry lands so the engine can
  resync.  ``protect`` pins entries against eviction while an export or the
  editor preview is using them.
- A ``PreRenderBuildPlan`` binds the plan to a timeline clip: it decodes the
  source range via the same PyAV path as the Clip Editor sampling
  (``decode_audio_range``) and applies the pre-render steps whole-array via
  ``audio_edit.render_pre_render_steps``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

from src.features.edit.core.clip_pcm import decode_audio_range, source_fingerprint

logger = logging.getLogger(__name__)

PCM_RENDER_CACHE_SCHEMA_VERSION = 1
# 与 ClipPcmProvider 同一护栏：单次预渲染的时长上限（内存 + 构建时间）。
MAX_PRE_RENDER_SECONDS = 300.0
# 内存 LRU 条目数（4 份立体声 5 分钟 ≈ 460 MB，与 PCM 取样层一致）。
MEMORY_CACHE_CAPACITY = 4
# 磁盘回收护栏（条数 + 总字节；savez_compressed 的 float32 音频通常再省一半）。
DISK_CACHE_MAX_ENTRIES = 64
DISK_CACHE_MAX_BYTES = 8 * 1024 * 1024 * 1024


class PreRenderCancelled(RuntimeError):
    """构建在完成前被协作取消（不落任何缓存文件）。"""


@dataclass(frozen=True)
class PreRenderSpec:
    """一个预渲染结果的全部身份输入（纯数据、可哈希）。

    ``total_samples`` 是源解码区间长度；``output_samples`` 是整栈烘焙后的
    长度（改长度步骤生效后的新长度；无改长度步骤时与 total 相同）。
    """

    source_fingerprint: str
    sample_rate: int
    channel_count: int
    total_samples: int
    steps_signature: tuple
    output_samples: int = -1  # -1 = 无改长度步骤（与 total_samples 一致）

    @property
    def expected_output_samples(self) -> int:
        return self.total_samples if self.output_samples < 0 else self.output_samples

    @property
    def key(self) -> str:
        identity = repr((
            PCM_RENDER_CACHE_SCHEMA_VERSION,
            self.source_fingerprint, self.sample_rate,
            self.channel_count, self.total_samples, self.steps_signature,
            self.expected_output_samples,
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


class _Inflight:
    """同进程内同 key 构建的去纤绳（后来者等待后直接读缓存）。"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.error: Optional[BaseException] = None


class PcmRenderCache:
    """规格哈希索引的预渲染 PCM 缓存（内存 LRU + 磁盘 npz + 原子替换）。"""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        memory_capacity: int = MEMORY_CACHE_CAPACITY,
        disk_max_entries: int = DISK_CACHE_MAX_ENTRIES,
        disk_max_bytes: int = DISK_CACHE_MAX_BYTES,
    ) -> None:
        self._root = Path(root) if root is not None else None
        self._memory_capacity = max(0, int(memory_capacity))
        self._disk_max_entries = max(0, int(disk_max_entries))
        self._disk_max_bytes = max(0, int(disk_max_bytes))
        self._lock = threading.RLock()
        self._inflight_lock = threading.Lock()
        self._inflight: dict[str, _Inflight] = {}
        self._memory: dict[str, np.ndarray] = {}
        self._memory_order: list[str] = []
        self._protected: dict[str, int] = {}
        self._listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def root(self) -> Optional[Path]:
        return self._root

    def add_listener(self, callback: Callable[[str], None]) -> None:
        """构建落盘后回调（构建线程调用；Qt 消费方需自行排队回主线程）。"""
        with self._lock:
            self._listeners.append(callback)

    @contextmanager
    def protect(self, spec: PreRenderSpec) -> Iterator[PreRenderSpec]:
        """导出/试听/undo-redo 使用期间钉住条目，避免被回收。"""
        key = spec.key
        with self._lock:
            self._protected[key] = self._protected.get(key, 0) + 1
        try:
            yield spec
        finally:
            with self._lock:
                count = self._protected.get(key, 0) - 1
                if count > 0:
                    self._protected[key] = count
                else:
                    self._protected.pop(key, None)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def has(self, spec: PreRenderSpec) -> bool:
        return self.lookup(spec) is not None

    def lookup(self, spec: PreRenderSpec) -> Optional[np.ndarray]:
        """命中返回整段 planar float32；未命中/校验失败返回 None。"""
        key = spec.key
        with self._lock:
            pcm = self._memory.get(key)
            if pcm is not None:
                self._touch_memory_locked(key)
                return pcm
        pcm = self._load_disk(spec)
        if pcm is not None:
            self._remember(key, pcm)
        return pcm

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(
        self,
        spec: PreRenderSpec,
        builder: Callable[[], np.ndarray],
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> np.ndarray:
        """同步构建并落盘（已存在则直接返回；取消/失败清理临时文件）。"""
        cached = self.lookup(spec)
        if cached is not None:
            return cached
        if cancel_check is not None and cancel_check():
            raise PreRenderCancelled(f"pre-render {spec.key} cancelled")
        pcm = builder()
        self._validate_payload(spec, pcm)
        self.store(spec, pcm)
        self._notify(spec.key)
        return pcm

    def get_or_build(
        self,
        spec: PreRenderSpec,
        builder_factory: Callable[[], np.ndarray],
    ) -> np.ndarray:
        """同步构建 + 同 key 去重：并发调用只跑一次 builder。"""
        cached = self.lookup(spec)
        if cached is not None:
            return cached
        key = spec.key
        with self._inflight_lock:
            entry = self._inflight.get(key)
            owner = entry is None
            if owner:
                entry = _Inflight()
                self._inflight[key] = entry
        if not owner:
            entry.event.wait()
            cached = self.lookup(spec)
            if cached is not None:
                return cached
            if entry.error is not None:
                raise RuntimeError(
                    f"pre-render {key} failed in a concurrent build"
                ) from entry.error
            raise RuntimeError(f"pre-render {key} vanished after concurrent build")
        try:
            cached = self.lookup(spec)  # 双检：等待期间可能已有并发写入
            if cached is not None:
                return cached
            pcm = builder_factory()
            self._validate_payload(spec, pcm)
            self.store(spec, pcm)
            self._notify(key)
            return pcm
        except BaseException as exc:  # noqa: BLE001 - 供等待方重试/诊断
            entry.error = exc
            raise
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            entry.event.set()

    def build_async(
        self,
        spec: PreRenderSpec,
        builder_factory: Callable[[], np.ndarray],
    ) -> bool:
        """后台构建（命中缓存立即返回 True；失败仅记日志）。"""
        if self.has(spec):
            return True
        try:
            self.get_or_build(spec, builder_factory)
            return True
        except Exception as exc:  # noqa: BLE001 - 后台路径失败不冒泡
            logger.warning("pre-render build failed for %s: %s", spec.key, exc)
            return False

    def spawn_build(
        self,
        spec: PreRenderSpec,
        builder_factory: Callable[[], np.ndarray],
    ) -> bool:
        """在守护线程中构建；返回 False 表示已在构建或已命中（无需重复）。"""
        if self.has(spec):
            return True
        key = spec.key
        with self._inflight_lock:
            if key in self._inflight:
                return False
            entry = _Inflight()
            self._inflight[key] = entry

        def _run() -> None:
            try:
                cached = self.lookup(spec)
                if cached is None:
                    pcm = builder_factory()
                    self._validate_payload(spec, pcm)
                    self.store(spec, pcm)
                    self._notify(key)
            except BaseException as exc:  # noqa: BLE001
                entry.error = exc
                logger.warning("async pre-render %s failed: %s", key, exc)
            finally:
                with self._inflight_lock:
                    self._inflight.pop(key, None)
                entry.event.set()

        threading.Thread(
            target=_run, name=f"pcm-pre-render-{key[:8]}", daemon=True,
        ).start()
        return True

    # ------------------------------------------------------------------
    # 落盘 / 回收
    # ------------------------------------------------------------------
    def store(self, spec: PreRenderSpec, pcm: np.ndarray) -> None:
        """写穿：临时文件 + 校验 + 原子替换；随后执行磁盘回收。"""
        self._validate_payload(spec, pcm)
        key = spec.key
        self._remember(key, pcm)
        if self._root is None:
            return
        root = self._root
        root.mkdir(parents=True, exist_ok=True)
        data_path = root / f"{key}.npz"
        meta_path = root / f"{key}.json"
        tmp_data = root / f".tmp-{key}-{os.getpid()}-{threading.get_ident()}.npz"
        tmp_meta = root / f".tmp-{key}-{os.getpid()}-{threading.get_ident()}.json"
        try:
            np.savez_compressed(tmp_data, pcm=pcm)
            # 落盘前回读校验：损坏负载绝不能原子替换进缓存。
            self._validate_payload(spec, np.load(tmp_data)["pcm"])
            with open(tmp_meta, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema": PCM_RENDER_CACHE_SCHEMA_VERSION,
                    "key": key,
                    "source_fingerprint": spec.source_fingerprint,
                    "sample_rate": spec.sample_rate,
                    "channel_count": spec.channel_count,
                    "total_samples": spec.total_samples,
                    "output_samples": spec.expected_output_samples,
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_data, data_path)
            os.replace(tmp_meta, meta_path)
            os.utime(data_path)  # LRU 触碰（读取时也调用）
        finally:
            for leftover in (tmp_data, tmp_meta):
                try:
                    leftover.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.debug("pre-render tmp cleanup failed: %s", leftover)
        self.trim_disk()

    def trim_disk(self) -> None:
        """按条数/总字节 LRU 回收；被 protect 的 key 跳过。"""
        if self._root is None:
            return
        root = self._root
        try:
            entries = [
                (path.stat().st_mtime_ns, path)
                for path in root.glob("*.npz")
                if path.is_file()
            ]
        except OSError:
            return
        entries.sort(reverse=True)
        with self._lock:
            protected = set(self._protected)
        kept = 0
        total_bytes = 0
        for _, path in entries:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            key = path.stem
            if key in protected:
                kept += 1
                total_bytes += size
                continue
            if (kept < self._disk_max_entries
                    and total_bytes + size <= self._disk_max_bytes):
                kept += 1
                total_bytes += size
                continue
            for suffix in (".npz", ".json"):
                try:
                    (root / f"{key}{suffix}").unlink()
                except OSError:
                    pass

    def clear(self, *, remove_files: bool = True) -> None:
        """清空内存（测试/显式清理）；默认同时删除磁盘条目。"""
        with self._lock:
            self._memory.clear()
            self._memory_order.clear()
        if remove_files and self._root is not None:
            try:
                for path in self._root.glob("*.npz"):
                    path.unlink(missing_ok=True)
                for path in self._root.glob("*.json"):
                    path.unlink(missing_ok=True)
            except OSError:
                logger.debug("pre-render cache clear failed", exc_info=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _notify(self, key: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(key)
            except Exception:  # noqa: BLE001 - 监听者故障不影响缓存
                logger.exception("pre-render listener failed")

    def _remember(self, key: str, pcm: np.ndarray) -> None:
        with self._lock:
            self._memory[key] = pcm
            self._touch_memory_locked(key)
            while len(self._memory_order) > self._memory_capacity:
                # 全部被 protect 时允许超额驻留（保护期结束后下次写入回收）。
                evict = next((k for k in self._memory_order
                              if self._protected.get(k, 0) <= 0), None)
                if evict is None:
                    break
                self._memory_order.remove(evict)
                self._memory.pop(evict, None)

    def _touch_memory_locked(self, key: str) -> None:
        if key in self._memory_order:
            self._memory_order.remove(key)
        self._memory_order.append(key)
        try:
            if self._root is not None:
                os.utime(self._root / f"{key}.npz")
        except OSError:
            pass

    def _load_disk(self, spec: PreRenderSpec) -> Optional[np.ndarray]:
        if self._root is None:
            return None
        root = self._root
        data_path = root / f"{spec.key}.npz"
        meta_path = root / f"{spec.key}.json"
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            if (int(meta.get("schema", -1)) != PCM_RENDER_CACHE_SCHEMA_VERSION
                    or meta.get("key") != spec.key
                    or int(meta.get("channel_count", -1)) != spec.channel_count
                    or int(meta.get("total_samples", -1)) != spec.total_samples
                    or int(meta.get("output_samples", -1))
                    != spec.expected_output_samples):
                raise ValueError("meta mismatch")
            with np.load(data_path) as payload:
                pcm = payload["pcm"]
                if (pcm.dtype != np.float32 or pcm.ndim != 2
                        or pcm.shape[0] != spec.channel_count
                        or pcm.shape[1] != spec.expected_output_samples):
                    raise ValueError("payload mismatch")
                result = np.ascontiguousarray(pcm, dtype=np.float32)
            os.utime(data_path)  # LRU 触碰
            return result
        except (OSError, ValueError, KeyError) as exc:
            logger.debug("pre-render cache miss/corrupt for %s: %s", spec.key, exc)
            # 损坏条目按未命中处理并清掉，避免每次都重读坏文件。
            for path in (data_path, meta_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            return None

    @staticmethod
    def _validate_payload(spec: PreRenderSpec, pcm: np.ndarray) -> None:
        if (not isinstance(pcm, np.ndarray) or pcm.ndim != 2
                or pcm.dtype != np.float32
                or pcm.shape[0] != spec.channel_count
                or pcm.shape[1] != spec.expected_output_samples):
            raise ValueError(
                f"pre-render payload {pcm.shape if isinstance(pcm, np.ndarray) else pcm!r}"
                f" does not match spec {spec.channel_count}x"
                f"{spec.expected_output_samples} float32")


# ----------------------------------------------------------------------
# 单例（平台缓存目录；TOPOS_PCM_RENDER_CACHE=0 关闭磁盘层）
# ----------------------------------------------------------------------
_singleton: Optional[PcmRenderCache] = None
_singleton_lock = threading.Lock()


def default_cache_root() -> Optional[Path]:
    override = os.environ.get("TOPOS_PCM_RENDER_CACHE_DIR")
    if override:
        return Path(override)
    if os.environ.get("TOPOS_PCM_RENDER_CACHE", "1") == "0":
        return None
    try:
        from src.shared.config.config_manager import ConfigManager
        return Path(ConfigManager.get_platform_cache_dir()) / "clip_pcm_render"
    except Exception:  # noqa: BLE001 - 无可用缓存目录时退内存态
        logger.debug("platform cache dir unavailable; pre-render cache is memory-only")
        return None


def get_pcm_render_cache() -> PcmRenderCache:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PcmRenderCache(default_cache_root())
        return _singleton


def reset_pcm_render_cache_for_tests(root: Optional[Path] = None) -> PcmRenderCache:
    """测试专用：换根目录重建单例（避免污染真实缓存目录）。"""
    global _singleton
    with _singleton_lock:
        _singleton = PcmRenderCache(root)
        return _singleton


# ----------------------------------------------------------------------
# 片段 → 预渲染构建计划
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class PreRenderBuildPlan:
    """绑定时间线片段的预渲染输入（decode + pre-render steps 的纯数据）。"""

    source_path: str
    start_sample: int
    end_sample: int
    sample_rate: int
    source_fingerprint: str
    steps: tuple  # 有序的 enabled 预渲染 step dict
    reverse: bool = False
    expected_channels: int = 2
    blocked_reason: Optional[str] = None
    diagnostics: tuple = field(default=())

    @property
    def total_samples(self) -> int:
        return max(0, self.end_sample - self.start_sample)

    def spec(self) -> PreRenderSpec:
        from src.features.edit.core.audio_edit import (
            output_length_after_steps, steps_signature,
        )
        return PreRenderSpec(
            source_fingerprint=self.source_fingerprint,
            sample_rate=int(self.sample_rate),
            channel_count=int(self.expected_channels),
            total_samples=self.total_samples,
            steps_signature=steps_signature({"steps": list(self.steps)}),
            # 改长度步骤后的基底长度（消费方按新输出域切块）。
            output_samples=output_length_after_steps(
                {"steps": list(self.steps)}, self.total_samples),
        )

    def builder(self) -> Callable[[], np.ndarray]:
        plan = self

        def _build() -> np.ndarray:
            pcm = decode_audio_range(
                plan.source_path, plan.start_sample, plan.end_sample,
                plan.sample_rate)
            if pcm is None:
                raise RuntimeError(
                    f"pre-render decode failed for {plan.source_path}")
            if plan.reverse:
                pcm = np.ascontiguousarray(pcm[:, ::-1])
            from src.features.edit.core.audio_edit import render_pre_render_steps
            return render_pre_render_steps(
                pcm, {"steps": list(plan.steps), "processing_rate": plan.sample_rate},
                sample_rate=plan.sample_rate)

        return _build

    def lookup_cached(self, cache: Optional[PcmRenderCache] = None) -> Optional[np.ndarray]:
        if self.blocked_reason:
            return None
        cache = cache or get_pcm_render_cache()
        return cache.lookup(self.spec())


def build_pre_render_plan(
    project, timeline, clip, asset, *,
    working_rate: int = 48000,
) -> Optional[PreRenderBuildPlan]:
    """从稳定模型解析片段的预渲染计划；无预渲染 step 返回 None。

    复用 ``resolve_clip_audio_spec`` 的绑定/帧域换算（与 Clip Editor 取样
    同一条数学路径）；超长片段/倒放给出明确 blocked 诊断而不是静默失败。
    """
    from src.features.edit.core.audio_edit import pre_render_steps
    audio_edit = getattr(clip, "audio_edit", None)
    steps = pre_render_steps(audio_edit)
    if not steps:
        return None
    from src.features.edit.core.clip_pcm import resolve_clip_audio_spec
    spec = resolve_clip_audio_spec(
        project, timeline, clip, asset, working_rate=working_rate)
    if spec is None:
        return PreRenderBuildPlan(
            source_path="", start_sample=0, end_sample=0,
            sample_rate=working_rate, source_fingerprint="",
            steps=tuple(steps), blocked_reason="audio source unavailable",
            diagnostics=("audio source unavailable",))
    from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE
    max_samples = int(AUDIO_REFERENCE_SAMPLE_RATE * MAX_PRE_RENDER_SECONDS)
    diagnostics: list[str] = []
    blocked: Optional[str] = None
    if spec.sample_count > max_samples:
        blocked = (f"clip longer than {MAX_PRE_RENDER_SECONDS:.0f}s cannot use "
                   "pre-rendered processors")
        diagnostics.append(blocked)
    if spec.reverse:
        blocked = blocked or "reverse-playback clips cannot use pre-rendered processors"
        diagnostics.append("reverse-playback clips cannot use pre-rendered processors")
    # 变速片段的基底 PCM 是源时间域，播放块定位在输出时间域——预渲染
    # 基底与块切片会错位；v1 明确阻断（诊断可见）而不是静默错放。
    speed = getattr(clip, "effective_speed", None)
    speed = float(speed() if callable(speed) else (speed if speed is not None else 1.0))
    if abs(speed - 1.0) > 1e-9:
        reason = "retimed clips cannot use pre-rendered processors yet"
        blocked = blocked or reason
        diagnostics.append(reason)
    return PreRenderBuildPlan(
        source_path=spec.source_path,
        start_sample=spec.start_sample,
        end_sample=spec.end_sample,
        sample_rate=spec.sample_rate,
        source_fingerprint=spec.source_fingerprint,
        steps=tuple(steps),
        reverse=spec.reverse,
        # decode_audio_range 的容器布局：mono / >2ch 混到 stereo。
        expected_channels=1 if spec.channel_count == 1 else 2,
        blocked_reason=blocked,
        diagnostics=tuple(diagnostics),
    )


def resolve_export_pre_render_base(
    project, timeline, clip, asset,
) -> tuple[Optional[np.ndarray], tuple]:
    """导出路径的预渲染基底：同步 get_or_build（离线等待构建完成）。

    返回 ``(base, diagnostics)``；base 为 None 时调用方回退 inline-only
    并应把 diagnostics 上报（SciPy 缺席/超长片段/倒放）。构建期间用
    ``protect`` 钉住条目，避免并发回收删掉刚落盘的结果。
    """
    plan = build_pre_render_plan(project, timeline, clip, asset)
    if plan is None:
        return None, ()
    if plan.blocked_reason:
        return None, plan.diagnostics or (plan.blocked_reason,)
    cache = get_pcm_render_cache()
    spec = plan.spec()
    with cache.protect(spec):
        try:
            return cache.get_or_build(spec, plan.builder()), ()
        except Exception as exc:  # noqa: BLE001 - 交付路径降级必须留痕
            logger.warning(
                "export pre-render failed for clip %s: %s",
                getattr(clip, "clip_id", "?"), exc)
            return None, (str(exc),)


def preflight_pre_render_steps(sequence) -> tuple:
    """交付预检：预渲染步骤的可构建性逐条诊断（SciPy/时长/倒放）。

    预渲染不可构建时导出会静默少掉降噪——与 EQ/LUFS 同一阻断语义
    （RMA-01：缺后端不得静默产出未处理文件）。
    """
    from src.features.edit.core.audio_edit import requires_pre_render
    from src.features.edit.core.audio_denoise import scipy_available

    problems: list[str] = []
    if sequence is None:
        return ()
    try:
        fps = float(getattr(sequence, "fps", 24.0) or 24.0) or 24.0
    except (TypeError, ValueError):
        fps = 24.0
    scipy_ok = scipy_available()
    for track in getattr(sequence, "audio_tracks", ()) or ():
        for clip in getattr(track, "clips", ()) or ():
            audio_edit = getattr(clip, "audio_edit", None)
            if audio_edit is None or not requires_pre_render(audio_edit):
                continue
            clip_id = str(getattr(clip, "clip_id", "?"))[:8]
            if not scipy_ok:
                problems.append(
                    f"clip {clip_id}: spectral_denoise 需要 SciPy DSP 后端，"
                    "缺后端时导出会静默跳过降噪")
            try:
                duration = float(getattr(clip, "timeline_duration", 0) or 0) / fps
            except (TypeError, ValueError):
                duration = 0.0
            if duration > MAX_PRE_RENDER_SECONDS:
                problems.append(
                    f"clip {clip_id}: 预渲染处理器仅支持 "
                    f"≤{MAX_PRE_RENDER_SECONDS:.0f} s 片段（当前 {duration:.0f} s）")
            is_reverse = getattr(clip, "is_reverse_playback", None)
            reverse = bool(is_reverse() if callable(is_reverse) else is_reverse)
            if reverse:
                problems.append(
                    f"clip {clip_id}: 倒放片段暂不支持预渲染处理器")
            speed = getattr(clip, "effective_speed", None)
            speed = float(
                speed() if callable(speed)
                else (speed if speed is not None else 1.0))
            if abs(speed - 1.0) > 1e-9:
                problems.append(
                    f"clip {clip_id}: 变速片段暂不支持预渲染处理器")
    return tuple(problems)
