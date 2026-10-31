"""P8.1 WorkflowNodeItem —— AI Workflow 节点视觉项（plan §7.2 / graph_ui_protocol）。

> 借鉴 Composite ``node_item.py`` 的绘制模式，但**精简独立**：不含 thumbnail /
> perf_badge / GPU 预览。所有视觉数据来自 :class:`VisualTokens`（单一来源）。

提供 :class:`WorkflowNodeItem`（``QGraphicsRectItem``），AI Workflow 节点画布的节点视觉：

- 圆角矩形 body + header 渐变（色来自 ``VisualTokens.node_token(category)``）。
- 端口区（input 左 / output 右，标签 + 圆点，色来自 ``port_token(artifact_type)``）。
- 状态色条（``set_status`` → ``status_color`` 画底部色条）。
- 选中 glow（橙色三层，与 Composite 一致）。
- 端口位置查询（``port_scene_pos``，给连线定位用）。

红线（plan §1.4.1 / §0 / graph_ui_protocol.py）：

- **不引入** ``moderngl`` / ``composite.core``（本文件是 UI 层，可引入 PyQt6）。
- **不传 GPU texture / OpenGL context**（端口 artifact_type 是字符串）。
- 视觉 token 单一来源：用注入的 :class:`VisualTokens`（默认 ``DEFAULT_VISUAL_TOKENS``）。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsRectItem

from src.shared.node_canvas.graph_ui_protocol import (
    DEFAULT_VISUAL_TOKENS,
    PortVisualToken,
    VisualTokens,
)


# ---------------------------------------------------------------------- #
# WorkflowNodeItem
# ---------------------------------------------------------------------- #


class WorkflowNodeItem(QGraphicsRectItem):
    """AI Workflow 节点视觉项（精简版，不含 GPU 缩略图）。

    Attributes:
        node_id: 节点唯一 ID。
        spec_id: 节点 spec ID（如 ``topos.analysis.transcribe_auto``）。
        category: 节点分类（来自 spec，映射到 NodeVisualToken）。
        status: 当前运行状态（``idle``/``running``/``succeeded`` 等）。
    """

    def __init__(
        self,
        node_id: str,
        spec_id: str,
        *,
        node_spec,
        visual_tokens: Optional[VisualTokens] = None,
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(parent)
        self._node_id = node_id
        self._spec_id = spec_id
        self._node_spec = node_spec
        self._tokens = visual_tokens or DEFAULT_VISUAL_TOKENS

        # 从 spec 读 category + 端口
        self._category: str = getattr(node_spec, "category", "control")
        self._display_name: str = getattr(node_spec, "display_name", spec_id)
        self._input_ports: Tuple = tuple(getattr(node_spec, "input_ports", ()))
        self._output_ports: Tuple = tuple(getattr(node_spec, "output_ports", ()))

        # 视觉状态
        self._status: str = "idle"
        self._has_glow: bool = False

        # 计算尺寸（从 visual_tokens 读，不硬编码）
        self._width = max(
            self._tokens.node_min_width,
            self._measure_min_width_for_labels(),
        )
        self._header_height = self._tokens.node_header_height
        port_rows = max(len(self._input_ports), len(self._output_ports), 1)
        self._body_height = port_rows * self._tokens.port_spacing + 16
        self._total_height = self._header_height + self._body_height

        # 端口中心 y 坐标（相对节点原点），按行排列
        self._input_port_ys: Dict[str, float] = {}
        self._output_port_ys: Dict[str, float] = {}
        for i, p in enumerate(self._input_ports):
            self._input_port_ys[p.name] = (
                self._header_height + 12 + i * self._tokens.port_spacing
            )
        for i, p in enumerate(self._output_ports):
            self._output_port_ys[p.name] = (
                self._header_height + 12 + i * self._tokens.port_spacing
            )

        # QGraphicsRectItem 设置
        self.setRect(0, 0, self._width, self._total_height)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

        # P10.1c：关联连线引用（拖动时调 conn.update_path 实现连线跟随）。
        # 连线生命周期由 scene/view 管理，这里只存引用用于位置变更通知。
        self._connections: list = []

    # ---------------- 基本属性 ---------------- #

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def spec_id(self) -> str:
        return self._spec_id

    @property
    def category(self) -> str:
        return self._category

    @property
    def status(self) -> str:
        return self._status

    @property
    def has_glow(self) -> bool:
        return self._has_glow

    @property
    def header_color(self) -> str:
        """header 色（来自 VisualTokens.node_token）。"""
        return self._tokens.node_token(self._category).color

    @property
    def status_color(self) -> str:
        """状态色（来自 VisualTokens.status_color）。"""
        return self._tokens.status_color(self._status)

    @property
    def input_artifact_types(self) -> Dict[str, str]:
        return {p.name: p.artifact_type for p in self._input_ports}

    @property
    def output_artifact_types(self) -> Dict[str, str]:
        return {p.name: p.artifact_type for p in self._output_ports}

    # ---------------- 状态变更 ---------------- #

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def set_selected_glow(self, enabled: bool) -> None:
        self._has_glow = enabled
        self.update()

    def set_position(self, x: float, y: float) -> None:
        self.setPos(x, y)

    # ---------------- 连线跟随（P10.1c）---------------- #

    @property
    def connections(self) -> list:
        """关联此节点的连线列表（拖动时调 ``conn.update_path``）。"""
        return self._connections

    def register_connection(self, conn: object) -> None:
        """注册一条连线（拖动节点时通知其 ``update_path``）。

        幂等：重复注册同一连线不会重复加入。
        """
        if conn not in self._connections:
            self._connections.append(conn)

    def unregister_connection(self, conn: object) -> None:
        """解除连线注册（幂等）。"""
        try:
            self._connections.remove(conn)
        except ValueError:
            pass  # 不存在：no-op

    def itemChange(
        self, change: QGraphicsItem.GraphicsItemChange, value: object,
    ) -> object:
        """节点位置变更时通知关联连线重绘（连线跟随）。

        ``ItemSendsGeometryChanges`` flag 已在 ``__init__`` 设；本 override 消费
        ``ItemPositionHasChanged`` 通知。调用 ``update_path`` 走连线自己的端口
        位置查询（``port_scene_pos`` 会反映新位置）。
        """
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for conn in self._connections:
                try:
                    conn.update_path()
                except Exception:  # noqa: BLE001 — 单条连线失败不影响其它
                    pass
        return super().itemChange(change, value)

    # ---------------- 端口位置 ---------------- #

    def port_scene_pos(self, port_name: str, direction: str) -> Optional[QPointF]:
        """返回端口在 scene 中的中心点（给连线定位用）。

        Args:
            port_name: 端口名。
            direction: ``input`` / ``output``。

        Returns:
            scene 坐标 QPointF；端口不存在返回 None。
        """
        if direction == "input":
            y = self._input_port_ys.get(port_name)
            if y is None:
                return None
            # input 端口在节点左边界（x=0），中心在 port_radius 处
            local = QPointF(self._tokens.port_radius * 0.5, y)
        elif direction == "output":
            y = self._output_port_ys.get(port_name)
            if y is None:
                return None
            # output 端口在节点右边界
            local = QPointF(self._width - self._tokens.port_radius * 0.5, y)
        else:
            return None
        return self.mapToScene(local)

    # ---------------- 绘制 ---------------- #

    def boundingRect(self) -> QRectF:
        rect = self.rect()
        # glow 需要额外空间
        if self._has_glow:
            return rect.adjusted(-6, -6, 6, 6)
        return rect

    def paint(
        self, painter: QPainter, option, widget=None,
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        radius = self._tokens.node_corner_radius

        # 1. 选中 glow（三层）
        if self._has_glow:
            for i, alpha in enumerate((40, 80, 140)):
                glow_rect = rect.adjusted(-6 + i * 2, -6 + i * 2, 6 - i * 2, 6 - i * 2)
                glow_path = QPainterPath()
                glow_path.addRoundedRect(glow_rect, radius + 2, radius + 2)
                painter.setPen(QPen(QColor(255, 180, 60, alpha), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(glow_path)

        # 2. body（圆角矩形 + 垂直渐变）
        body_path = QPainterPath()
        body_path.addRoundedRect(rect, radius, radius)
        body_grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
        body_grad.setColorAt(0.0, QColor("#3a3a40"))
        body_grad.setColorAt(1.0, QColor("#2a2a2e"))
        painter.setBrush(QBrush(body_grad))
        painter.setPen(QPen(QColor(70, 70, 75), 1))
        painter.drawPath(body_path)

        # 3. header（顶部彩色矩形，裁掉底部圆角）
        header_rect = QRectF(rect.left(), rect.top(), rect.width(), self._header_height)
        header_clip = QPainterPath()
        header_clip.addRoundedRect(rect, radius, radius)
        header_path = QPainterPath()
        header_path.addRect(header_rect)
        header_path = header_path.intersected(header_clip)
        header_color = QColor(self.header_color)
        header_grad = QLinearGradient(0, header_rect.top(), 0, header_rect.bottom())
        header_grad.setColorAt(0.0, header_color.lighter(130))
        header_grad.setColorAt(1.0, header_color)
        painter.setBrush(QBrush(header_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(header_path)

        # 4. header 标题（居中）
        painter.setPen(QPen(QColor(255, 255, 255, 230)))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            header_rect, Qt.AlignmentFlag.AlignCenter, self._display_name,
        )

        # 5. 端口（input 左 / output 右）
        self._paint_ports(painter, rect)

        # 6. 状态色条（底部）
        if self._status != "idle":
            status_color = QColor(self.status_color)
            bar_path = QPainterPath()
            # 底部色条也圆角（与 body 底部一致）
            bar_path.addRoundedRect(
                QRectF(rect.left(), rect.bottom() - 8, rect.width(), 8),
                radius, radius,
            )
            bar_path = bar_path.intersected(body_path)
            painter.setBrush(QBrush(status_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(bar_path)

    def _paint_ports(self, painter: QPainter, rect: QRectF) -> None:
        """绘制端口圆点 + 标签。"""
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        # input 端口（左）
        for p in self._input_ports:
            y = self._input_port_ys.get(p.name)
            if y is None:
                continue
            token = self._tokens.port_token(p.artifact_type)
            self._draw_port_dot(painter, 0.0, y, token)
            # 标签在右侧
            painter.setPen(QPen(QColor(200, 200, 205, 200)))
            painter.drawText(
                int(self._tokens.port_spacing * 0.7), int(y + 3),
                p.name,
            )

        # output 端口（右）
        for p in self._output_ports:
            y = self._output_port_ys.get(p.name)
            if y is None:
                continue
            token = self._tokens.port_token(p.artifact_type)
            self._draw_port_dot(painter, self._width, y, token)
            # 标签在左侧（右对齐）
            painter.setPen(QPen(QColor(200, 200, 205, 200)))
            metrics = painter.fontMetrics()
            text_w = metrics.horizontalAdvance(p.name)
            painter.drawText(
                int(self._width - text_w - self._tokens.port_spacing * 0.7),
                int(y + 3),
                p.name,
            )

    def _draw_port_dot(
        self, painter: QPainter, x: float, y: float, token: PortVisualToken,
    ) -> None:
        """画端口圆点（按 token.shape：circle/square/diamond）。"""
        r = token.radius
        color = QColor(token.color)
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(QColor(255, 255, 255, 180), 1))
        if token.shape == "square":
            from PyQt6.QtCore import QRectF as _QRectF
            painter.drawRect(_QRectF(x - r, y - r, r * 2, r * 2))
        elif token.shape == "diamond":
            path = QPainterPath()
            path.moveTo(x, y - r)
            path.lineTo(x + r, y)
            path.lineTo(x, y + r)
            path.lineTo(x - r, y)
            path.closeSubpath()
            painter.drawPath(path)
        else:  # circle
            painter.drawEllipse(QPointF(x, y), r, r)

    # ---------------- 内部 ---------------- #

    def _measure_min_width_for_labels(self) -> float:
        """估算容纳端口标签的最小宽度（避免文字溢出）。"""
        from PyQt6.QtGui import QFontMetrics, QFont
        font = QFont()
        font.setPointSize(8)
        metrics = QFontMetrics(font)
        max_label = 0
        for p in (*self._input_ports, *self._output_ports):
            w = metrics.horizontalAdvance(p.name)
            if w > max_label:
                max_label = w
        # 左右各一列标签 + 间距
        return max_label * 2 + self._tokens.port_spacing * 2 + 40
