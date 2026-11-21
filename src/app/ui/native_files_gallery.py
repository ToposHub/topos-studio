"""Content-first gallery cards; only real service thumbnails become images."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QModelIndex, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QTextOption
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from src.features.files.contracts import Entry, format_duration
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.app.ui.native_files_gallery_view import GALLERY_CAPTION_HEIGHT, GALLERY_ITEM_INSET


class FilesCardDelegate(QStyledItemDelegate):
    def __init__(
        self, entry_role: int, image_for_entry: Callable[[Entry], QImage | None],
        parent: QWidget, strip_for_entry: Callable[[Entry], QImage | None] | None = None,
        *, ai_analyzed: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry_role = entry_role
        self.image_for_entry = image_for_entry
        self.strip_for_entry = strip_for_entry
        self.ai_analyzed = ai_analyzed
        self.card_width = 180
        self.layout_mode = "grid"
        self.grid_fill = False

    def sizeHint(self, option: QStyleOptionViewItem | None, index: QModelIndex) -> QSize:
        return QSize(self.card_width + 2 * GALLERY_ITEM_INSET,
                     self.card_width + GALLERY_CAPTION_HEIGHT + 2 * GALLERY_ITEM_INSET)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry: Entry | None = index.data(self.entry_role)
        if not isinstance(entry, Entry):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipRect(option.rect)
        rect = QRectF(option.rect).adjusted(3, 3, -3, -3)
        cover = QRectF(option.rect).adjusted(GALLERY_ITEM_INSET, GALLERY_ITEM_INSET,
                                            -GALLERY_ITEM_INSET,
                                            -GALLERY_ITEM_INSET - GALLERY_CAPTION_HEIGHT)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        offline = entry.availability == "offline"
        if selected or hovered:
            painter.setBrush(QColor(C.BG_ACTIVE if selected else C.BG_HOVER))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 5, 5)
        clip = QPainterPath()
        clip.addRoundedRect(cover, 4, 4)
        painter.save()
        painter.setClipPath(clip)
        image = self.image_for_entry(entry)
        if image is not None and not image.isNull():
            fit = max if self.layout_mode == "grid" and self.grid_fill else min
            scale = fit(cover.width() / image.width(), cover.height() / image.height())
            width, height = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF(cover.center().x() - width / 2, cover.center().y() - height / 2, width, height), image)
        else:
            painter.fillRect(cover, QColor(C.BG_INPUT_DARK))
            icon = index.data(Qt.ItemDataRole.DecorationRole)
            if icon is not None:
                icon.paint(painter, int(cover.center().x() - 19), int(cover.center().y() - 19), 38, 38)
        painter.restore()
        if hovered and self.strip_for_entry is not None and not entry.is_dir:
            strip = self.strip_for_entry(entry)
            if strip is not None and not strip.isNull():
                height = cover.height() * 0.42
                strip_rect = QRectF(cover.left(), cover.bottom() - height,
                                    cover.width(), height)
                painter.save()
                painter.setClipPath(clip)
                painter.setOpacity(0.96)
                painter.drawImage(strip_rect, strip)
                painter.setOpacity(1.0)
                cursor = option.widget.mapFromGlobal(QCursor.pos()) if option.widget else None
                if cursor is not None and cover.width() > 0:
                    x = min(max(cursor.x(), cover.left()), cover.right())
                    painter.setPen(QPen(QColor(C.ORANGE), 1.4))
                    painter.drawLine(int(x), int(strip_rect.top()), int(x), int(strip_rect.bottom()))
                painter.restore()

        if selected or hovered:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(C.ORANGE if selected else C.BORDER_HOVER), 1.0))
            painter.drawRoundedRect(cover, 4, 4)

        kind = entry.kind
        if entry.is_dir:
            badge = I18N.tr("files.native.kind.dir", "文件夹")
        elif not entry.has_known_media:
            badge = I18N.tr("files.native.kind.doc", "文件")
        else:
            badge = entry.name.rsplit(".", 1)[-1].upper()[:8] if "." in entry.name else kind.upper()
        badge_font = QFont(option.font)
        badge_font.setPointSizeF(9)
        painter.setFont(badge_font)
        metrics = painter.fontMetrics()
        badge_rect = QRectF(cover.left() + 8, cover.top() + 8, metrics.horizontalAdvance(badge) + 12, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 18, 18, 190))
        painter.drawRoundedRect(badge_rect, 4, 4)
        painter.setPen(QColor(C.TEXT_PRIMARY))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)

        if entry.user.rating:
            rating_width = metrics.horizontalAdvance("★" * entry.user.rating) + 12
            rating_rect = QRectF(cover.right() - rating_width - 8, cover.top() + 8, rating_width, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 18, 18, 190))
            painter.drawRoundedRect(rating_rect, 4, 4)
            painter.setPen(QColor(C.ORANGE))
            painter.drawText(rating_rect, Qt.AlignmentFlag.AlignCenter, "★" * entry.user.rating)

        if self.ai_analyzed is not None and self.ai_analyzed(entry.path):
            ai_rect = QRectF(cover.right() - 31, cover.top() + (29 if entry.user.rating else 8), 23, 18)
            badge_font.setBold(True)
            painter.setFont(badge_font)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 18, 18, 216))
            painter.drawRoundedRect(ai_rect, 4, 4)
            painter.setPen(QColor(C.ORANGE))
            painter.drawText(ai_rect, Qt.AlignmentFlag.AlignCenter, "AI")
            badge_font.setBold(False)

        title_font = QFont(option.font)
        title_font.setPointSizeF(10)
        painter.setFont(title_font)
        title_rect = QRectF(rect.left() + 4, cover.bottom() + 4, rect.width() - 8, 34)
        painter.setPen(QColor(C.TEXT_HINT if offline else C.TEXT_PRIMARY))
        text_option = QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        text_option.setWrapMode(QTextOption.WrapMode.WrapAnywhere)
        painter.drawText(title_rect, entry.name, text_option)
        # 时间和离线状态覆在封面上，名称区域留给两行文件名。
        duration = entry.technical.duration_seconds
        label = (I18N.tr("files.native.offline", "离线") if offline
                 else format_duration(duration) if entry.kind in ("video", "audio")
                 and duration and 0 < duration < 360000 else "")
        if label:
            painter.setFont(badge_font)
            width = painter.fontMetrics().horizontalAdvance(label) + 12
            badge_rect = QRectF(cover.right() - width - 6, cover.bottom() - 24, width, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 18, 18, 190))
            painter.drawRoundedRect(badge_rect, 4, 4)
            painter.setPen(QColor(C.TEXT_PRIMARY))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, label)
        if selected:
            marker = QRectF(cover.left() + 6, cover.bottom() - 22, 16, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(C.BG_DARKEST))
            painter.drawEllipse(marker)
            center = marker.center()
            check = QPainterPath(center + QPointF(-3.5, 0))
            check.lineTo(center + QPointF(-1, 2.5))
            check.lineTo(center + QPointF(3.5, -2.5))
            painter.setPen(QPen(QColor(C.ORANGE), 1.5, cap=Qt.PenCapStyle.RoundCap,
                                join=Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(check)
        elif option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor(C.TEXT_DIM), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 5, 5)
        painter.restore()


class FilesListDelegate(QStyledItemDelegate):
    """真实缩略图与属性列共用当前selection/model，不创建逐行控件。"""

    def __init__(self, entry_role: int, image_for_entry: Callable[[Entry], QImage | None],
                 parent: QWidget) -> None:
        super().__init__(parent)
        self.entry_role = entry_role
        self.image_for_entry = image_for_entry
        self.row_height = 46

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        size.setHeight(self.row_height)
        return size

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry = index.data(self.entry_role)
        if index.column() != 0 or not isinstance(entry, Entry):
            super().paint(painter, option, index)
            return
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        background.icon = QIcon()
        widget = option.widget
        if widget is not None:
            widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, widget)
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        cover = QRectF(option.rect.left() + 6, option.rect.top() + 4, 54, option.rect.height() - 8)
        image = self.image_for_entry(entry)
        if image is not None and not image.isNull():
            scale = min(cover.width() / image.width(), cover.height() / image.height())
            width, height = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF(cover.center().x() - width / 2,
                                     cover.center().y() - height / 2, width, height), image)
        else:
            icon = index.data(Qt.ItemDataRole.DecorationRole)
            if icon is not None:
                icon.paint(painter, int(cover.center().x() - 12), int(cover.center().y() - 12), 24, 24)
        title_rect = QRectF(option.rect).adjusted(70, 0, -6, 0)
        painter.setFont(option.font)
        painter.setPen(QColor(C.TEXT_HINT if entry.availability == "offline" else C.TEXT_PRIMARY))
        title = painter.fontMetrics().elidedText(entry.name, Qt.TextElideMode.ElideMiddle,
                                                max(1, int(title_rect.width())))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.restore()
