"""F15 并排比较：2–4 张受支持图片，同步缩放与滚动。"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C


class CompareDialog(QDialog):
    images_requested = pyqtSignal(list)

    def __init__(self, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = list(paths)[:4]
        self._images: dict[str, QImage] = {}
        self._labels: dict[str, QLabel] = {}
        self._zoom = 1.0
        self.setWindowTitle("并排比较")
        self.resize(1100, 720)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        controls = QHBoxLayout()
        self.summary = QLabel(f"{len(self.paths)} 张图片 · 同步缩放/滚动", self)
        self.summary.setObjectName("filesHint")
        controls.addWidget(self.summary, 1)
        for label, delta in (("－", -0.25), ("＋", 0.25)):
            button = QPushButton(label, self)
            button.setFixedWidth(36)
            button.clicked.connect(lambda _=False, d=delta: self._change_zoom(d))
            controls.addWidget(button)
        fit = QPushButton("适应", self)
        fit.clicked.connect(self._fit)
        controls.addWidget(fit)
        actual = QPushButton("100%", self)
        actual.clicked.connect(lambda: self._set_zoom(1.0))
        controls.addWidget(actual)
        close = QPushButton("关闭", self)
        close.clicked.connect(self.reject)
        controls.addWidget(close)
        root.addLayout(controls)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.canvas = QWidget(self.scroll)
        self.row = QHBoxLayout(self.canvas)
        self.row.setContentsMargins(4, 4, 4, 4)
        self.row.setSpacing(8)
        self.scroll.setWidget(self.canvas)
        root.addWidget(self.scroll, 1)
        for path in self.paths:
            tile = QFrame(self.canvas)
            tile.setObjectName("compareTile")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(4, 4, 4, 4)
            view = QLabel("正在读取…", tile)
            view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            view.setMinimumSize(260, 220)
            view.setStyleSheet(f"background: {C.BG_INPUT_DARK}; border-radius: 6px;")
            tile_layout.addWidget(view, 1)
            name = QLabel(path.rsplit("/", 1)[-1], tile)
            name.setObjectName("filesHint")
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tile_layout.addWidget(name)
            self._labels[path] = view
            self.row.addWidget(tile, 1)
        self.setStyleSheet(f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.images_requested.emit(list(self.paths))

    def _change_zoom(self, delta: float) -> None:
        self._set_zoom(self._zoom + delta)

    def _set_zoom(self, value: float) -> None:
        self._zoom = max(0.25, min(4.0, value))
        self._repaint()
        self.summary.setText(f"{len(self.paths)} 张图片 · 缩放 {int(self._zoom * 100)}% · 同步滚动")

    def _fit(self) -> None:
        if not self._images:
            return
        width = max(1, (self.scroll.viewport().width() - 40) // max(1, len(self._images)))
        first = next(iter(self._images.values()))
        if first.width() <= 0:
            return
        self._set_zoom(width / first.width())

    def _repaint(self) -> None:
        for path, image in self._images.items():
            label = self._labels.get(path)
            if label is None or image.isNull():
                continue
            pixmap = QPixmap.fromImage(image).scaled(
                max(1, int(image.width() * self._zoom)),
                max(1, int(image.height() * self._zoom)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            label.setPixmap(pixmap)
            label.resize(pixmap.size())

    def set_image(self, path: str, data: bytes) -> None:
        if path not in self._labels:
            return
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        image = reader.read()
        buffer.deleteLater()
        if image.isNull():
            self.set_error(path, reader.errorString() or "不支持的图片格式")
            return
        self._images[path] = image
        self._repaint()

    def set_error(self, path: str, message: str) -> None:
        label = self._labels.get(path)
        if label is not None:
            label.setText(str(message))
