"""B04 / U03：原生 Files 的图片预览浮层（缩放、平移、前后切换和释放）。

职责边界
--------
* **取字节** 在 ``NativeFilesTransport.load_preview``（网络线程友好，64MB 上限）；
* **解码** 在共享的 ``ImageDecodeRuntime``（E1/E2，FUP-06）：预览/比较
  窗口共用同一线程池与总字节/像素预算，本模块只持有租约；
* **呈现** 在本模块的 ``NativeFilesPreview``（浮层，只做像素与交互）。

视频使用共享播放器宿主。其他格式可使用同一浮层呈现预览或文件状态。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QContextMenuEvent, QEnterEvent, QImage, QKeyEvent, QMouseEvent, QPainter,
    QPaintEvent, QPixmap, QResizeEvent, QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
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


class _PreviewCanvas(QLabel):
    def paintEvent(self, event: QPaintEvent) -> None:
        pixmap = self.pixmap()
        if pixmap.isNull():
            super().paintEvent(event)
            return
        # Draw the exposed part from the source; zoom never allocates a larger pixmap.
        target = QRectF(event.rect())
        scale_x = pixmap.width() / self.width()
        scale_y = pixmap.height() / self.height()
        source = QRectF(target.x() * scale_x, target.y() * scale_y,
                        target.width() * scale_x, target.height() * scale_y)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, pixmap, source)
        painter.end()


class NativeFilesPreview(QWidget):
    """Native preview overlay with centered fit, point-anchored zoom and panning."""

    closed = pyqtSignal()
    open_in_legacy_requested = pyqtSignal(str)
    next_requested = pyqtSignal()
    previous_requested = pyqtSignal()
    detail_requested = pyqtSignal()
    grade_requested = pyqtSignal(str)
    tools_visibility_changed = pyqtSignal(bool)
    video_pointer_moved = pyqtSignal(QPoint)
    video_pointer_left = pyqtSignal()
    video_settings_requested = pyqtSignal(QPoint)

    def __init__(self, parent: QWidget, *, bg: str = "#141414", fg: str = "#d8d8d8",
                 muted: str = "#8b8b8b") -> None:
        super().__init__(parent)
        self._bg = bg
        self._fg = fg
        self._muted = muted
        self._path = ""
        self._file_title = ""
        self._meta_text = ""
        self._image: Optional[QImage] = None
        self._placeholder: QImage | None = None
        self._has_detail = False
        self._preview_only = False
        self._mode = "image"
        self._fit = True
        self._zoom: float | None = None
        self._pixmap_key: int | None = None
        self._pan_last: QPoint | None = None
        self._decode_pending = False
        self._close_on_space = False
        self._pointer_at_top = False
        self._tools_visible = False
        self._can_grade = False
        self._can_fit = False
        self._can_open_externally = False
        self._info_timer = QTimer(self, singleShot=True, interval=2000)
        self._info_timer.timeout.connect(self._hide_chrome)
        self.setObjectName("nativeFilesPreview")
        self.setStyleSheet(f"""
            #nativeFilesPreview {{ background: {bg}; }}
            QWidget#nativeLightboxTools {{ background: transparent; border: none; }}
            QFrame#lightboxInfo {{
                background: rgba(26, 26, 28, 224);
                border: none;
                border-radius: 8px;
            }}
            QToolButton[lightboxControl="true"] {{
                color: {fg};
                background: rgba(25, 25, 28, 150);
                border: none;
                border-radius: 7px;
                padding: 0;
            }}
            QToolButton[lightboxControl="true"]:hover,
            QToolButton[lightboxControl="true"]:focus {{ background: #353537; }}
            QToolButton[lightboxControl="true"]:pressed {{ background: #464649; }}
            QToolButton[lightboxArrow="true"] {{ border-radius: 18px; }}
        """)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        self._file_info = QFrame(self)
        self._file_info.setObjectName("lightboxInfo")
        info_layout = QVBoxLayout(self._file_info)
        info_layout.setContentsMargins(12, 7, 12, 8)
        info_layout.setSpacing(2)
        self.title = QLabel(self._file_info)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setStyleSheet(f"color: {fg}; font-size: 13px; font-weight: 600;")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meta = QLabel(self._file_info)
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setStyleSheet(f"color: {muted}; font-size: 11px;")
        self.meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info_layout.addWidget(self.title)
        info_layout.addWidget(self.meta)

        self._header_widget = QWidget(self)
        self._header_widget.setObjectName("nativeLightboxTools")
        header = QHBoxLayout(self._header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        fit_label = I18N.tr("files.native.lightbox.fit", "切换适配与原尺寸（F）")
        self.fit_button = self._icon_button("fa5s.expand", fit_label, self.toggle_fit, parent=self._header_widget)
        header.addWidget(self.fit_button)

        external_label = I18N.tr("files.native.lightbox.external", "使用素材查看器打开")
        self.legacy_button = self._icon_button("fa5s.external-link-alt", external_label,
                                               lambda: self.open_in_legacy_requested.emit(self._path),
                                               parent=self._header_widget)
        header.addWidget(self.legacy_button)

        grade_label = I18N.tr("grade_sets.open_in_color", "Open in Color")
        self.grade_button = self._icon_button("mdi.palette", grade_label,
                                             lambda: self.grade_requested.emit(self._path),
                                             parent=self._header_widget)
        self.grade_button.setObjectName("nativeLightboxGrade")
        self.grade_button.hide()
        header.addWidget(self.grade_button)

        close_label = I18N.tr("files.native.lightbox.close", "关闭预览（Esc）")
        self.close_button = self._icon_button("mdi.close", close_label, self.request_close,
                                             parent=self._header_widget)
        header.addWidget(self.close_button)
        previous_label = I18N.tr("files.native.lightbox.previous", "上一张（←）")
        next_label = I18N.tr("files.native.lightbox.next", "下一张（→）")
        self.prev_button = self._icon_button("mdi.chevron-left", previous_label, self.previous_requested.emit, arrow=True)
        self.next_button = self._icon_button("mdi.chevron-right", next_label, self.next_requested.emit, arrow=True)

        self.video_slot = QWidget(self)
        self.video_slot.setObjectName("nativeVideoSlot")
        self.video_slot.setStyleSheet("#nativeVideoSlot { background: #000; }")
        self.video_slot.hide()
        root.addWidget(self.video_slot, 1)

        self.body = _PreviewCanvas("", self)
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.setStyleSheet(f"color: {muted}; background: transparent;")
        self.body.setMinimumSize(1, 1)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.setWidget(self.body)
        for surface in (self, self.body, self.scroll.viewport(), self._file_info, self._header_widget):
            surface.setMouseTracking(True)
        root.addWidget(self.scroll, 1)

        for control in (self._file_info, self._header_widget, self.prev_button, self.next_button):
            control.raise_()

        if self.parent() is not None:
            self.parent().installEventFilter(self)

    def _icon_button(self, icon: str, label: str, action: Callable[[], None], *,
                     parent: QWidget | None = None, arrow: bool = False) -> QToolButton:
        button = QToolButton(parent or self)
        button.setProperty("lightboxControl", True)
        button.setProperty("lightboxArrow", arrow)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setIcon(qta.icon(icon, color=self._fg))
        button.setIconSize(QSize(24, 24) if arrow else QSize(16, 16))
        button.setFixedSize(QSize(36, 36) if arrow else QSize(30, 30))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(action)
        return button

    def _place_overlay_controls(self) -> None:
        inset = 14
        self._header_widget.adjustSize()
        self._header_widget.move(max(inset, self.width() - self._header_widget.width() - inset), inset)
        arrow_y = (self.height() - self.prev_button.height()) // 2
        self.prev_button.move(inset, arrow_y)
        self.next_button.move(self.width() - inset - self.next_button.width(), arrow_y)
        labels = ((self.title, self._file_title), (self.meta, self._meta_text))
        text_width = max(label.fontMetrics().horizontalAdvance(text) for label, text in labels)
        max_width = min(360, max(1, self._header_widget.x() - inset * 2))
        card_width = min(max_width, text_width + 32)
        for label, text in labels:
            display = label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(0, card_width - 26))
            label.setText(display)
            label.setToolTip(text if display != text else "")
        self._file_info.resize(card_width, self._file_info.sizeHint().height())
        self._file_info.move(inset, inset)

    def _set_meta(self, text: str) -> None:
        if self._meta_text != text:
            self._meta_text = text
            if not self._file_info.isHidden():
                self._place_overlay_controls()

    # -- 状态 ---------------------------------------------------------------
    @property
    def current_path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return not self.isHidden()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if obj is self.parent() and kind == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
        if not self.is_open or not isinstance(obj, QWidget):
            return super().eventFilter(obj, event)
        if obj is not self and not self.isAncestorOf(obj):
            return super().eventFilter(obj, event)
        if self.is_video and obj.window().windowType() == Qt.WindowType.Popup:
            return super().eventFilter(obj, event)
        if (self.is_video and kind in (
                QEvent.Type.ContextMenu, QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.Leave)):
            if isinstance(event, QContextMenuEvent):
                point = event.globalPos()
                if self.video_slot.rect().contains(self.video_slot.mapFromGlobal(point)):
                    self.video_settings_requested.emit(point)
                    event.accept()
                    return True
            elif isinstance(event, QEnterEvent) or (
                    kind == QEvent.Type.MouseMove and isinstance(event, QMouseEvent)):
                self.video_pointer_moved.emit(event.globalPosition().toPoint())
            elif obj is self and kind == QEvent.Type.Leave:
                self.video_pointer_left.emit()
        if self._handle_viewport_event(obj, event):
            return True
        if kind in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            if self._handle_dismiss_key(event):
                return True
        elif kind == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            position = self.mapFromGlobal(event.globalPosition().toPoint())
            self._set_pointer_at_top(self.rect().contains(position) and position.y() < 64)
        elif obj is self and kind == QEvent.Type.Leave:
            self._set_pointer_at_top(False)
        return super().eventFilter(obj, event)

    def _handle_viewport_event(self, obj: QWidget, event: QEvent) -> bool:
        if self.is_video or obj not in (self.body, self.scroll.viewport(), self.scroll):
            return False
        if self._image is None and self._placeholder is None:
            return False
        if isinstance(event, QWheelEvent):
            pixels = event.pixelDelta().y()
            steps = pixels / 100 if pixels else event.angleDelta().y() / 120
            if steps:
                point = self.scroll.viewport().mapFromGlobal(event.globalPosition().toPoint())
                self._zoom_at(point, steps)
            event.accept()
            return True
        if not isinstance(event, QMouseEvent):
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.MiddleButton:
            self._pan_last = event.globalPosition().toPoint()
            self.scroll.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.MiddleButton:
            self._finish_pan()
        elif kind == QEvent.Type.MouseMove and self._pan_last is not None:
            if not event.buttons() & Qt.MouseButton.MiddleButton:
                self._finish_pan()
                return False
            point = event.globalPosition().toPoint()
            shift = point - self._pan_last
            self._pan_last = point
            horizontal, vertical = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
            horizontal.setValue(horizontal.value() - shift.x())
            vertical.setValue(vertical.value() - shift.y())
        else:
            return False
        event.accept()
        return True

    def _finish_pan(self) -> None:
        if self._pan_last is not None:
            self._pan_last = None
            self.scroll.viewport().unsetCursor()

    def _image_fraction(self, point: QPoint) -> QPointF:
        local = self.body.mapFrom(self.scroll.viewport(), point)
        return QPointF(max(0.0, min(1.0, local.x() / max(1, self.body.width()))),
                       max(0.0, min(1.0, local.y() / max(1, self.body.height()))))

    def _restore_anchor(self, fraction: QPointF, point: QPoint) -> None:
        horizontal, vertical = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
        horizontal.setValue(horizontal.value() + round(self.body.x() + fraction.x() * self.body.width() - point.x()))
        vertical.setValue(vertical.value() + round(self.body.y() + fraction.y() * self.body.height() - point.y()))

    def _zoom_at(self, point: QPoint, steps: float) -> None:
        image = self._image if self._image is not None else self._placeholder
        if image is None:
            return
        fit_width = max(1, self._fit_size(image).width())
        current = self._zoom if self._zoom is not None else self.body.width() / fit_width
        factor = 1.2 ** max(-10.0, min(10.0, steps))
        max_zoom = max(16.0, image.width() / fit_width)
        zoom = max(0.25, min(max_zoom, current * factor))
        if zoom == self._zoom:
            return
        fraction = self._image_fraction(point)
        self._zoom = zoom
        if self._fit:
            self._fit = False
            self._sync_fit_button()
        self._render()
        self._restore_anchor(fraction, point)
        self._request_zoom_detail()

    def _request_zoom_detail(self) -> None:
        image = self._image
        if (self._zoom is not None and image is not None and self._can_fit
                and not self._has_detail and not self._decode_pending
                and self.body.width() * self.devicePixelRatioF() > image.width()):
            self.begin_load()
            self.detail_requested.emit()

    def _handle_dismiss_key(self, event: QKeyEvent) -> bool:
        if event.modifiers() != Qt.KeyboardModifier.NoModifier or event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            return False
        if self.is_video and event.key() == Qt.Key.Key_Space and not self._close_on_space:
            return False
        event.accept()
        if (event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat()
                and (event.key() == Qt.Key.Key_Escape or self._close_on_space)):
            self.close_preview()
        return True

    @property
    def tools_visible(self) -> bool:
        return self._tools_visible

    def _set_pointer_at_top(self, at_top: bool) -> None:
        if self._pointer_at_top == at_top:
            return
        self._pointer_at_top = at_top
        if at_top:
            self._set_tools_visible(True)
            self._show_file_info()
        else:
            self._info_timer.start()

    def _set_tools_visible(self, visible: bool) -> None:
        changed = visible != self._tools_visible
        self._tools_visible = visible
        self.fit_button.setVisible(visible and self._can_fit)
        self.grade_button.setVisible(visible and self._can_grade)
        self.legacy_button.setVisible(visible and self._can_open_externally)
        self._place_overlay_controls()
        if changed:
            self.tools_visibility_changed.emit(visible)

    def _show_file_info(self) -> None:
        self._file_info.show()
        self._place_overlay_controls()
        if self._pointer_at_top:
            self._info_timer.stop()
        else:
            self._info_timer.start()

    def _hide_chrome(self) -> None:
        self._file_info.hide()
        self._set_tools_visible(False)

    def _show_overlay(self) -> None:
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        if app := QApplication.instance():
            app.installEventFilter(self)
        self._set_tools_visible(self._pointer_at_top)
        self._show_file_info()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_for(self, path: str, title: str = "", *, preview_only: bool = False,
                 can_grade: bool = False, close_on_space: bool = False) -> None:
        """Open a lightbox with the dismissal policy of its entry point."""
        self._path = path
        self._image = None
        self._placeholder = None
        self._has_detail = False
        self._preview_only = preview_only
        self._mode = "image"
        self._close_on_space = close_on_space
        self._decode_pending = True
        self._fit = True
        self._zoom = None
        self._finish_pan()
        self._sync_fit_button()
        self._file_title = title or path.rsplit("/", 1)[-1]
        self._meta_text = "载入中…"
        self._can_fit = not preview_only
        self._can_grade = can_grade
        self._can_open_externally = False
        self.video_slot.hide()
        self.scroll.show()
        self.body.clear()
        self.body.setText("载入中…")
        self.body.setPixmap(QPixmap())
        self._resize_body_text()
        self._show_overlay()

    @property
    def is_video(self) -> bool:
        return self._mode == "video"

    def open_video(self, path: str, title: str = "", *, close_on_space: bool = False) -> None:
        """Open the same lightbox with the shared video player as its surface."""
        self._path = path
        self._image = None
        self._placeholder = None
        self._has_detail = False
        self._preview_only = True
        self._decode_pending = False
        self._mode = "video"
        self._zoom = None
        self._finish_pan()
        self._close_on_space = close_on_space
        self._can_fit = self._can_grade = self._can_open_externally = False
        self._file_title = title or path.rsplit("/", 1)[-1]
        self._meta_text = I18N.tr("files.native.lightbox.video", "视频")
        self.scroll.hide()
        self.video_slot.show()
        self._show_overlay()
        if layout := self.layout():
            layout.activate()

    def show_placeholder(self, path: str, image: QImage) -> None:
        if path == self._path and self._image is None and (self._decode_pending or self._preview_only):
            self._placeholder = image
            self._render()

    def show_image(self, path: str, image: QImage, *, has_detail: bool = True) -> None:
        if path != self._path:
            return
        center = self.scroll.viewport().rect().center()
        fraction = self._image_fraction(center) if self._zoom is not None else None
        self._decode_pending = False
        self._image = image
        self._placeholder = None
        self._has_detail = has_detail and not self._preview_only
        self._can_open_externally = False
        self._set_tools_visible(self._tools_visible)
        self._render()
        if fraction is not None:
            self._restore_anchor(fraction, center)
            self._request_zoom_detail()

    def show_message(self, path: str, message: str, *, open_in_legacy: bool = True) -> None:
        if path != self._path:
            return
        video_error = self.is_video
        self._decode_pending = False
        self._mode = "image"
        if not self._has_detail and self._zoom is None:
            self._fit = True
            self._sync_fit_button()
        self._meta_text = message
        self._can_fit = not video_error and not self._preview_only and self._image is not None
        self._can_open_externally = open_in_legacy
        self._set_tools_visible(self._tools_visible)
        self._show_file_info()
        self.video_slot.hide()
        self.scroll.show()
        if self._image is None and self._placeholder is None:
            self.body.setPixmap(QPixmap())
            self.body.setText(message)
            self._resize_body_text()
        if video_error:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_steppers_enabled(self, enabled: bool) -> None:
        """单张图没有"上一张/下一张"；多张才启用，避免点了没反应。"""
        for button in (self.prev_button, self.next_button):
            button.setEnabled(enabled)
            button.setVisible(enabled)

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
        self._pointer_at_top = False
        self._finish_pan()
        if app := QApplication.instance():
            app.removeEventFilter(self)
        self._hide_chrome()
        self._jobs_cleanup()
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
        if not self._can_fit:
            return
        self._fit = not self._fit
        self._zoom = None
        self._show_file_info()
        self._sync_fit_button()
        if not self._fit and not self._has_detail:
            self.begin_load()
            self.detail_requested.emit()
        self._render()

    def _sync_fit_button(self) -> None:
        icon = "fa5s.expand" if self._fit else "fa5s.compress"
        self.fit_button.setIcon(qta.icon(icon, color=self._fg))

    def _kind_text(self) -> str:
        if self._zoom is not None:
            return I18N.tr("files.native.lightbox.zoom", "{percent}% · 相对适配",
                           percent=round(self._zoom * 100))
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

    def _fit_size(self, image: QImage) -> QSize:
        return image.size().scaled(self._viewport_size(), Qt.AspectRatioMode.KeepAspectRatio)

    def _render(self) -> None:
        image = self._image if self._image is not None else self._placeholder
        if image is None:
            return
        if self._zoom is not None:
            size = self._fit_size(image) * self._zoom
        elif self._fit or not self._has_detail:
            size = self._fit_size(image)
        else:
            size = image.size()
        # Reuse the same source pixels when zooming, panning or resizing.
        if self._pixmap_key != image.cacheKey() or self.body.pixmap().isNull():
            self.body.setPixmap(QPixmap.fromImage(image))
            self._pixmap_key = image.cacheKey()
        self.body.resize(size.expandedTo(QSize(1, 1)))
        if self._decode_pending:
            self._set_meta("载入中…" if self._fit else "载入原尺寸…")
        elif self._image is not None:
            kind = "浏览预览" if self._preview_only and self._zoom is None else self._kind_text()
            if not self._has_detail:
                kind += " · 预览像素"
            self._set_meta(f"{image.width()} × {image.height()} · {kind}")

    def _resize_body_text(self) -> None:
        width = max(200, self._viewport_size().width() - 24)
        self.body.resize(width, max(80, self._viewport_size().height() - 24))
        self.body.setWordWrap(True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._place_overlay_controls()
        if self._image is not None or self._placeholder is not None:
            self._render()
        elif self.body.pixmap().isNull():
            self._resize_body_text()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        key = event.key()
        if self._handle_dismiss_key(event):
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
