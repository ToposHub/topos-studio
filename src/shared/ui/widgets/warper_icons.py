"""Small, resolution independent glyphs for the Color Warper palette."""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF


def warper_icon(name: str, color: str = "#b8b8bd") -> QIcon:
    """Draw native-size line icons without font/emoji dependencies."""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(2.0, 2.0)
    ink = QColor(color)
    p.setPen(QPen(ink, 1.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))

    def line(x: float, y: float, xx: float, yy: float) -> None:
        p.drawLine(QPointF(x, y), QPointF(xx, yy))

    if name in {"select", "column", "ring"}:
        if name != "select":
            p.setPen(QPen(ink, 0.6, Qt.PenStyle.DotLine))
            if name == "column":
                line(7, 2, 7, 17)
            else:
                p.drawEllipse(QRectF(2, 2, 14, 14))
            p.setPen(QPen(ink, 1.0))
        p.setBrush(ink)
        p.drawPolygon(QPolygonF([QPointF(5, 3), QPointF(7, 16), QPointF(10, 12),
                                QPointF(14, 16), QPointF(16, 14), QPointF(12, 10), QPointF(17, 8)]))
    elif name in {"hs", "chroma", "resolution_h", "resolution_s", "all"}:
        if name == "chroma":
            p.drawPolygon(QPolygonF([QPointF(3, 2), QPointF(17, 6), QPointF(5, 17)]))
            line(3, 2, 10, 9); line(17, 6, 10, 9); line(5, 17, 10, 9)
        else:
            vertices = [QPointF(10 + 8 * math.cos(i * math.pi / 3),
                               10 + 8 * math.sin(i * math.pi / 3)) for i in range(6)]
            p.drawPolygon(QPolygonF(vertices))
            for v in vertices:
                p.drawLine(QPointF(10, 10), v)
    elif name in {"cl", "row", "columns", "rows", "single"}:
        for i in range(4):
            q = 3 + i * 4.5
            line(q, 3, q, 16.5); line(3, q, 16.5, q)
        if name in {"row", "rows"}:
            p.setPen(QPen(ink, 2.0)); line(3, 7.5, 16.5, 7.5)
        elif name == "columns":
            p.setPen(QPen(ink, 2.0)); line(7.5, 3, 7.5, 16.5)
        elif name == "single":
            p.setBrush(ink); p.drawEllipse(QPointF(7.5, 7.5), 2, 2)
    elif name in {"draw", "stroke", "point_stroke", "smooth"}:
        line(5, 14, 14, 4); line(14, 4, 16, 6); line(16, 6, 7, 16)
        line(5, 14, 4, 18); line(4, 18, 7, 16)
        if name in {"stroke", "point_stroke"}:
            p.drawEllipse(QRectF(1, 2, 10, 10)); line(5, 5, 5, 9); line(3, 7, 7, 7)
        if name == "point_stroke":
            p.drawEllipse(QPointF(14, 4), 2, 2)
    elif name in {"pin", "add_pin"}:
        p.drawEllipse(QRectF(6, 2, 8, 9)); line(10, 11, 10, 17); line(6, 17, 14, 17)
        if name == "add_pin":
            line(15, 11, 15, 17); line(12, 14, 18, 14)
    elif name in {"pull", "push", "expand"}:
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            a, b = (3, 7) if name in {"push", "expand"} else (7, 3)
            line(10 + sx * a, 10 + sy * a, 10 + sx * b, 10 + sy * b)
            line(10 + sx * b, 10 + sy * b, 10 + sx * (b - 3), 10 + sy * b)
            line(10 + sx * b, 10 + sy * b, 10 + sx * b, 10 + sy * (b - 3))
    elif name in {"falloff_add", "falloff_sub"}:
        p.drawArc(QRectF(3, 3, 12, 12), 0, 300 * 16)
        line(11, 13, 18, 13)
        if name == "falloff_add":
            line(14.5, 9.5, 14.5, 16.5)
    elif name == "invert":
        p.drawArc(QRectF(4, 4, 12, 12), 30 * 16, 260 * 16)
        line(13, 2, 17, 4); line(17, 4, 14, 7)
        p.setBrush(ink); p.drawEllipse(QPointF(5, 6), 1.5, 1.5)
    elif name == "convert_pin":
        for x in (3, 8, 17):
            p.drawEllipse(QPointF(x, 10), 1.5, 1.5)
        line(10, 10, 14, 10); line(12, 8, 14, 10); line(12, 12, 14, 10)
    elif name == "reset":
        p.drawArc(QRectF(4, 3, 13, 13), -50 * 16, 290 * 16)
        line(3, 3, 3, 8); line(3, 8, 8, 8)
    elif name == "link":
        p.rotate(-35)
        p.drawRoundedRect(QRectF(-2, 9, 10, 5), 2.5, 2.5)
        p.drawRoundedRect(QRectF(5, 9, 10, 5), 2.5, 2.5)
    elif name == "delete":
        line(4, 5, 16, 5); line(7, 3, 13, 3)
        p.drawRect(QRectF(6, 6, 8, 11)); line(9, 8, 9, 15); line(12, 8, 12, 15)
    elif name == "more":
        p.setBrush(ink)
        for x in (4, 10, 16):
            p.drawEllipse(QPointF(x, 10), 1, 1)
    elif name == "space":
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(12):
            p.setBrush(QColor.fromHsvF(i / 12, 0.9, 0.9))
            p.drawPie(QRectF(2, 2, 16, 16), i * 30 * 16, 32 * 16)
    else:
        path = QPainterPath(QPointF(3, 15)); path.cubicTo(5, 2, 12, 18, 17, 4)
        p.drawPath(path)
    p.end()
    return QIcon(pixmap)
