"""Read-only media-pool presentation for thumbnail and metadata views."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PyQt6.QtCore import QModelIndex, QPoint, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QHelpEvent, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygon
from PyQt6.QtWidgets import QAbstractItemView, QLineEdit, QListView, QStyle, QStyleOptionViewItem, QToolTip, QTreeWidget, QWidget

from .media_pool_panel import AssetListModel, GridDelegate
from .media_pool_icons import draw_timeline_glyph, pool_icon
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


def pool_tree_style(row_height: int) -> str:
    return f"""
        QTreeWidget {{ background: {C.BG_PANEL}; alternate-background-color: {C.BG_INPUT};
            color: {C.TEXT_SECONDARY}; border: none; font-size: 11px; }}
        QTreeWidget::item {{ height: {row_height}px; padding: 0 4px; border: none; }}
        QTreeWidget::item:selected {{ background: {C.BG_DARKEST}; color: {C.TEXT_PRIMARY}; }}
        QTreeWidget::item:hover {{ background: {C.BG_ACTIVE}; }}
        QHeaderView::section {{ background: {C.BG_INPUT}; color: {C.TEXT_LABEL}; border: none;
            border-right: 1px solid {C.BORDER}; border-bottom: 1px solid {C.PANEL_DIVIDER}; padding: 3px 6px; }}
    """


def configure_pool_columns(tree: QTreeWidget) -> None:
    tree.setHeaderLabels([
        I18N.tr("edit.media_pool.clip_name", "Clip Name"),
        I18N.tr("edit.media_pool.header_reel", "Reel Name"),
        I18N.tr("edit.media_pool.header_scene", "Scene/Shot/Take"),
        I18N.tr("edit.media_pool.header_duration", "Duration"),
        I18N.tr("edit.media_pool.header_fps", "FPS"),
        I18N.tr("edit.media_pool.header_resolution", "Resolution"),
        I18N.tr("edit.media_pool.header_codec", "Codec"),
        I18N.tr("edit.media_pool.header_audio", "Audio"),
        I18N.tr("edit.media_pool.header_path", "File Path"),
    ])
    for column, width in enumerate((200, 90, 120, 90, 65, 110, 90, 70, 250)):
        tree.setColumnWidth(column, width)
    tree.setAlternatingRowColors(True)
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setStyleSheet(pool_tree_style(23))
    tree.header().setStretchLastSection(False)


def configure_pool_posters(view: QListView, delegate: ResolvePoolDelegate, *, metadata: bool) -> None:
    delegate.metadata_mode = metadata
    view.setViewMode(QListView.ViewMode.ListMode if metadata else QListView.ViewMode.IconMode)
    margins = (0, 0, 0, 0) if metadata else (15, 8, 0, 0)
    view.setViewportMargins(*margins)
    view.setFlow(QListView.Flow.TopToBottom if metadata else QListView.Flow.LeftToRight)
    view.setWrapping(not metadata)
    view.setMovement(QListView.Movement.Static)
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setSpacing(0)
    view.setUniformItemSizes(True)
    if metadata:
        view.setGridSize(QSize())


def resize_pool_posters(view: QListView, delegate: ResolvePoolDelegate, preferred: int) -> None:
    width = preferred
    padding = delegate.THUMBNAIL_CELL_PADDING
    cell_width = preferred + padding
    if view.isVisible():
        maximum = view.maximumViewportSize().width()
        columns = max(1, maximum // cell_width)
        available = maximum - view.verticalScrollBar().sizeHint().width() - 2
        min_padding = delegate.MIN_THUMBNAIL_CELL_PADDING
        # Fit a near-full trailing slot by tightening the gutters if one
        # more poster fits at the requested zoom.
        if (available - columns * cell_width >= cell_width * 7 // 10
                and available // (columns + 1) >= preferred + min_padding):
            columns += 1
        cell_width = min(cell_width, max(1, available // columns))
        width = min(preferred, max(1, cell_width - min_padding))
    height = round(width * 9 / 16)
    icon = QSize(width, height)
    grid = QSize(cell_width, height + padding)
    if view.iconSize() != icon or view.gridSize() != grid:
        delegate.set_thumb_size((width, height))
        delegate.thumbnail_cell_width = cell_width
        view.setIconSize(icon)
        view.setGridSize(grid)


def configure_pool_shelf(view: QListView, delegate: ResolvePoolDelegate) -> None:
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setFlow(QListView.Flow.LeftToRight)
    view.setWrapping(True)
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setMovement(QListView.Movement.Static)
    view.setGridSize(QSize(116, 88))
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    delegate.view = view
    delegate.set_thumb_size((86, 48))
    view.setItemDelegate(delegate)


def pool_shelf_height(width: int, count: int) -> int:
    columns = max(1, (width - 16) // 116)
    rows = (count + columns - 1) // columns
    return max(88, min(176, rows * 88))


class ResolvePoolDelegate(GridDelegate):
    """Compact 16:9 posters, or a poster with existing metadata beside it."""

    THUMBNAIL_CELL_PADDING = 34
    MIN_THUMBNAIL_CELL_PADDING = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.metadata_mode: bool = False
        self.show_duration_badge: bool = False
        self.show_clip_information: bool = True
        self.card_data_role: int = Qt.ItemDataRole.UserRole
        self.thumbnail_cell_width: int | None = None
        self._identity_icons = {name: pool_icon(name, C.TEXT_PRIMARY) for name in ("info", "video", "audio", "image")}

    def _item_rects(self, rect: QRect) -> tuple[QRect, QRect]:
        """Return shared poster and name geometry, including rename editors."""
        if self.metadata_mode:
            width = min(144, max(1, rect.width() - 24))
            poster = QRect(rect.left() + 10, rect.top() + 12, width, 81)
            name = QRect(poster.right() + 12, rect.top() + 10,
                         max(0, rect.right() - poster.right() - 22), 22)
            return poster, name
        width = max(1, min(self.thumb_size[0], rect.width() - 12))
        height = max(1, round(width * 9 / 16))
        poster = QRect(rect.left() + (rect.width() - width) // 2,
                       rect.top() + 6, width, height)
        name = QRect(rect.left() + 3, poster.bottom() + 1,
                     max(0, rect.width() - 6), 20)
        return poster, name

    @staticmethod
    def _text(painter: QPainter, rect: QRect, text: str, *,
              muted: bool = False, centered: bool = False) -> None:
        """Use actual font metrics for every single-line label."""
        if rect.width() <= 0:
            return
        painter.setPen(QColor(C.TEXT_HINT if muted else C.TEXT_PRIMARY))
        text = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, rect.width())
        align = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignHCenter if centered else Qt.AlignmentFlag.AlignLeft)
        painter.drawText(rect, align, text)

    @staticmethod
    def _folder(painter: QPainter, rect: QRect) -> None:
        """Draw a softly rounded folder with a recessed paper lip."""
        width = max(12.0, min(rect.width() - 8.0, rect.width() * .78))
        height = width * .65
        x, y = rect.center().x() - width / 2.0, rect.center().y() - height / 2.0
        painter.save()
        painter.translate(x, y)
        painter.scale(width / 64.0, height / 42.0)
        painter.setPen(Qt.PenStyle.NoPen)
        back = QPainterPath()
        back.moveTo(2, 12)
        back.lineTo(2, 3)
        back.quadTo(2, 1, 4, 1)
        back.lineTo(23, 1)
        back.quadTo(24, 1, 25, 3)
        back.lineTo(28, 7)
        back.lineTo(60, 7)
        back.quadTo(62, 7, 62, 9)
        back.lineTo(62, 38)
        back.lineTo(2, 38)
        back.closeSubpath()
        painter.setBrush(QColor(C.TEXT_HINT).darker(118))
        painter.drawPath(back)
        painter.setBrush(QColor(C.TEXT_LABEL).lighter(108))
        painter.drawRoundedRect(QRectF(5, 10, 54, 24), 1.3, 1.3)
        painter.setPen(QPen(QColor(C.TEXT_HINT).darker(125), .7))
        front = QPainterPath()
        front.moveTo(2, 14)
        front.lineTo(62, 14)
        front.quadTo(64, 14, 63.8, 16)
        front.lineTo(61.8, 39)
        front.quadTo(61.6, 41, 59.5, 41)
        front.lineTo(4.5, 41)
        front.quadTo(2.4, 41, 2.2, 39)
        front.lineTo(.2, 16)
        front.quadTo(0, 14, 2, 14)
        gradient = QLinearGradient(0, 14, 0, 41)
        gradient.setColorAt(0, QColor(C.TEXT_HINT).lighter(110))
        gradient.setColorAt(.18, QColor(C.TEXT_HINT))
        gradient.setColorAt(1, QColor(C.TEXT_HINT).darker(125))
        painter.setBrush(gradient)
        painter.drawPath(front)
        painter.setPen(QPen(QColor(C.TEXT_LABEL), .65))
        painter.drawLine(3, 15, 61, 15)
        painter.restore()

    @staticmethod
    def _timeline(painter: QPainter, rect: QRect, index: QModelIndex) -> None:
        """Use the cached timeline poster and small film/active identity marks."""
        preview = index.data(AssetListModel.TIMELINE_PREVIEW_ROLE)
        painter.fillRect(rect, QColor(C.BG_DEEPEST))
        if isinstance(preview, QPixmap) and not preview.isNull():
            scaled = preview.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(rect.left() + (rect.width() - scaled.width()) // 2,
                               rect.top() + (rect.height() - scaled.height()) // 2,
                               scaled)
        # The filmstrip plus tracks is distinct from an ordinary video icon.
        glyph = QRect(rect.left() + 3, rect.bottom() - 18, 20, 15)
        shade = QColor(C.BG_DARKEST)
        shade.setAlpha(180)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shade)
        painter.drawRoundedRect(glyph.adjusted(-1, -1, 2, 1), 2, 2)
        draw_timeline_glyph(painter, glyph, C.TEXT_PRIMARY)
        if index.data(AssetListModel.IS_ACTIVE_ROLE):
            x, y = rect.left(), rect.top()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(C.ORANGE))
            painter.drawPolygon(QPolygon([QPoint(x, y), QPoint(x + 18, y),
                                          QPoint(x, y + 18)]))
            painter.setPen(QPen(QColor(C.TEXT_PRIMARY), 1.2))
            painter.drawLine(x + 3, y + 6, x + 5, y + 8)
            painter.drawLine(x + 5, y + 8, x + 9, y + 3)

    def info_button_rect(self, cell: QRect) -> QRect:
        """One geometry contract for painting, hit testing and popup anchoring."""
        poster, _name = self._item_rects(cell)
        return QRect(poster.right() - 19, poster.bottom() - 19, 19, 19)

    def info_button_visible(self, index: QModelIndex, option: QStyleOptionViewItem | None = None) -> bool:
        """The info affordance belongs only to the card under the pointer."""
        key = (index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE))
        return (self.view is not None and getattr(self.view, "_info_hover_key", None) == key
                or option is not None and bool(option.state & QStyle.StateFlag.State_MouseOver))

    def _draw_info_button(self, painter: QPainter, cell: QRect) -> None:
        rect = self.info_button_rect(cell)
        shade = QColor(C.BG_DARKEST)
        shade.setAlpha(175)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shade)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        self._identity_icons["info"].paint(painter, rect.adjusted(2, 2, -2, -2))

    def _draw_analysis_status_dot(self, painter: QPainter, rect: QRect, asset: Any) -> None:
        """Keep AI state visible above the independent lower-right info target."""
        panel = self._get_panel()
        if panel is None or not getattr(panel, "_media_analysis_enabled", False):
            return
        status = panel._analysis_status_cache.get(asset.asset_id, "")
        color_key = self._ANALYSIS_STATUS_COLOR_KEY.get(status)
        if color_key is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.BADGE_COLORS[color_key]))
            painter.drawEllipse(rect.right() - 9, rect.top() + 3, 6, 6)

    def _draw_comp_clip_icon(self, painter: QPainter, rect: QRect) -> None:
        super()._draw_comp_clip_icon(painter, rect)

    def helpEvent(self, event: QHelpEvent, view: QAbstractItemView, option: QStyleOptionViewItem, index: QModelIndex) -> bool:
        if (self.show_clip_information and index.data(AssetListModel.ITEM_TYPE_ROLE) in ("asset", "timeline")
                and self.info_button_rect(option.rect).contains(event.pos())):
            QToolTip.showText(event.globalPos(), I18N.tr("edit.media_pool.show_clip_information", "Show Clip Information"), view)
            return True
        return super().helpEvent(event, view, option, index)

    @staticmethod
    def _value(value: Any) -> str:
        """Show unavailable metadata as an em dash."""
        return str(value) if value is not None and str(value).strip() else "—"

    def _metadata_lines(self, item_type: str, data: Any) -> list[str]:
        """Read asset/reference fields without synchronous media probing."""
        if item_type == "bin":
            if data is None:
                return []
            count = len(getattr(data, "asset_ids", ()))
            return [I18N.tr("edit.media_pool.bin", "Bin"),
                    I18N.tr("edit.media_pool.item_count", "{count} clips", count=count)]
        if item_type == "timeline":
            return [I18N.tr("edit.media_pool.timeline", "Timeline"),
                    f"{self._value(getattr(data, 'width', None))} × "
                    f"{self._value(getattr(data, 'height', None))}  ·  "
                    f"{self._value(getattr(data, 'fps', None))} fps"]
        metadata = getattr(data, "metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        production = []
        for key, fallback in (("reel", "Reel"), ("scene", "Scene"),
                              ("shot", "Shot"), ("take", "Take")):
            label = I18N.tr(f"edit.media_pool.{key}", fallback)
            production.append(f"{label}: {self._value(getattr(data, key, None))}")
        duration = getattr(data, "duration_seconds", None)
        duration_text = (self._format_duration(float(duration))
                         if isinstance(duration, (int, float)) and duration >= 0 else "—")
        width, height = getattr(data, "width", None), getattr(data, "height", None)
        resolution = f"{width} × {height}" if width and height else "—"
        return ["  ·  ".join(production[:2]), "  ·  ".join(production[2:]),
                f"{duration_text}  ·  {resolution}  ·  "
                f"{self._value(getattr(data, 'fps', None))} fps  ·  "
                f"{self._value(getattr(data, 'codec', None))}"]

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        """Paint posters and existing media states without a card surface."""
        if not index.isValid():
            return
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        poster, name = self._item_rects(option.rect)
        item_type = index.data(AssetListModel.ITEM_TYPE_ROLE)
        data = index.data(self.card_data_role)
        if item_type in ("asset", "timeline"):
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(poster), 4.0, 4.0)
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        if item_type == "bin":
            self._folder(painter, poster)
        elif item_type == "timeline":
            self._timeline(painter, poster, index)
        elif item_type == "asset" and data is not None:
            self._draw_thumbnail_area(painter, poster, data, index)
            self._draw_compact_indicators(painter, poster, data)
            if not getattr(data, "is_comp_clip", False) and poster.width() >= 56:
                media_type = getattr(data, "media_type", "video")
                badge_name = "audio" if media_type == "audio" or getattr(data, "audio_channels", 0) else ("image" if media_type in ("image", "image_sequence") else "video")
                self._identity_icons[badge_name].paint(painter, QRect(poster.left() + 3, poster.bottom() - 16, 15, 13))
            color_label = getattr(data, "color_label", "none")
            if color_label != "none" and QColor.isValidColorName(color_label):
                painter.fillRect(QRect(poster.left(), poster.bottom() - 2, poster.width(), 3), QColor(color_label))
        if item_type in ("asset", "timeline"):
            painter.restore()
            if self.show_clip_information and self.info_button_visible(index, option):
                self._draw_info_button(painter, option.rect)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(C.PANEL_DIVIDER), .8))
            painter.drawRoundedRect(QRectF(poster).adjusted(.4, .4, -.4, -.4), 4, 4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        panel = self._get_panel()
        if item_type == "timeline" and panel is not None:
            timeline_id = index.data(AssetListModel.ITEM_ID_ROLE)
            if isinstance(timeline_id, str):
                panel.retry_timeline_preview_when_idle(timeline_id)
        drop_target = (item_type == "bin" and panel is not None and
                       getattr(panel, "_drop_target_bin_id", None) ==
                       index.data(AssetListModel.ITEM_ID_ROLE))
        if selected or hovered or drop_target:
            painter.setPen(QPen(QColor(C.ORANGE if selected or drop_target
                                      else C.BORDER_HOVER), 1.5 if drop_target else 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(poster.adjusted(-1, -1, 1, 1), 4, 4)
        font = QFont(option.font)
        font.setPixelSize(11)
        painter.setFont(font)
        self._text(painter, name, str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                   centered=not self.metadata_mode)
        if self.metadata_mode:
            for row, text in enumerate(self._metadata_lines(str(item_type), data)):
                self._text(painter, QRect(name.left(), name.bottom() + 1 + row * 20,
                                         name.width(), 20), text, muted=True)
            painter.setPen(QPen(QColor(C.BORDER_SUBTLE), 1))
            painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Metadata rows have fixed height; posters retain 16:9 geometry."""
        if self.metadata_mode:
            width = self.view.viewport().width() if self.view is not None else 380
            return QSize(max(1, width), 106)
        return self.thumbnail_cell_size()

    def thumbnail_cell_size(self) -> QSize:
        width, height = self.thumb_size
        return QSize(self.thumbnail_cell_width or width + self.THUMBNAIL_CELL_PADDING,
                     height + self.THUMBNAIL_CELL_PADDING)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem,
                             index: QModelIndex) -> None:
        """Align inline rename with the painted name in either mode."""
        _, name = self._item_rects(option.rect)
        if isinstance(editor, QLineEdit):
            editor.setAlignment(Qt.AlignmentFlag.AlignVCenter | (
                Qt.AlignmentFlag.AlignLeft if self.metadata_mode
                else Qt.AlignmentFlag.AlignHCenter))
        editor.setGeometry(name)
