# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Audio Health Report（RMA-12 阶段 D2）。

统一聚合音频子系统的健康状态，供 preflight、健康面板、issue 报告复用。

设计动机
--------
RMA-12：DSP、设备、routing graph、underrun、缓存/临时文件此前没有统一的可见
健康面板。AudioCapabilities 只覆盖启动期 DSP/设备探测；graph validation（B1）、
underrun、disk/cache 散落各处。本模块把它们收敛为一份可序列化、可解释、
可一键导出进 issue 的报告。

聚合维度（计划表 D2）：
- DSP backend（scipy/pyav/numpy）+ 输出设备：来自 AudioCapabilities。
- graph validation：来自 build_audio_graph_snapshot（环/未知目标/重复 ID）。
- sample rate / layout：运行态采样率与输出声道布局。
- disk：输出/缓存目录所在磁盘剩余空间（shutil.disk_usage）。
- cache：缓存目录大小与待清理状态（render_cache / scene_cut）。
- underrun：ring buffer underrun/overflow 计数（可选，运行态注入）。

线程安全：本模块只读探测，无共享可变状态；运行态字段（underrun）由调用方
在采集后注入，报告本身是不可变快照。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 磁盘空间探测
# ─────────────────────────────────────────────────────────────────────────────

# 磁盘剩余空间告警阈值（GB）。低于此值标记 warning（导出/录音可能失败）。
DISK_WARNING_THRESHOLD_GB: float = 5.0
# 磁盘剩余空间阻断阈值（GB）。低于此值标记 blocking（导出/录音几乎必失败）。
DISK_BLOCKING_THRESHOLD_GB: float = 1.0


def probe_disk_free_gb(path: str) -> Optional[float]:
    """探测 path 所在磁盘的剩余空间（GB）。

    Args:
        path: 任意路径（取所在挂载点）。不存在时退化到 cwd。

    Returns:
        剩余空间 GB，探测失败返回 None。
    """
    try:
        target = path or os.getcwd()
        # 确保目录存在（取父级），否则 disk_usage 抛错
        check_path = target if os.path.exists(target) else os.path.dirname(target)
        if not check_path or not os.path.exists(check_path):
            check_path = os.getcwd()
        usage = shutil.disk_usage(check_path)
        return usage.free / (1024.0 ** 3)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 缓存目录探测
# ─────────────────────────────────────────────────────────────────────────────

# AUD-15423: 同步 os.walk 整个大缓存目录会卡住调用线程（健康报告常在 UI
# 线程构建）。给遍历加时间预算：超时后记录告警并返回已统计的部分大小，
# 最坏阻塞时间有界。
DIR_SIZE_WALK_BUDGET_SECONDS: float = 5.0


def probe_dir_size_mb(
    path: str,
    max_seconds: float = DIR_SIZE_WALK_BUDGET_SECONDS,
) -> Optional[float]:
    """探测目录总大小（MB）。不存在返回 None。

    Args:
        path: 目标目录。
        max_seconds: 遍历时间预算（秒，AUD-15423）。超大目录超时后停止遍历，
            告警并返回部分统计，避免同步 walk 无界阻塞调用线程。
    """
    if not path or not os.path.isdir(path):
        return None
    deadline = time.monotonic() + max(0.0, float(max_seconds))
    total = 0
    timed_out = False
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError as e:
                    logger.debug(
                        "probe_dir_size_mb: stat failed for %s: %s",
                        os.path.join(root, f), e,
                    )
            if time.monotonic() >= deadline:
                timed_out = True
                break
    except OSError as e:
        logger.warning("probe_dir_size_mb: walk failed for %s: %s", path, e)
        return None
    if timed_out:
        logger.warning(
            "probe_dir_size_mb: walk budget (%.1fs) exceeded for %s; "
            "returning partial size", max_seconds, path,
        )
    return total / (1024.0 ** 2)


