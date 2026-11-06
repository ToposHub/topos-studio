"""Resolve-style Color Nodes panel, intentionally detached from the backend.

This shell mounts in the existing layer dock. Each scope and compound has an
independent UI draft. The host-facing signals are integration seams, not slots
which mutate ImageEngine or the project.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QPointF, QSize, Qt, QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QMenu, QSizePolicy,
    QRadioButton, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C

from .node_editor_state import GraphDraft, NodeEditorState, NodeKind
from .node_editor_view import ColorNodeView, tr


def _tool_icon(kind: str, active: bool = True) -> QIcon:
    """Draw small original vector controls at the reference's optical size."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2.0, 2.0)
    foreground = C.TEXT_PRIMARY if active else C.TEXT_HINT
    painter.setPen(QPen(QColor(foreground), 1.0))
    if kind == "pointer":
        painter.setBrush(QColor(foreground))
        painter.drawPolygon(QPolygonF([QPointF(3, 1), QPointF(3, 13), QPointF(6, 10), QPointF(8, 15),
                                      QPointF(10, 14), QPointF(8, 9), QPointF(12, 9)]))
    elif kind == "hand":
        path = QPainterPath(QPointF(5, 14))
        path.lineTo(1, 7)
        path.cubicTo(1, 5, 3, 5, 4, 8)
        path.lineTo(4, 3)
        path.cubicTo(4, 1, 6, 1, 6, 3)
        path.lineTo(6, 7)
        path.lineTo(7, 1)
        path.cubicTo(8, 0, 9, 1, 9, 2)
        path.lineTo(9, 7)
        path.lineTo(10, 2)
        path.cubicTo(11, 1, 12, 2, 12, 3)
        path.lineTo(12, 8)
        path.cubicTo(15, 3, 16, 5, 14, 10)
        path.lineTo(12, 15)
        path.closeSubpath()
        painter.setBrush(QColor(foreground))
        painter.drawPath(path)
    elif kind == "more":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.TEXT_LABEL))
        for x in (3.0, 8.0, 13.0):
            painter.drawEllipse(QPointF(x, 8.0), 1.25, 1.25)
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


