"""Viewport Overlays — 3D 视口叠加层（Phase 4）。

绘制 C4D 风格的视口辅助元素（独立于 scene 输出，纯屏幕空间 QPainter 绘制）：
- 四视图布局分区线 + 活动视口高亮边框
- 各子视图标签（Perspective / Top / Right / Front）

这些是**视图状态**的视觉反馈，不进合成输出、不写深度（对应 audit plan
"helpers never write depth for composite"）。

设计：
- 本模块只负责绘制，不持有状态——由 CompositeViewer 的 paintEvent 调用，
  传入 Viewport3DState + painter + 视口矩形。
- 四视图布局：2×2 网格，左上 Perspective / 右上 Top / 左下 Right / 右下 Front
  （QUAD_VIEW_ORDER 定义顺序）。
- 单视图（SINGLE）时不画分区（仅可选画透视标签）。

Phase 4 范围：分区线 + 标签 + 活动边框。Phase 5/6 会扩展（对象轮廓、吸附指示）。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen

from src.features.composite.ui.viewport3d_state import (
    Viewport3DState, ViewportViewKind,
    QUAD_VIEW_ORDER, VIEW_KIND_LABELS)

# ── 视觉常量 ──
_DIVIDER_COLOR = QColor(40, 40, 40, 200)        # 四视图分割线
_DIVIDER_WIDTH = 1.0
_ACTIVE_BORDER_COLOR = QColor(255, 170, 40, 220)  # 活动视口高亮（C4D 橙）
_ACTIVE_BORDER_WIDTH = 2.0
_LABEL_COLOR = QColor(220, 220, 220, 200)
_LABEL_BG_COLOR = QColor(20, 20, 20, 160)
_LABEL_PADDING = 4.0
_LABEL_FONT_PT = 9


def quad_view_rects(viewport_rect: QRectF) -> dict:
    """计算四视图 2×2 布局下各子视图的矩形（QRectF）。

    布局（屏幕坐标，y 向下）：
        ┌─────────────┬─────────────┐
        │ PERSPECTIVE │     TOP     │   上半
        ├─────────────┼─────────────┤
        │    RIGHT    │    FRONT    │   下半
        └─────────────┴─────────────┘

    Returns:
        {ViewportViewKind: QRectF}，按 QUAD_VIEW_ORDER 顺序对应
        左上/右上/左下/右下。
    """
    x, y = viewport_rect.x(), viewport_rect.y()
    w, h = viewport_rect.width(), viewport_rect.height()
    cx = x + w / 2.0   # 中心 x
    cy = y + h / 2.0   # 中心 y
    return {
        ViewportViewKind.PERSPECTIVE: QRectF(x, y, w / 2.0, h / 2.0),       # 左上
        ViewportViewKind.TOP:         QRectF(cx, y, w / 2.0, h / 2.0),       # 右上
        ViewportViewKind.RIGHT:       QRectF(x, cy, w / 2.0, h / 2.0),       # 左下
        ViewportViewKind.FRONT:       QRectF(cx, cy, w / 2.0, h / 2.0),      # 右下
    }


def draw_quad_view_overlay(painter: QPainter, state: Viewport3DState,
                           viewport_rect: QRectF) -> None:
    """绘制四视图布局叠加层（分割线 + 活动边框 + 标签）。

    仅当 state.is_quad() 时绘制；SINGLE 布局 no-op。

    Args:
        painter: 已激活的 QPainter（paintEvent 内传入）。
        state: 3D 视口状态（读 layout_mode / active_view）。
        viewport_rect: 视口在 widget 坐标系的矩形。
    """
    if not state.is_quad():
        return
    painter.save()
    rects = quad_view_rects(viewport_rect)
    x, y = viewport_rect.x(), viewport_rect.y()
    w, h = viewport_rect.width(), viewport_rect.height()
    cx = x + w / 2.0
    cy = y + h / 2.0

    # ── 分割线（十字）──
    pen = QPen(_DIVIDER_COLOR, _DIVIDER_WIDTH)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.drawLine(int(cx), int(y), int(cx), int(y + h))  # 竖线
    painter.drawLine(int(x), int(cy), int(x + w), int(cy))  # 横线

    # ── 各子视图标签（左上角）──
    font = QFont("", _LABEL_FONT_PT)
    painter.setFont(font)
    metrics = painter.fontMetrics()
    for view_kind in QUAD_VIEW_ORDER:
        rect = rects[view_kind]
        _draw_view_label(painter, rect, VIEW_KIND_LABELS[view_kind], metrics)

    # ── 活动视口高亮边框 ──
    active_rect = rects.get(state.active_view)
    if active_rect is not None:
        pen = QPen(_ACTIVE_BORDER_COLOR, _ACTIVE_BORDER_WIDTH)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(active_rect.adjusted(1, 1, -1, -1))

    painter.restore()


def _draw_view_label(painter: QPainter, rect: QRectF, text: str,
                     metrics) -> None:
    """在子视图矩形左上角绘制标签（带半透明背景）。"""
    tw = metrics.horizontalAdvance(text)
    th = metrics.height()
    bg = QRectF(rect.x() + _LABEL_PADDING,
                rect.y() + _LABEL_PADDING,
                tw + _LABEL_PADDING * 2,
                th + _LABEL_PADDING)
    painter.setBrush(_LABEL_BG_COLOR)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(bg, 3, 3)
    painter.setPen(_LABEL_COLOR)
    painter.drawText(
        bg.adjusted(_LABEL_PADDING, 0, -_LABEL_PADDING, 0),
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        text)


# ── Render region mask ───────────────────────────────────────────────────────

def render_region_rect(content_rect: QRectF, target_aspect: float) -> QRectF:
    """Fit the final render aspect ratio inside the live viewport content rect."""
    if content_rect.width() <= 0.0 or content_rect.height() <= 0.0:
        return QRectF(content_rect)
    if target_aspect <= 0.0:
        return QRectF(content_rect)

    content_aspect = content_rect.width() / max(1e-6, content_rect.height())
    if content_aspect > target_aspect:
        height = content_rect.height()
        width = height * target_aspect
    else:
        width = content_rect.width()
        height = width / target_aspect
    center = content_rect.center()
    return QRectF(
        center.x() - width / 2.0,
        center.y() - height / 2.0,
        width,
        height,
    )


def draw_render_region_mask(
    painter: QPainter,
    content_rect: QRectF,
    target_aspect: float,
    settings: dict,
) -> QRectF:
    """Draw a C4D-style final render region mask and return the clear frame."""
    frame = render_region_rect(content_rect, target_aspect)
    if not bool(settings.get("render_region_enabled", True)):
        return frame

    painter.save()
    painter.setClipRect(content_rect)

    if bool(settings.get("render_region_mask_enabled", True)):
        opacity = max(0.0, min(1.0, float(
            settings.get("render_region_mask_opacity", 0.25))))
        r = max(0, min(255, int(round(float(
            settings.get("render_region_mask_r", 0.0)) * 255.0))))
        g = max(0, min(255, int(round(float(
            settings.get("render_region_mask_g", 0.0)) * 255.0))))
        b = max(0, min(255, int(round(float(
            settings.get("render_region_mask_b", 0.0)) * 255.0))))
        color = QColor(r, g, b, int(round(opacity * 255.0)))
        # Draw only the outside bars. This avoids CompositionMode_Clear inside a
        # QOpenGLWidget paint pass, which is fragile across Qt/OpenGL backends.
        painter.fillRect(QRectF(
            content_rect.left(), content_rect.top(),
            content_rect.width(), max(0.0, frame.top() - content_rect.top())),
            color)
        painter.fillRect(QRectF(
            content_rect.left(), frame.bottom(),
            content_rect.width(), max(0.0, content_rect.bottom() - frame.bottom())),
            color)
        painter.fillRect(QRectF(
            content_rect.left(), frame.top(),
            max(0.0, frame.left() - content_rect.left()), frame.height()),
            color)
        painter.fillRect(QRectF(
            frame.right(), frame.top(),
            max(0.0, content_rect.right() - frame.right()), frame.height()),
            color)

    if bool(settings.get("render_region_border_enabled", True)):
        opacity = max(0.0, min(1.0, float(
            settings.get("render_region_border_opacity", 0.1))))
        r = max(0, min(255, int(round(float(
            settings.get("render_region_border_r", 1.0)) * 255.0))))
        g = max(0, min(255, int(round(float(
            settings.get("render_region_border_g", 1.0)) * 255.0))))
        b = max(0, min(255, int(round(float(
            settings.get("render_region_border_b", 1.0)) * 255.0))))
        width = max(0.25, float(settings.get("render_region_border_width", 1.0)))
        pen = QPen(QColor(r, g, b, int(round(opacity * 255.0))), width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(frame.adjusted(0.5, 0.5, -0.5, -0.5))

    painter.restore()
    return frame


# ── P3 安全框（Safe Areas）──
_SAFE_TITLE_COLOR = QColor(255, 255, 255, 90)   # 标题安全框（90%）淡白
_SAFE_ACTION_COLOR = QColor(255, 255, 255, 60)  # 动作安全框（80%）更淡
_SAFE_WIDTH = 1.0
_SAFE_TITLE_RATIO = 0.90   # 标题安全区（中心 90%）
_SAFE_ACTION_RATIO = 0.80  # 动作安全区（中心 80%）


def draw_safe_areas(painter: QPainter, viewport_rect: QRectF) -> None:
    """绘制安全框叠加层（标题安全 90% + 动作安全 80%）。

    在渲染输出区域内画两层居中矩形：外层（80%）是动作安全框，
    内层（90%）是标题安全框。纯屏幕空间 QPainter 绘制，不进合成输出。

    Args:
        painter: 已激活的 QPainter。
        viewport_rect: 渲染内容在 widget 坐标系的矩形（已扣除 letterbox）。
    """
    painter.save()
    cx = viewport_rect.center().x()
    cy = viewport_rect.center().y()

    # 动作安全框（80%）
    aw = viewport_rect.width() * _SAFE_ACTION_RATIO
    ah = viewport_rect.height() * _SAFE_ACTION_RATIO
    action_rect = QRectF(cx - aw / 2.0, cy - ah / 2.0, aw, ah)
    pen = QPen(_SAFE_ACTION_COLOR, _SAFE_WIDTH)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(action_rect)

    # 标题安全框（90%）
    tw = viewport_rect.width() * _SAFE_TITLE_RATIO
    th = viewport_rect.height() * _SAFE_TITLE_RATIO
    title_rect = QRectF(cx - tw / 2.0, cy - th / 2.0, tw, th)
    pen = QPen(_SAFE_TITLE_COLOR, _SAFE_WIDTH)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.drawRect(title_rect)

    painter.restore()