# ─────────────────────────────────────────────────────────────────────────────
# AudioHealthReport
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DiskStatus:
    """磁盘空间状态。"""
    path: str = ""
    free_gb: Optional[float] = None
    warning: bool = False
    blocking: bool = False
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "free_gb": round(self.free_gb, 2) if self.free_gb is not None else None,
            "warning": self.warning,
            "blocking": self.blocking,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CacheStatus:
    """缓存目录状态。"""
    path: str = ""
    size_mb: Optional[float] = None
    purgable: bool = False
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size_mb": round(self.size_mb, 2) if self.size_mb is not None else None,
            "purgable": self.purgable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AudioHealthReport:
    """音频健康报告（不可变快照，RMA-12）。

    Attributes:
        dsp_backend_available: numpy+pyav+scipy 是否齐全。
        eq_lufs_available: scipy（EQ/LUFS）是否可用。
        output_device_available: 是否有音频输出设备。
        disabled_components: 被旁路的组件名列表（DSP/设备）。
        graph_valid: 路由图是否有效（无环/无未知目标）。None=无 sequence 可校验。
        graph_errors: 路由图错误列表。
        sample_rate: 运行态采样率（Hz），None=未知。
        output_channels: 输出声道数，None=未知。
        disk: 输出/工程目录磁盘状态。
        caches: 缓存目录状态列表。
        underruns: ring buffer underrun 计数（运行态注入，None=未采集）。
        overflows: ring buffer overflow 计数（None=未采集）。
    """
    dsp_backend_available: bool = True
    eq_lufs_available: bool = True
    output_device_available: bool = True
    disabled_components: tuple = ()
    graph_valid: Optional[bool] = None
    graph_errors: tuple = ()
    sample_rate: Optional[int] = None
    output_channels: Optional[int] = None
    disk: DiskStatus = field(default_factory=DiskStatus)
    caches: tuple = ()
    underruns: Optional[int] = None
    overflows: Optional[int] = None

    @property
    def has_blocking(self) -> bool:
        """是否存在阻断性问题（不可继续导出/录音）。"""
        if not self.dsp_backend_available:
            # AUD-15420: numpy/pyav 缺失是阻断性的（音频链不可用）——注释如此
            # 断言而旧代码却是 `pass`：最严重降级场景下 has_blocking=False、
            # has_warnings=False，to_text 末行输出 "Status: OK"，与同报告的
            # "DSP backend : DEGRADED" 自相矛盾，preflight 不会阻止注定失败的
            # 导出/录音。scipy 缺失只降级 EQ/LUFS（走 has_warnings），不阻断。
            return True
        if self.disk.blocking:
            return True
        if self.graph_valid is False:
            return True
        return False

    @property
    def has_warnings(self) -> bool:
        """是否存在非阻断告警（应提示用户但不阻止操作）。"""
        if not self.eq_lufs_available:
            return True
        if not self.output_device_available:
            return True
        if self.disk.warning:
            return True
        if self.underruns and self.underruns > 0:
            return True
        if self.overflows and self.overflows > 0:
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "dsp_backend_available": self.dsp_backend_available,
            "eq_lufs_available": self.eq_lufs_available,
            "output_device_available": self.output_device_available,
            "disabled_components": list(self.disabled_components),
            "graph_valid": self.graph_valid,
            "graph_errors": list(self.graph_errors),
            "sample_rate": self.sample_rate,
            "output_channels": self.output_channels,
            "disk": self.disk.to_dict(),
            "caches": [c.to_dict() for c in self.caches],
            "underruns": self.underruns,
            "overflows": self.overflows,
            "has_blocking": self.has_blocking,
            "has_warnings": self.has_warnings,
        }

    def to_text(self) -> str:
        """人类可读的健康报告文本。"""
        lines = ["=== Audio Health Report ==="]
        lines.append(f"DSP backend     : {'OK' if self.dsp_backend_available else 'DEGRADED'}")
        lines.append(f"EQ/LUFS (scipy) : {'OK' if self.eq_lufs_available else 'UNAVAILABLE'}")
        lines.append(f"Output device   : {'OK' if self.output_device_available else 'NOT FOUND'}")
        if self.disabled_components:
            lines.append(f"Disabled        : {', '.join(self.disabled_components)}")
        if self.graph_valid is not None:
            lines.append(f"Routing graph   : {'valid' if self.graph_valid else 'INVALID'}")
            if self.graph_errors:
                for e in self.graph_errors:
                    lines.append(f"  - {e}")
        if self.sample_rate is not None:
            lines.append(f"Sample rate     : {self.sample_rate} Hz")
        if self.output_channels is not None:
            lines.append(f"Output channels : {self.output_channels}")
        if self.disk.free_gb is not None:
            tag = "BLOCKING" if self.disk.blocking else ("WARNING" if self.disk.warning else "OK")
            lines.append(f"Disk free       : {self.disk.free_gb:.1f} GB [{tag}] ({self.disk.path})")
        for c in self.caches:
            if c.size_mb is not None:
                lines.append(f"Cache           : {c.size_mb:.0f} MB ({c.path})"
                             + (" [purgable]" if c.purgable else ""))
        if self.underruns is not None and self.underruns > 0:
            lines.append(f"Underruns       : {self.underruns} (audio dropouts detected)")
        if self.overflows is not None and self.overflows > 0:
            lines.append(f"Overflows       : {self.overflows}")
        if self.has_blocking:
            lines.append("Status          : BLOCKING (resolve before export/record)")
        elif self.has_warnings:
            lines.append("Status          : WARNING (degraded but usable)")
        else:
            lines.append("Status          : OK")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 报告构建
