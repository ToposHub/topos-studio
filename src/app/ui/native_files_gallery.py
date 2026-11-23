"""Content-first gallery cards; only real service thumbnails become images."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from math import cos, pi, sin

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QLineEdit, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from src.features.files.contracts import Entry, format_duration
from src.shared.i18n.i18n import I18N
from src.shared.media.format_registry import is_image
from src.shared.ui.style_sheet import C
from src.app.ui.native_files_gallery_view import DEFAULT_GALLERY_SPACING, GALLERY_ENTRY_ROLE, GallerySpacing

FILES_LOCATION_BACKGROUND = C.BG_PANEL_HEADER
FILE_LABEL_COLORS = (
    ("none", "清除颜色标签", C.TEXT_DIM),
    ("red", "红色", "#e16a65"),
    ("yellow", "黄色", "#eac363"),
    ("green", "绿色", "#74bd83"),
    ("blue", "蓝色", "#6da5e3"),
    ("purple", "紫色", "#aa8bd8"),
)
FILE_CAPTION_LINE_HEIGHT = 14
FILE_CAPTION_FIELDS = (("resolution", "尺寸"), ("size", "文件大小"), ("tags", "标签"), ("rating", "星级评分"))

RATING_STAR_COLOR = "#ed951a"
UNRATED_STAR_COLOR = "#505353"
RATING_BADGE_STAR_SIZE = 15
RATING_CAPTION_HEIGHT = 16
_CARD_RATING_STAR_SIZE = 13
_CARD_RATING_BACKGROUND_ALPHA = 145
_STAR_ANGLES = tuple(index * pi / 5 - pi / 2 for index in range(10))
_STAR_POINTS = tuple(
    (cos(angle) * radius, sin(angle) * radius)
    for angle, radius in zip(_STAR_ANGLES, (1.0, 0.44) * 5)
)


def _paint_star(painter: QPainter, x: float, y: float, size: float, color: str) -> None:
    radius = size / 2
    star = QPolygonF([QPointF(x + radius * (1 + dx), y + radius * (1 + dy))
                      for dx, dy in _STAR_POINTS])
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawPolygon(star)


def rating_star_icon(size: int, color: str) -> QIcon:
    icon = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _paint_star(painter, 1, 1, size - 2, color)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def paint_rating_badge(painter: QPainter, rect: QRectF, rating: int, *,
                       star_size: int = RATING_BADGE_STAR_SIZE, background_alpha: int = 210) -> None:
    """Draw the awarded stars using the same shape and color as the inspector."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(18, 18, 18, background_alpha))
    painter.drawRoundedRect(rect, 4, 4)
    top = rect.center().y() - star_size / 2
    if rect.width() < rating * star_size + 8:
        _paint_star(painter, rect.left() + 1, top, star_size, RATING_STAR_COLOR)
        painter.setPen(QColor(RATING_STAR_COLOR))
        painter.drawText(rect.adjusted(star_size, 0, -1, 0),
                         Qt.AlignmentFlag.AlignCenter, str(rating))
        return
    for index in range(rating):
        left = rect.left() + 4 + index * star_size
        _paint_star(painter, left + 1, top + 1, star_size - 2, RATING_STAR_COLOR)


def rating_badge_rect(cover: QRectF, rating: int, format_badge: QRectF, star_size: int) -> QRectF:
    width = min(rating * star_size + 8, cover.width())
    rect = QRectF(max(cover.left(), cover.right() - width - 8), cover.top() + 8, width, 18)
    if rect.intersects(format_badge):
        rect.translate(0, format_badge.height() + 3)
    return rect


def full_rating_fits(cover: QRectF, rating: int, rect: QRectF, star_size: int) -> bool:
    return (rect.width() >= rating * star_size + 8
            and cover.adjusted(6, 0, -6, -6).contains(rect))


