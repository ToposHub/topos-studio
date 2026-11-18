"""G6：image_editor 独立编辑工具窗口（预览=导出/undo/安全输出）。"""
from __future__ import annotations

from typing import Any, Callable
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from src.features.files.image_editor_service import (
    ImageEditorError,
    ImageEditorService,
    OPERATIONS,
)
from src.shared.ui.style_sheet import C

_OP_LABELS = {"rotate-90": "左转 90°", "rotate-270": "右转 90°",
              "flip-h": "水平翻转", "flip-v": "垂直翻转",
              "brightness+10": "调亮 +10", "brightness-10": "调暗 -10"}


def _pil_to_qimage(image: Any) -> QImage:
    import numpy as np

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode or
                              image.mode in ("P", "LA") else "RGB")
    array = np.asarray(image)
    height, width, channels = array.shape
    fmt = QImage.Format.Format_RGBA8888 if channels == 4 else QImage.Format.Format_RGB888
    out = QImage(array.data, width, height, width * channels, fmt)
    return out.copy()   # 脱离 numpy 缓冲生命周期


class _TaskSignals(QObject):
    done = pyqtSignal(object, str)


class _ImageTask(QRunnable):
    def __init__(self, work: Callable[[], Any]) -> None:
        super().__init__()
        self.work = work
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.work(), "")
        except (ImageEditorError, OSError, ValueError) as exc:
            self.signals.done.emit(None, str(exc))


class ImageEditorWindow(QDialog):
    """图片编辑器：操作栈 → 预览（=导出结果）→ 安全输出。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *,
                 service: ImageEditorService) -> None:
        super().__init__(parent)
        self._service = service
        self._task: _ImageTask | None = None
        self._closed = False
        self._exporting = False
        self._buttons: list[QPushButton] = []
        self.setWindowTitle("图片编辑器")
        self.resize(720, 520)
        self.setStyleSheet(
            f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")
        root = QVBoxLayout(self)
        self.source_label = QLabel(f"源：{service.source_path}（只读——导出为副本）")
        root.addWidget(self.source_label)
        self.preview = QLabel("（预览）")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(1, 1)
        root.addWidget(self.preview, 1)
        self.notice = QLabel("")
        root.addWidget(self.notice)
        ops = QHBoxLayout()
        for op in OPERATIONS:
            button = QPushButton(_OP_LABELS.get(op, op))
            button.clicked.connect(lambda _=False, o=op: self._apply(o))
            self._buttons.append(button)
            ops.addWidget(button)
        self.undo_button = QPushButton("撤销")
        self.undo_button.clicked.connect(self._undo)
        ops.addWidget(self.undo_button)
        self.export_button = QPushButton("导出副本…")
        self.export_button.setObjectName("pluginPrimary")
        self.export_button.clicked.connect(self._export)
        ops.addWidget(self.export_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        ops.addWidget(close)
        root.addLayout(ops)
        self._buttons.extend((self.undo_button, self.export_button))
        self._refresh()

    # ------------------------------------------------------------ 操作
    def _apply(self, operation: str) -> None:
        if self._task:
            return
        self._service.apply(operation)
        self._refresh()

    def _undo(self) -> None:
        if self._task:
            return
        self._service.undo()
        self._refresh()

    def _refresh(self) -> None:
        self._start(lambda: _pil_to_qimage(self._service.render()), exporting=False)

    def _start(self, work: Callable[[], Any], *, exporting: bool) -> None:
        if self._task or self._closed:
            return
        self._exporting = exporting
        self._task = _ImageTask(work)
        self._task.signals.done.connect(self._finished)
        for button in self._buttons:
            button.setEnabled(False)
        self.notice.setText("正在导出…" if exporting else "正在处理图片…")
        QThreadPool.globalInstance().start(self._task)

    def _finished(self, result: Any, error: str) -> None:
        self._task = None
        if self._closed:
            return
        for button in self._buttons:
            button.setEnabled(True)
        if error:
            self.notice.setText(error)
            return
        if self._exporting:
            self.notice.setText(f"已导出：{result}")
            return
        image = result
        pixmap = QPixmap.fromImage(image).scaled(
            max(1, self.preview.width()), max(1, self.preview.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pixmap)
        count = len(self._service.state.operations)
        self.notice.setText(f"操作 {count} 步（可撤销）")

    def _export(self) -> None:
        self._start(self._service.export, exporting=True)

    def closeEvent(self, event: Any) -> None:
        self._closed = True
        self._service.cancel()
        self.closed.emit()
        super().closeEvent(event)
