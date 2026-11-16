"""Content-first gallery cards; only real service thumbnails become images."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QModelIndex, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from src.features.files.contracts import Entry, format_duration
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


class FilesCardDelegate(QStyledItemDelegate):
    def __init__(
        self, entry_role: int, image_for_entry: Callable[[Entry], QImage | None],
        parent: QWidget, strip_for_entry: Callable[[Entry], QImage | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry_role = entry_role
        self.image_for_entry = image_for_entry
        self.strip_for_entry = strip_for_entry
        self.card_width = 180

    def sizeHint(self, option: QStyleOptionViewItem | None, index: QModelIndex) -> QSize:
        return QSize(self.card_width + 16, int(self.card_width * 0.625) + 72)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry: Entry | None = index.data(self.entry_role)
        if not isinstance(entry, Entry):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(5, 5, -5, -5)
        cover = QRectF(rect.left() + 1, rect.top() + 1, rect.width() - 2, rect.width() * 0.625)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        offline = entry.availability == "offline"
        body = QColor(C.BG_PANEL)
        if selected:
            body = QColor(C.ORANGE)
            body.setAlpha(22)
        painter.setBrush(body)
        painter.setPen(QPen(QColor(C.ORANGE if selected else C.BORDER_HOVER if hovered else C.BORDER_SUBTLE),
                            1.2 if selected else 0.7))
        painter.drawRoundedRect(rect, 8, 8)
        clip = QPainterPath()
        clip.addRoundedRect(cover, 7, 7)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(cover, QColor(C.BG_INPUT_DARK))
        image = self.image_for_entry(entry)
        if image is not None and not image.isNull():
            scale = max(cover.width() / image.width(), cover.height() / image.height())
            width, height = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF(cover.center().x() - width / 2, cover.center().y() - height / 2, width, height), image)
        else:
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
            rating_rect = QRectF(cover.right() - 46, cover.top() + 8, 38, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 18, 18, 190))
            painter.drawRoundedRect(rating_rect, 4, 4)
            painter.setPen(QColor(C.ORANGE))
            painter.drawText(rating_rect, Qt.AlignmentFlag.AlignCenter, "★" * entry.user.rating)

        title_font = QFont(option.font)
        title_font.setPointSizeF(11)
        title_font.setWeight(title_font.Weight.Medium)
        painter.setFont(title_font)
        title_rect = QRectF(rect.left() + 10, cover.bottom() + 6, rect.width() - 20, 22)
        painter.setPen(QColor(C.TEXT_HINT if offline else C.TEXT_PRIMARY))
        title = painter.fontMetrics().elidedText(entry.name, Qt.TextElideMode.ElideMiddle, int(title_rect.width()))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

        # 辅助信息只用真实已知值：时长来自技术元数据，大小来自 stat。
        details: list[str] = []
        duration = entry.technical.duration_seconds
        if duration and 0 < duration < 360000:
            details.append(format_duration(duration))
        if not entry.is_dir and entry.size_label != "—":
            details.append(entry.size_label)
        if entry.subdir:
            details.append(entry.subdir.rsplit("/", 1)[-1])
        if offline:
            details.append(I18N.tr("files.native.offline", "离线"))
        subtitle = " · ".join(details)
        if entry.is_dir:
            subtitle = I18N.tr("files.native.open_folder", "双击浏览文件夹")
        painter.setFont(badge_font)
        painter.setPen(QColor(C.TEXT_HINT))
        painter.drawText(QRectF(title_rect.left(), title_rect.bottom(), title_rect.width(), 17),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor(C.ORANGE), 1, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 6, 6)
        painter.restore()
