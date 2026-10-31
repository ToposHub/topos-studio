"""P8.3 WorkflowCanvasView —— AI Workflow 画布视图（QGraphicsView）。

> 借鉴 Composite ``node_editor_widget.py`` 的视图交互，但精简独立：不含拖拽连线
> 创建 / backdrop / sticky / preview / Tab 搜索弹窗（编辑态留 P9+）。

提供 :class:`WorkflowCanvasView`（``QGraphicsView``），AI Workflow 画布的视图层：

- ``QGraphicsScene``（sceneRect -5000..5000）。
- **缩放**：wheelEvent（factor 1.15，clamp zoom_min..zoom_max，AnchorUnderMouse）。
- **平移**：中键拖 / Space+左键拖。
- **框选**：空地左键 → 自画 rubber band → 选中节点。
- **节点/连线管理**：``add_node_item`` / ``remove_node_item`` / ``add_connection_item``。
- **选中**：``selected_node_ids`` / ``clear_selection``。
- **drawBackground**：点阵网格（暗色背景）。
- **信号**：对齐 :class:`GraphUiSignals.ALL`。

红线（plan §1.4.1 / §0 / graph_ui_protocol）：

- **不引入** ``moderngl`` / ``composite.core``。
- **不传 GPU texture**。
- 画布**不直接写 timeline**（只渲染 + emit 信号）。
- 视觉 token 单一来源。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from PyQt6.QtCore import (
    QMimeData,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QTransform,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
)

from src.shared.node_canvas.graph_ui_protocol import (
    DEFAULT_VISUAL_TOKENS,
    VisualTokens,
)
from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
from src.features.ai_workflow.ui.workflow_connection_item import (
    WorkflowConnectionItem, make_temp_connection,
)


class WorkflowCanvasView(QGraphicsView):
    """AI Workflow 画布视图（精简版，只读渲染 + 选中 + 缩放/平移）。

    信号对齐 :class:`GraphUiSignals.ALL`（``run_requested`` / ``cancel_requested``
    是 AI Workflow 独有，Composite 不复用）。
    """

    # ---- 信号（对齐 GraphUiSignals.ALL）---- #
    node_added = pyqtSignal(str, str)         # node_id, spec_id
    node_removed = pyqtSignal(str)
    node_connected = pyqtSignal(str, str, str, str)  # from_node, from_port, to_node, to_port
    node_disconnected = pyqtSignal(str, str, str, str)
    node_selected = pyqtSignal(str)
    nodes_selected = pyqtSignal(list)
    selection_cleared = pyqtSignal()
    node_double_clicked = pyqtSignal(str)
    nodes_moved = pyqtSignal(list, list)       # node_ids, positions
    graph_changed = pyqtSignal()
    zoom_changed = pyqtSignal(float)
    undo_requested = pyqtSignal()
    redo_requested = pyqtSignal()
    run_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    assets_dropped = pyqtSignal(list, tuple)
    # P10.2b：Tab → 请求加节点（host 弹搜索窗）
    add_node_requested = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        visual_tokens: Optional[VisualTokens] = None,
    ) -> None:
        super().__init__(parent)
        self._tokens = visual_tokens or DEFAULT_VISUAL_TOKENS

        # Scene
        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(-5000, -5000, 10000, 10000)
        self.setScene(self._scene)

        # 视图渲染设置
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.SmartViewportUpdate
        )
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)  # 自画 rubber band
        self.setBackgroundBrush(QColor("#121212"))
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 节点 / 连线管理
        self._node_items: Dict[str, WorkflowNodeItem] = {}
        self._connection_items: Dict[Tuple[str, str, str, str], WorkflowConnectionItem] = {}

        # 缩放
        self._current_zoom = 1.0

        # 平移状态
        self._space_held = False
        self._mid_button_panning = False
        self._pan_start: Optional[QPoint] = None
        self._pan_start_center: Optional[QPointF] = None

        # Rubber band 状态
        self._rubber_active = False
        self._rubber_start: Optional[QPoint] = None
        self._rubber_rect = QRectF()

        # P10.1c：节点拖拽 move 捕获（press 记 start 快照，release 比对 emit nodes_moved）
        self._move_start_positions: Dict[str, QPointF] = {}

        # P10.2a：连线拖拽创建状态
        self._dragging_connection = False
        self._drag_src_node_id: Optional[str] = None
        self._drag_src_port_name: Optional[str] = None
        self._drag_src_artifact_type: Optional[str] = None
        self._drag_direction: Optional[str] = None  # "forward" / "reverse"
        self._temp_connection: Optional[WorkflowConnectionItem] = None

        # 选中信号桥接
        self._scene.selectionChanged.connect(self._on_selection_changed)

    # ---------------- 属性 ---------------- #

    @property
    def current_zoom(self) -> float:
        return self._current_zoom

    @property
    def node_count(self) -> int:
        return len(self._node_items)

    @property
    def connection_count(self) -> int:
        return len(self._connection_items)

    @property
    def connection_items(self) -> Dict[Tuple[str, str, str, str], "WorkflowConnectionItem"]:
        """连线项字典（key=(from_node, from_port, to_node, to_port)）。

        供 adapter / host 查连线（如 remove 时解除节点注册）。
        """
        return self._connection_items

    def set_node_position(self, node_id: str, x: float, y: float) -> None:
        """设置节点位置（undo/redo 用；触发 itemChange → 连线跟随）。

        与鼠标拖拽不同，本方法**不 emit nodes_moved**（避免 undo/redo 循环）。
        """
        item = self._node_items.get(node_id)
        if item is not None:
            item.setPos(x, y)

    # ---------------- 节点 / 连线管理 ---------------- #

    def add_node_item(self, item: WorkflowNodeItem) -> None:
        """添加节点视觉项到画布。

        AUD-23904：重复 node_id 先移除旧视觉再写 dict，避免 dict 覆盖后
        旧 item 孤儿残留 scene（模型层已保证 node_id 唯一，此处兜底）。
        """
        old = self._node_items.get(item.node_id)
        if old is not None and old is not item:
            self._scene.removeItem(old)
        self._scene.addItem(item)
        self._node_items[item.node_id] = item
        self.node_added.emit(item.node_id, item.spec_id)

    def remove_node_item(self, node_id: str) -> None:
        """移除节点（同时移除关联连线）。"""
        item = self._node_items.pop(node_id, None)
        if item is None:
            return
        # 移除关联连线
        related = [
            key for key in self._connection_items
            if key[0] == node_id or key[2] == node_id
        ]
        for key in related:
            self._remove_connection_internal(key)
        self._scene.removeItem(item)
        self.node_removed.emit(node_id)
        self.graph_changed.emit()

    def get_node_item(self, node_id: str) -> Optional[WorkflowNodeItem]:
        return self._node_items.get(node_id)

    def add_connection_item(self, conn: WorkflowConnectionItem) -> None:
        """添加连线视觉项到画布（程序化路径；**不发** ``node_connected``）。

        - AUD-23904：重复 key 先移除旧连线再写 dict，避免 dict 覆盖后旧
          item 孤儿残留 scene、且无法经 remove_connection_item 回收。
        - AUD-23906：``node_connected`` 是**用户意图**信号，仅在
          ``_finish_connection_drag`` 手势路径 emit。本方法被 adapter
          （build_from_graph 重建 / host 槽回调）程序化调用，此处 emit 会
          造成 host 编辑层必然再入（.clinerules「程序化更新须防递归信号」）。
        """
        old = self._connection_items.get(conn.key)
        if old is not None and old is not conn:
            self._remove_connection_internal(conn.key)
        self._scene.addItem(conn)
        self._connection_items[conn.key] = conn
        conn.update_path()

    def remove_connection_item(self, key: Tuple[str, str, str, str]) -> None:
        self._remove_connection_internal(key)

    def _remove_connection_internal(
        self, key: Tuple[str, str, str, str],
    ) -> None:
        conn = self._connection_items.pop(key, None)
        if conn is None:
            return
        self._scene.removeItem(conn)
        self.node_disconnected.emit(*key)

    def clear_all(self) -> None:
        """清空所有节点和连线。"""
        for key in list(self._connection_items):
            self._remove_connection_internal(key)
        for node_id in list(self._node_items):
            item = self._node_items.pop(node_id, None)
            if item is not None:
                self._scene.removeItem(item)
                self.node_removed.emit(node_id)
        self.graph_changed.emit()

    # ---------------- 选中 ---------------- #

    def selected_node_ids(self) -> Tuple[str, ...]:
        return tuple(
            node_id for node_id, item in self._node_items.items()
            if item.isSelected()
        )

    def clear_selection(self) -> None:
        self._scene.clearSelection()
        self.selection_cleared.emit()

    def _on_selection_changed(self) -> None:
        ids = self.selected_node_ids()
        if len(ids) == 0:
            self.selection_cleared.emit()
        elif len(ids) == 1:
            self.node_selected.emit(ids[0])
        else:
            self.nodes_selected.emit(list(ids))

    # ---------------- 缩放 ---------------- #

    def zoom_to(self, zoom: float) -> None:
        """设置缩放（clamp 到 zoom_min..zoom_max）。"""
        clamped = max(
            self._tokens.zoom_min,
            min(self._tokens.zoom_max, zoom),
        )
        if abs(clamped - self._current_zoom) < 1e-6:
            return
        self._current_zoom = clamped
        self.setTransform(QTransform().scale(clamped, clamped))
        self.zoom_changed.emit(clamped)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """缩放（Ctrl+滚轮 或 滚轮）。"""
        factor = 1.15
        if event.angleDelta().y() > 0:
            new_zoom = self._current_zoom * factor
        else:
            new_zoom = self._current_zoom / factor
        self.zoom_to(new_zoom)

    # ---------------- 平移 ---------------- #

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 中键拖动平移
        if event.button() == Qt.MouseButton.MiddleButton:
            self._mid_button_panning = True
            self._pan_start = event.position().toPoint()
            self._pan_start_center = self.mapToScene(self.viewport().rect().center())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        # Space + 左键拖动平移
        if self._space_held and event.button() == Qt.MouseButton.LeftButton:
            self._mid_button_panning = True
            self._pan_start = event.position().toPoint()
            self._pan_start_center = self.mapToScene(self.viewport().rect().center())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        # P10.2a：左键按下先查端口命中——命中则开始连线拖拽（优先于节点移动）
        if event.button() == Qt.MouseButton.LeftButton and not self._space_held:
            scene_pos = self.mapToScene(event.position().toPoint())
            port_hit = self._find_port_at(scene_pos)
            if port_hit is not None:
                node_item, port_name, direction = port_hit
                self._start_connection_drag(node_item, port_name, direction, scene_pos)
                event.accept()
                return
        # 点空地左键 → 框选
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is None:
                # AUD-23902：空地按下事件被 accept 后 Qt 默认「点空地清除
                # 选中」失效，须手动清一次（无 Ctrl 修饰时；Ctrl 保留追加
                # 框选语义）——否则选中集合只增不减、用户无法取消选中。
                if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                    self._scene.clearSelection()
                self._rubber_active = True
                self._rubber_start = event.position().toPoint()
                self._rubber_rect = QRectF()
                event.accept()
                return
        super().mousePressEvent(event)
        # P10.1c / AUD-23903：移动起点快照必须在 super().mousePressEvent
        # **之后**采集——Qt 在场景 press 分发阶段才把被点击的未选中节点置
        # 选中；快照早于该时刻则首次点选未选中节点直接拖动时 nodes_moved
        # 不提交。移动发生在 mouseMove，press 阶段位置尚未变化。
        if event.button() == Qt.MouseButton.LeftButton:
            self._move_start_positions = {
                nid: QPointF(it.x(), it.y())
                for nid, it in self._node_items.items()
                if it.isSelected()
            }

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # P10.2a：连线拖拽中 → 更新临时连线端点 + 命中校验改色
        if self._dragging_connection and self._temp_connection is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            src_node = self._node_items.get(self._drag_src_node_id or "")
            if src_node is not None and self._drag_src_port_name:
                src_pt = src_node.port_scene_pos(
                    self._drag_src_port_name,
                    "output" if self._drag_direction == "forward" else "input",
                )
                if src_pt is not None:
                    # 查最近的有效目标端口（类型匹配 + 非自连）
                    target = self._find_nearest_valid_target(
                        scene_pos,
                        self._drag_src_node_id or "",
                        self._drag_src_port_name,
                        self._drag_src_artifact_type or "",
                        "input" if self._drag_direction == "forward" else "output",
                    )
                    if target is not None:
                        tgt_item, tgt_port, _ = target
                        tgt_pt = tgt_item.port_scene_pos(tgt_port, target[2])
                        self._temp_connection.set_drag_validity(True)
                        self._temp_connection.set_temp_endpoints(
                            src_pt, tgt_pt if tgt_pt is not None else scene_pos,
                        )
                    else:
                        self._temp_connection.set_drag_validity(False)
                        self._temp_connection.set_temp_endpoints(src_pt, scene_pos)
            event.accept()
            return
        if self._mid_button_panning and self._pan_start is not None:
            delta = event.position().toPoint() - self._pan_start
            # 反缩放 delta（按当前 zoom）
            new_center = QPointF(
                self._pan_start_center.x() - delta.x() / self._current_zoom,
                self._pan_start_center.y() - delta.y() / self._current_zoom,
            )
            self.centerOn(new_center)
            event.accept()
            return
        if self._rubber_active and self._rubber_start is not None:
            current = event.position().toPoint()
            x = min(self._rubber_start.x(), current.x())
            y = min(self._rubber_start.y(), current.y())
            w = abs(current.x() - self._rubber_start.x())
            h = abs(current.y() - self._rubber_start.y())
            self._rubber_rect = QRectF(x, y, w, h)
            self._update_rubber_selection()
            self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # P10.2a：连线拖拽释放 → 校验目标 + emit node_connected
        if self._dragging_connection:
            self._finish_connection_drag(event)
            event.accept()
            return
        if self._mid_button_panning:
            self._mid_button_panning = False
            self._pan_start = None
            self.setCursor(Qt.CursorShape.ArrowCursor if not self._space_held
                           else Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if self._rubber_active:
            self._rubber_active = False
            self._rubber_start = None
            self._rubber_rect = QRectF()
            self.viewport().update()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # P10.1c：左键释放后，比对选中节点当前 pos 与 start 快照；
        # 有移动则 emit nodes_moved(node_ids, old_positions)。
        if event.button() == Qt.MouseButton.LeftButton and self._move_start_positions:
            moved_ids: list = []
            old_positions: list = []
            for nid, old_pos in self._move_start_positions.items():
                item = self._node_items.get(nid)
                if item is None:
                    continue
                new_pos = item.pos()
                if (new_pos.x(), new_pos.y()) != (old_pos.x(), old_pos.y()):
                    moved_ids.append(nid)
                    old_positions.append((old_pos.x(), old_pos.y()))
            if moved_ids:
                self.nodes_moved.emit(moved_ids, old_positions)
            self._move_start_positions = {}

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """P10.2c：双击节点 → emit node_double_clicked(node_id)。

        host 据 signals 打开/聚焦 Inspector。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            # item 可能是 WorkflowNodeItem（无子 PortItem）
            nid = getattr(item, "node_id", None)
            if nid is not None:
                self.node_double_clicked.emit(nid)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _update_rubber_selection(self) -> None:
        """框选：逐个判断节点是否在 rubber_rect 内。"""
        if self._rubber_rect.isNull():
            return
        for item in self._node_items.values():
            item_rect = item.sceneBoundingRect()
            # PyQt6 绑定：mapFromScene(QRectF) 返回整型 QPolygon，其
            # boundingRect() 是 QRect——喂给 QRectF.intersects 会 TypeError
            # （PyQt6 无隐式 QRect→QRectF 转换），必须显式转 QRectF。
            view_rect = QRectF(self.mapFromScene(item_rect).boundingRect())
            if self._rubber_rect.intersects(view_rect):
                item.setSelected(True)

    # ---------------- 连线拖拽创建（P10.2a）---------------- #

    #: 端口命中半径（scene 坐标，平方距离比较避免 sqrt）
    _PORT_HIT_RADIUS_SQ = 12.0 * 12.0
    _PORT_SNAP_RADIUS_SQ = 30.0 * 30.0

    def _find_port_at(
        self, scene_pos: QPointF,
    ) -> Optional[Tuple[WorkflowNodeItem, str, str]]:
        """精确命中检测：返回 (node_item, port_name, direction) 或 None。

        AI Workflow 无独立 PortItem，端口画在节点上；本方法遍历节点，把
        scene_pos 映射到节点局部坐标，比对每个端口的局部中心。
        """
        best = None
        best_dist_sq = self._PORT_HIT_RADIUS_SQ
        for item in self._node_items.values():
            local = item.mapFromScene(scene_pos)
            # input 端口 x ≈ port_radius*0.5；output x ≈ width - port_radius*0.5
            in_x = item._tokens.port_radius * 0.5
            out_x = item._width - item._tokens.port_radius * 0.5
            for pname, py in item._input_port_ys.items():
                d = (local.x() - in_x) ** 2 + (local.y() - py) ** 2
                if d < best_dist_sq:
                    best_dist_sq = d
                    best = (item, pname, "input")
            for pname, py in item._output_port_ys.items():
                d = (local.x() - out_x) ** 2 + (local.y() - py) ** 2
                if d < best_dist_sq:
                    best_dist_sq = d
                    best = (item, pname, "output")
        return best

    def _find_nearest_valid_target(
        self, scene_pos: QPointF,
        src_node_id: str, src_port_name: str, src_artifact_type: str,
        target_direction: str,
    ) -> Optional[Tuple[WorkflowNodeItem, str, str]]:
        """找最近的有效目标端口（snap 半径内；类型匹配；非自连）。

        Args:
            target_direction: 目标端口方向（forward 拖拽找 input；reverse 找 output）。
        """
        best = None
        best_dist_sq = self._PORT_SNAP_RADIUS_SQ
        for item in self._node_items.values():
            if item.node_id == src_node_id:
                continue  # 自连禁止
            local = item.mapFromScene(scene_pos)
            port_ys = (
                item._input_port_ys if target_direction == "input"
                else item._output_port_ys
            )
            x = (
                item._tokens.port_radius * 0.5 if target_direction == "input"
                else item._width - item._tokens.port_radius * 0.5
            )
            types_map = (
                item.input_artifact_types if target_direction == "input"
                else item.output_artifact_types
            )
            for pname, py in port_ys.items():
                if types_map.get(pname) != src_artifact_type:
                    continue  # 类型不匹配
                d = (local.x() - x) ** 2 + (local.y() - py) ** 2
                if d < best_dist_sq:
                    best_dist_sq = d
                    best = (item, pname, target_direction)
        return best

    def _start_connection_drag(
        self, node_item: WorkflowNodeItem, port_name: str,
        direction: str, scene_pos: QPointF,
    ) -> None:
        """开始连线拖拽（forward=从 output 拉；reverse=从 input 拉）。"""
        self._dragging_connection = True
        self._drag_src_node_id = node_item.node_id
        self._drag_src_port_name = port_name
        self._drag_direction = "forward" if direction == "output" else "reverse"
        # 源 artifact_type（用于类型校验）
        types = (
            node_item.output_artifact_types if direction == "output"
            else node_item.input_artifact_types
        )
        self._drag_src_artifact_type = types.get(port_name, "")
        # 临时连线
        self._temp_connection = make_temp_connection(self._drag_src_artifact_type or "asset_ref")
        self._scene.addItem(self._temp_connection)

    def _finish_connection_drag(self, event: QMouseEvent) -> None:
        """释放：校验目标端口 → emit node_connected；清状态 + 移除临时连线。"""
        scene_pos = self.mapToScene(event.position().toPoint())
        src_node_id = self._drag_src_node_id or ""
        src_port = self._drag_src_port_name or ""
        src_type = self._drag_src_artifact_type or ""
        forward = self._drag_direction == "forward"
        target_dir = "input" if forward else "output"

        target = self._find_nearest_valid_target(
            scene_pos, src_node_id, src_port, src_type, target_dir,
        )
        if target is None:
            # 退化：精确命中检测（snap 未中但精确命中端口）
            target = self._find_port_at(scene_pos)
            if target is not None:
                tgt_item, tgt_port, tgt_dir = target
                if tgt_dir != target_dir or tgt_item.node_id == src_node_id:
                    target = None
                else:
                    # 类型校验
                    types = (
                        tgt_item.input_artifact_types if tgt_dir == "input"
                        else tgt_item.output_artifact_types
                    )
                    if types.get(tgt_port) != src_type:
                        target = None

        if target is not None:
            tgt_item, tgt_port, _ = target
            if forward:
                self.node_connected.emit(src_node_id, src_port, tgt_item.node_id, tgt_port)
            else:
                # reverse：从 input 拉 → output；emit 时 output 为 from
                self.node_connected.emit(tgt_item.node_id, tgt_port, src_node_id, src_port)

        # 清理
        if self._temp_connection is not None:
            self._scene.removeItem(self._temp_connection)
            self._temp_connection = None
        self._dragging_connection = False
        self._drag_src_node_id = None
        self._drag_src_port_name = None
        self._drag_src_artifact_type = None
        self._drag_direction = None

    # ---------------- 键盘 ---------------- #

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_held = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        # P10.2b：Tab → 请求加节点（host 据 add_node_requested 弹搜索窗）
        if event.key() == Qt.Key.Key_Tab:
            self.add_node_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Z and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.redo_requested.emit()
            else:
                self.undo_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_held = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    # ---------------- 绘制背景 / 前景 ---------------- #

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        """点阵网格。"""
        painter.fillRect(rect, QColor("#121212"))
        # 点阵
        grid = self._tokens.grid_size
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        left = int(rect.left()) - (int(rect.left()) % grid)
        top = int(rect.top()) - (int(rect.top()) % grid)
        x = left
        while x < rect.right():
            y = top
            while y < rect.bottom():
                painter.drawPoint(x, y)
                y += grid
            x += grid

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """框选矩形 overlay（视口坐标绘制）。

        AUD-23901：drawForeground 的 painter 处于**场景坐标系**，而
        ``_rubber_rect`` 存的是视口坐标（``event.position().toPoint()``，
        见 mouseMoveEvent）。场景原点与视口原点在平移/缩放后不重合，须先
        切回单位变换（视口坐标）再绘制，否则框选反馈画到错误位置。
        """
        if self._rubber_active and not self._rubber_rect.isNull():
            painter.save()
            painter.setTransform(QTransform())
            painter.setBrush(QColor(255, 180, 60, 30))
            painter.setPen(QPen(QColor(255, 180, 60, 150), 1))
            painter.drawRect(self._rubber_rect)
            painter.restore()

    # ---------------- 拖放（assets_dropped）---------------- #

    @staticmethod
    def _mime_has_assets(mime: QMimeData) -> bool:
        """拖入 mime 是否携带素材载荷（URL 列表或文本行）。"""
        return mime.hasUrls() or mime.hasText()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """AUD-23905：接受携带素材的拖入（否则留 Qt 默认处理）。"""
        if self._mime_has_assets(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        """拖动经过画布：与 dragEnterEvent 同一接受准则。"""
        if self._mime_has_assets(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """放下素材 → emit ``assets_dropped(urls, (x, y))``（scene 坐标）。

        GraphUiSignals.ALL 承诺的 ASSETS_DROPPED 通道在此落地（此前
        ``setAcceptDrops(True)`` + 信号声明但无任何拖放实现，为死通道）。
        画布本身不改图（红线：只 emit 信号，由 host 决定如何消费素材）。
        """
        mime = event.mimeData()
        if not self._mime_has_assets(mime):
            event.ignore()
            return
        urls: list = (
            [url.toString() for url in mime.urls()] if mime.hasUrls() else []
        )
        if not urls and mime.hasText():
            urls = [line for line in mime.text().splitlines() if line]
        if not urls:
            event.ignore()
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        self.assets_dropped.emit(urls, (scene_pos.x(), scene_pos.y()))
        event.acceptProposedAction()
