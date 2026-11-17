"""F15 并排比较：2–4 张受支持图片，同步缩放与滚动。

[FR-15] 会话与资源安全：
* 每个窗口持有唯一 session_id，与 transport 的会话化队列配对——
  多窗口互不串台，关闭即取消该会话在途请求；
* [E1/E2] 解码走共享 ``ImageDecodeRuntime``（预览/比较窗口统一并发
  与总字节/像素预算）；窗口只持有租约，销毁不牵连线程池，也不本地
  持有 job 引用（在途引用保全/完成回收由 runtime 负责）；
* 像素预算：单图解码上限 ``MAX_DECODE_PIXELS``（runtime 统一），显示
  用 QPixmap 上限 ``MAX_PIXMAP_PIXELS``——不再按 4× 放大预生成巨图
  （4 张 7K RGBA ×16 像素面积 ≈ 784MB 的路径已封死）；
* 「适应」同时考虑**所有**图片的宽与高，且不受 25% 手动缩放下限约束
  （大图也能真正 fit）。
"""
from __future__ import annotations

import uuid
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from src.app.ui.image_decode_runtime import (
    ImageDecodeRuntime,
    decode_image_scaled,
    shared_runtime,
)
from src.shared.ui.style_sheet import C

MAX_DECODE_PIXELS = 8192          # 与预览/缩略图解码同源的边长上限
MAX_PIXMAP_PIXELS = 16 * 1024 * 1024  # 单格显示位图预算（约 4K×4K）


def decode_compare(data: bytes) -> tuple[QImage, str]:
    """比较图解码（与预览共用 ``decode_image_scaled``；保留独立入口供
    测试与差异化的错误文案）。"""
    image, error = decode_image_scaled(data)
    if image.isNull() and not error:
        error = "不支持的图片格式"
    return image, error


class CompareDialog(QDialog):
    images_requested = pyqtSignal(list, str)   # paths, session_id

    def __init__(self, paths: list[str], parent: QWidget | None = None,
                 *, runtime: ImageDecodeRuntime | None = None) -> None:
        super().__init__(parent)
        self.paths = list(paths)[:4]
        self.session_id = uuid.uuid4().hex
        self._images: dict[str, QImage] = {}
        self._labels: dict[str, QLabel] = {}
        self._zoom = 1.0
        self._closed = False
        self._runtime = runtime if runtime is not None else shared_runtime()
        self._decode_lease = self._runtime.acquire(f"compare:{self.session_id[:8]}")
        self._decode_generation = 0
        self._teardown_generation = 2 ** 31  # teardown 后压到当前值以下即作废
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
        """[FR-15/17] 关闭：作废解码结果、取消剩余任务；transport 侧由宿主取消。

        [E1/FUP-06] 不再 tryTake 后清引用：排队任务由 runtime 立即回收，
        在途任务作废回包、引用保留到真实完成——窗口销毁不动共享线程池。
        """
        if self._closed:
            return
        self._closed = True
        self._teardown_generation = self._decode_generation
        self._decode_lease.close()

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
        self._decode_lease.submit(
            path, data,
            decode=lambda payload: decode_compare(payload),
            on_done=lambda p, image, error, g=generation: self._on_decoded(g, p, image, error))

    def _on_decoded(self, generation: int, path: str,
                    image: QImage, error: str) -> None:
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
