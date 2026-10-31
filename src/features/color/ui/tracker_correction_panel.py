"""M23.1b · Tracker 修正/增量重跟 UI 接线件。

接 M23.1 的 tracker_corrections 领域服务（TrackerNode 委托方法）：
帧号修正输入（apply_manual_correction）+ 失败段列表与逐段重跟按钮
（plan_incremental_retrack / merge_retrack_results）。跟踪引擎回调由
控制器注入（retrack_fn(segment) → new_transforms）。

N13 修复（审计 2026-10-25 §2 N13）：

- **Undo/持久化刷新链**：手工修正与重跟合并此前直接改 node 无人可撤
  销——现经 ``undo_sink(description, undo_fn, redo_fn)`` 注册到页面
  UndoStack（控制器注入；track_data 存节点参数=持久化随工程往返，
  ``on_changed`` 负责视图刷新）；
- **异步可取消重跟**：同步重跟移 ``_RetrackWorker``（QThread），UI
  不冻结；取消协作中止（部分结果不合并）；完成合并/undo 在 UI 线程
  （信号槽）；关闭对话框先取消并等在途（保活防半途销毁）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

UndoSink = Callable[[str, Callable[[], None], Callable[[], None]], None]


class _RetrackWorker(QThread):
    """一段重跟的后台执行体（引擎回调在 worker 线程运行）。

    回调契约为 ``retrack_fn(segment, is_cancelled)``——引擎在帧循环
    轮询 is_cancelled 实现协作取消（N13）。
    """

    finished_with_result = pyqtSignal(object, str)

    def __init__(self, retrack_fn, segment, parent=None) -> None:
        super().__init__(parent)
        self._retrack_fn = retrack_fn
        self._segment = segment
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # pragma: no cover — 线程体
        try:
            result = self._retrack_fn(self._segment, self.is_cancelled)
            error = ""
        except Exception as exc:  # noqa: BLE001 — 引擎失败明示
            result, error = None, str(exc)
        self.finished_with_result.emit(result, error)


class TrackerCorrectionPanel(QDialog):
    """Tracker 手工修正与失败段增量重跟。"""

    def __init__(
        self,
        tracker_node: Any,
        *,
        retrack_fn: Optional[Callable[[Any], dict]] = None,
        on_changed: Optional[Callable[[], None]] = None,
        undo_sink: Optional[UndoSink] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._node = tracker_node
        self._retrack_fn = retrack_fn
        self._on_changed = on_changed
        self._undo_sink = undo_sink
        self._worker: Optional[_RetrackWorker] = None
        self._retrack_queue: list = []

        self.setWindowTitle(I18N.tr("tracker_correction.title", "跟踪修正与增量重跟"))
        layout = QVBoxLayout(self)
        self._status = QLabel()
        layout.addWidget(self._status)

        btn_fix = QPushButton(I18N.tr(
            "tracker_correction.manual_fix", "在当前帧手工修正…"))
        btn_fix.clicked.connect(self._on_manual_fix)
        layout.addWidget(btn_fix)

        layout.addWidget(QLabel(I18N.tr(
            "tracker_correction.failed_segments", "失败段（增量重跟——只跑失败帧）：")))
        self._segments = QListWidget()
        layout.addWidget(self._segments, 1)
        btn_retrack = QPushButton(I18N.tr(
            "tracker_correction.retrack_selected", "重跟选中段"))
        btn_retrack.clicked.connect(self._on_retrack)
        layout.addWidget(btn_retrack)
        btn_cancel = QPushButton(I18N.tr(
            "tracker_correction.cancel_retrack", "取消重跟"))
        btn_cancel.clicked.connect(self._cancel_retrack)
        btn_cancel.setEnabled(False)
        self._cancel_btn = btn_cancel
        layout.addWidget(btn_cancel)
        btn_retrack_all = QPushButton(I18N.tr(
            "tracker_correction.retrack_all", "全部重跟"))
        btn_retrack_all.clicked.connect(lambda: self._retrack_all())
        layout.addWidget(btn_retrack_all)

        self._refresh()

    # ── Undo 注册（N13）──

    def _register_undo(self, description: str) -> None:
        """把一次 track_data 变更注册为可撤销动作（捕获前后快照）。"""
        if self._undo_sink is None:
            return
        after = self._node.get_track_data()
        node = self._node
        try:
            before = self._pending_before
        except AttributeError:
            return
        del self._pending_before
        if before == after:
            return

        def _undo() -> None:
            node.set_track_data(before)

        def _redo() -> None:
            node.set_track_data(after)

        self._undo_sink(description, _undo, _redo)

    def _snapshot_before(self) -> None:
        self._pending_before = self._node.get_track_data()

    def _refresh(self) -> None:
        plan = self._node.plan_incremental_retrack()
        corrections = self._node.get_manual_corrections()
        self._segments.clear()
        for seg in plan.segments:
            item = QListWidgetItem(
                f"帧 {seg.start_frame}–{seg.end_frame}"
                f"（{seg.end_frame - seg.start_frame + 1} 帧，"
                f"锚@{seg.anchor_frame}）")
            item.setData(0x0100, seg)
            self._segments.addItem(item)
        total = sum(s.end_frame - s.start_frame + 1 for s in plan.segments)
        self._status.setText(
            f"手工修正 {len(corrections)} 帧｜失败段 {len(plan.segments)} 个"
            f"（共 {total} 帧待重跟）——修正帧作为锚不重跟")

    def _on_manual_fix(self) -> None:
        frame, ok = QInputDialog.getInt(
            self, "修正帧", "帧号：", min=0)
        if not ok:
            return
        tx, ok = QInputDialog.getDouble(self, "修正 TX", "TX（像素）：", 0.0)
        if not ok:
            return
        ty, ok = QInputDialog.getDouble(self, "修正 TY", "TY（像素）：", 0.0)
        if not ok:
            return
        self._snapshot_before()
        self._node.apply_manual_correction(frame, {"tx": tx, "ty": ty})
        self._register_undo(f"Tracker 手工修正 帧{frame}")
        if self._on_changed:
            self._on_changed()
        self._refresh()

    # ── 异步重跟（N13：可取消，合并/Undo 在 UI 线程）──

    def _start_retrack(self, segment) -> bool:
        if self._worker is not None and self._worker.isRunning():
            self._status.setText(I18N.tr(
                "tracker_correction.retrack_busy", "重跟进行中——先取消或等待完成"))
            return False
        if self._retrack_fn is None or segment is None:
            self._status.setText(I18N.tr(
                "tracker_correction.no_engine", "未提供跟踪引擎回调——无法重跟"))
            return False
        self._cancel_btn.setEnabled(True)
        self._status.setText(f"重跟 帧 {segment.start_frame}–"
                             f"{segment.end_frame}…（可取消）")
        self._worker = _RetrackWorker(self._retrack_fn, segment, self)
        self._worker.finished_with_result.connect(self._on_retrack_result)
        self._worker.start()
        return True

    def _cancel_retrack(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        self._retrack_queue.clear()
        self._status.setText(I18N.tr(
            "tracker_correction.cancel_requested",
            "已请求取消（当前帧完成后中止，部分结果不合并）…"))

    def _on_retrack_result(self, new_transforms, error: str) -> None:
        self._cancel_btn.setEnabled(False)
        if self._worker is not None:
            self._worker.wait(5000)
        if error:
            self._retrack_queue.clear()
            self._status.setText(f"重跟失败：{error}")
            return
        if not new_transforms:
            self._status.setText(I18N.tr(
                "tracker_correction.no_result", "重跟无结果（段未变化）"))
            return
        self._snapshot_before()
        self._node.merge_retrack_results(new_transforms)
        self._register_undo("Tracker 增量重跟合并")
        if self._on_changed:
            self._on_changed()
        self._refresh()
        self._status.setText(self._status.text() + "——已合并（可撤销）")
        # 全部重跟：完成一段续下一段（单 worker 顺序）
        if self._retrack_queue:
            nxt = self._retrack_queue.pop(0)
            self._start_retrack(nxt)

    def _on_retrack(self) -> None:
        item = self._segments.currentItem()
        self._start_retrack(item.data(0x0100) if item else None)

    def _retrack_all(self) -> None:
        plan = self._node.plan_incremental_retrack()
        if not plan.segments:
            return
        # 顺序逐段：启动首段，完成后在结果槽里续段（保持单 worker）
        self._retrack_queue = list(plan.segments)
        self._start_retrack(self._retrack_queue.pop(0))

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt 约定
        # N13：关闭前取消并等在途 worker（保活语义：超时拒绝关闭）
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            if not self._worker.wait(10000):
                event.ignore()
                self._status.setText(I18N.tr(
                    "tracker_correction.still_running",
                    "重跟仍在途——完成后关闭（防半途销毁）"))
                return
        event.accept()


__all__ = ["TrackerCorrectionPanel"]
