"""ChevronComboBox -- 全局统一的下拉选择框

在 macOS 上，原生 QComboBox 渲染（NSPopUpButton）会忽略 Qt stylesheet 中
::down-arrow 和 ::drop-down 的样式规则。此子类通过 paintEvent 自绘 chevron
箭头，保证所有平台上箭头可见且风格一致，而不替换 Qt 的控件样式所有权。

交互特性：
- 自绘 chevron 箭头（默认灰色、悬停白色、展开时橙色）
- 弹出列表定位在控件下方，自动适配屏幕边界
- 自动应用 COMBO_DROPDOWN_VIEW_STYLE 弹出列表样式
"""
from PyQt6.QtWidgets import QComboBox
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QColor, QPainter

from src.shared.ui.style_sheet import apply_combo_dropdown_style


class ChevronComboBox(QComboBox):
    """QComboBox that draws its own chevron arrow via paintEvent.

    On macOS, native QComboBox rendering (NSPopUpButton) ignores Qt stylesheet
    rules for ::down-arrow and ::drop-down sub-controls. This subclass draws a
    custom chevron indicator directly in paintEvent while preserving the
    application-owned Qt style for safe native teardown.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._popup_open = False
        # Apply the shared QListView palette/spacing without replacing QStyle.
        apply_combo_dropdown_style(self)
        self.setStyleSheet("""
            QComboBox {
                background-color: #232325;
                border: 1px solid #151517;
                border-radius: 6px;
                padding: 2px 12px;
                padding-right: 30px;
                color: #ffffff;
                font-size: 12px;
                min-height: 18px;
                max-height: 18px;
            }
            QComboBox:hover { background-color: #2e2e34; }
            QComboBox:disabled {
                color: #666666; background-color: #1d1d1f;
                border-color: #1a1a1c;
            }
            QComboBox::drop-down { border: none; width: 0px; }
            QComboBox::down-arrow { image: none; border: none; }
        """)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = painter.pen()
        # Arrow color: orange when open, white on hover, light gray default;
        # disabled combos keep a dim chevron (达芬奇式整列变灰)
        if not self.isEnabled():
            pen.setColor(QColor("#55555a"))
        elif self._popup_open:
            pen.setColor(QColor("#ff8800"))
        elif self.underMouse():
            pen.setColor(QColor("#ffffff"))
        else:
            pen.setColor(QColor("#b0b0b0"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        w = self.width()
        h = self.height()
        cx = w - 14
        cy = h // 2
        painter.drawLine(cx, cy - 2, cx + 4, cy + 2)
        painter.drawLine(cx + 4, cy + 2, cx + 8, cy - 2)
        painter.end()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def showPopup(self):
        """Show the dropdown popup below the combo box, not overlaying it."""
        self._popup_open = True
        self.update()
        super().showPopup()
        popup = self.view().window()
        if popup:
            # Keep popup container transparent; the QListView inside already has
            # COMBO_DROPDOWN_VIEW_STYLE (background + border + border-radius)
            # applied by apply_combo_dropdown_style() in __init__.
            popup.setStyleSheet("background: transparent; border: none;")
            pos = self.mapToGlobal(QPointF(0, self.height()))
            popup.move(int(pos.x()), int(pos.y()) + 2)
            # Guarantee the popup is wide enough to show the longest item
            # fully (the default popup geometry can be narrower than the
            # combo itself when the list view has no explicit size hint).
            fm = self.fontMetrics()
            needed = max(
                (fm.horizontalAdvance(self.itemText(i))
                 for i in range(self.count())),
                default=0) + 42  # padding + chevron/scroller slack
            if popup.width() < needed:
                self.view().setMinimumWidth(needed)
                popup.resize(needed, popup.height())
            # Keep within screen bounds
            screen = self.screen()
            if screen:
                available = screen.availableGeometry()
                popup_rect = popup.geometry()
                if popup_rect.bottom() > available.bottom():
                    popup.move(popup_rect.x(),
                               int(self.mapToGlobal(QPointF(0, 0)).y())
                               - popup_rect.height() - 2)

    def hidePopup(self):
        self._popup_open = False
        self.update()
        super().hidePopup()
