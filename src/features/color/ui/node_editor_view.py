"""Color-page node canvas. All edits belong to a presentation-only draft.

There is deliberately no engine, renderer, project, or layer-controller import.
The host can consume ``edit_requested`` when the ColorGraph backend is ready.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QContextMenuEvent, QKeyEvent, QMouseEvent, QPainter, QPainterPath,
    QKeySequence, QPen, QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsView,
    QGraphicsTextItem, QFileDialog, QMenu, QToolTip,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from .node_resource_mime import decode_resource_mime
from .node_editor_items import ConnectionItem, NodeCardItem, NodePortItem
from .node_editor_state import ConnectionDraft, GraphDraft, NodeEditorState, NodeKind


def tr(key: str, fallback: str) -> str:
    """Use the feature's existing localization service."""
    return I18N.tr(f"color_nodes.{key}", fallback)


class _LabelEditor(QGraphicsTextItem):
    """Resolve-style label above a node; Enter accepts and Escape cancels."""

    def __init__(self, text: str, done: Callable[[str | None], None]) -> None:
        super().__init__(text)
        self._done = done
        self._finished = False
        self.setDefaultTextColor(QColor("#eeeeef"))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setZValue(20)

    def _finish(self, value: str | None) -> None:
        if not self._finished:
            self._finished = True
            # scene.clear() must not delete this editor inside its own Qt event.
            QTimer.singleShot(0, lambda: self._done(value))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(self.toPlainText().strip())
        elif event.key() == Qt.Key.Key_Escape:
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        self._finish(self.toPlainText().strip())
        super().focusOutEvent(event)


