"""P8.2 WorkflowConnectionItem —— AI Workflow 连线视觉项（plan §7.2）。

> 借鉴 Composite ``connection_item.py`` 的贝塞尔绘制，但精简：不含流动动画 /
> 重连/断开/环检测（编辑态留后续）。

提供 :class:`WorkflowConnectionItem`（``QGraphicsPathItem``），AI Workflow 画布的连线：

- 贝塞尔 S 曲线（控制点水平偏移）。
- ``update_path`` 跟随端口位置重绘（节点拖动时调用）。
- artifact_type 颜色 + 宽度来自 :class:`VisualTokens`。
- 命中区扩到 12px（``QPainterPathStroker``）便于点击。
- ``key`` 属性 = ``(from_node, from_port, to_node, to_port)``，用于 dict 管理。

红线（plan §1.4.1 / §0）：

- **不引入** ``moderngl`` / ``composite.core``。
- **不传 GPU texture**（artifact_type 是字符串）。
- 视觉 token 单一来源。
"""
from __future__ import annotations

from typing import Tuple

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainterPath, QPainterPathStroker, QPen
from PyQt6.QtWidgets import QGraphicsPathItem

from src.shared.node_canvas.graph_ui_protocol import (
    DEFAULT_VISUAL_TOKENS,
    VisualTokens,
)


class WorkflowConnectionItem(QGraphicsPathItem):
    """AI Workflow 连线视觉项（精简版，不含流动动画）。

    端口项需实现 ``port_scene_pos() -> QPointF``（鸭子类型，避免硬绑 NodeItem）。
    """

    def __init__(
        self,
        from_node_id: str,
        from_port: str,
        to_node_id: str,
        to_port: str,
        *,
        src_port,
        dst_port,
        artifact_type: str,
        visual_tokens: VisualTokens | None = None,
    ) -> None:
        super().__init__()
        self._from_node = from_node_id
        self._from_port = from_port
        self._to_node = to_node_id
        self._to_port = to_port
        self._src_port = src_port    # WorkflowNodeItem（鸭子类型）
        self._dst_port = dst_port    # WorkflowNodeItem（鸭子类型）
        self._artifact_type = artifact_type
        self._tokens = visual_tokens or DEFAULT_VISUAL_TOKENS

        # 颜色 + 宽度
        token = self._tokens.port_token(artifact_type)
        self._color = token.color
        self._width = self._tokens.connection_width
        self.setPen(self._qt_pen())
        self.setZValue(0)  # 连线在节点之下

        # 初始 path
        self.update_path()

    # ---------------- 属性 ---------------- #

    @property
    def from_node_id(self) -> str:
        return self._from_node

    @property
    def from_port(self) -> str:
        return self._from_port

    @property
    def to_node_id(self) -> str:
        return self._to_node

    @property
    def to_port(self) -> str:
        return self._to_port

    @property
    def artifact_type(self) -> str:
        return self._artifact_type

    @property
    def color(self) -> str:
        return self._color

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (self._from_node, self._from_port, self._to_node, self._to_port)

    # ---------------- 路径 ---------------- #

    def update_path(self) -> None:
        """跟随端口位置重绘贝塞尔曲线。

        src_port / dst_port 是 :class:`WorkflowNodeItem`（鸭子类型），调
        ``port_scene_pos(port_name, direction)``。
        """
        src = self._src_port.port_scene_pos(self._from_port, "output")
        dst = self._dst_port.port_scene_pos(self._to_port, "input")
        if src is None or dst is None:
            # 端口不存在：空 path
            self.setPath(QPainterPath())
            return
        path = self._create_bezier_path(src, dst)
        self.setPath(path)

    # ---------------- 拖拽预览（P10.2a）---------------- #

    def set_temp_endpoints(self, src: QPointF, dst: QPointF) -> None:
        """用两个 scene 点临时画连线（拖拽创建连线时的预览）。

        绕过 ``port_scene_pos``（被拖端点尚未绑定到真实端口）。
        """
        self.setPath(self._create_bezier_path(src, dst))

    def set_drag_validity(self, is_valid: bool) -> None:
        """拖拽时按有效性改色：有效→原色（绿），无效→红。

        保留原 pen 的宽度，只换 color。再次调 True 恢复原色。
        """
        pen = self.pen()
        if is_valid:
            pen.setColor(QColor(self._color))
        else:
            pen.setColor(QColor("#c0392b"))  # 红（无效）
        self.setPen(pen)

    def _create_bezier_path(self, src: QPointF, dst: QPointF) -> QPainterPath:
        """贝塞尔 S 曲线（控制点水平偏移）。"""
        path = QPainterPath()
        path.moveTo(src)
        dx = dst.x() - src.x()
        offset = max(abs(dx) * 0.5, 60.0)
        ctrl1 = QPointF(src.x() + offset, src.y())
        ctrl2 = QPointF(dst.x() - offset, dst.y())
        path.cubicTo(ctrl1, ctrl2, dst)
        return path

    # ---------------- 命中区 ---------------- #

    def shape(self) -> QPainterPath:
        """扩宽命中区到 12px（便于点击）。"""
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        return stroker.createStroke(self.path())

    def _qt_pen(self) -> QPen:
        return QPen(QColor(self._color), self._width,
                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)


def make_temp_connection(artifact_type: str) -> "WorkflowConnectionItem":
    """产一个轻量临时连线（拖拽创建时的预览；无真实端点绑定）。

    src_port/dst_port 用 dummy 占位（避免 port_scene_pos 被调时崩溃；temp 连线
    只用 ``set_temp_endpoints``，不调 ``update_path``）。画完用
    ``scene.removeItem`` 移除。
    """
    class _DummyPort:
        def port_scene_pos(self, *_a, **_k):
            return QPointF(0, 0)
    dummy = _DummyPort()
    return WorkflowConnectionItem(
        from_node_id="", from_port="",
        to_node_id="", to_port="",
        src_port=dummy, dst_port=dummy,
        artifact_type=artifact_type,
    )