class ColorNodePanel(QWidget):
    """Complete local interaction surface; backend connections are deferred."""

    edit_requested = pyqtSignal(str, object)
    active_node_changed = pyqtSignal(str)
    scope_changed = pyqtSignal(str)
    preview_requested = pyqtSignal(str)

    SCOPES = {
        "group_pre_clip": ("group_pre_clip", "Group Pre-Clip"),
        "clip": ("clip", "Clip"),
        "group_post_clip": ("group_post_clip", "Group Post-Clip"),
        "timeline": ("timeline", "Timeline"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ColorNodePanel")
        self.setAccessibleName(tr("nodes", "Nodes"))
        self.setMinimumSize(280, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._scope = "clip"
        self._group_available = False
        self._scope_states: dict[str, NodeEditorState] = {}
        self._parents: list[tuple[NodeEditorState, str]] = []
        self._edited_children: set[NodeEditorState] = set()
        self._syncing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.toolbar = QWidget(self)
        self.toolbar.setObjectName("ColorNodeToolbar")
        self.toolbar.setFixedHeight(30)
        self.toolbar.setStyleSheet(f"""
            QWidget#ColorNodeToolbar {{ background: {C.BG_PANEL};
                border-top: 1px solid {C.PANEL_DIVIDER}; border-bottom: 1px solid {C.PANEL_DIVIDER}; }}
            QToolButton {{ background: transparent; border: none; border-radius: 0;
                padding: 0; margin: 0; color: {C.TEXT_PRIMARY}; }}
            QToolButton:hover {{ background: {C.BG_HOVER}; }}
            QToolButton:checked {{ background: transparent; }}
        """)
        bar = QGridLayout(self.toolbar)
        bar.setContentsMargins(0, 1, 0, 1)
        bar.setSpacing(0)
        bar.setColumnStretch(0, 1)
        bar.setColumnStretch(2, 1)
        left = QWidget(self.toolbar)
        left_bar = QHBoxLayout(left)
        left_bar.setContentsMargins(4, 0, 0, 0)
        left_bar.setSpacing(0)
        dots = QWidget(self.toolbar)
        dot_bar = QHBoxLayout(dots)
        dot_bar.setContentsMargins(0, 0, 0, 0)
        dot_bar.setSpacing(2)
        right = QWidget(self.toolbar)
        right_bar = QHBoxLayout(right)
        right_bar.setContentsMargins(0, 0, 0, 0)
        right_bar.setSpacing(0)
        bar.addWidget(left, 0, 0, Qt.AlignmentFlag.AlignLeft)
        bar.addWidget(dots, 0, 1)
        bar.addWidget(right, 0, 2, Qt.AlignmentFlag.AlignRight)
        self.pointer_button = self._tool("pointer", tr("pointer_mode", "Pointer Mode"), "NodePointer")
        self.hand_button = self._tool("hand", tr("hand_mode", "Hand Mode"), "NodeHand")
        self.pointer_button.setCheckable(True)
        self.hand_button.setCheckable(True)
        self.pointer_button.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.pointer_button)
        self.mode_group.addButton(self.hand_button)
        left_bar.addWidget(self.pointer_button)
        left_bar.addWidget(self.hand_button)
        self.tool_separator = self._separator("NodeToolSeparator")
        left_bar.addWidget(self.tool_separator)
        self.clip_radio = QRadioButton(self)
        self.timeline_radio = QRadioButton(self)
        self.scope_group = QButtonGroup(self)
        self.scope_group.addButton(self.clip_radio)
        self.scope_group.addButton(self.timeline_radio)
        # Resolve uses two small scope dots, not enable/disable switches.
        for radio in (self.clip_radio, self.timeline_radio):
            radio.setFixedSize(17, 28)
            radio.setStyleSheet(f"""
                QRadioButton {{ background: transparent; spacing: 0; }}
                QRadioButton::indicator {{ width: 8px; height: 8px;
                    border: none; border-radius: 4px; background: {C.BG_HOVER}; }}
                QRadioButton::indicator:checked {{ background: {C.TEXT_PRIMARY}; }}
            """)
        self.clip_radio.setObjectName("NodeClipMode")
        self.timeline_radio.setObjectName("NodeTimelineMode")
        self.clip_radio.setToolTip(tr("clip", "Clip"))
        self.timeline_radio.setToolTip(tr("timeline", "Timeline"))
        self.clip_radio.setAccessibleName(tr("clip", "Clip"))
        self.timeline_radio.setAccessibleName(tr("timeline", "Timeline"))
        self.clip_radio.setChecked(True)
        dot_bar.addWidget(self.clip_radio)
        dot_bar.addWidget(self.timeline_radio)
        self.scope_button = QToolButton(self)
        self.scope_button.setObjectName("NodeScope")
        self.scope_button.setFixedHeight(28)
        self.scope_button.setMinimumWidth(56)
        self.scope_button.setStyleSheet(f"""
            QToolButton {{ padding: 0 14px 0 6px; font-weight: bold; }}
            QToolButton::menu-indicator {{ image: none; width: 0; }}
        """)
        self.scope_button.setText(tr("clip", "Clip") + " ⌄")
        self.scope_button.setToolTip(tr("grade_scope", "Node Editor Mode"))
        self.scope_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.scope_menu = QMenu(self.scope_button)
        self.scope_menu.aboutToShow.connect(self._build_scope_menu)
        self.scope_button.setMenu(self.scope_menu)
        right_bar.addWidget(self.scope_button)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setObjectName("NodeZoom")
        self.zoom_slider.setAccessibleName(tr("node_size", "Node Editor Zoom"))
        self.zoom_slider.setRange(round(ColorNodeView.MIN_ZOOM * 100), round(ColorNodeView.MAX_ZOOM * 100))
        self.zoom_slider.setValue(round(ColorNodeView.ORIGINAL_ZOOM * 100))
        self.zoom_slider.setFixedSize(118, 28)
        self.zoom_slider.setStyleSheet(f"""
            QSlider {{ background: transparent; margin: 0 8px 0 0; }}
            QSlider::groove:horizontal {{ height: 2px; border: none;
                background: {C.BG_DARK}; border-radius: 0; margin: 0; }}
            QSlider::sub-page:horizontal, QSlider::add-page:horizontal {{
                background: {C.BG_DARK}; border: none; height: 2px; border-radius: 0; }}
            QSlider::handle:horizontal {{ width: 9px; height: 9px;
                margin: -4px 0; border: none; border-radius: 4px; background: {C.TEXT_SECONDARY}; }}
        """)
        self.zoom_slider.setToolTip(tr("node_size", "Node Editor Zoom"))
        right_bar.addWidget(self.zoom_slider)
        self.more_button = self._tool("more", tr("node_options", "Node Editor Options"), "NodeOptions")
        self.menu_separator = self._separator("NodeMenuSeparator")
        right_bar.addWidget(self.menu_separator)
        self.more_button.setFixedWidth(35)
        right_bar.addWidget(self.more_button)
        layout.addWidget(self.toolbar)
        self.breadcrumb = QWidget(self)
        self.breadcrumb.setObjectName("ColorNodeBreadcrumb")
        breadcrumb_layout = QHBoxLayout(self.breadcrumb)
        breadcrumb_layout.setContentsMargins(6, 0, 6, 0)
        self.back_button = QToolButton(self.breadcrumb)
        self.back_button.setText("‹ " + tr("back", "Back"))
        self.back_button.setObjectName("NodeCompoundBack")
        self.breadcrumb_label = QLabel(self.breadcrumb)
        breadcrumb_layout.addWidget(self.back_button)
        breadcrumb_layout.addWidget(self.breadcrumb_label)
        breadcrumb_layout.addStretch()
        self.breadcrumb.hide()
        layout.addWidget(self.breadcrumb)
        self.view = ColorNodeView(self)
        layout.addWidget(self.view, 1)
        self._scope_states[self._scope] = self.view.state
        self.pointer_button.clicked.connect(lambda: self.view.set_hand_mode(False))
        self.hand_button.clicked.connect(lambda: self.view.set_hand_mode(True))
        self.view.hand_mode_changed.connect(
            lambda hand: (self.hand_button if hand else self.pointer_button).setChecked(True))
        self.clip_radio.toggled.connect(lambda checked: self._scope_toggled("clip", checked))
        self.timeline_radio.toggled.connect(lambda checked: self._scope_toggled("timeline", checked))
        self.zoom_slider.valueChanged.connect(lambda value: self.view.set_zoom(value / 100.0))
        self.view.zoom_changed.connect(self._zoom_changed)
        self.view.edit_requested.connect(self._forward_edit)
        self.view.active_node_changed.connect(self.active_node_changed)
        self.view.preview_requested.connect(self.preview_requested)
        self.view.compound_open_requested.connect(self.open_compound)
        self.view.parent_requested.connect(self.close_compound)
        self.back_button.clicked.connect(self.close_compound)
        self.more_button.clicked.connect(self._show_options)

    def _tool(self, icon: str, tooltip: str, name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(name)
        button.setStyleSheet("QToolButton { padding: 0; margin: 0; border: none; background: transparent; }")
        tool_icon = _tool_icon(icon)
        if icon in {"pointer", "hand"}:
            tool_icon = QIcon()
            for state, active in ((QIcon.State.Off, False), (QIcon.State.On, True)):
                tool_icon.addPixmap(_tool_icon(icon, active).pixmap(QSize(16, 16)), QIcon.Mode.Normal, state)
        button.setIcon(tool_icon)
        button.setIconSize(QSize(16, 16))
        button.setFixedSize(30, 28)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    def _separator(self, name: str) -> QWidget:
        separator = QWidget(self.toolbar)
        separator.setObjectName(name)
        separator.setFixedSize(1, 28)
        separator.setStyleSheet(f"background: {C.PANEL_DIVIDER};")
        return separator

    @property
    def scope(self) -> str:
        return self._scope

    def set_group_available(self, available: bool) -> None:
        """Host supplies membership; no group is invented by the UI."""
        self._group_available = bool(available)
        if not available and self._scope.startswith("group_"):
            self.set_scope("clip")

    def _build_scope_menu(self) -> None:
        self.scope_menu.clear()
        for scope, (key, title) in self.SCOPES.items():
            action = self.scope_menu.addAction(tr(key, title))
            action.setObjectName("node_scope_" + scope)
            action.setCheckable(True)
            action.setChecked(self._scope == scope)
            action.setEnabled(not scope.startswith("group_") or self._group_available)
            action.triggered.connect(lambda _checked=False, s=scope: self.set_scope(s))

    def _scope_toggled(self, scope: str, checked: bool) -> None:
        """Select scope only on activation of an exclusive reference-style dot."""
        if checked:
            self.set_scope(scope)

    def set_scope(self, scope: str) -> None:
        if scope not in self.SCOPES or (scope.startswith("group_") and not self._group_available):
            return
        if self._scope == scope:
            return
        self.view.commit_label()
        while self._parents:
            if not self.close_compound():
                return
        self._scope_states[self._scope] = self.view.state
        self._scope = scope
        self.view.state = self._scope_states.setdefault(scope, NodeEditorState())
        self.view.rebuild()
        self.view.fit_graph()
        self._syncing = True
        try:
            self.clip_radio.setChecked(scope != "timeline")
            self.timeline_radio.setChecked(scope == "timeline")
            key, title = self.SCOPES[scope]
            self.scope_button.setText(tr(key, title) + " ⌄")
        finally:
            self._syncing = False
        self.scope_changed.emit(scope)
        self.active_node_changed.emit(self.view.state.graph.active_id)

    def load_graph(self, graph: GraphDraft, *, scope: str = "clip", fit: bool = True) -> None:
        """Hydrate a scope without emitting edits or disturbing an ongoing view."""
        if scope not in self.SCOPES:
            raise ValueError("unknown node scope")
        state = NodeEditorState(graph)
        self._scope_states[scope] = state
        if scope == self._scope:
            self._parents.clear()
            self.breadcrumb.hide()
            self.view.state = state
            self.view.rebuild()
            if fit:
                self.view.fit_graph()

    def snapshot(self, scope: str | None = None) -> GraphDraft:
        state = self.view.state if scope is None else self._scope_states[scope]
        return state.snapshot()

    def _zoom_changed(self, value: float) -> None:
        blocker = QSignalBlocker(self.zoom_slider)
        self.zoom_slider.setValue(round(value * 100))
        del blocker

    def _forward_edit(self, action: str, payload: object) -> None:
        if self._parents:
            self._edited_children.add(self.view.state)
        self.edit_requested.emit(action, {"scope": self._scope,
                                          "compound_path": [node_id for _, node_id in self._parents],
                                          "payload": payload})

    def open_compound(self, node_id: str, *, fit: bool = True) -> None:
        self.view.commit_label()
        node = self.view.state.graph.nodes.get(node_id)
        if node is None or node.kind != NodeKind.COMPOUND or node.children is None:
            return
        self._parents.append((self.view.state, node_id))
        self.view.state = NodeEditorState(node.children)
        self.view.rebuild()
        if fit:
            self.view.fit_graph()
        self.breadcrumb_label.setText(" / ".join(parent.graph.nodes[nid].label or tr("compound_node", "Compound Node")
                                                 for parent, nid in self._parents))
        self.breadcrumb.show()
        self.active_node_changed.emit(self.view.state.graph.active_id)

    def close_compound(self) -> bool:
        if not self._parents:
            return False
        self.view.commit_label()
        child = self.view.state.snapshot()
        parent, node_id = self._parents[-1]
        if self.view.state in self._edited_children and not parent.replace_compound_children(node_id, child):
            # Keep the child open if a boundary in use would be orphaned.
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, tr("compound_node", "Compound Node"),
                                    tr("compound_boundary", "Restore connected compound input and output nodes before closing."))
            return False
        self._edited_children.discard(self.view.state)
        self._parents.pop()
        self.view.state = parent
        self.view.rebuild()
        self.view.fit_graph()
        self.breadcrumb.setVisible(bool(self._parents))
        if self._parents:
            self.breadcrumb_label.setText(" / ".join(p.graph.nodes[nid].label or tr("compound_node", "Compound Node") for p, nid in self._parents))
        self._forward_edit("close_compound", {"node_id": node_id, "draft": parent.snapshot()})
        self.active_node_changed.emit(self.view.state.graph.active_id)
        return True

    def build_options_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setObjectName("ColorNodeOptionsMenu")
        add = self.view._action
        add(menu, "cleanup", "Cleanup Node Graph", self.view.cleanup_graph)
        add(menu, "fit", "Zoom to Window", self.view.fit_graph)
        add(menu, "zoom_reset", "Original Size", lambda: self.view.set_zoom(self.view.ORIGINAL_ZOOM))
        menu.addSeparator()
        add(menu, "show_grid", "Show Grid", self._toggle_grid, checked=self.view.show_grid)
        add(menu, "show_thumbnails", "Show Thumbnails", self._toggle_thumbnails, checked=self.view.show_thumbnails)
        menu.addSeparator()
        add(menu, "undo", "Undo", lambda: self.view.apply_edit("undo", self.view.state.undo), enabled=(self.view.project_history_available(False) if self.view.project_history_available else self.view.state.can_undo))
        add(menu, "redo", "Redo", lambda: self.view.apply_edit("redo", self.view.state.redo), enabled=(self.view.project_history_available(True) if self.view.project_history_available else self.view.state.can_redo))
        menu.addSeparator()
        add(menu, "reset_keep_nodes", "Reset Grades and Keep Nodes", lambda: self.view.apply_edit("reset_grades", lambda: self.view.state.reset_grades(set(self.view.state.graph.nodes))))
        add(menu, "reset_all", "Reset All Grades and Nodes", self.view.reset_graph)
        return menu

    def _toggle_grid(self) -> None:
        self.view.show_grid = not self.view.show_grid
        self.view.viewport().update()

    def _toggle_thumbnails(self) -> None:
        self.view.show_thumbnails = not self.view.show_thumbnails
        self.view.rebuild()

    def _show_options(self) -> None:
        menu = self.build_options_menu()
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))
