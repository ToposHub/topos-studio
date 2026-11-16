"""F15 并排比较：2–4 张受支持图片，同步缩放与滚动。

[FR-15] 会话与资源安全：
* 每个窗口持有唯一 session_id，与 transport 的会话化队列配对——
  多窗口互不串台，关闭即取消该会话在途请求；
* 解码在独立 QThreadPool（每窗口一个），GUI 线程只贴图；
* 像素预算：单图解码上限 ``MAX_DECODE_PIXELS``，显示用 QPixmap 上限
  ``MAX_PIXMAP_PIXELS``——不再按 4× 放大预生成巨图（4 张 7K RGBA ×16
  像素面积 ≈ 784MB 的路径已封死）；
* 「适应」同时考虑**所有**图片的宽与高，且不受 25% 手动缩放下限约束
  （大图也能真正 fit）。
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from PyQt6.QtCore import (
    QBuffer, QByteArray, QIODevice, QObject, QRunnable, QSize, Qt,
    QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import QImage, QImageReader, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C

MAX_DECODE_PIXELS = 8192          # 与预览/缩略图解码同源的边长上限
MAX_PIXMAP_PIXELS = 16 * 1024 * 1024  # 单格显示位图预算（约 4K×4K）


class _DecodeSignals(QObject):
    finished = pyqtSignal(str, object, str)


class _CompareDecodeJob(QRunnable):
    """后台解码一张比较图；超预算先按等比缩小再解码。"""

    def __init__(self, path: str, data: bytes) -> None:
        super().__init__()
        self.path = path
        self.data = data
        self.signals = _DecodeSignals()
        self.setAutoDelete(False)

    def run(self) -> None:
        buffer = QBuffer()
        buffer.setData(QByteArray(self.data))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self.signals.finished.emit(self.path, QImage(), "无法读取数据")
            return
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and (size.width() > MAX_DECODE_PIXELS
                                   or size.height() > MAX_DECODE_PIXELS):
                reader.setScaledSize(size.scaled(
                    QSize(MAX_DECODE_PIXELS, MAX_DECODE_PIXELS),
                    Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if image.isNull():
                self.signals.finished.emit(
                    self.path, QImage(), reader.errorString() or "不支持的图片格式")
            else:
                self.signals.finished.emit(self.path, image, "")
        finally:
            buffer.close()


class CompareDialog(QDialog):
    images_requested = pyqtSignal(list, str)   # paths, session_id

    def __init__(self, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = list(paths)[:4]
        self.session_id = uuid.uuid4().hex
        self._images: dict[str, QImage] = {}
        self._labels: dict[str, QLabel] = {}
        self._zoom = 1.0
        self._closed = False
        self._decode_pool = QThreadPool(self)
        self._decode_pool.setMaxThreadCount(2)
        self._decode_generation = 0
        self._teardown_generation = 2 ** 31  # teardown 后压到当前值以下即作废
        self._decode_jobs: dict[int, _CompareDecodeJob] = {}
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

    # -- 会话生命周期 -------------------------------------------------------
    def reject(self) -> None:
        self._teardown()
        super().reject()

    def closeEvent(self, event: Any) -> None:
        self._teardown()
        super().closeEvent(event)

    def _teardown(self) -> None:
        """[FR-15/17] 关闭：作废解码结果、取消剩余任务；transport 侧由宿主取消。"""
        if self._closed:
            return
        self._closed = True
        self._teardown_generation = self._decode_generation
        for job in list(self._decode_jobs.values()):
            self._decode_pool.tryTake(job)
        self._decode_jobs.clear()
        self._decode_pool.clear()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self._closed:
            self.images_requested.emit(list(self.paths), self.session_id)

    # -- 缩放 ---------------------------------------------------------------
    def _change_zoom(self, delta: float) -> None:
        self._set_zoom(self._zoom + delta)

    def _set_zoom(self, value: float, *, clamp: bool = True) -> None:
        self._zoom = max(0.25, min(4.0, value)) if clamp else max(0.01, value)
        self._repaint()
        self.summary.setText(
            f"{len(self.paths)} 张图片 · 缩放 {int(self._zoom * 100)}% · 同步滚动")

    def _fit(self) -> None:
        """[FR-15] 同时考虑所有图片的宽与高；不受手动缩放下限约束。"""
        if not self._images:
            return
        count = max(1, len(self._images))
        tile_width = max(1.0, (self.scroll.viewport().width() - 40 - 8 * count) / count)
        avail_height = max(1.0, self.scroll.viewport().height() - 40)
        zoom = 4.0
        for image in self._images.values():
            if image.width() <= 0 or image.height() <= 0:
                continue
            zoom = min(zoom, tile_width / image.width(),
                       avail_height / image.height())
        if zoom < 4.0:
            self._set_zoom(zoom, clamp=False)

    def _repaint(self) -> None:
        for path, image in self._images.items():
            label = self._labels.get(path)
            if label is None or image.isNull():
                continue
            target_w = max(1, int(image.width() * self._zoom))
            target_h = max(1, int(image.height() * self._zoom))
            # [FR-15] 像素预算：超预算的放大档按比例折减，不预生成巨图
            pixels = target_w * target_h
            if pixels > MAX_PIXMAP_PIXELS:
                scale = (MAX_PIXMAP_PIXELS / pixels) ** 0.5
                target_w = max(1, int(target_w * scale))
                target_h = max(1, int(target_h * scale))
            pixmap = QPixmap.fromImage(image).scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            label.setPixmap(pixmap)
            label.resize(pixmap.size())

    # -- 数据入口（transport 携带 session；他窗结果直接忽略） ----------------
    def set_image(self, path: str, data: bytes, session_id: str = "") -> None:
        if self._closed or (session_id and session_id != self.session_id):
            return
        if path not in self._labels:
            return
        self._decode_generation += 1  # 每张图独立世代；teardown 时批量作废
        generation = self._decode_generation
        job = _CompareDecodeJob(path, data)
        job.signals.finished.connect(
            lambda p, image, error, g=generation: self._on_decoded(g, p, image, error))
        self._decode_jobs[generation] = job
        self._decode_pool.start(job)

    def _on_decoded(self, generation: int, path: str,
                    image: QImage, error: str) -> None:
        self._decode_jobs.pop(generation, None)
        if self._closed or generation > self._teardown_generation:
            return  # teardown 之后的迟到结果：丢弃
        if image.isNull():
            self.set_error(path, error or "不支持的图片格式")
            return
        self._images[path] = image
        self._repaint()

    def set_error(self, path: str, message: str, session_id: str = "") -> None:
        if self._closed or (session_id and session_id != self.session_id):
            return
        label = self._labels.get(path)
        if label is not None:
            label.setText(str(message))