# ─────────────────────────────────────────────────────────────────────────────

def build_audio_health_report(
    sequence=None,
    output_path: str = "",
    cache_dirs: Tuple[str, ...] = (),
    sample_rate: Optional[int] = None,
    output_channels: Optional[int] = None,
    underruns: Optional[int] = None,
    overflows: Optional[int] = None,
) -> AudioHealthReport:
    """构建音频健康报告（RMA-12）。

    Args:
        sequence: TimelineSequence（提供则校验路由图），None=跳过 graph 校验。
        output_path: 导出/工程输出路径（探测其磁盘剩余空间）。
        cache_dirs: 缓存目录路径列表（探测大小 + purgable 标记）。
        sample_rate: 运行态采样率。
        output_channels: 输出声道数。
        underruns/overflows: ring buffer 运行态计数（可选）。

    Returns:
        不可变 AudioHealthReport。
    """
    from src.features.edit.core.audio_capabilities import (
        get_audio_capabilities,
        reset_audio_capabilities_cache,
    )

    # AUD-15421: 健康报告需要**真实**的输出设备探测结果。get_audio_capabilities
    # 的缓存语义是"check_output_device 仅首次探测生效"——若进程内任何 DSP 热路径
    # 已用默认 check_output_device=False 填充缓存，直接读取会拿到
    # CapabilityResult(True, "跳过探测") 的占位结果，在无输出设备的机器上
    # 误报 "Output device : OK"。报告是低频诊断路径，先清缓存再以
    # check_output_device=True 强制重新探测（探测本身轻量幂等）。
    reset_audio_capabilities_cache()
    caps = get_audio_capabilities(check_output_device=True)

    # graph validation（B1 接入的校验）
    graph_valid: Optional[bool] = None
    graph_errors: tuple = ()
    if sequence is not None:
        try:
            from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
            snap = build_audio_graph_snapshot(sequence)
            graph_valid = snap.is_valid
            graph_errors = snap.validation_errors
        except Exception as e:
            graph_valid = False
            graph_errors = (f"graph validation failed: {e}",)

    # disk
    free_gb = probe_disk_free_gb(output_path) if output_path else probe_disk_free_gb(os.getcwd())
    disk_warning = free_gb is not None and free_gb < DISK_WARNING_THRESHOLD_GB
    disk_blocking = free_gb is not None and free_gb < DISK_BLOCKING_THRESHOLD_GB
    disk_reason = None
    if disk_blocking:
        disk_reason = f"磁盘剩余 {free_gb:.1f} GB < {DISK_BLOCKING_THRESHOLD_GB} GB，导出/录音几乎必失败"
    elif disk_warning:
        disk_reason = f"磁盘剩余 {free_gb:.1f} GB < {DISK_WARNING_THRESHOLD_GB} GB，空间紧张"
    disk = DiskStatus(
        path=output_path or os.getcwd(),
        free_gb=free_gb,
        warning=disk_warning,
        blocking=disk_blocking,
        reason=disk_reason,
    )

    # caches
    cache_statuses = []
    for cd in cache_dirs:
        size_mb = probe_dir_size_mb(cd)
        cache_statuses.append(CacheStatus(
            path=cd, size_mb=size_mb,
            purgable=size_mb is not None and size_mb > 0,
        ))

    return AudioHealthReport(
        dsp_backend_available=caps.all_critical_available,
        eq_lufs_available=caps.eq_lufs_available,
        output_device_available=caps.output_device.available,
        disabled_components=tuple(caps.disabled_components()),
        graph_valid=graph_valid,
        graph_errors=graph_errors,
        sample_rate=sample_rate,
        output_channels=output_channels,
        disk=disk,
        caches=tuple(cache_statuses),
        underruns=underruns,
        overflows=overflows,
    )
