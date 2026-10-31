"""Frame-scrubbable teaching canvas with zoom and drag-to-edit support."""

from __future__ import annotations

import json
import math

from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPen, QColor
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QWidget

from ..core.model import TeachingSceneDocument
from ..core.vector_shapes import SHAPE_KINDS, path_points_in_scene
from ..core.contracts import RenderedFrame
from ..presets import MG_PRESET_MIME
from src.shared.ui.style_sheet import C


class SceneView(QWidget):
    elementDragged = pyqtSignal(str, float, float)
    elementDragStarted = pyqtSignal(str)
    elementDragFinished = pyqtSignal(str, float, float)
    elementSelected = pyqtSignal(str)
    # Shift-click toggles an element into/out of the multi-selection without
    # starting a drag (Figma-style additive select).
    elementSelectionToggled = pyqtSignal(str)
    # (element ids, additive) — emitted after a background drag on empty
    # canvas.  SceneView only reports hit-test results; the editor window owns
    # the actual selection set.
    marqueeSelectionChanged = pyqtSignal(object, bool)
    shapeDrawn = pyqtSignal(str, float, float, float, float)
    pathDrawn = pyqtSignal(object)
    pathAnchorsChanged = pyqtSignal(str, object)
    # Double-click on a text layer asks the editor to start editing its content
    # in the Parameters panel (the SVG canvas itself is not a text surface).
    elementEditRequested = pyqtSignal(str)
    # A preset dragged from the asset library: (preset_id, scene_x, scene_y).
    # SceneView only reports the drop point; the window owns the insert command.
    presetDropped = pyqtSignal(str, float, float)
    templateDropped = pyqtSignal(str, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene: TeachingSceneDocument | None = None
        self._frame: RenderedFrame | None = None
        self._renderer: QSvgRenderer | None = None
        # Pre-rasterized RGBA frame (HTML scenes capture pixels, not SVG).
        # When set, paintEvent draws this image instead of the SVG renderer.
        self._image: QImage | None = None
        self._zoom = 1.0
        self._drag_id: str | None = None
        self._drag_start = QPointF()
        self._element_start = QPointF()
        self._marquee_start: QPointF | None = None
        self._marquee_rect: QRectF | None = None
        self._draw_shape_kind: str | None = None
        self._draw_shape_start: QPointF | None = None
        self._draw_shape_rect: QRectF | None = None
        self._path_draw_points: list[QPointF] = []
        self._path_hover_point: QPointF | None = None
        self._selected_elements: frozenset[str] = frozenset()
        self._drag_path_id: str | None = None
        self._drag_path_anchor_index: int | None = None
        self._drag_path_points: list[QPointF] = []
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)
        # Accept preset drags from the asset library panel (MG_PRESET_MIME).
        self.setAcceptDrops(True)

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_scene(self, scene: TeachingSceneDocument) -> None:
        previous = self._scene
        self._scene = scene
        # A scene switch or immutable scene edit invalidates the cached raster;
        # a pure playhead change reuses the last frame while the HTML worker
        # captures the requested frame in the background.
        if previous is None or previous != scene:
            self._frame = None
            self._discard_renderer()
            self._image = None
        self.update()

    def set_frame(self, frame: RenderedFrame) -> None:
        if frame.pixel_format == "svg":
            self._frame = frame
            self._image = None
            renderer = QSvgRenderer(QByteArray(frame.data), self)
            # AUD-31011: the renderer is Qt-parented, so dropping the Python
            # reference alone keeps the parsed SVG DOM alive until the view
            # dies.  Retire the previous renderer explicitly — the preview
            # path builds one per controller notification (per drag tick).
            self._discard_renderer()
            self._renderer = renderer
        elif frame.pixel_format == "rgba8":
            # HTML captures produce tightly-packed RGBA8888 bytes. Build the
            # QImage from the buffer, then .copy() so it owns its memory and
            # does not reference the (possibly GC'd) Python bytes.
            image = QImage(frame.data, frame.width, frame.height,
                           frame.width * 4, QImage.Format.Format_RGBA8888)
            self._frame = frame
            self._discard_renderer()
            self._image = image.copy() if not image.isNull() else None
        else:
            raise ValueError(f"SceneView cannot display pixel_format={frame.pixel_format!r}")
        self.update()

    def _discard_renderer(self) -> None:
        """Retire the cached SVG renderer (deferred delete is paint-safe)."""
        renderer = self._renderer
        self._renderer = None
        if renderer is not None:
            renderer.deleteLater()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.1, min(float(zoom), 8.0))
        self.update()

    def fit_canvas(self) -> None:
        if self._scene is None or self._scene.width <= 0 or self._scene.height <= 0:
            return
        self._zoom = min(self.width() / self._scene.width, self.height() / self._scene.height)
        self._zoom = max(0.1, self._zoom)
        self.update()

    @property
    def draw_shape_kind(self) -> str | None:
        return self._draw_shape_kind

    def set_draw_shape_kind(self, kind: str | None) -> None:
        """Switch between selection and one of the native vector draw tools."""
        normalized = str(kind).lower() if kind is not None else None
        if normalized is not None and normalized not in SHAPE_KINDS:
            raise ValueError(f"Unsupported draw shape: {kind!r}")
        self._draw_shape_kind = normalized
        self._draw_shape_start = None
        self._draw_shape_rect = None
        self._path_draw_points.clear()
        self._path_hover_point = None
        self.setCursor(Qt.CursorShape.CrossCursor if normalized is not None else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_selected_elements(self, element_ids: object) -> None:
        """Receive the editor-owned selection for path-anchor overlays."""
        if isinstance(element_ids, str):
            self._selected_elements = frozenset({element_ids})
        elif isinstance(element_ids, (tuple, list, set, frozenset)):
            self._selected_elements = frozenset(str(item) for item in element_ids)
        else:
            self._selected_elements = frozenset()
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(C.BG_DEEPEST))
        if self._scene is None:
            painter.end()
            return
        if self._renderer is not None or self._image is not None:
            scale = min(self.width() / self._scene.width, self.height() / self._scene.height) * self._zoom
            target_width = self._scene.width * scale
            target_height = self._scene.height * scale
            target = QRectF((self.width() - target_width) / 2, (self.height() - target_height) / 2, target_width, target_height)
            if self._image is not None:
                painter.drawImage(target, self._image)
            else:
                self._renderer.render(painter, target)
        if self._marquee_rect is not None:
            painter.save()
            painter.setPen(QPen(QColor("#60a5fa"), 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(96, 165, 250, 45))
            marquee = self._scene_rect_to_view_rect(self._marquee_rect)
            painter.drawRect(marquee)
            painter.restore()
        if self._draw_shape_rect is not None:
            painter.save()
            painter.setPen(QPen(QColor("#ff8800"), 1.5, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(255, 136, 0, 28))
            preview = self._scene_rect_to_view_rect(self._draw_shape_rect)
            if self._draw_shape_kind in {"ellipse", "circle"}:
                painter.drawEllipse(preview)
            else:
                painter.drawRect(preview)
            painter.restore()
        if self._path_draw_points:
            painter.save()
            painter.setPen(QPen(QColor("#ff8800"), 1.5, Qt.PenStyle.DashLine))
            view_points = [self._to_view_point(point) for point in self._path_draw_points]
            if self._path_hover_point is not None:
                view_points.append(self._to_view_point(self._path_hover_point))
            for index in range(1, len(view_points)):
                painter.drawLine(view_points[index - 1], view_points[index])
            painter.setBrush(QColor("#ffffff"))
            for point in view_points[:len(self._path_draw_points)]:
                painter.drawEllipse(point, 3.5, 3.5)
            painter.restore()
        active_path = self._selected_path_anchors()
        if active_path is not None:
            _, points = active_path
            painter.save()
            painter.setPen(QPen(QColor("#ff8800"), 1.25))
            painter.setBrush(QColor("#ffffff"))
            for point in points:
                painter.drawEllipse(self._to_view_point(point), 4.0, 4.0)
            painter.restore()
        painter.end()

    # -- preset drag & drop -------------------------------------------------

    def _package_payload_from_mime(self, mime: object) -> tuple[str, str] | None:
        """(package_id, kind) from the unified MG library drag payload."""
        if not hasattr(mime, "hasFormat") or not mime.hasFormat(MG_PRESET_MIME):
            return None
        try:
            payload = json.loads(bytes(mime.data(MG_PRESET_MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        package_id = str(payload.get("preset_id") or "")
        kind = str(payload.get("kind") or "scene_preset")
        return (package_id, kind) if package_id else None

    def _preset_id_from_mime(self, mime: object) -> str | None:
        payload = self._package_payload_from_mime(mime)
        if payload is None or payload[1] != "scene_preset":
            return None
        return payload[0]

    def dragEnterEvent(self, event: object) -> None:
        if self._package_payload_from_mime(getattr(event, "mimeData", lambda: None)()) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event: object) -> None:
        if self._package_payload_from_mime(getattr(event, "mimeData", lambda: None)()) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event: object) -> None:
        payload = self._package_payload_from_mime(getattr(event, "mimeData", lambda: None)())
        if payload is None:
            return
        package_id, kind = payload
        point = self._to_scene_point(event.position())
        event.acceptProposedAction()
        if kind == "html_template":
            self.templateDropped.emit(package_id, float(point.x()), float(point.y()))
        else:
            self.presetDropped.emit(package_id, float(point.x()), float(point.y()))

    def mousePressEvent(self, event: object) -> None:
        if self._scene is None or not hasattr(event, "position"):
            return
        if (
            self._draw_shape_kind == "path"
            and hasattr(event, "button")
            and event.button() == Qt.MouseButton.RightButton
        ):
            self._finish_path_draw()
            return
        if hasattr(event, "button") and event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position()
        scene_point = self._to_scene_point(position)
        if self._draw_shape_kind == "path":
            self._append_path_anchor(scene_point)
            return
        anchor_hit = self._path_anchor_hit(scene_point)
        if anchor_hit is not None:
            element_id, index, points = anchor_hit
            self._drag_path_id = element_id
            self._drag_path_anchor_index = index
            self._drag_path_points = [QPointF(point) for point in points]
            self.elementSelected.emit(element_id)
            return
        if self._draw_shape_kind is not None:
            self._draw_shape_start = scene_point
            self._draw_shape_rect = QRectF(scene_point, scene_point)
            self.update()
            return
        # Locked elements (SG-01 hierarchy) are not selectable/draggable on the
        # canvas — they can only be unlocked from the layer tree.
        from ..core.hierarchy import read_hierarchy, sort_for_paint
        hierarchy = read_hierarchy(self._scene.metadata)
        locked = frozenset(hierarchy.locked)
        additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        # Topmost-first hit testing uses the same hierarchy-aware paint order
        # as the renderer, so a frame's plate never eats clicks aimed at its
        # own children.
        for element in reversed(sort_for_paint(self._scene.elements, hierarchy.parents)):
            if element.element_id in locked:
                continue
            transform = element.transform
            if self._contains_element(transform, scene_point):
                if additive:
                    # Shift-click toggles selection only — no drag starts.
                    self.elementSelectionToggled.emit(element.element_id)
                    return
                self._drag_id = element.element_id
                self._drag_start = scene_point
                self._element_start = QPointF(transform.x, transform.y)
                self.elementSelected.emit(element.element_id)
                self.elementDragStarted.emit(element.element_id)
                return
        # Empty-canvas press starts a rubber-band selection.  The editor
        # remains the selection authority; this widget only computes bounds.
        self._marquee_start = scene_point
        self._marquee_rect = QRectF(scene_point, scene_point)
        self.update()

    def mouseMoveEvent(self, event: object) -> None:
        if not hasattr(event, "position"):
            return
        if self._draw_shape_kind == "path" and self._path_draw_points:
            self._path_hover_point = self._to_scene_point(event.position())
            self.update()
            return
        if self._drag_path_id is not None and self._drag_path_anchor_index is not None:
            self._drag_path_points[self._drag_path_anchor_index] = self._to_scene_point(event.position())
            self.update()
            return
        if self._draw_shape_start is not None:
            current = self._to_scene_point(event.position())
            rect = QRectF(self._draw_shape_start, current).normalized()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                size = max(rect.width(), rect.height())
                rect.setWidth(size)
                rect.setHeight(size)
            self._draw_shape_rect = rect
            self.update()
            return
        if self._drag_id is None and self._marquee_start is not None:
            current = self._to_scene_point(event.position())
            self._marquee_rect = QRectF(self._marquee_start, current).normalized()
            self.update()
            return
        if self._drag_id is None:
            return
        delta = self._to_scene_point(event.position()) - self._drag_start
        self.elementDragged.emit(self._drag_id, self._element_start.x() + delta.x(), self._element_start.y() + delta.y())

    def mouseReleaseEvent(self, event: object) -> None:
        if self._drag_path_id is not None:
            self.pathAnchorsChanged.emit(
                self._drag_path_id,
                tuple((point.x(), point.y()) for point in self._drag_path_points),
            )
            self._drag_path_id = None
            self._drag_path_anchor_index = None
            self._drag_path_points = []
        elif self._draw_shape_start is not None:
            rect = self._draw_shape_rect.normalized() if self._draw_shape_rect is not None else QRectF()
            kind = self._draw_shape_kind
            is_stroke = kind in {"line", "arrow"}
            can_create = (
                rect.width() >= 2.0 or rect.height() >= 2.0
                if is_stroke else rect.width() >= 2.0 and rect.height() >= 2.0
            )
            if kind is not None and can_create:
                # A perfectly horizontal/vertical stroke has a zero extent on
                # one axis. Preserve the user's endpoints while supplying the
                # renderer a minimal hit-testable local rectangle.
                self.shapeDrawn.emit(
                    kind, rect.x(), rect.y(), max(2.0, rect.width()), max(2.0, rect.height()),
                )
            self._draw_shape_start = None
            self._draw_shape_rect = None
        elif self._drag_id is not None:
            element_id = self._drag_id
            element = self._scene.element_by_id(element_id) if self._scene is not None else None
            if element is not None:
                self.elementDragFinished.emit(element_id, element.transform.x, element.transform.y)
        elif self._marquee_rect is not None:
            rect = self._marquee_rect.normalized()
            selected: list[str] = []
            if self._scene is not None:
                from ..core.hierarchy import read_hierarchy
                locked = frozenset(read_hierarchy(self._scene.metadata).locked)
                for element in self._scene.elements:
                    if element.element_id in locked or not element.visible:
                        continue
                    if rect.contains(self._element_bounds(element.transform)):
                        selected.append(element.element_id)
            additive = bool(hasattr(event, "modifiers") and
                            event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.marqueeSelectionChanged.emit(tuple(selected), additive)
        self._drag_id = None
        self._marquee_start = None
        self._marquee_rect = None
        self.update()

    def mouseDoubleClickEvent(self, event: object) -> None:
        if (
            self._draw_shape_kind == "path"
            and hasattr(event, "button")
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._finish_path_draw()
            return
        if (
            self._draw_shape_kind is None
            and self._scene is not None
            and hasattr(event, "button")
            and event.button() == Qt.MouseButton.LeftButton
        ):
            from ..core.hierarchy import read_hierarchy, sort_for_paint
            hierarchy = read_hierarchy(self._scene.metadata)
            locked = frozenset(hierarchy.locked)
            point = self._to_scene_point(event.position())
            for element in reversed(sort_for_paint(self._scene.elements, hierarchy.parents)):
                if element.element_id in locked or not element.visible:
                    continue
                if element.type == "text" and self._contains_element(element.transform, point):
                    self.elementEditRequested.emit(element.element_id)
                    return
        super().mouseDoubleClickEvent(event)  # type: ignore[arg-type]

    def keyPressEvent(self, event: object) -> None:
        if hasattr(event, "key") and event.key() == Qt.Key.Key_Escape:
            if self._path_draw_points:
                self._path_draw_points.clear()
                self._path_hover_point = None
                self.update()
                event.accept()
                return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def _to_scene_point(self, point: QPointF) -> QPointF:
        if self._scene is None:
            return point
        scale = min(self.width() / self._scene.width, self.height() / self._scene.height) * self._zoom
        target_width = self._scene.width * scale
        target_height = self._scene.height * scale
        origin_x = (self.width() - target_width) / 2
        origin_y = (self.height() - target_height) / 2
        return QPointF((point.x() - origin_x) / scale, (point.y() - origin_y) / scale)

    def _to_view_point(self, point: QPointF) -> QPointF:
        rect = self._scene_rect_to_view_rect(QRectF(point, point))
        return rect.topLeft()

    def _append_path_anchor(self, point: QPointF) -> None:
        if len(self._path_draw_points) >= 3:
            first = self._path_draw_points[0]
            if math.hypot(point.x() - first.x(), point.y() - first.y()) <= 8.0 / max(self._zoom, 0.1):
                self._finish_path_draw(closed=True)
                return
        self._path_draw_points.append(point)
        self._path_hover_point = point
        self.update()

    def _finish_path_draw(self, *, closed: bool = False) -> None:
        if len(self._path_draw_points) >= 2:
            points = list(self._path_draw_points)
            if closed:
                points.append(QPointF(points[0]))
            self.pathDrawn.emit(tuple((point.x(), point.y()) for point in points))
        self._path_draw_points.clear()
        self._path_hover_point = None
        self.update()

    def _selected_path_anchors(self) -> tuple[str, tuple[QPointF, ...]] | None:
        if self._scene is None or len(self._selected_elements) != 1:
            return None
        element_id = next(iter(self._selected_elements))
        element = self._scene.element_by_id(element_id)
        if element is None or element.type != "shape" or not isinstance(element.content, dict):
            return None
        if str(element.content.get("kind", "")) != "path":
            return None
        points = path_points_in_scene(
            element.content, x=element.transform.x, y=element.transform.y,
        )
        if not points:
            return None
        if self._drag_path_id == element_id and self._drag_path_points:
            return element_id, tuple(self._drag_path_points)
        return element_id, tuple(QPointF(x, y) for x, y in points)

    def _path_anchor_hit(self, point: QPointF) -> tuple[str, int, tuple[QPointF, ...]] | None:
        active = self._selected_path_anchors()
        if active is None:
            return None
        element_id, anchors = active
        radius = 8.0 / max(self._zoom, 0.1)
        for index, anchor in enumerate(anchors):
            if math.hypot(point.x() - anchor.x(), point.y() - anchor.y()) <= radius:
                return element_id, index, anchors
        return None

    def _scene_rect_to_view_rect(self, rect: QRectF) -> QRectF:
        if self._scene is None:
            return rect
        scale = min(self.width() / self._scene.width, self.height() / self._scene.height) * self._zoom
        target_width = self._scene.width * scale
        target_height = self._scene.height * scale
        origin_x = (self.width() - target_width) / 2
        origin_y = (self.height() - target_height) / 2
        return QRectF(origin_x + rect.x() * scale, origin_y + rect.y() * scale,
                      rect.width() * scale, rect.height() * scale)

    @staticmethod
    def _element_bounds(transform: object) -> QRectF:
        """Return a rotated element's scene-space AABB for marquee hit-test."""
        import math
        cx = float(transform.x) + float(transform.width) / 2.0
        cy = float(transform.y) + float(transform.height) / 2.0
        half_w = float(transform.width) * float(transform.scale_x) / 2.0
        half_h = float(transform.height) * float(transform.scale_y) / 2.0
        angle = math.radians(float(transform.rotation))
        points = tuple(
            (cx + sx * half_w * math.cos(angle) - sy * half_h * math.sin(angle),
             cy + sx * half_w * math.sin(angle) + sy * half_h * math.cos(angle))
            for sx, sy in ((-1, -1), (-1, 1), (1, -1), (1, 1))
        )
        xs, ys = zip(*points)
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    @staticmethod
    def _contains_element(transform: object, point: QPointF) -> bool:
        """Hit-test the element's rendered scale and rotation, not its AABB."""
        center_x = float(transform.x) + float(transform.width) / 2.0
        center_y = float(transform.y) + float(transform.height) / 2.0
        dx = point.x() - center_x
        dy = point.y() - center_y
        angle = math.radians(-float(transform.rotation))
        local_x = (dx * math.cos(angle) - dy * math.sin(angle)) / float(transform.scale_x)
        local_y = (dx * math.sin(angle) + dy * math.cos(angle)) / float(transform.scale_y)
        return abs(local_x) <= float(transform.width) / 2.0 and abs(local_y) <= float(transform.height) / 2.0