class FilesNameDelegate(QStyledItemDelegate):
    """Edit the filename in-place while the model submits the filesystem task."""

    name_alignment = Qt.AlignmentFlag.AlignLeft

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem,
                     index: QModelIndex) -> QLineEdit:
        editor = QLineEdit(parent)
        editor.setObjectName("filesNameEditor")
        editor.setAlignment(self.name_alignment)
        editor.setFont(self.name_font(option.font))
        editor.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return editor

    def setEditorData(self, editor: QLineEdit, index: QModelIndex) -> None:
        entry = index.data(GALLERY_ENTRY_ROLE)
        if isinstance(entry, Entry) and not editor.isModified():
            editor.setText(entry.name)
            dot = entry.name.rfind(".")
            editor.setSelection(0, dot if not entry.is_dir and dot > 0 else len(entry.name))

    def setModelData(self, editor: QLineEdit, model: QAbstractItemModel, index: QModelIndex) -> None:
        model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: QLineEdit, option: QStyleOptionViewItem,
                             index: QModelIndex) -> None:
        rect = self.name_rect(option.rect)
        center = rect.center()
        bounds = editor.parentWidget().rect()
        width = min(max(116, rect.width()), bounds.width())
        rect.setWidth(width)
        if self.name_alignment == Qt.AlignmentFlag.AlignHCenter:
            rect.moveCenter(center)
        rect.moveLeft(max(bounds.left(), min(rect.left(), bounds.right() - width + 1)))
        editor.setGeometry(rect)

    def name_font(self, font: QFont) -> QFont:
        return font

    def name_rect(self, rect: QRect) -> QRect:
        return rect