class ColorNodeView(QGraphicsView):
    """Native Qt canvas with independent selection, navigation and graph edits."""

    edit_requested = pyqtSignal(str, object)
    active_node_changed = pyqtSignal(str)
    graph_changed = pyqtSignal(object)
    zoom_changed = pyqtSignal(float)
    compound_open_requested = pyqtSignal(str)
    parent_requested = pyqtSignal()
    preview_requested = pyqtSignal(str)
    resource_drop_requested = pyqtSignal(str, object)
    node_lut_apply_requested = pyqtSignal(str, str)   # node_id, lut_ref（''=移除）
    node_lut_browse_requested = pyqtSignal(str)        # node_id（文件选择经 R02 导入）
    scene_refreshed = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("ColorNodeView")
        self.setAccessibleName(tr("canvas", "Node Editor"))
        self.state = NodeEditorState()
        self.cards: dict[str, NodeCardItem] = {}
        self.links: list[ConnectionItem] = []
        self._syncing = False
        self._resource_hover_id = ""
        self.resource_drop_enabled = True
        self.setAcceptDrops(True)
        self.project_history_enabled = False
        self.project_history_available: Callable[[bool], bool] | None = None
        self._hand_mode = False
        self._space_down = False
        self._panning = False
        self._pan_start = QPoint()
        self._move_start: dict[str, tuple[float, float]] = {}
        self._duplicate_drag = False
        self._wire_origin: NodePortItem | None = None
        self._wire: QGraphicsPathItem | None = None
        self._rewire: ConnectionDraft | None = None
        self._wire_press = QPoint()
        self._wire_dragged = False
        self._insert_target: ConnectionItem | None = None
        self._label: _LabelEditor | None = None
        self._label_node_id = ""
        self._label_generation = 0
        self._copied_grade: dict[str, Any] | None = None
        self._copied_graph: GraphDraft | None = None
        self._copied_domains: dict[str, Any] | None = None  # N12 多域剪贴板
        self._thumbnails: dict[str, Any] = {}
        self._source_thumbnail: Any = None
        self._source_preview_text = ""
        self.show_grid = True
        self.show_thumbnails = True
        self._has_fitted = False
        self.setScene(QGraphicsScene(self))
        self.scene().setSceneRect(-3000.0, -2000.0, 6000.0, 4000.0)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setBackgroundBrush(QColor(C.BG_PANEL))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.ItemSelectionMode.IntersectsItemShape)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.scene().selectionChanged.connect(self._selection_changed)
        self.rebuild()

    def load_graph(self, graph: GraphDraft, *, fit: bool = False) -> None:
        """Hydrate a UI snapshot without emitting an edit or changing history."""
        self.state.load_graph(graph)
        self.rebuild()
        if fit:
            self.fit_graph()

    def rebuild(self) -> None:
        """Refresh visual items; never submit renderer work."""
        self._syncing = True
        try:
            self._set_resource_hover("")
            self._cancel_wire()
            self._insert_target = None
            self._move_start = {}
            self._label_generation += 1
            if self._label is not None:
                self._label._finished = True
            self._label = None
            self.scene().clear()
            self.cards.clear()
            self.links.clear()
            for node in self.state.graph.nodes.values():
                card = NodeCardItem(node, self.state.ports(node.id))
                self.scene().addItem(card)
                self.cards[node.id] = card
                card.setSelected(node.id in self.state.graph.selected_ids)
                card.moved.connect(self._card_moved)
                card.opened.connect(self._open_node)
                if self.show_thumbnails and node.id in self._thumbnails:
                    card.set_thumbnail(self._thumbnails[node.id])
                elif self.show_thumbnails and self._source_thumbnail is not None and node.kind in (NodeKind.CORRECTOR, NodeKind.COMPOUND):
                    card.set_thumbnail(self._source_thumbnail)
                    card.setToolTip((node.label + "\n" if node.label else "") + self._source_preview_text)
            for connection in self.state.graph.connections:
                start = self._port(connection.src_node, connection.src_port)
                end = self._port(connection.dst_node, connection.dst_port)
                if start is None or end is None:
                    continue
                link = ConnectionItem(connection, start.scenePos(), end.scenePos(), start.data_type)
                self.scene().addItem(link)
                self.links.append(link)
        finally:
            self._syncing = False
        self.viewport().update()
        self.scene_refreshed.emit()

    def set_thumbnail(self, node_id: str, pixmap: Any) -> None:
        """Accept a host-supplied thumbnail on the GUI thread (no GPU readback)."""
        self._thumbnails[node_id] = pixmap
        if self.show_thumbnails and node_id in self.cards:
            self.cards[node_id].set_thumbnail(pixmap)

    def set_cache_hits(self, node_ids: Any) -> None:
        """N06：按实际命中事件点亮/熄灭卡片命中徽标。

        ``node_ids``：本帧真实命中缓存（hit 事件）的节点 id 集合——
        不在场的一律熄灭（「状态仅实际命中后显示」，不用策略蓝条冒充）。
        """
        hits = set(node_ids or ())
        for node_id, card in self.cards.items():
            card.set_cache_hit(node_id in hits)

    def set_source_thumbnail(self, pixmap: Any, description: str = "") -> None:
        """Display a clearly identified source preview until node output exists.

        Source thumbnails never overwrite a real per-node thumbnail. They are
        not stored in the draft, its clipboard, or its undo history.
        """
        self._source_thumbnail = pixmap
        self._source_preview_text = description
        for node_id, card in self.cards.items():
            if node_id not in self._thumbnails and card.node.kind in (NodeKind.CORRECTOR, NodeKind.COMPOUND):
                card.set_thumbnail(pixmap if self.show_thumbnails else None)
                card.setToolTip((card.node.label + "\n" if card.node.label else "") + description)

    def _port(self, node_id: str, name: str) -> NodePortItem | None:
        card = self.cards.get(node_id)
        return card.ports.get(name) if card is not None else None

    def _card_at(self, position: QPoint) -> NodeCardItem | None:
        item = self.itemAt(position)
        while item is not None:
            if isinstance(item, NodeCardItem):
                return item
            item = item.parentItem()
        return None

    def resource_target_at(self, position: QPoint) -> str:
        """Only a writable Corrector accepts library resources."""
        if not self.resource_drop_enabled:
            return ""
        card = self._card_at(position)
        if card is not None and card.node.kind == NodeKind.CORRECTOR and not card.node.locked:
            return card.node.id
        return ""

    def _set_resource_hover(self, node_id: str) -> None:
        if node_id == self._resource_hover_id:
            return
        for key in (self._resource_hover_id, node_id):
            card = self.cards.get(key)
            if card is not None:
                card.resource_drop_hover = key == node_id
                card.update()
        self._resource_hover_id = node_id

    def dragEnterEvent(self, event: Any) -> None:
        if decode_resource_mime(event.mimeData()) is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:
        target = self.resource_target_at(event.position().toPoint()) if decode_resource_mime(event.mimeData()) else ""
        self._set_resource_hover(target)
        if target:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:
        self._set_resource_hover("")
        event.accept()

    def dropEvent(self, event: Any) -> None:
        token = decode_resource_mime(event.mimeData())
        target = self.resource_target_at(event.position().toPoint())
        self._set_resource_hover("")
        if token is None or not target:
            event.ignore()
            return
        self.resource_drop_requested.emit(target, token)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def _port_at(self, position: QPoint) -> NodePortItem | None:
        for item in self.items(position):
            if isinstance(item, NodePortItem):
                return item
        return None

    def _link_at(self, position: QPoint) -> ConnectionItem | None:
        return next((item for item in self.items(position) if isinstance(item, ConnectionItem)), None)

    def _selection_changed(self) -> None:
        if self._syncing:
            return
        ids = {item.node.id for item in self.scene().selectedItems() if isinstance(item, NodeCardItem)}
        active = self.state.graph.active_id
        if active not in ids:
            active = next((node_id for node_id in self.cards if node_id in ids), "")
        self.state.select(ids, active)
        self.active_node_changed.emit(active)

    def _card_moved(self, node_id: str) -> None:
        if self._syncing:
            return
        for link in self.links:
            connection = link.connection
            if node_id in (connection.src_node, connection.dst_node):
                start = self._port(connection.src_node, connection.src_port)
                end = self._port(connection.dst_node, connection.dst_port)
                if start is not None and end is not None:
                    link.set_endpoints(start.scenePos(), end.scenePos())

    def apply_edit(self, action: str, operation: Callable[[], Any], payload: Any = None) -> Any:
        """One UI operation, one draft history entry and one future host intent."""
        if self.project_history_enabled and action in {"undo", "redo"}:
            self.edit_requested.emit(action, {"arguments": payload})
            return True
        before = self.state.graph
        result = operation()
        changed = before is not self.state.graph
        self.rebuild()
        if changed:
            after = self.state.snapshot()
            self.edit_requested.emit(action, {"result": result, "arguments": payload, "draft": after})
            self.graph_changed.emit(after)
        self.active_node_changed.emit(self.state.graph.active_id)
        return result

    def navigate_node(self, direction: int) -> None:
        """Select the previous/next numbered node without editing the graph."""
        nodes = sorted((node for node in self.state.graph.nodes.values()
                        if node.kind in (NodeKind.CORRECTOR, NodeKind.COMPOUND)),
                       key=lambda node: node.number)
        if not nodes:
            return
        ids = [node.id for node in nodes]
        current = self.state.graph.active_id
        index = ids.index(current) if current in ids else (-1 if direction > 0 else len(ids))
        target = ids[max(0, min(len(ids) - 1, index + direction))]
        self.state.select({target}, target)
        self.rebuild()
        self.active_node_changed.emit(target)
        self.ensureVisible(self.cards[target])

    def append_node(self) -> None:
        """Insert a serial corrector immediately before the main output."""
        output = next((node.id for node in self.state.graph.nodes.values()
                       if node.kind == NodeKind.OUTPUT), "")
        edge = next((edge for edge in self.state.graph.connections if edge.dst_node == output), None)
        anchor = edge.src_node if edge else next((node.id for node in self.state.graph.nodes.values()
                                                 if node.kind == NodeKind.SOURCE), "")
        if not anchor:
            return
        self.state.select({anchor}, anchor)
        self.active_node_changed.emit(anchor)
        self.apply_edit("add_serial", self.state.add_serial)

    def set_hand_mode(self, hand: bool) -> None:
        self._hand_mode = hand
        self.setCursor(Qt.CursorShape.OpenHandCursor if hand else Qt.CursorShape.ArrowCursor)
        self.setDragMode(QGraphicsView.DragMode.NoDrag if hand else QGraphicsView.DragMode.RubberBandDrag)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(C.BG_PANEL))
        if not self.show_grid:
            return
        import math
        for step, color in ((24, C.BG_INPUT), (120, C.BORDER_SUBTLE)):
            pen = QPen(QColor(color), 0.0)
            painter.setPen(pen)
            left = math.floor(rect.left() / step) * step
            top = math.floor(rect.top() / step) * step
            for x in range(left, math.ceil(rect.right()), step):
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            for y in range(top, math.ceil(rect.bottom()), step):
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self._has_fitted:
            self._has_fitted = True
            self.fit_graph()

    def fit_graph(self) -> None:
        bounds = QRectF()
        for card in self.cards.values():
            bounds = bounds.united(card.sceneBoundingRect())
        if bounds.isEmpty():
            return
        self.resetTransform()
        scale = min(1.0, (self.viewport().width() - 44) / max(1.0, bounds.width() + 60),
                    (self.viewport().height() - 44) / max(1.0, bounds.height() + 90))
        self.scale(max(0.25, scale), max(0.25, scale))
        self.centerOn(bounds.center())
        self.zoom_changed.emit(self.transform().m11())

    def set_zoom(self, value: float) -> None:
        current = self.transform().m11()
        target = max(0.25, min(3.0, value))
        self.scale(target / current, target / current)
        self.zoom_changed.emit(target)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not delta:
            event.ignore()
            return
        before = self.mapToScene(event.position().toPoint())
        self.set_zoom(self.transform().m11() * (1.12 if delta > 0 else 1.0 / 1.12))
        after = self.mapToScene(event.position().toPoint())
        self.translate(after.x() - before.x(), after.y() - before.y())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        position = event.position().toPoint()
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and (self._hand_mode or self._space_down)
        ):
            self._panning = True
            self._pan_start = position
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        port = self._port_at(position)
        if event.button() == Qt.MouseButton.LeftButton and port is not None:
            self._wire_origin = port
            self._wire_press = position
            self._wire_dragged = False
            if port.direction == "in":
                self._rewire = next((link for link in self.state.graph.connections
                                     if (link.dst_node, link.dst_port) == (port.node_id, port.port_name)), None)
            self._wire = QGraphicsPathItem()
            self._wire.setZValue(10)
            self._wire.setPen(QPen(QColor("#26a9e0" if port.data_type == "key" else "#a6a6ad"), 2.0))
            self.scene().addItem(self._wire)
            event.accept()
            return
        card = self._card_at(position)
        if event.button() == Qt.MouseButton.LeftButton and card is not None:
            modifiers = event.modifiers()
            self._duplicate_drag = bool(modifiers & Qt.KeyboardModifier.AltModifier and not card.node.locked)
            if card is not None:
                self.state.graph.active_id = card.node.id
                self._move_start = {node_id: (item.pos().x(), item.pos().y())
                                    for node_id, item in self.cards.items()}
        super().mousePressEvent(event)
        if card is not None:
            self.state.graph.active_id = card.node.id
            self.active_node_changed.emit(card.node.id)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        position = event.position().toPoint()
        if self._panning:
            delta = position - self._pan_start
            self._pan_start = position
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        if self._wire is not None and self._wire_origin is not None:
            if (position - self._wire_press).manhattanLength() >= QApplication.startDragDistance():
                self._wire_dragged = True
            if self._wire_dragged and self._rewire is not None:
                for link in self.links:
                    if link.connection == self._rewire:
                        link.setVisible(False)
            start, end = self._wire_origin.scenePos(), self.mapToScene(position)
            path = QPainterPath(start)
            path.lineTo(end)
            self._wire.setPath(path)
            target = self._port_at(position)
            valid = target is not None and target.direction != self._wire_origin.direction and target.data_type == self._wire_origin.data_type
            self.viewport().setCursor(Qt.CursorShape.CrossCursor if target is None or valid else Qt.CursorShape.ForbiddenCursor)
            event.accept()
            return
        super().mouseMoveEvent(event)
        if self._move_start and event.buttons() & Qt.MouseButton.LeftButton:
            link = self._link_at(position)
            if link is not self._insert_target:
                if self._insert_target is not None:
                    self._insert_target.set_highlighted(False)
                self._insert_target = link
                if link is not None:
                    link.set_highlighted(True)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hand_mode else Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        if self._wire_origin is not None:
            origin = self._wire_origin
            target = self._port_at(event.position().toPoint())
            old = self._rewire
            dragged = self._wire_dragged
            self._cancel_wire()
            if target is not None and origin.direction != target.direction:
                src, dst = (origin, target) if origin.direction == "out" else (target, origin)
                connection = ConnectionDraft(src.node_id, src.port_name, dst.node_id, dst.port_name)
                accepted = self.apply_edit("connect", lambda: self.state.connect(connection), connection)
                if not accepted:
                    QToolTip.showText(event.globalPosition().toPoint(), tr("invalid_connection", "Incompatible connection or circular path."), self)
            elif target is None and old is not None and dragged:
                self.apply_edit("disconnect", lambda: self.state.disconnect(old), old)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if self._move_start:
            positions = {node_id: (item.pos().x(), item.pos().y()) for node_id, item in self.cards.items()
                         if (item.pos().x(), item.pos().y()) != self._move_start.get(node_id)}
            self._move_start = {}
            insertion = self._insert_target
            self._insert_target = None
            if positions:
                if self._duplicate_drag:
                    graph = self.state.snapshot()
                    first = next(iter(positions))
                    old = self.state.graph.nodes[first]
                    offset = (positions[first][0] - old.x, positions[first][1] - old.y)
                    self.apply_edit("duplicate_nodes", lambda: self.state.paste_nodes(graph, positions, offset), positions)
                elif insertion is not None and len(positions) == 1:
                    connection = insertion.connection
                    node_id = next(iter(positions))
                    self.apply_edit("move_and_insert", lambda: self.state.move_nodes_and_insert(positions, node_id, connection), positions)
                else:
                    self.apply_edit("move_nodes", lambda: self.state.move_nodes(positions), positions)
            self._duplicate_drag = False

    def _cancel_wire(self) -> None:
        if self._rewire is not None:
            for link in self.links:
                if link.connection == self._rewire:
                    link.setVisible(True)
        self._wire_dragged = False
        if self._wire is not None and self._wire.scene() is self.scene():
            self.scene().removeItem(self._wire)
        self._wire = None
        self._wire_origin = None
        self._rewire = None
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._hand_mode else Qt.CursorShape.ArrowCursor)

    def _open_node(self, node_id: str) -> None:
        node = self.state.graph.nodes.get(node_id)
        if node is not None and node.kind == NodeKind.COMPOUND:
            self.compound_open_requested.emit(node_id)
        else:
            self.preview_requested.emit(node_id)

    def label_selected(self) -> None:
        node_id = self.state.graph.active_id
        node = self.state.graph.nodes.get(node_id)
        card = self.cards.get(node_id)
        if node is None or card is None or node.kind in (NodeKind.SOURCE, NodeKind.OUTPUT, NodeKind.ALPHA_OUTPUT):
            return
        self._label_generation += 1
        generation = self._label_generation
        editor = _LabelEditor(node.label, lambda value: self._finish_label(node_id, value, generation))
        self._label = editor
        self._label_node_id = node_id
        self.scene().addItem(editor)
        editor.setPos(card.pos() + QPointF(-10.0, -28.0))
        editor.setTextWidth(max(100.0, len(node.label) * 8.0))
        editor.setFocus()
        cursor = editor.textCursor()
        cursor.select(cursor.SelectionType.Document)
        editor.setTextCursor(cursor)

    def _finish_label(self, node_id: str, value: str | None, generation: int) -> None:
        if generation != self._label_generation:
            return
        editor, self._label = self._label, None
        self._label_generation += 1
        if editor is not None:
            editor._finished = True
            if editor.scene() is self.scene():
                self.scene().removeItem(editor)
        if value is not None:
            self.apply_edit("label_node", lambda: self.state.set_node_fields({node_id}, label=value), {"node_id": node_id, "label": value})
        self.setFocus()

    def commit_label(self) -> None:
        """Finish a label before an intentional user navigation to another scope."""
        if self._label is not None:
            self._finish_label(self._label_node_id, self._label.toPlainText().strip(), self._label_generation)

    def _editable_ids(self) -> set[str]:
        return {node_id for node_id in self.state.graph.selected_ids
                if self.state.graph.nodes[node_id].kind not in (NodeKind.SOURCE, NodeKind.OUTPUT, NodeKind.ALPHA_OUTPUT)}

    def toggle_selected(self) -> None:
        ids = self._editable_ids()
        enabled = any(not self.state.graph.nodes[node_id].enabled for node_id in ids)
        if ids:
            self.apply_edit("set_enabled", lambda: self.state.set_node_fields(ids, enabled=enabled), {"ids": sorted(ids), "enabled": enabled})

    def toggle_all(self) -> None:
        ids = {node.id for node in self.state.graph.nodes.values()
               if node.kind not in (NodeKind.SOURCE, NodeKind.OUTPUT, NodeKind.ALPHA_OUTPUT)}
        enabled = any(not self.state.graph.nodes[node_id].enabled for node_id in ids)
        self.apply_edit("set_all_enabled", lambda: self.state.set_node_fields(ids, enabled=enabled))

    def copy_grade(self) -> None:
        from copy import deepcopy
        node = self.state.graph.nodes.get(self.state.graph.active_id)
        if node is not None:
            self._copied_grade = deepcopy(node.grade)
            self._copied_graph = self.state.snapshot()
            # N12：多域剪贴板（grade/curves/lut/window/effects 全捕获）。
            self._copied_domains = self._capture_node_domains(node)

    def _capture_node_domains(self, node) -> dict:
        from copy import deepcopy
        payload = node.backend_payload if isinstance(
            node.backend_payload, dict) else {}
        legacy = payload.get("legacy_payload") or {}
        params = payload.get("params") or {}
        resources = params.get("resources") or {}
        return {
            "grade": deepcopy(dict(node.grade or {})),
            "curves": deepcopy(legacy.get("curves_data")),
            "lut": ({"lut_ref": resources.get("lut_ref"),
                     "lut_strength": resources.get("lut_strength", 1.0)}
                    if isinstance(resources, dict) and resources.get("lut_ref")
                    else None),
            "window": deepcopy(legacy.get("mask_shapes")),
            "effects": deepcopy(legacy.get("plugins")),
        }

    def paste_grade(self) -> None:
        if self._copied_grade is not None:
            self.apply_edit("paste_grade", lambda: self.state.set_node_fields(self._editable_ids(), grade=self._copied_grade))

    def paste_domains(self, domains: tuple[str, ...]) -> None:
        """N12：选择性粘贴（经 op_paste_selected_domains：身份重建、
        只写选定域；幂等空载荷无操作）。"""
        payload = getattr(self, "_copied_domains", None)
        if not payload:
            return
        selected = {d: payload.get(d) for d in domains}
        if not any(v is not None for v in selected.values()):
            return
        ids = self._editable_ids()
        if not ids:
            return
        self.apply_edit(
            "paste_domains",
            lambda: self._paste_domains_via_service(ids, selected, domains))

    def _paste_domains_via_service(self, ids, selected, domains) -> None:
        """本地状态直达 service 语义（编辑经 apply_edit 提交权威）。"""
        from src.features.color.core.color_graph_service import ColorGraphService
        result = ColorGraphService().apply_intent(
            self.state.graph, "paste_selected_domains",
            {"node_ids": sorted(ids), "payload": selected,
             "domains": list(domains)})
        if result.ok:
            self.state._commit(result.graph)

    def select_adjacent(self, offset: int) -> None:
        ids = [node.id for node in sorted(self.state.graph.nodes.values(), key=lambda n: n.number)
               if node.kind not in (NodeKind.SOURCE, NodeKind.OUTPUT, NodeKind.ALPHA_OUTPUT)]
        if not ids:
            return
        current = ids.index(self.state.graph.active_id) if self.state.graph.active_id in ids else 0
        node_id = ids[(current + offset) % len(ids)]
        self.state.select({node_id}, node_id)
        self.rebuild()
        self.active_node_changed.emit(node_id)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._label is not None:
            super().keyPressEvent(event)
            return
        key, mods = event.key(), event.modifiers()
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        # Qt maps macOS Command to ControlModifier unless AA_MacDontSwapCtrlAndMeta.
        command = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Escape:
            self._cancel_wire()
            self._move_start = {}
            self.rebuild()
        elif key == Qt.Key.Key_Space:
            self._space_down = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif alt and key == Qt.Key.Key_S:
            self.apply_edit("add_serial", lambda: self.state.add_serial(before=shift))
        elif alt and key == Qt.Key.Key_P:
            self.apply_edit("add_parallel", lambda: self.state.add_branch(NodeKind.PARALLEL_MIXER))
        elif alt and key == Qt.Key.Key_L:
            self.apply_edit("add_layer", lambda: self.state.add_branch(NodeKind.LAYER_MIXER))
        elif alt and key == Qt.Key.Key_O:
            self.apply_edit("add_outside", self.state.add_outside)
        elif command and key == Qt.Key.Key_D:
            self.toggle_all() if alt else self.toggle_selected()
        elif command and key == Qt.Key.Key_Z:
            self.apply_edit("redo" if shift else "undo", self.state.redo if shift else self.state.undo)
        elif command and key == Qt.Key.Key_C:
            self.copy_grade()
        elif command and key == Qt.Key.Key_V:
            self.paste_grade()
        elif command and key == Qt.Key.Key_A:
            self.state.select(set(self.cards), self.state.graph.active_id)
            self.rebuild()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            links = [item.connection for item in self.scene().selectedItems() if isinstance(item, ConnectionItem)]
            if links:
                self.apply_edit("disconnect", lambda: self.state.disconnect_many(links), links)
            else:
                self.apply_edit("extract_nodes" if alt else "delete_nodes",
                                lambda: self.state.extract_nodes(self._editable_ids()) if alt else self.state.delete_nodes(self._editable_ids()))
        elif key == Qt.Key.Key_F2:
            self.label_selected()
        elif key == Qt.Key.Key_Home:
            self.fit_graph()
        elif alt and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.select_adjacent(-1 if key == Qt.Key.Key_Left else 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def event(self, event: QEvent) -> bool:
        # Existing host layer actions use WindowShortcut. Claim canvas keys
        # before QAction dispatch so a node gesture cannot edit legacy layers.
        if event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent):
            if self._label is None:
                key, mods = event.key(), event.modifiers()
                canvas_key = key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace, Qt.Key.Key_Escape, Qt.Key.Key_F2)
                canvas_key |= bool(mods & Qt.KeyboardModifier.AltModifier) and key in (Qt.Key.Key_S, Qt.Key.Key_P, Qt.Key.Key_L, Qt.Key.Key_O, Qt.Key.Key_Left, Qt.Key.Key_Right)
                canvas_key |= bool(mods & Qt.KeyboardModifier.ControlModifier) and key in (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_V, Qt.Key.Key_D, Qt.Key.Key_Z)
                if canvas_key:
                    event.accept()
                    return True
        return super().event(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_down = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hand_mode else Qt.CursorShape.ArrowCursor)
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        self._space_down = False
        self._panning = False
        super().focusOutEvent(event)

    def _action(self, menu: QMenu, key: str, title: str, callback: Callable[[], Any],
                shortcut: str = "", enabled: bool = True, checked: bool | None = None) -> Any:
        action = menu.addAction(tr(key, title))
        action.setObjectName("node_action_" + key)
        if shortcut:
            # Display only: canvas handles keys with WidgetShortcut scope.
            display = QKeySequence(shortcut.replace("Cmd", "Ctrl")).toString(QKeySequence.SequenceFormat.NativeText)
            action.setText(action.text() + "\t" + display)
        action.setEnabled(enabled)
        if checked is not None:
            action.setCheckable(True)
            action.setChecked(checked)
        action.triggered.connect(lambda _checked=False: callback())
        return action

    def build_context_menu(self, position: QPoint | None = None) -> QMenu:
        """Build once per invocation so enabled states always match selection."""
        menu = QMenu(self)
        menu.setObjectName("ColorNodeContextMenu")
        selected = self._editable_ids()
        active = self.state.graph.nodes.get(self.state.graph.active_id)
        has_node = bool(selected)
        if active is not None and active.kind == NodeKind.ALPHA_OUTPUT:
            self._action(menu, "remove_alpha_output", "Remove Alpha Output", lambda: self.apply_edit("remove_alpha_output", lambda: self.state.remove_alpha_output(active.id)))
            return menu
        if position is not None:
            link = self._link_at(position)
            if link is not None and self._card_at(position) is None:
                self._action(menu, "delete_connection", "Delete Connection", lambda: self.apply_edit("disconnect", lambda: self.state.disconnect(link.connection)))
                return menu
        add = menu.addMenu(tr("add_node", "Add Node"))
        self._action(add, "add_serial", "Add Serial", lambda: self.apply_edit("add_serial", self.state.add_serial), "Alt+S")
        self._action(add, "add_before", "Add Serial Before", lambda: self.apply_edit("add_serial_before", lambda: self.state.add_serial(before=True)), "Shift+Alt+S")
        self._action(add, "add_parallel", "Add Parallel", lambda: self.apply_edit("add_parallel", lambda: self.state.add_branch(NodeKind.PARALLEL_MIXER)), "Alt+P", has_node)
        self._action(add, "add_layer", "Add Layer", lambda: self.apply_edit("add_layer", lambda: self.state.add_branch(NodeKind.LAYER_MIXER)), "Alt+L", has_node)
        self._action(add, "add_outside", "Add Outside", lambda: self.apply_edit("add_outside", self.state.add_outside), "Alt+O", has_node)
        self._action(add, "splitter_combiner", "Add Splitter Combiner", lambda: self.apply_edit("add_splitter_combiner", self.state.add_splitter_combiner), enabled=has_node)
        add.addSeparator()
        self._action(add, "add_key_mixer", "Add Key Mixer", lambda: self.apply_edit("add_key_mixer", self.state.add_key_mixer), enabled=has_node)
        location = self.mapToScene(position or self.viewport().rect().center())
        for kind, title in ((NodeKind.CORRECTOR, "Corrector"), (NodeKind.PARALLEL_MIXER, "Parallel Mixer"),
                            (NodeKind.LAYER_MIXER, "Layer Mixer"), (NodeKind.KEY_MIXER, "Key Mixer"),
                            (NodeKind.SPLITTER, "Splitter"), (NodeKind.COMBINER, "Combiner")):
            self._action(add, kind.value, title, lambda k=kind: self.apply_edit("add_node", lambda: self.state.add_node(k, location.x(), location.y())))
        self._action(menu, "alpha_output", "Add Alpha Output", lambda: self.apply_edit("add_alpha_output", self.state.add_alpha_output))
        menu.addSeparator()
        self._action(menu, "label", "Node Label", self.label_selected, enabled=has_node)
        colors = menu.addMenu(tr("node_color", "Node Color"))
        for name, color in (("Default", ""), ("Red", "#b55148"), ("Orange", "#ba824b"), ("Yellow", "#b5a457"),
                            ("Green", "#6c9462"), ("Cyan", "#589c9e"), ("Blue", "#587fa9"), ("Purple", "#926ba1"), ("Pink", "#b77892")):
            self._action(colors, "color_" + name.lower(), name, lambda c=color: self.apply_edit("node_color", lambda: self.state.set_node_fields(selected, color=c)), enabled=has_node)
        self._action(menu, "enable", "Enable Node", self.toggle_selected, "Cmd+D", has_node,
                     active.enabled if active is not None else False)
        self._action(menu, "lock", "Lock Node", lambda: self.apply_edit("lock_node", lambda: self.state.set_node_fields(selected, locked=not bool(active and active.locked))), enabled=has_node, checked=bool(active and active.locked))
        menu.addSeparator()
        self._action(menu, "reset", "Reset Node Grade", lambda: self.apply_edit("reset_grade", lambda: self.state.reset_grades(selected)), enabled=has_node)
        self._action(menu, "copy", "Copy", self.copy_grade, "Cmd+C", has_node)
        paste_menu = menu.addMenu(tr("paste_domains", "Paste Domains"))
        has_clip = getattr(self, "_copied_domains", None) is not None
        for domain, title in (("grade", "Grade"), ("curves", "Curves"),
                              ("lut", "LUT"), ("window", "Window"),
                              ("effects", "Effects")):
            self._action(paste_menu, "paste_" + domain, title,
                         lambda d=(domain,): self.paste_domains(d),
                         enabled=has_clip and has_node)
        self._action(paste_menu, "paste_all", "All",
                     lambda: self.paste_domains(
                         ("grade", "curves", "lut", "window", "effects")),
                     enabled=has_clip and has_node)
        self._action(menu, "paste", "Paste", self.paste_grade, "Cmd+V", has_node and self._copied_grade is not None)
        self._action(menu, "paste_nodes", "Paste Nodes", lambda: self.apply_edit("paste_nodes", lambda: self.state.paste_nodes(self._copied_graph, self._copied_graph.selected_ids)), enabled=self._copied_graph is not None)
        self._action(menu, "duplicate", "Duplicate Node", lambda: self.apply_edit("duplicate_nodes", lambda: self.state.duplicate_nodes(selected)), enabled=has_node)
        self._action(menu, "delete", "Delete Node", lambda: self.apply_edit("delete_nodes", lambda: self.state.delete_nodes(selected)), "Backspace", has_node)
        self._action(menu, "extract", "Extract Node", lambda: self.apply_edit("extract_nodes", lambda: self.state.extract_nodes(selected)), "Alt+Backspace", has_node)
        menu.addSeparator()
        compound = bool(active is not None and active.kind == NodeKind.COMPOUND)
        self._action(menu, "compound", "Create Compound Node", lambda: self.apply_edit("make_compound", lambda: self.state.make_compound(selected)), enabled=has_node)
        self._action(menu, "show_compound", "Show Compound Node", lambda: self.compound_open_requested.emit(self.state.graph.active_id), enabled=compound)
        self._action(menu, "decompose", "Decompose Compound Node", lambda: self.apply_edit("expand_compound", lambda: self.state.expand_compound(self.state.graph.active_id)), enabled=compound)
        self._action(menu, "shared", "Save as Shared Node", self._save_shared, enabled=has_node)
        if active is not None and active.shared_id:
            self._action(menu, "local", "Convert to Corrector Node", lambda: self.apply_edit("unlink_shared", lambda: self.state.make_local(selected)))
        if active is not None and active.kind == NodeKind.CORRECTOR:
            # N02：Invert Key Input 已由图执行消费（key_in 外部 key 取反，
            # GL/CPU 同组合顺序）——菜单启用。
            self._action(menu, "key_invert", "Invert Key Input", lambda: self._set_grade_value("key_invert", not bool(active.node_options.get("key_invert"))), enabled=True, checked=bool(active.node_options.get("key_invert")))
            lut = menu.addMenu(tr("lut", "LUT"))
            # N04：经 R02 资源注册 + R05 节点命令（与浏览器共用同一
            # topos-res 选择/命令路径）。
            node_id = active.id
            self._action(lut, "no_lut", "No LUT",
                         lambda: self.node_lut_apply_requested.emit(node_id, ""),
                         checked=not bool(active.node_options.get("lut_path")))
            self._action(lut, "browse_lut", "Browse…",
                         lambda: self.node_lut_browse_requested.emit(node_id))
            from src.shared.color.lut_resource_registry import (
                get_lut_resource_session,
            )
            for entry in get_lut_resource_session().lut_entries()[:12]:
                uri = entry.uri
                title = (entry.metadata or {}).get("title") or \
                        entry.original_filename or entry.resource_id
                self._action(lut, "lut_res_" + entry.resource_id[:12], str(title),
                             lambda u=uri: self.node_lut_apply_requested.emit(node_id, u),
                             checked=active.node_options.get("lut_path") == uri)
            for key, title, values in (("color_space", "Color Space", ("Use Timeline", "Rec.709", "Rec.2020", "P3-D65", "ACES AP0", "ACES AP1", "DaVinci Wide Gamut")),
                                       ("gamma", "Gamma", ("Use Timeline", "Linear", "Gamma 2.2", "Gamma 2.4", "sRGB", "ST2084", "HLG", "DaVinci Intermediate"))):
                options = menu.addMenu(tr(key, title))
                # N05：后端已消费（GL/CPU 包裹变换；Metal/导出显式拒
                # 绝）——菜单启用；Use Timeline = 恒等原结果。
                options.setToolTip(tr("node_color_space_hint",
                                      "Input transform wraps this node's "
                                      "grade chain (GL/CPU); Metal/export "
                                      "reject non-timeline values."))
                for value in values:
                    self._action(options, key + "_" + value.lower().replace(" ", "_"), value,
                                 lambda k=key, v=value: self._set_grade_value(k, v), checked=active.node_options.get(key, "Use Timeline") == value)
        if active is not None and active.kind in (NodeKind.PARALLEL_MIXER, NodeKind.LAYER_MIXER, NodeKind.KEY_MIXER):
            self._action(menu, "add_input", "Add One Input", lambda: self.apply_edit("add_input", lambda: self.state.set_node_fields({active.id}, input_count=active.input_count + 1)))
            if active.kind != NodeKind.KEY_MIXER:
                target_kind = NodeKind.LAYER_MIXER if active.kind == NodeKind.PARALLEL_MIXER else NodeKind.PARALLEL_MIXER
                self._action(menu, "morph", tr("morph_to_layer" if target_kind == NodeKind.LAYER_MIXER else "morph_to_parallel", "Morph into " + ("Layer Mixer Node" if target_kind == NodeKind.LAYER_MIXER else "Parallel Mixer Node")),
                             lambda: self._morph_mixer(active.id, target_kind))
            if active.kind == NodeKind.LAYER_MIXER:
                blend = menu.addMenu(tr("composite_mode", "Composite Mode"))
                for mode in ("Normal", "Add", "Subtract", "Multiply", "Screen", "Overlay", "Soft Light", "Hard Light", "Difference", "Color", "Luminosity"):
                    self._action(blend, "blend_" + mode.lower().replace(" ", "_"), mode, lambda m=mode: self._set_grade_value("blend_mode", m),
                                 checked=active.node_options.get("blend_mode", "Normal") == mode)
        cache = menu.addMenu(tr("node_cache", "Node Cache"))
        # N06：执行器 per-node 输出缓存已交付（Auto=分叉复用/On=强制/
        # Off=禁止；预算 LRU + 世代老化 + 命中事件）——菜单启用。
        cache.setToolTip(tr("node_cache_hint",
                            "Auto: cache forked outputs · On: always · "
                            "Off: never (within the node-cache budget)"))
        for mode in ("auto", "on", "off"):
            self._action(cache, "cache_" + mode, mode.title(), lambda m=mode: self.apply_edit("node_cache", lambda: self.state.set_node_fields(selected, cache_mode=m)), enabled=has_node,
                         checked=active is not None and active.cache_mode == mode)
        menu.addSeparator()
        self._action(menu, "cleanup", "Cleanup Node Graph", lambda: self.apply_edit("auto_layout", self.state.auto_layout))
        self._action(menu, "fit", "Zoom to Fit", self.fit_graph)
        return menu

    def _save_shared(self) -> None:
        import uuid
        self.apply_edit("save_shared", lambda: self.state.set_node_fields(self._editable_ids(), shared_id=str(uuid.uuid4()), locked=True))

    def _choose_lut(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("choose_lut", "Choose LUT"), "", "LUT (*.cube *.3dl *.look)")
        if path:
            self._set_grade_value("lut_path", path)

    def _morph_mixer(self, node_id: str, kind: NodeKind) -> None:
        self.apply_edit("morph_mixer", lambda: self.state.morph_mixer(node_id), {"node_id": node_id, "kind": kind.value})

    def _set_grade_value(self, name: str, value: Any) -> None:
        from copy import deepcopy
        active = self.state.graph.nodes.get(self.state.graph.active_id)
        if active is not None:
            options = deepcopy(active.node_options)
            options[name] = value
            self.apply_edit("set_node_option", lambda: self.state.set_node_fields({active.id}, node_options=options), {"name": name, "value": value})

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        card = self._card_at(event.pos())
        if card is not None and not card.isSelected():
            self.state.select({card.node.id}, card.node.id)
            self.rebuild()
        menu = self.build_context_menu(event.pos())
        menu.exec(event.globalPos())
        event.accept()
