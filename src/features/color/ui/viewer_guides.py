"""Viewer Guides — aspect-ratio and safe-area overlay system for the Color viewport.

Provides:
- GuideOverlayItem: QGraphicsItem that draws letterbox bars / safe-area outlines
  in image coordinates (follows zoom & pan).
- ViewerGuidesPanel: dark-themed popup panel with thumbnail cards for selecting
  guides, matching the app's visual style.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QFrame,
    QCheckBox,
    QGraphicsItem,
    QHBoxLayout,
    QLabel,
    QStyle,
    QVBoxLayout,
    QWidget)

from src.shared.i18n.i18n import I18N


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class GuideType(Enum):
    ASPECT_RATIO = "aspect_ratio"
    SAFE_AREA = "safe_area"


@dataclass(frozen=True)
class GuideDefinition:
    id: str
    label: str
    guide_type: GuideType
    value: float
    secondary_value: float = 0.0


SOCIAL_MEDIA_GUIDES: List[GuideDefinition] = [
    GuideDefinition("1:1",    "1:1",    GuideType.ASPECT_RATIO, 1.0),
    GuideDefinition("4:5",    "4:5",    GuideType.ASPECT_RATIO, 4.0 / 5.0),
    GuideDefinition("9:16",   "9:16",   GuideType.ASPECT_RATIO, 9.0 / 16.0),
    GuideDefinition("1.91:1", "1.91:1", GuideType.ASPECT_RATIO, 1.91),
    GuideDefinition("16:9",   "16:9",   GuideType.ASPECT_RATIO, 16.0 / 9.0),
]

BROADCAST_FILM_GUIDES: List[GuideDefinition] = [
    GuideDefinition("default", "Default", GuideType.ASPECT_RATIO, 0.0),
    GuideDefinition("1.33",    "1.33",    GuideType.ASPECT_RATIO, 4.0 / 3.0),
    GuideDefinition("1.77",    "1.77",    GuideType.ASPECT_RATIO, 16.0 / 9.0),
    GuideDefinition("1.85",    "1.85",    GuideType.ASPECT_RATIO, 1.85),
    GuideDefinition("2.35",    "2.35",    GuideType.ASPECT_RATIO, 2.35),
    GuideDefinition("2.39",    "2.39",    GuideType.ASPECT_RATIO, 2.39),
]

SAFE_AREA_GUIDES: List[GuideDefinition] = [
    GuideDefinition("extents", "Extents", GuideType.SAFE_AREA, 0.0),
    GuideDefinition("action",  "Action",  GuideType.SAFE_AREA, 0.05),
    GuideDefinition("title",   "Title",   GuideType.SAFE_AREA, 0.10),
    GuideDefinition("center",  "Center",  GuideType.SAFE_AREA, 0.0, 0.05),
]


# ---------------------------------------------------------------------------
# GuideOverlayItem — drawn on the Canvas QGraphicsScene
# ---------------------------------------------------------------------------

class GuideOverlayItem(QGraphicsItem):
    """Overlay that draws aspect-ratio letterbox bars or safe-area outlines.

    Bound to image coordinates (no ItemIgnoresTransformations), so the guides
    follow the image during zoom and pan.  Uses cosmetic pens for constant
    screen-space line width.
    """

    def __init__(
        self,
        image_width: float,
        image_height: float,
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(parent)
        self._image_w: float = image_width
        self._image_h: float = image_height
        self._guide: Optional[GuideDefinition] = None
        self.setZValue(90)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)

    # -- public API ----------------------------------------------------------

    def set_guide(self, guide: Optional[GuideDefinition]) -> None:
        self._guide = guide
        self.update()

    def update_image_size(self, w: float, h: float) -> None:
        self._image_w = w
        self._image_h = h
        self.update()

    # -- QGraphicsItem overrides ---------------------------------------------

    def boundingRect(self) -> QRectF:  # type: ignore[override]
        return QRectF(-100000.0, -100000.0, 200000.0, 200000.0)

    def paint(  # type: ignore[override]
        self,
        painter: QPainter,
        option: QStyle.QStyleOptionGraphicsItem,
        widget: Optional[QWidget],
    ) -> None:
        if self._guide is None:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._guide.guide_type == GuideType.ASPECT_RATIO:
            self._paint_aspect_ratio(painter)
        elif self._guide.guide_type == GuideType.SAFE_AREA:
            self._paint_safe_area(painter)

    # -- private drawing -----------------------------------------------------

    def _paint_aspect_ratio(self, painter: QPainter) -> None:
        ratio: float = self._guide.value  # type: ignore[union-attr]
        if ratio <= 0.0:
            return

        img_w: float = self._image_w
        img_h: float = self._image_h
        img_rect = QRectF(0.0, 0.0, img_w, img_h)

        img_aspect: float = img_w / img_h
        if ratio > img_aspect:
            target_h: float = img_h
            target_w: float = img_h * ratio
        else:
            target_w = img_w
            target_h = img_w / ratio

        tx: float = (img_w - target_w) / 2.0
        ty: float = (img_h - target_h) / 2.0
        target_rect = QRectF(tx, ty, target_w, target_h)

        # Dimmed bars outside the target area
        img_path = QPainterPath()
        img_path.addRect(img_rect)
        target_path = QPainterPath()
        target_path.addRect(target_rect)
        dim_path = img_path - target_path

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 80)))
        painter.drawPath(dim_path)

        # Outline
        outline_pen = QPen(QColor(255, 255, 255, 180), 1.0)
        outline_pen.setCosmetic(True)
        painter.setPen(outline_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(target_rect)

    def _paint_safe_area(self, painter: QPainter) -> None:
        img_w: float = self._image_w
        img_h: float = self._image_h
        inset: float = self._guide.value  # type: ignore[union-attr]

        if self._guide.id == "center":  # type: ignore[union-attr]
            cx: float = img_w / 2.0
            cy: float = img_h / 2.0
            half_len: float = self._guide.secondary_value * min(img_w, img_h)

            pen = QPen(QColor(255, 255, 255, 180), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(QPointF(cx - half_len, cy), QPointF(cx + half_len, cy))
            painter.drawLine(QPointF(cx, cy - half_len), QPointF(cx, cy + half_len))
            return

        if self._guide.id == "extents":  # type: ignore[union-attr]
            safe_rect = QRectF(0.0, 0.0, img_w, img_h)
        else:
            dx: float = img_w * inset
            dy: float = img_h * inset
            safe_rect = QRectF(dx, dy, img_w - 2.0 * dx, img_h - 2.0 * dy)

        pen = QPen(QColor(255, 255, 255, 160), 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(safe_rect)

        # Midpoint ticks for action / title
        if self._guide.id in ("action", "title"):  # type: ignore[union-attr]
            tick_pen = QPen(QColor(255, 255, 255, 100), 1.0)
            tick_pen.setCosmetic(True)
            painter.setPen(tick_pen)
            mx = safe_rect.center().x()
            my = safe_rect.center().y()
            t = 8.0
            painter.drawLine(QPointF(mx, safe_rect.top()), QPointF(mx, safe_rect.top() + t))
            painter.drawLine(QPointF(mx, safe_rect.bottom()), QPointF(mx, safe_rect.bottom() - t))
            painter.drawLine(QPointF(safe_rect.left(), my), QPointF(safe_rect.left() + t, my))
            painter.drawLine(QPointF(safe_rect.right(), my), QPointF(safe_rect.right() - t, my))


# ---------------------------------------------------------------------------
# GuideItemWidget — single clickable thumbnail card
# ---------------------------------------------------------------------------

# The 3:2 source frame leaves visible side or top mattes for both portrait
# and widescreen guides. The retained image inside it uses the actual ratio.
def _guide_preview_svg(guide: GuideDefinition) -> QByteArray:
    if guide.guide_type == GuideType.ASPECT_RATIO:
        width, height = 33.0, 22.0
        if guide.value > 0.0:
            width = min(width, height * guide.value)
            height = width / guide.value
        x = (38.0 - width) / 2.0
        y = (26.0 - height) / 2.0
        detail = (
            f'<rect x="{x:.3f}" y="{y:.3f}" width="{width:.3f}" height="{height:.3f}" '
            'fill="#444851" stroke="#edf0f4" stroke-width="1.15"/>'
        )
    else:
        # Enlarge the inset differences for legibility at thumbnail size.
        marks = {
            "extents": "M2.5 7V2h6m21 0h6v5m0 12v5h-6m-21 0h-6v-5",
            "action": "M6 9V5h5m16 0h5v4m0 8v4h-5m-16 0H6v-4",
            "title": "M9 11V7h4m12 0h4v4m0 4v4h-4m-12 0H9v-4M19 7v2m0 8v2",
            "center": "M14.5 13h9M19 8.5v9",
        }
        detail = (
            f'<path d="{marks[guide.id]}" fill="none" stroke="#edf0f4" '
            'stroke-width="1.5" stroke-linecap="square"/>'
        )

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="38" height="26" viewBox="0 0 38 26">'
        '<rect x="2.5" y="2" width="33" height="22" rx="0.5" '
        'fill="#16181c" stroke="#7c828d" stroke-width="1"/>'
        f'{detail}</svg>'
    )
    return QByteArray(svg.encode("ascii"))


class GuideItemWidget(QWidget):
    """A clickable, vector-rendered miniature of a viewer guide."""

    clicked = pyqtSignal(str)

    def __init__(
        self,
        guide: GuideDefinition,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._guide = guide
        self._preview = QSvgRenderer(_guide_preview_svg(guide), self)
        self._is_active = False
        self.setFixedSize(44, 40)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(guide.label)
        self.setAccessibleName(guide.label)

    def set_active(self, active: bool) -> None:
        self._is_active = active
        self.update()

    def mousePressEvent(self, event: object) -> None:
        self.clicked.emit(self._guide.id)

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._is_active:
            label_color, opacity = "#f3f4f6", 1.0
        elif self.underMouse():
            label_color, opacity = "#d9dce2", 0.9
        else:
            label_color, opacity = "#a8adb6", 0.7

        painter.setOpacity(opacity)
        self._preview.render(painter, QRectF((self.width() - 38) / 2, 0, 38, 26))
        painter.setOpacity(1.0)
        painter.setPen(QColor(label_color))
        font = painter.font()
        font.setPixelSize(10)
        font.setBold(self._is_active)
        painter.setFont(font)
        painter.drawText(0, 26, self.width(), 14, Qt.AlignmentFlag.AlignCenter, self._guide.label)
        painter.end()


# ---------------------------------------------------------------------------
# ViewerGuidesPanel — popup selection panel
# ---------------------------------------------------------------------------

class ViewerGuidesPanel(QWidget):
    """Popup panel for selecting viewer guide overlays.

    Emitted signal ``guide_selected`` carries (guide_type_str, guide_id, params).
    Selecting ``"default"`` (id='default') means clear the overlay.
    """

    guide_selected = pyqtSignal(str, str, dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(304)

        self._active_guide: Optional[GuideDefinition] = None
        self._items: Dict[str, GuideItemWidget] = {}

        self._setup_ui()
        self.set_active_guide(None)

    # -- public API ----------------------------------------------------------

    def set_active_guide(self, guide_id: Optional[str]) -> None:
        self._active_guide = self._guide_by_id(guide_id)
        for key, item in self._items.items():
            item.set_active(key == (guide_id or "default"))

    def show_at(self, global_pos: QPointF) -> None:
        self.move(int(global_pos.x()), int(global_pos.y()))
        self.show()
        self.setFocus()

    # -- UI construction -----------------------------------------------------

    def _setup_ui(self) -> None:
        outer = QFrame(self)
        outer.setObjectName("guidesPanelOuter")
        outer.setStyleSheet("""
            QFrame#guidesPanelOuter {
                background-color: #202126;
                border: 1px solid #34353a;
                border-radius: 4px;
            }
            QFrame#guidesPanelOuter QCheckBox {
                background: transparent;
                border: none;
            }
        """)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(6)

        # Section 1: Social Media
        outer_layout.addWidget(
            self._section_header(I18N.tr("guides.social_media", "Social Media"))
        )
        outer_layout.addWidget(self._guide_row(SOCIAL_MEDIA_GUIDES))

        outer_layout.addWidget(self._divider())

        # Section 2: Broadcast & Film
        outer_layout.addWidget(
            self._section_header(I18N.tr("guides.broadcast_film", "Broadcast & Film"))
        )
        outer_layout.addWidget(self._guide_row(BROADCAST_FILM_GUIDES))

        # Aspect-ratio guides default to a non-destructive outline.  Black
        # fill turns the guide into a cinema matte; the optional export flag
        # is carried with the guide selection so the Edit timeline can burn
        # it into its final render deliberately.
        self._black_fill_check = QCheckBox(
            I18N.tr("guides.black_fill", "Black matte"))
        self._black_fill_check.toggled.connect(self._on_black_fill_toggled)
        outer_layout.addWidget(self._black_fill_check)

        self._burn_in_check = QCheckBox(
            I18N.tr("guides.burn_in_export", "Burn into export"))
        self._burn_in_check.setEnabled(False)
        self._burn_in_check.toggled.connect(self._on_burn_in_toggled)
        outer_layout.addWidget(self._burn_in_check)

        outer_layout.addWidget(self._divider())

        # Section 3: Safe Area
        outer_layout.addWidget(
            self._section_header(I18N.tr("guides.safe_area", "Safe Area Guidelines"))
        )
        outer_layout.addWidget(self._guide_row(SAFE_AREA_GUIDES))

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(outer)

    def _section_header(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            "color: #d7d8db; font-size: 11px; font-weight: 600; "
            "background: transparent; border: none;"
        )
        return label

    def _divider(self) -> QFrame:
        """Create the subtle separator used between compact guide groups."""
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #414247; border: none;")
        return divider

    def _guide_row(self, guides: List[GuideDefinition]) -> QWidget:
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        for guide in guides:
            item = GuideItemWidget(guide)
            item.clicked.connect(
                lambda gid=guide.id, g=guide: self._on_item_clicked(gid, g)
            )
            self._items[guide.id] = item
            row_layout.addWidget(item)

        row_layout.addStretch()
        return row

    def _on_item_clicked(self, guide_id: str, guide: GuideDefinition) -> None:
        self.set_active_guide(guide_id)
        self._emit_guide_selection(guide)
        self.close()

    def _emit_guide_selection(self, guide: GuideDefinition) -> None:
        """Emit the current guide together with the live matte options."""
        params: Dict[str, float | bool] = {}
        if guide.guide_type == GuideType.ASPECT_RATIO:
            params = {
                "ratio": guide.value,
                "fill_black": self._black_fill_check.isChecked(),
                "burn_in": self._burn_in_check.isChecked(),
            }
        elif guide.guide_type == GuideType.SAFE_AREA:
            params = {"inset": guide.value, "secondary": guide.secondary_value}

        self.guide_selected.emit(guide.guide_type.value, guide.id, params)

    def _emit_active_aspect_guide(self) -> None:
        """Refresh the active aspect guide when a matte option changes."""
        guide = self._active_guide
        if guide is not None and guide.guide_type == GuideType.ASPECT_RATIO:
            self._emit_guide_selection(guide)

    def _on_black_fill_toggled(self, enabled: bool) -> None:
        """Only allow export burn-in when the black cinema matte is enabled."""
        self._burn_in_check.setEnabled(enabled)
        if not enabled and self._burn_in_check.isChecked():
            self._burn_in_check.setChecked(False)
            return
        self._emit_active_aspect_guide()

    def _on_burn_in_toggled(self, _enabled: bool) -> None:
        """Persist the export choice immediately for the active cinema guide."""
        self._emit_active_aspect_guide()

    @staticmethod
    def _guide_by_id(guide_id: Optional[str]) -> Optional[GuideDefinition]:
        """Find a guide definition by its stable selection identifier."""
        if guide_id is None:
            return None
        for guide in (
            SOCIAL_MEDIA_GUIDES + BROADCAST_FILM_GUIDES + SAFE_AREA_GUIDES
        ):
            if guide.id == guide_id:
                return guide
        return None