class FilesCardDelegate(FilesNameDelegate):
    name_alignment = Qt.AlignmentFlag.AlignHCenter
    def __init__(
        self, entry_role: int, image_for_entry: Callable[[Entry], QImage | None],
        parent: QWidget, strip_for_entry: Callable[[Entry], QImage | None] | None = None,
        *, ai_analyzed: Callable[[str], bool] | None = None,
        is_graded: Callable[[str], bool] | None = None,
        is_active: Callable[[str], bool] | None = None,
        spacing: GallerySpacing = DEFAULT_GALLERY_SPACING,
        title_font_size: float = 10.0,
    ) -> None:
        super().__init__(parent)
        self.entry_role = entry_role
        self.image_for_entry = image_for_entry
        self.strip_for_entry = strip_for_entry
        self.ai_analyzed = ai_analyzed
        self.is_graded = is_graded
        self.is_active = is_active
        self.spacing = spacing
        self.title_font_size = title_font_size
        self.show_rating_badges = True
        self.show_filename = True
        self.rating_at_bottom = False
        self.caption_fields: frozenset[str] = frozenset()
        self.placeholder_color = C.BG_INPUT_DARK
        self.show_color_label = False
        self.resolution_for_entry: Callable[[Entry], str | None] = lambda entry: entry.technical.resolution
        self.card_width = 180
        self.layout_mode = "grid"
        self.grid_fill = False

    def sizeHint(self, option: QStyleOptionViewItem | None, index: QModelIndex) -> QSize:
        side = self.card_width + 2 * self.spacing.inset
        return QSize(side, side + self.spacing.caption_height)

    def name_font(self, font: QFont) -> QFont:
        title_font = QFont(font)
        title_font.setPointSizeF(self.title_font_size)
        return title_font

    def name_rect(self, rect: QRect) -> QRect:
        inset = self.spacing.inset
        metadata_height = FILE_CAPTION_LINE_HEIGHT * len(self.caption_fields - {"rating"})
        if self.rating_at_bottom:
            metadata_height += RATING_CAPTION_HEIGHT
        return rect.adjusted(inset, rect.height() - inset - self.spacing.caption_height,
                             -inset, -inset - metadata_height)

    def _cover_rect(self, rect: QRect) -> QRectF:
        inset = self.spacing.inset
        return QRectF(rect).adjusted(inset, inset, -inset, -inset - self.spacing.caption_height)

    @staticmethod
    def _format_badge(entry: Entry) -> str:
        if entry.is_dir:
            return I18N.tr("files.native.kind.dir", "文件夹")
        if not entry.has_known_media:
            return I18N.tr("files.native.kind.doc", "文件")
        return entry.name.rsplit(".", 1)[-1].upper()[:8] if "." in entry.name else entry.kind.upper()

    def update_rating_layout(self, rated: Sequence[tuple[int, Entry]],
                             rectangles: Sequence[QRect], font: QFont) -> None:
        if self.rating_at_bottom:
            return
        self.show_rating_badges = True
        if not rated:
            return
        badge_font = QFont(font)
        badge_font.setPointSizeF(9)
        metrics = QFontMetrics(badge_font)
        widths: dict[str, int] = {}
        for row, entry in rated:
            cover = self._cover_rect(rectangles[row])
            label = self._format_badge(entry)
            if label not in widths:
                widths[label] = metrics.horizontalAdvance(label) + 12
            format_badge = QRectF(cover.left() + 8, cover.top() + 8, widths[label], 18)
            rating = entry.user.rating
            badge = rating_badge_rect(cover, rating, format_badge, _CARD_RATING_STAR_SIZE)
            if not full_rating_fits(cover, rating, badge, _CARD_RATING_STAR_SIZE):
                self.show_rating_badges = False
                break

    def metadata_lines(self, entry: Entry) -> list[str]:
        values: list[str] = []
        if "resolution" in self.caption_fields:
            values.append(self.resolution_for_entry(entry) or "")
        if "size" in self.caption_fields and not entry.is_dir and entry.size_bytes is not None:
            values.append(entry.size_label)
        if "tags" in self.caption_fields:
            values.append(entry.user.tag_text)
        return [value for value in values if value]

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry: Entry | None = index.data(self.entry_role)
        if not isinstance(entry, Entry):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipRect(option.rect)
        rect = QRectF(option.rect).adjusted(3, 3, -3, -3)
        inset = self.spacing.inset
        cover = self._cover_rect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        active = self.is_active is not None and self.is_active(entry.path)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        offline = entry.availability == "offline"
        if hovered or (selected and self.is_active is not None):
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
            painter.fillRect(cover, QColor(self.placeholder_color))
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

        if active or selected or hovered:
            accented = active or (selected and self.is_active is None)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(C.ORANGE if accented else C.BORDER_HOVER), 1.0))
            painter.drawRoundedRect(cover, 4, 4)

        badge = self._format_badge(entry)
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

        badge_bottom = badge_rect.bottom()
        rating_visible = False
        if self.show_rating_badges and not self.rating_at_bottom and entry.user.rating:
            rating = entry.user.rating
            size = _CARD_RATING_STAR_SIZE
            rating_rect = rating_badge_rect(cover, rating, badge_rect, size)
            rating_visible = full_rating_fits(cover, rating, rating_rect, size)
            if rating_visible:
                badge_bottom = rating_rect.bottom()
                paint_rating_badge(painter, rating_rect, rating, star_size=size,
                                   background_alpha=_CARD_RATING_BACKGROUND_ALPHA)

        if self.ai_analyzed is not None and self.ai_analyzed(entry.path):
            ai_top = badge_bottom + 3 if rating_visible else cover.top() + 8
            ai_rect = QRectF(cover.right() - 31, ai_top, 23, 18)
            badge_font.setBold(True)
            painter.setFont(badge_font)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 18, 18, 216))
            painter.drawRoundedRect(ai_rect, 4, 4)
            painter.setPen(QColor(C.ORANGE))
            painter.drawText(ai_rect, Qt.AlignmentFlag.AlignCenter, "AI")
            badge_font.setBold(False)

        if self.show_color_label and 0 < entry.user.color < len(FILE_LABEL_COLORS):
            painter.setPen(QPen(QColor(C.BG_DEEPEST), 1))
            painter.setBrush(QColor(FILE_LABEL_COLORS[entry.user.color][2]))
            painter.drawEllipse(QRectF(cover.left() + 8, cover.bottom() - 18, 10, 10))

        if self.spacing.caption_height and self.show_filename:
            painter.setFont(self.name_font(option.font))
            title_rect = QRectF(self.name_rect(option.rect))
            painter.setPen(QColor(C.TEXT_HINT if offline else C.TEXT_PRIMARY))
            metrics = painter.fontMetrics()
            title = metrics.elidedText(entry.name, Qt.TextElideMode.ElideMiddle,
                                       max(1, int(title_rect.width()) - 6))
            if selected and self.is_active is None:
                highlight = QRectF(0, 0, min(title_rect.width(), metrics.horizontalAdvance(title) + 6),
                                   min(title_rect.height(), metrics.height() + 2))
                highlight.moveCenter(title_rect.center())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(C.ORANGE))
                painter.drawRoundedRect(highlight, 2, 2)
                painter.setPen(QColor(C.BG_DEEPEST))
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, title)
        if self.rating_at_bottom and entry.user.rating:
            rating = entry.user.rating
            pitch = min(float(_CARD_RATING_STAR_SIZE), max(1.0, (cover.width() - 4) / rating))
            left = cover.center().x() - rating * pitch / 2
            y = option.rect.bottom() - inset - (RATING_CAPTION_HEIGHT + pitch) / 2
            for star in range(rating):
                _paint_star(painter, left + star * pitch + 0.5, y + 0.5, max(1.0, pitch - 1), RATING_STAR_COLOR)
        if self.caption_fields and (lines := self.metadata_lines(entry)):
            detail_font = self.name_font(option.font)
            detail_font.setPointSizeF(max(8.0, self.title_font_size - 1))
            painter.setFont(detail_font)
            details = QRectF(self.name_rect(option.rect))
            details.moveTop(details.bottom())
            details.setHeight(FILE_CAPTION_LINE_HEIGHT)
            painter.setPen(QColor(C.TEXT_DIM))
            for line in lines:
                text = painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight,
                                                       max(1, int(details.width()) - 4))
                painter.drawText(details, Qt.AlignmentFlag.AlignCenter, text)
                details.translate(0, FILE_CAPTION_LINE_HEIGHT)
        # 时间和离线状态覆在封面上。
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
        if (self.is_graded is not None and not entry.is_dir and is_image(entry.path)
                and self.is_graded(entry.path)):
            painter.setFont(badge_font)
            label_width = max(0, int(cover.width()) - 24)
            graded_label = painter.fontMetrics().elidedText(
                I18N.tr("files.native.graded", "Graded"), Qt.TextElideMode.ElideRight, label_width)
            if graded_label:
                width = painter.fontMetrics().horizontalAdvance(graded_label) + 12
                graded_rect = QRectF(cover.right() - width - 6, cover.bottom() - (44 if label else 24),
                                     width, 18)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(18, 18, 18, 216))
                painter.drawRoundedRect(graded_rect, 4, 4)
                painter.setPen(QColor(C.ORANGE))
                painter.drawText(graded_rect, Qt.AlignmentFlag.AlignCenter, graded_label)
        if selected and self.is_active is not None:
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
        elif not selected and option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor(C.TEXT_DIM), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 5, 5)
        painter.restore()


class FilesListDelegate(FilesNameDelegate):
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

    def name_rect(self, rect: QRect) -> QRect:
        return rect.adjusted(70, 0, -6, 0)

    def updateEditorGeometry(self, editor: QLineEdit, option: QStyleOptionViewItem,
                             index: QModelIndex) -> None:
        super().updateEditorGeometry(editor, option, index)
        rect = editor.geometry()
        rect.setHeight(editor.sizeHint().height())
        rect.moveTop(option.rect.center().y() - rect.height() // 2)
        editor.setGeometry(rect)

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
        title_rect = QRectF(self.name_rect(option.rect))
        painter.setFont(option.font)
        painter.setPen(QColor(C.TEXT_HINT if entry.availability == "offline" else C.TEXT_PRIMARY))
        title = painter.fontMetrics().elidedText(entry.name, Qt.TextElideMode.ElideMiddle,
                                                max(1, int(title_rect.width())))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.restore()
