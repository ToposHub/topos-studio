"""Media Management 后台 worker（P2-2）——收集/整理移出 UI 线程。

现状缺口（复用并核验结论）：``collect_project`` / ``consolidate_project``
核心已支持 progress_callback / cancelled_fn / 分段超时，但媒体池面板此前
在 **UI 线程同步调用**且未接进度/取消；也没有磁盘空间预检。本模块补齐：

- :class:`MediaManagementWorker`（QRunnable）：后台执行收集或整理，
  信号回主线程（progress/finished/failed/cancelled）；取消走核心的
  协作取消（收集=素材级部分结果+警告；整理=分段边界零半成品）。
- :func:`estimate_collect_bytes` / :func:`estimate_consolidate_bytes`：
  启动前空间预检的保守估算（收集=待复制文件总和；整理=被引用源文件
  总和上界——实际分段更小，宁可误拒不写满磁盘）。

UI 侧约定：worker 运行期间入口置灰；完成/失败/取消都在主线程信号里
收尾，不在回调里直接弹模态框以外的工程写操作。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Optional

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from src.shared.media.disk_proxy_service import check_free_space

__all__ = [
    "KIND_COLLECT",
    "KIND_CONSOLIDATE",
    "MediaManagementWorker",
    "estimate_collect_bytes",
    "estimate_consolidate_bytes",
    "preflight_disk_space",
]

KIND_COLLECT = "collect"
KIND_CONSOLIDATE = "consolidate"


# ================================================================
# 空间预检
# ================================================================

def _existing_files_size(paths: Iterable[str]) -> int:
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError:
            continue  # 缺失素材由核心流程报告，不阻塞预检
    return total


def _asset_paths(project: Any, asset_ids: Optional[Iterable[str]] = None) -> list[str]:
    assets = getattr(project, "assets", None) or {}
    if asset_ids is None:
        return [getattr(a, "path", "") for a in assets.values()]
    return [getattr(assets.get(aid), "path", "") for aid in asset_ids]


def estimate_collect_bytes(project: Any, *,
                           include_unused: bool = True,
                           sequences: Iterable[Any] = ()) -> int:
    """收集空间估算：待复制媒体文件字节和（保守，缺失文件不计）。"""
    if include_unused:
        return _existing_files_size(_asset_paths(project))
    from src.features.edit.core.asset_usage_index import build_asset_usage_index
    index = build_asset_usage_index(sequences, project)
    used = set(index.used_asset_ids())
    return _existing_files_size(_asset_paths(project, used))


def estimate_consolidate_bytes(project: Any,
                               sequences: Iterable[Any]) -> int:
    """整理空间估算：被引用源文件总和上界（实际只写使用区间+handles，
    分段更小——保守预留，宁可误拒）。"""
    from src.features.edit.core.asset_usage_index import build_asset_usage_index
    index = build_asset_usage_index(sequences, project)
    used = set(index.used_asset_ids())
    return _existing_files_size(_asset_paths(project, used))


def preflight_disk_space(target_dir: str, required_bytes: int) -> tuple[bool, int]:
    """目标目录空间预检（复用 shared 磁盘探测；探测失败 fail-open）。"""
    return check_free_space(target_dir, required_bytes)


# ================================================================
# 后台 worker
# ================================================================

class _MediaManagementSignals(QObject):
    """QRunnable 不能自带 Qt 信号，经独立 signals 对象发射。"""

    progress = pyqtSignal(str, int, int)      # kind, done, total
    finished = pyqtSignal(str, object, object)  # kind, payload, report
    failed = pyqtSignal(str, str)             # kind, message
    cancelled = pyqtSignal(str)               # kind


class MediaManagementWorker(QRunnable):
    """后台执行 collect_project / consolidate_project。

    Args:
        kind: ``collect`` | ``consolidate``（信号载荷标识）。
        work: ``lambda progress_cb, cancel_check: (payload, report)``——
            由调用方绑定核心函数与其参数（worker 不重复封装参数面）。
    """

    def __init__(self, kind: str, work: Callable[..., tuple[Any, Any]]) -> None:
        super().__init__()
        self._kind = kind
        self._work = work
        self._cancelled = False
        self.signals = _MediaManagementSignals()

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # QThreadPool 工作线程
        def _progress(*args: Any) -> None:
            # 核心回调签名：(done, total) 或 ConsolidateProgress
            if len(args) == 2:
                done, total = int(args[0]), int(args[1])
            else:
                progress = args[0] if args else None
                done = int(getattr(progress, "completed_segments", 0) or 0)
                total = int(getattr(progress, "total_segments", 0) or 0)
            self.signals.progress.emit(self._kind, done, total)

        def _cancel_check() -> bool:
            return self._cancelled

        try:
            payload, report = self._work(_progress, _cancel_check)
        except Exception as exc:  # noqa: BLE001 — 失败经信号回主线程
            name = type(exc).__name__
            if "Cancelled" in name:
                # 核心的协作取消异常（ConsolidateCancelled / 收集取消约定）
                self.signals.cancelled.emit(self._kind)
                return
            self.signals.failed.emit(self._kind, str(exc))
            return
        if self._cancelled:
            self.signals.cancelled.emit(self._kind)
            return
        self.signals.finished.emit(self._kind, payload, report)
