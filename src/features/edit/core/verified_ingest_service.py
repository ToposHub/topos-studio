"""N08 · verified ingest 生产入口（可取消 worker + 逐项报告 + 统一入库）。

审计缺口（2026-10-25 §2 N08）：``ingest_files``/``verified_ingest`` 无
UI/service 消费——helper 完成不等于功能闭环。本模块提供生产入口：

- ``VerifiedIngestService``（QObject）：可取消后台 worker 跑
  ``ingest_files``，逐项 ``item_done`` 信号（UI 线程）+ 终态
  ``finished(IngestReport)``；worker 保活注册表防 GC 销毁运行中线程；
- ``ImportIngestAssetsCommand``：把报告的 **verified** 项统一入工程
  （复用 ``EditProject.import_asset`` 的去重/元数据探测语义，不另造
  拷贝旁路）——一次可撤销；宿主经正典链（ProjectCommandStack）提交
  （N24 adapter 口径）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.features.edit.core.verified_ingest import (
    IngestReport, ingest_files)

logger = logging.getLogger(__name__)

#: worker 保活注册表（同 N12 模式：线程在跑期间持 Python 强引用）
_ingest_worker_keepalive: set = set()


class _IngestWorker(QThread):
    """ingest_files 的后台执行体（拷贝/hash 均在 worker 线程）。

    逐项结果经 ``item_done`` 信号发射（worker 线程 emit → Qt 排队
    投递到接收线程——UI 消费零跨线程控件访问）。
    """

    item_done = pyqtSignal(object)
    finished_with_report = pyqtSignal(object)

    def __init__(self, sources: List[str], dest_root: str,
                 relative_to: Optional[str], parent=None) -> None:
        super().__init__(parent)
        self._sources = list(sources)
        self._dest_root = dest_root
        self._relative_to = relative_to
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # pragma: no cover — 线程体
        report = ingest_files(
            self._sources, self._dest_root,
            relative_to=self._relative_to,
            cancelled_fn=self.is_cancelled,
            on_item=lambda item: self.item_done.emit(item))
        self.finished_with_report.emit(report)


class VerifiedIngestService(QObject):
    """受控校验拷贝的生产入口（N08）。"""

    #: 逐项结果（UI 线程；worker 经排队信号转发）
    item_done = pyqtSignal(object)
    #: 终态报告（UI 线程；取消/部分失败如实呈现）
    finished = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._worker: Optional[_IngestWorker] = None

    @property
    def running(self) -> bool:
        worker = self._worker
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # deleteLater 已清（线程真结束）——语义上不在跑
            return False

    def start(self, sources: List[str], dest_root: str, *,
              relative_to: Optional[str] = None) -> bool:
        """启动一批受控拷贝（在途时拒绝新批——UI 应先等/取消）。"""
        if self.running:
            logger.warning("[Ingest] batch already running — start refused")
            return False
        if not sources:
            return False
        worker = _IngestWorker(sources, dest_root, relative_to)
        self._worker = worker
        _ingest_worker_keepalive.add(worker)
        worker.finished.connect(lambda w=worker:
                                (_ingest_worker_keepalive.discard(w),
                                 w.deleteLater()))
        worker.item_done.connect(self.item_done)
        worker.finished_with_report.connect(self._on_report)
        worker.start()
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def _on_report(self, report: IngestReport) -> None:
        self.finished.emit(report)

    # ── 统一入库（命令由宿主经正典链提交） ──

    @staticmethod
    def build_registration_command(report: IngestReport) -> "ImportIngestAssetsCommand":
        """verified 项 → 工程入库命令（一次可撤销；宿主走命令栈提交）。"""
        return ImportIngestAssetsCommand.create(report)


@dataclass(frozen=True)
class ImportIngestAssetsCommand:
    """verified 项统一入库（N08）。

    apply 复用 ``EditProject.import_asset``（路径去重/媒体类型/元数据
    探测/颜色元数据语义与媒体池导入一致，不另造旁路）；undo 还原
    apply 前的完整工程快照字段（assets/bins 及关联表）。
    """

    command_id: str
    label: str
    targets: tuple  # verified 项的 target 路径
    _before: Any = None  # apply 期捕获（undo 用）

    def apply(self, project: Any) -> Any:
        # 复用媒体池导入同源语义：类型/探测来自 core.project，颜色元
        # 数据提取复用媒体池实现（同函数，无旁路）。
        from src.features.edit.core.project import (
            get_media_type, probe_media_metadata,
        )
        from src.features.edit.ui.media_pool_panel import (
            _extract_color_metadata,
        )
        before = project
        new_project = project
        for path in self.targets:
            try:
                media_type = get_media_type(path)
                metadata = probe_media_metadata(path)
                candidate = new_project.import_asset(
                    path=path, media_type=media_type, metadata=metadata,
                    color_metadata=_extract_color_metadata(metadata))
                if candidate is not new_project:
                    new_project = candidate
            except Exception:  # noqa: BLE001 — 单项失败不阻断其余入库
                logger.warning("[Ingest] register asset failed: %s",
                               path, exc_info=True)
        object.__setattr__(self, "_before", before)
        return new_project

    def undo(self, project: Any) -> Any:
        if self._before is None:
            raise ValueError("命令未 apply，无法撤销。")
        return self._before

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "command_type": "ImportIngestAssetsCommand",
            "label": self.label,
            "targets": list(self.targets),
        }

    @staticmethod
    def create(report: IngestReport) -> "ImportIngestAssetsCommand":
        targets = tuple(r.target for r in report.results
                        if r.status == "verified" and r.target)
        return ImportIngestAssetsCommand(
            command_id=str(uuid.uuid4()),
            label=f"Import {len(targets)} verified asset(s)",
            targets=targets)


__all__ = ["VerifiedIngestService", "ImportIngestAssetsCommand"]
