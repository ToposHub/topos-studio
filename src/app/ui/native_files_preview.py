"""B04 / U03：原生 Files 的图片预览浮层（适配 / 原尺寸 / 前后切换 / Esc 释放）。

职责边界
--------
* **取字节** 在 ``NativeFilesTransport.load_preview``（网络线程友好，64MB 上限）；
* **解码** 在共享的 ``ImageDecodeRuntime``（E1/E2，FUP-06）：预览/比较
  窗口共用同一线程池与总字节/像素预算，本模块只持有租约；
* **呈现** 在本模块的 ``NativeFilesPreview``（浮层，只做像素与交互）。

非图片（视频/音频/模型）不在这里硬塞一个播放器：浮层提供"在完整 Files 中
打开"，由既有播放器接管，避免两套播放会话并存。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QKeyEvent, QPainter, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
import qtawesome as qta

from src.app.ui.image_decode_runtime import (
    ImageDecodeRuntime,
    decode_image_scaled,
    shared_runtime,
)
from src.shared.i18n.i18n import I18N

# 兼容旧引用：解码上限现由共享 runtime 统一（8192 边长）
MAX_DECODE_PIXELS = 8192


def decode_preview(data: bytes) -> tuple[QImage, str]:
    """把字节解成 QImage；返回 ``(image, error)``，二选一为空。

    实现已并入共享 runtime 的 ``decode_image_scaled``（预览/比较同一份
    解码逻辑）；保留本入口供既有调用与测试 monkeypatch 使用。
    """
    return decode_image_scaled(data)


class PreviewDecoder(QObject):
    """预览解码的租约持有者（同一时刻只关心最新一张）。

    [FR-14] 任务身份 = 递增 generation + path，不再只用 path：同路径
    关闭后重开时，旧任务的完成回调不得删除/顶替新任务的结果——
    "旧 1px 任务先到、新 2px 后到必须只显示新图"。

    [E1/E2] 线程池/预算/取消分层归共享 ``ImageDecodeRuntime``：本类不再
    自建线程池（窗口销毁不牵连池），也不再本地持有 job 引用——运行中
    任务的引用保全与完成回收由 runtime 负责。
    """

    image_ready = pyqtSignal(str, object)
    decode_failed = pyqtSignal(str, str)

    def __init__(self, parent: QObject | None = None,
                 runtime: ImageDecodeRuntime | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime if runtime is not None else shared_runtime()
        self._lease = self._runtime.acquire("preview")
        self._generation = 0
        self._current: Optional[tuple[int, str]] = None

    def start(self, path: str, data: bytes, *, max_edge: int = MAX_DECODE_PIXELS) -> None:
        self.cancel()
        self._generation += 1
        generation = self._generation
        self._current = (generation, path)
        self._lease.submit(
            path, data,
            decode=(decode_preview if max_edge == MAX_DECODE_PIXELS
                    else lambda payload: decode_image_scaled(payload, max_edge=max_edge)),
            on_done=lambda p, image, error, g=generation: self._on_finished(g, p, image, error))

    def cancel(self) -> None:
        self._current = None
        # 排队任务即丢；在途任务由 runtime 作废回包并保留引用到真实完成
        self._lease.cancel_all()

    def _on_finished(self, generation: int, path: str,
                     image: QImage, error: str) -> None:
        if self._current is None or self._current[0] != generation:
            return  # 已被取消或被更新的同路径任务取代：结果作废
        self._current = None
        if image is None or image.isNull():
            self.decode_failed.emit(path, error or "无法解码该文件")
        else:
            self.image_ready.emit(path, image)

    def shutdown(self) -> None:
        """[FR-17/E1] 关闭：先作废回包，运行中的任务由 runtime 等真实
        完成后回收——不再同步等待两秒后清引用假装线程结束。
        """
        self._current = None
        self._lease.close()


class NativeFilesPreview(QWidget):
    """覆盖在原生面板上的预览浮层（滚轮不缩放，只有适配/原尺寸两档）。"""

    closed = pyqtSignal()
    open_in_legacy_requested = pyqtSignal(str)
    next_requested = pyqtSignal()
    previous_requested = pyqtSignal()
    detail_requested = pyqtSignal()
    grade_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget, *, bg: str = "#141414", fg: str = "#d8d8d8",
                 muted: str = "#8b8b8b") -> None:
        super().__init__(parent)
        self._bg = bg
        self._fg = fg
        self._muted = muted
        self._path = ""
        self._image: Optional[QImage] = None
        self._placeholder: QImage | None = None
        self._has_detail = False
        self._preview_only = False
        self._mode = "image"
        self._fit = True
        self._decode_pending = False
        self._auto_hide_info = False
        self._info_timer = QTimer(self, singleShot=True, interval=3000)
        self._info_timer.timeout.connect(self._hide_file_info)
        self.setObjectName("nativeFilesPreview")
        self.setStyleSheet(f"#nativeFilesPreview {{ background: {bg}; }}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._header_widget = QWidget(self)
        self._header_widget.setLayout(header)
        self._header_widget.installEventFilter(self)
        header.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("", self)
        self.title.setStyleSheet(f"color: {fg};")
        title_font = QFont(self.title.font())
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.meta = QLabel("", self)
        self.meta.setStyleSheet(f"color: {muted};")
        file_info = QWidget(self._header_widget)
        file_info_layout = QHBoxLayout(file_info)
        file_info_layout.setContentsMargins(0, 0, 0, 0)
        file_info_layout.addWidget(self.title, 1)
        file_info_layout.addWidget(self.meta)
        header.addWidget(file_info, 1)

        self.fit_button = QPushButton("原尺寸", self)
        self.fit_button.setToolTip("在「适配窗口」与「原尺寸」之间切换（F）")
        self.fit_button.clicked.connect(self.toggle_fit)
        header.addWidget(self.fit_button)

        self.prev_button = QPushButton("上一张", self)
        self.prev_button.setToolTip("上一张（←）")
        self.prev_button.clicked.connect(self.previous_requested)
        header.addWidget(self.prev_button)

        self.next_button = QPushButton("下一张", self)
        self.next_button.setToolTip("下一张（→）")
        self.next_button.clicked.connect(self.next_requested)
        header.addWidget(self.next_button)

        self.legacy_button = QPushButton("使用素材查看器打开", self)
        self.legacy_button.setToolTip("交给原生素材查看器；不可预览时显示")
        self.legacy_button.clicked.connect(
            lambda: self.open_in_legacy_requested.emit(self._path))
        header.addWidget(self.legacy_button)

        self.grade_button = QToolButton(self)
        self.grade_button.setObjectName("nativeLightboxGrade")
        grade_label = I18N.tr("grade_sets.open_in_color", "Open in Color")
        self.grade_button.setToolTip(grade_label)
        self.grade_button.setAccessibleName(grade_label)
        self.grade_button.setIcon(qta.icon("mdi.palette", color=fg))
        self.grade_button.clicked.connect(lambda: self.grade_requested.emit(self._path))
        self.grade_button.hide()
        header.addWidget(self.grade_button)

        self.close_button = QPushButton("关闭", self)
        self.close_button.setToolTip("关闭预览（Esc）")
        self.close_button.clicked.connect(self.request_close)
        header.addWidget(self.close_button)
        root.addWidget(self._header_widget)

        self.video_slot = QWidget(self)
        self.video_slot.setObjectName("nativeVideoSlot")
        self.video_slot.setStyleSheet("#nativeVideoSlot { background: #000; }")
        self.video_slot.hide()
        root.addWidget(self.video_slot, 1)

        self.body = QLabel("", self)
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.setStyleSheet(f"color: {muted}; background: transparent;")
        self.body.setMinimumSize(1, 1)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        if self.parent() is not None:
            self.parent().installEventFilter(self)

    # -- 状态 ---------------------------------------------------------------
    @property
    def current_path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return not self.isHidden()

    def eventFilter(self, obj: QObject, event) -> bool:  # type: ignore[override]
        if obj is self.parent() and event.type() == event.Type.Resize:
            self.setGeometry(self.parent().rect())
        if obj is getattr(self, "_header_widget", None) and event.type() == QEvent.Type.Enter:
            if self._auto_hide_info and self.is_open and not self.is_video:
                self._show_file_info()
        return super().eventFilter(obj, event)

    def _show_file_info(self) -> None:
        self.title.show()
        self.meta.show()
        if self._auto_hide_info:
            self._info_timer.start()

    def _hide_file_info(self) -> None:
        self.title.hide()
        self.meta.hide()

    def open_for(self, path: str, title: str = "", *, preview_only: bool = False,
                 auto_hide_info: bool = False, can_grade: bool = False) -> None:
        """显示浮层并进入"载入中"状态（字节与解码由调用方驱动）。"""
        self._path = path
        self._image = None
        self._placeholder = None
        self._has_detail = False
        self._preview_only = preview_only
        self._mode = "image"
        self._auto_hide_info = auto_hide_info
        self._decode_pending = True
        self._fit = True
        self._sync_fit_button()
        self.title.setText(title or path.rsplit("/", 1)[-1])
        self.meta.setText("载入中…")
        self._show_file_info()
        self.fit_button.setVisible(not preview_only)
        self.prev_button.show()
        self.next_button.show()
        self.legacy_button.hide()
        self.grade_button.setVisible(can_grade)
        self._header_widget.show()
        self.video_slot.hide()
        self.scroll.show()
        self.body.clear()
        self.body.setText("载入中…")
        self.body.setPixmap(QPixmap())
        self._resize_body_text()
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def is_video(self) -> bool:
        return self._mode == "video"

    def open_video(self, path: str, title: str = "") -> None:
        """Open the overlay in video mode; the player is hosted in video_slot."""
        self._path = path
        self._image = None
        self._placeholder = None
        self._decode_pending = False
        self._mode = "video"
        self._auto_hide_info = False
        self._info_timer.stop()
        self.title.show()
        self.grade_button.hide()
        self.title.setText(title or path.rsplit("/", 1)[-1])
        self.meta.setText("正在打开视频…")
        self.body.clear()
        self.body.setText("")
        self.body.setPixmap(QPixmap())
        self._header_widget.hide()
        self.scroll.hide()
        self.video_slot.show()
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.video_slot.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_placeholder(self, path: str, image: QImage) -> None:
        if path == self._path and self._image is None and (self._decode_pending or self._preview_only):
            self._placeholder = image
            self._render()

    def show_image(self, path: str, image: QImage, *, has_detail: bool = True) -> None:
        if path != self._path:
            return
        self._decode_pending = False
        self._image = image
        self._placeholder = None
        self._has_detail = has_detail and not self._preview_only
        self.legacy_button.hide()
        self._render()

    def show_message(self, path: str, message: str, *, open_in_legacy: bool = True) -> None:
        if path != self._path:
            return
        video_error = self.is_video
        self._decode_pending = False
        self._mode = "image"
        if not self._has_detail:
            self._fit = True
            self._sync_fit_button()
        self.meta.setText(message)
        if not video_error:
            self._show_file_info()
        else:
            self.meta.hide()
        self.fit_button.setVisible(not video_error and not self._preview_only)
        self.prev_button.setVisible(not video_error)
        self.next_button.setVisible(not video_error)
        self._header_widget.show()
        self.video_slot.hide()
        self.scroll.show()
        if self._image is None and self._placeholder is None:
            self.body.setPixmap(QPixmap())
            self.body.setText(message)
            self._resize_body_text()
        self.legacy_button.setVisible(open_in_legacy)
        if video_error:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_steppers_enabled(self, enabled: bool) -> None:
        """单张图没有"上一张/下一张"；多张才启用，避免点了没反应。"""
        self.prev_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)

    def begin_load(self) -> None:
        self._decode_pending = True

    def request_close(self) -> None:
        self.close_preview()

    def close_preview(self) -> None:
        was_open = self.is_open
        self._image = None
        self._placeholder = None
        self._path = ""
        self._mode = "image"
        self._decode_pending = False
        self._info_timer.stop()
        self._jobs_cleanup()
        self._header_widget.show()
        self.video_slot.hide()
        self.scroll.show()
        self.hide()
        if was_open:
            self.closed.emit()

    def _jobs_cleanup(self) -> None:
        # 解码线程由宿主的 PreviewDecoder 管理；这里只是清掉本地像素引用
        self.body.clear()
        self.body.setPixmap(QPixmap())

    def toggle_fit(self) -> None:
        if self._preview_only:
            return
        self._fit = not self._fit
        if self._auto_hide_info:
            self._show_file_info()
        self._sync_fit_button()
        if not self._fit and not self._has_detail:
            self.begin_load()
            self.detail_requested.emit()
        self._render()

    def _sync_fit_button(self) -> None:
        self.fit_button.setText("原尺寸" if self._fit else "适配窗口")

    def _kind_text(self) -> str:
        if self._fit:
            return "适配窗口"
        return "原尺寸 1:1"

    # -- 绘制 ---------------------------------------------------------------
    def _viewport_size(self) -> QSize:
        size = self.scroll.viewport().size()
        return QSize(max(1, size.width()), max(1, size.height()))

    def fit_decode_edge(self) -> int:
        size = self.size()
        return max(1024, min(4096, int(max(size.width(), size.height()) * self.devicePixelRatioF())))

    def _render(self) -> None:
        image = self._image if self._image is not None else self._placeholder
        if image is None:
            return
        if self._fit or not self._has_detail:
            dpr = self.devicePixelRatioF()
            pixmap = QPixmap.fromImage(image).scaled(
                self._viewport_size() * dpr,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
        else:
            pixmap = QPixmap.fromImage(image)
        self.body.setPixmap(pixmap)
        self.body.resize(pixmap.deviceIndependentSize().toSize())
        if self._decode_pending:
            self.meta.setText("载入中…" if self._fit else "载入原尺寸…")
        elif self._image is not None:
            kind = "浏览预览" if self._preview_only else self._kind_text()
            if not self._has_detail:
                kind += " · 预览像素"
            self.meta.setText(f"{image.width()} × {image.height()} · {kind}")

    def _resize_body_text(self) -> None:
        width = max(200, self._viewport_size().width() - 24)
        self.body.resize(width, max(80, self._viewport_size().height() - 24))
        self.body.setWordWrap(True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._image is not None or self._placeholder is not None:
            self._render()
        elif self.body.pixmap().isNull():
            self._resize_body_text()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_preview()
            return
        if self._mode == "video":
            super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fit()
            return
        if key == Qt.Key.Key_Left:
            self.previous_requested.emit()
            return
        if key == Qt.Key.Key_Right:
            self.next_requested.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self._bg))
        painter.end()
        super().paintEvent(event)
