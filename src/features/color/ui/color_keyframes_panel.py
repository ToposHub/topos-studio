"""Resolve-style Color Keyframes editor, with an isolated UI demonstration."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import replace
from math import ceil, log10

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QContextMenuEvent, QFont, QHelpEvent, QKeyEvent, QKeySequence,
    QMouseEvent, QPaintEvent, QPainter, QPen, QPixmap, QPolygon, QWheelEvent,
)
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMenu,
    QScrollBar, QSizePolicy, QSlider, QToolButton, QToolTip, QVBoxLayout, QWidget,
)

from src.features.color.core.keyframe_tracks import DYNAMIC_WEIGHT_MAX, segment_weight

from .color_keyframes_view_state import (
    EditKind, EditRequest, KeyKind, KeyRef, Marker, Scope, Snapshot, Track, demo_snapshot, tr,
)

_ROW = 25
_RULER = 24
_RED = "#e65c51"
_MASTER = ""
_HORIZONTAL = Qt.Orientation.Horizontal
_LEFT = Qt.MouseButton.LeftButton
_MIDDLE = Qt.MouseButton.MiddleButton
_ADD_MOD = Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier


class _DissolveGraph(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.start = self.end = 1.0
        self.setMinimumSize(255, 145)

    def paintEvent(self, event: QPaintEvent | None) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#171719"))
        rect = self.rect().adjusted(15, 12, -15, -12)
        p.setPen(QColor("#444449"))
        p.drawRect(rect)
        p.setPen(QPen(QColor("#d5d5d9"), 1.5))
        points = []
        for i in range(65):
            x = i / 64
            points.append(QPoint(round(rect.left() + x * rect.width()),
                                 round(rect.bottom() - segment_weight(x, self.start, self.end) * rect.height())))
        p.drawPolyline(QPolygon(points))


class _DissolveDialog(QDialog):
    def __init__(self, marker: Marker, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("dissolve", "Dissolve Type"))
        self.setObjectName("colorKeyframesDissolve")
        layout = QVBoxLayout(self)
        self.graph = _DissolveGraph(self)
        layout.addWidget(self.graph)
        self.start = self._slider(layout, tr("start", "Start Dissolve:"), marker.start)
        self.end = self._slider(layout, tr("end", "End Dissolve:"), marker.end)
        self.start.valueChanged.connect(self._refresh)
        self.end.valueChanged.connect(self._refresh)
        self._refresh()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _slider(self, layout: QVBoxLayout, name: str, initial: float) -> QSlider:
        row = QHBoxLayout()
        label = QLabel(name)
        slider = QSlider(_HORIZONTAL)
        slider.setAccessibleName(name)
        slider.setRange(0, round(DYNAMIC_WEIGHT_MAX * 10))
        slider.setValue(round(initial * 10))
        value = QLabel(f"{slider.value() / 10:g}")
        value.setMinimumWidth(28)
        slider.valueChanged.connect(lambda amount: value.setText(f"{amount / 10:g}"))
        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(value)
        layout.addLayout(row)
        return slider

    def _refresh(self) -> None:
        self.graph.start, self.graph.end = self.start.value() / 10, self.end.value() / 10
        self.graph.update()


class ColorKeyframesPanel(QWidget):
    """Owns only interaction state. Non-demo edits/seek are intents for a future host."""

    edit_requested = pyqtSignal(object)  # EditRequest
    seek_requested = pyqtSignal(int)  # clip-local displayed frame
    # KF-02: ruler drag boundaries. The host maps presses/moves onto the
    # viewport's nonblocking scrub lane and settles the exact release frame.
    scrub_started = pyqtSignal()
    scrub_finished = pyqtSignal()
    scrub_cancelled = pyqtSignal()
    expand_requested = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None, *, demo: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("colorKeyframesPanel")
        self.setMinimumSize(295, 140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._demo = demo
        self.snapshot = Snapshot()
        self.expanded: set[str] = set()
        self.selected: set[KeyRef] = set()
        self.active_row = _MASTER
        self._demo_corrector = ""
        self.scope: Scope = "all"
        self.playhead = 0
        self._clipboard: tuple[tuple[KeyRef, Marker], ...] = ()
        self._clipboard_context = ""
        self._tracks: dict[str, Track] = {}
        self._parents: dict[str, str] = {}
        self._leaves: dict[str, tuple[str, ...]] = {}
        self._markers: dict[KeyRef, Marker] = {}
        self._aggregate: dict[str, dict[int, tuple[KeyRef, ...]]] = {}
        self._frame_index: dict[str, list[int]] = {}
        self._dynamic_frames: dict[str, set[int]] = {}
        self._rows: list[tuple[str, int]] = []
        self.setStyleSheet("""
            QWidget#colorKeyframesPanel { background: #25252a; color: #e0e0e2; }
            QWidget#colorKeyframesHeader { background: #222227; border-bottom: 1px solid #151518; }
            QDialog#colorKeyframesDissolve { background: #27272c; }
            QDialog#colorKeyframesDissolve QPushButton {
                background: #3b3b42; color: #e0e0e2; border: 1px solid #53535a;
                border-radius: 3px; padding: 4px 14px;
            }
            QDialog#colorKeyframesDissolve QPushButton:hover { background: #4c4c53; }
            QComboBox QAbstractItemView, QMenu {
                background: #2c2c32; color: #e0e0e2; border: 1px solid #47474e;
                selection-background-color: #505058;
            }
            QMenu::item { padding: 4px 18px 4px 12px; }
            QMenu::item:selected { background: #505058; }
            QMenu::item:disabled { color: #85858a; }
            QLabel { color: #e0e0e2; background: transparent; }
            QLabel#colorKeyframesDemo { color: #aaa9ad; }
            QComboBox, QToolButton { color: #e0e0e2; background: #25252a; border: 0; padding: 2px 5px; }
            QComboBox:hover, QToolButton:hover, QToolButton:checked { background: #393940; }
            QScrollBar { background: #222227; border: 0; }
            QScrollBar:vertical { width: 9px; margin: 2px 1px; }
            QScrollBar:horizontal { height: 9px; margin: 1px 2px; }
            QScrollBar::handle { background: #585960; border-radius: 3px; min-height: 18px; min-width: 18px; }
            QScrollBar::handle:hover { background: #757781; }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
            QScrollBar::add-page, QScrollBar::sub-page { background: none; }
            QSlider::groove:horizontal { height: 3px; background: #151518; }
            QSlider::handle:horizontal { width: 9px; margin: -3px 0; background: #a6a6ac; border-radius: 4px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("colorKeyframesHeader")
        header.setFixedHeight(30)
        toolbar = QHBoxLayout(header)
        toolbar.setContentsMargins(8, 0, 4, 0)
        toolbar.setSpacing(5)
        title = QLabel(tr("title", "Keyframes"))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        toolbar.addWidget(title)
        if demo:
            hint = QLabel(tr("demo", "Sample"))
            hint.setObjectName("colorKeyframesDemo")
            hint.setToolTip(tr("demo_tip", "Sample clip. Edits are temporary and do not affect the project."))
            toolbar.addWidget(hint)
        toolbar.addStretch()
        self.mode = QComboBox()
        self.mode.setAccessibleName(tr("scope", "Keyframe scope"))
        self.mode.setToolTip(self.mode.accessibleName())
        self.mode.currentIndexChanged.connect(self._scope_changed)
        toolbar.addWidget(self.mode)
        self.zoom = QSlider(_HORIZONTAL)
        self.zoom.setRange(0, 100)
        self.zoom.setFixedWidth(78)
        self.zoom.setAccessibleName(tr("zoom", "Timeline zoom"))
        self.zoom.setToolTip(self.zoom.accessibleName())
        self.zoom.valueChanged.connect(self._zoom_changed)
        toolbar.addWidget(self.zoom)
        self.expand_button = QToolButton()
        self.expand_button.setText("⇤")
        self.expand_button.setCheckable(True)
        self.expand_button.setAccessibleName(tr("expand", "Expand Keyframes"))
        self.expand_button.setToolTip(self.expand_button.accessibleName())
        self.expand_button.toggled.connect(self._expand_changed)
        toolbar.addWidget(self.expand_button)
        layout.addWidget(header)
        self.edit_notice = QLabel()
        self.edit_notice.setContentsMargins(8, 3, 8, 3)
        self.edit_notice.setWordWrap(True)
        layout.addWidget(self.edit_notice)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.timeline = _Timeline(self)
        self.vertical = QScrollBar(Qt.Orientation.Vertical)
        self.vertical.setSingleStep(_ROW)
        self.vertical.valueChanged.connect(self.timeline.invalidate)
        body.addWidget(self.timeline, 1)
        body.addWidget(self.vertical)
        layout.addLayout(body, 1)
        self.horizontal = QScrollBar(_HORIZONTAL)
        self.horizontal.valueChanged.connect(self.timeline.invalidate)
        layout.addWidget(self.horizontal)
        self.set_snapshot(demo_snapshot() if demo else Snapshot())

    def set_snapshot(self, snapshot: Snapshot) -> None:
        """Replace the projection without emitting intent. Track IDs must be unique and nonempty."""
        if self.snapshot.context != snapshot.context:
            self.selected.clear()
            self.expanded.clear()
            self.active_row = _MASTER
            self._demo_corrector = snapshot.corrector
            self.zoom.blockSignals(True)
            self.zoom.setValue(0)
            self.zoom.blockSignals(False)
            self.horizontal.setValue(0)
            self.vertical.setValue(0)
        self.snapshot = snapshot
        self.edit_notice.setText(snapshot.edit_notice)
        self.edit_notice.setVisible(bool(snapshot.edit_notice))
        self.playhead = self.clamp(snapshot.playhead)
        self.timeline.cancel_gesture()
        self._tracks.clear()
        self._parents.clear()
        self._leaves.clear()
        self._markers.clear()
        self._aggregate.clear()

        def visit(track: Track, parent: str) -> tuple[str, ...]:
            self._tracks[track.id] = track
            self._parents[track.id] = parent
            leaves = tuple(leaf for child in track.children for leaf in visit(child, track.id))
            if not track.children:
                leaves = (track.id,)
                self._markers.update({(track.id, m.frame): m for m in track.markers
                                      if 0 <= m.frame < snapshot.frame_count})
            self._leaves[track.id] = leaves
            return leaves

        self._leaves[_MASTER] = tuple(leaf for top in snapshot.tracks for leaf in visit(top, _MASTER))

        def index(track_id: str) -> dict[int, tuple[KeyRef, ...]]:
            if track_id in self._aggregate:
                return self._aggregate[track_id]
            kids = snapshot.tracks if track_id == _MASTER else self._tracks[track_id].children
            frames: dict[int, list[KeyRef]] = {}
            if kids:
                for child in kids:
                    for frame, refs in index(child.id).items():
                        frames.setdefault(frame, []).extend(refs)
            elif track_id != _MASTER:
                for marker in self._tracks[track_id].markers:
                    ref = (track_id, marker.frame)
                    if ref in self._markers:
                        frames[marker.frame] = [ref]
            result = {frame: tuple(refs) for frame, refs in frames.items()}
            self._aggregate[track_id] = result
            return result

        index(_MASTER)
        self._frame_index = {row: sorted(frames) for row, frames in self._aggregate.items()}
        self._dynamic_frames = {
            row: {frame for frame, refs in frames.items()
                  if any(self._markers[ref].kind == "dynamic" for ref in refs)}
            for row, frames in self._aggregate.items()
        }
        self.selected.intersection_update(self._markers)
        self.expanded.intersection_update(self._tracks)
        if self.active_row not in self._tracks:
            self.active_row = _MASTER
        self._configure_scopes()
        self._rebuild_rows()

    def set_playhead(self, frame: int) -> None:
        """Playback-only update for a future host; avoids rebuilding track indexes."""
        self.playhead = self.clamp(frame)
        self.timeline.update()

    def clamp(self, frame: int) -> int:
        return max(0, min(frame, self.snapshot.frame_count - 1))

    def seek(self, frame: int) -> None:
        if self.snapshot.frame_count:
            frame = self.clamp(frame)
            if frame != self.playhead:
                self.set_playhead(frame)
                self.seek_requested.emit(frame)

    def _configure_scopes(self) -> None:
        entries: list[tuple[Scope, str]] = [
            ("all", tr("all", "All")), ("color", tr("color", "Color")), ("sizing", tr("sizing", "Sizing")),
        ]
        if any(t.scope == "matte" for t in self.snapshot.tracks):
            entries.append(("matte", tr("matte", "EXT MATTE")))
        if self.scope not in [scope for scope, _ in entries]:
            self.scope = "all"
        self.mode.blockSignals(True)
        self.mode.clear()
        for scope, label in entries:
            self.mode.addItem(label, scope)
        self.mode.setCurrentIndex(self.mode.findData(self.scope))
        self.mode.blockSignals(False)

    def _scope_changed(self, index: int) -> None:
        self.scope = self.mode.itemData(index)
        self.timeline.invalidate()

    def activate_row(self, row: str) -> None:
        self.active_row = row
        if self._demo and row:
            while self._parents[row]:
                row = self._parents[row]
            if self._tracks[row].scope == "color":
                self._demo_corrector = row

    def scope_row(self) -> str | None:
        if self.scope == "color":
            return (self._demo_corrector if self._demo else self.snapshot.corrector) or None
        if self.scope != "all":
            return next((t.id for t in self.snapshot.tracks if t.scope == self.scope), None)
        return _MASTER

    def scope_targets(self, frame: int) -> tuple[KeyRef, ...]:
        if self.scope == "all":
            return self.targets(_MASTER, frame)
        if self.scope == "color":
            row = self.scope_row()
            return self.targets(row, frame) if row else ()
        return tuple(ref for t in self.snapshot.tracks if t.scope == self.scope
                     for ref in self.targets(t.id, frame))

    def _expand_changed(self, expanded: bool) -> None:
        self.expand_button.setText("⇥" if expanded else "⇤")
        name = tr("collapse", "Collapse Keyframes") if expanded else tr("expand", "Expand Keyframes")
        self.expand_button.setToolTip(name)
        self.expand_button.setAccessibleName(name)
        self.expand_requested.emit(expanded)

    def _zoom_changed(self) -> None:
        self.timeline.sync_scrollbars()
        if self.zoom.value() == 0:
            self.horizontal.setValue(0)
        else:
            self.timeline.reveal_playhead()
        self.timeline.invalidate()

    def _rebuild_rows(self) -> None:
        self._rows = [(_MASTER, 0)] if self.snapshot.frame_count else []

        def append(track: Track, depth: int) -> None:
            self._rows.append((track.id, depth))
            if track.id in self.expanded:
                for child in track.children:
                    append(child, depth + 1)

        if self.snapshot.frame_count:
            for top in self.snapshot.tracks:
                append(top, 0)
        self.timeline.sync_scrollbars()
        self.timeline.invalidate()

    def toggle_row(self, track_id: str) -> None:
        track = self._tracks.get(track_id)
        if track is None or not track.children:
            return
        if track_id in self.expanded:
            self.expanded.remove(track_id)
        else:
            self.expanded.add(track_id)
        self._rebuild_rows()

    def can_edit(self, track_id: str) -> bool:
        if self.snapshot.edit_notice:
            return False
        while track_id:
            track = self._tracks[track_id]
            if track.locked or not track.available:
                return False
            track_id = self._parents[track_id]
        return True

    def targets(self, track_id: str, frame: int) -> tuple[KeyRef, ...]:
        return tuple((leaf, frame) for leaf in self._leaves.get(track_id, ()) if self.can_edit(leaf))

    def request(self, action: EditKind, targets: tuple[KeyRef, ...], *,
                kind: KeyKind = "static", delta: int = 0, start: float = 1.0,
                end: float = 1.0, enabled: bool = False,
                sources: tuple[KeyRef, ...] = (), source_context: str = "") -> None:
        if self.snapshot.edit_notice or not self.snapshot.frame_count or not targets or any(
            track not in self._tracks or not 0 <= frame < self.snapshot.frame_count
            for track, frame in targets
        ):
            return
        edit = EditRequest(self.snapshot.context, action, targets, kind, delta, start, end,
                           enabled, sources, source_context)
        if self._demo:
            self._apply_demo(edit)
        else:
            self.edit_requested.emit(edit)
            if action == "move" and self.snapshot.context == edit.context:
                moved = {(track, frame + delta) for track, frame in targets}
                if moved.issubset(self._markers) and not (set(targets) - moved).intersection(self._markers):
                    self.selected.update(moved)
                    self.timeline.invalidate()

    def row_action(self, track_id: str, column: int) -> None:
        track = self._tracks.get(track_id)
        if track is None or not track.available or not track.row_controls:
            return
        if column == 1 and self.can_edit(self._parents[track_id]):
            self.request("lock", ((track_id, self.playhead),), enabled=not track.locked)
        elif self.can_edit(track_id):
            if column == 0:
                self.request("enable", ((track_id, self.playhead),), enabled=not track.enabled)
            elif column == 2:
                self.request("auto", ((track_id, self.playhead),), enabled=not track.auto)

    def move_allowed(self, refs: set[KeyRef], delta: int) -> bool:
        return bool(refs) and all(
            self.can_edit(track_id)
            and 0 <= frame + delta < self.snapshot.frame_count
            and ((track_id, frame + delta) not in self._markers or (track_id, frame + delta) in refs)
            for track_id, frame in refs
        )

    def copy(self) -> None:
        if self.selected:
            self._clipboard_context = self.snapshot.context
            self._clipboard = tuple((ref, self._markers[ref]) for ref in sorted(self.selected))
            # 计划 §edit_requested：复制时向宿主提交来源请求——宿主立即
            # 解析并保存不可变值/动态属性/相对帧偏移/能力签名（正式跨
            # 片段剪贴板；粘贴不再读可能已删改的源）。
            self.request("copy", tuple(ref for ref, _ in self._clipboard),
                         source_context=self._clipboard_context)

    def paste(self, frame: int, row: str) -> None:
        if not self._clipboard:
            return
        anchor = min(ref[1] for ref, _ in self._clipboard)
        source_tracks = {ref[0] for ref, _ in self._clipboard}
        remap = row in self._tracks and not self._tracks[row].children and len(source_tracks) == 1
        cross = self._clipboard_context != self.snapshot.context
        active = self.snapshot.corrector

        def _target_track(ref_track: str) -> str:
            # 跨 context：源轨道 id 属于旧片段——按轨道名重映射到当前
            # 活动 Corrector 的同名叶轨（grade/qualifier 字段名即稳定
            # 身份，与节点/片段无关）
            if not cross:
                return ref_track
            _, _, track_name = ref_track.partition("|")
            mapped = f"{active}|{track_name}" if active else ""
            return mapped if mapped in self._tracks else ref_track

        targets = tuple((row if remap else _target_track(ref[0]),
                         frame + ref[1] - anchor)
                        for ref, _ in self._clipboard)
        if all(t in self._tracks and self.can_edit(t) and 0 <= f < self.snapshot.frame_count
               and (t, f) not in self._markers for t, f in targets):
            self.request("paste", targets, sources=tuple(ref for ref, _ in self._clipboard),
                         source_context=self._clipboard_context)

    def _apply_demo(self, edit: EditRequest) -> None:
        """Apply display-only edits; there are no parameter values to evaluate."""
        markers = self._markers.copy()
        if edit.action == "move":
            moving = {ref: markers.pop(ref) for ref in edit.targets}
            for (track, frame), marker in moving.items():
                markers[track, frame + edit.delta] = replace(marker, frame=frame + edit.delta)
            self.selected = {(t, f + edit.delta) for t, f in edit.targets}
        elif edit.action == "paste":
            for (ref, marker), (track, frame) in zip(self._clipboard, edit.targets, strict=True):
                markers[track, frame] = replace(marker, frame=frame)
            self.selected = set(edit.targets)
        else:
            for ref in edit.targets:
                marker = markers.get(ref)
                if edit.action == "add" and marker is None:
                    markers[ref] = Marker(ref[1], edit.kind)
                elif edit.action == "delete":
                    markers.pop(ref, None)
                elif edit.action == "convert" and marker is not None:
                    markers[ref] = replace(marker, kind=edit.kind)
                elif edit.action == "dynamics" and marker is not None:
                    markers[ref] = replace(marker, start=edit.start, end=edit.end)
        by_track: dict[str, list[Marker]] = {}
        for (track, _), marker in markers.items():
            by_track.setdefault(track, []).append(marker)

        def rebuild(track: Track) -> Track:
            flags = {}
            if (track.id, self.playhead) in edit.targets:
                key = {"lock": "locked", "auto": "auto", "enable": "enabled"}.get(edit.action)
                if key:
                    flags[key] = edit.enabled
            if edit.action == "auto" and any(
                track.id in self._leaves.get(ref[0], ()) and track.id != ref[0]
                for ref in edit.targets
            ):
                flags["auto"] = edit.enabled
            return replace(track, **flags,
                           children=tuple(rebuild(c) for c in track.children),
                           markers=tuple(sorted(by_track.get(track.id, ()), key=lambda m: m.frame)))

        self.set_snapshot(replace(self.snapshot, playhead=self.playhead,
                                  tracks=tuple(rebuild(t) for t in self.snapshot.tracks)))

    def edit_dynamics(self, refs: tuple[KeyRef, ...]) -> None:
        if not refs:
            return
        dialog = _DissolveDialog(self._markers[refs[0]], self)
        original = self.snapshot
        if dialog.exec() == QDialog.DialogCode.Accepted and original is self.snapshot:
            refs = tuple(ref for ref in refs if ref in self._markers and self.can_edit(ref[0]))
            self.request("dynamics", refs, start=dialog.graph.start, end=dialog.graph.end)
        dialog.deleteLater()

    def jump(self, direction: int) -> None:
        frames = self._frame_index.get(self.active_row, [])
        index = (bisect_left(frames, self.playhead) - 1 if direction < 0
                 else bisect_right(frames, self.playhead))
        if 0 <= index < len(frames):
            self.seek(frames[index])
        self.timeline.reveal_playhead()


class _Timeline(QWidget):
    """A shared coordinate system keeps the row headers, ruler and keys aligned."""

    def __init__(self, panel: ColorKeyframesPanel) -> None:
        super().__init__(panel)
        self.panel = panel
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("timeline", "Color Keyframes timeline"))
        self._origin: QPoint | None = None
        self._cursor = QPoint()
        self._gesture = ""
        self._prior_selection: set[KeyRef] = set()
        self._base_selection: set[KeyRef] = set()
        self._move_refs: set[KeyRef] = set()
        self._delta = 0
        self._pan_value = 0
        self._surface: QPixmap | None = None

    def invalidate(self) -> None:
        self._surface = None
        self.update()

    @property
    def divider(self) -> int:
        return max(133, min(171, round(self.width() * 0.35)))

    @property
    def lane_width(self) -> int:
        return max(1, self.width() - self.divider - 11)

    @property
    def scale(self) -> float:
        fit = self.lane_width / max(1, self.panel.snapshot.frame_count - 1)
        return fit * (max(1.0, 22 / fit) ** (self.panel.zoom.value() / 100))

    def frame_x(self, frame: int) -> int:
        return self.divider + 5 + round(frame * self.scale) - self.panel.horizontal.value()

    def x_frame(self, x: int) -> int:
        return self.panel.clamp(round((x - self.divider - 5 + self.panel.horizontal.value()) / self.scale))

    def sync_scrollbars(self) -> None:
        panel = self.panel
        width = round(max(0, panel.snapshot.frame_count - 1) * self.scale)
        panel.horizontal.setRange(0, min(2**31 - 1, max(0, width - self.lane_width)))
        panel.horizontal.setPageStep(self.lane_width)
        panel.horizontal.setVisible(panel.horizontal.maximum() > 0)
        visible_height = max(0, self.height() - _RULER)
        panel.vertical.setRange(0, max(0, len(panel._rows) * _ROW - visible_height))
        panel.vertical.setPageStep(visible_height)

    def reveal_playhead(self) -> None:
        x = self.frame_x(self.panel.playhead)
        bar = self.panel.horizontal
        if x < self.divider + 5:
            bar.setValue(bar.value() + x - self.divider - 5)
        elif x > self.width() - 6:
            bar.setValue(bar.value() + x - self.width() + 6)

    def resizeEvent(self, event: QEvent | None) -> None:
        self.sync_scrollbars()
        self.invalidate()

    def _row_at(self, y: int) -> str | None:
        if y < _RULER:
            return None
        index = (y - _RULER + self.panel.vertical.value()) // _ROW
        return self.panel._rows[index][0] if index < len(self.panel._rows) else None

    def _visible_rows(self) -> list[tuple[str, int, int]]:
        top = self.panel.vertical.value()
        first = top // _ROW
        last = min(len(self.panel._rows), ceil((self.height() - _RULER + top) / _ROW))
        return [(self.panel._rows[i][0], self.panel._rows[i][1], _RULER + i * _ROW - top)
                for i in range(first, last)]

    def _marker_at(self, row: str, x: int) -> tuple[KeyRef, ...]:
        frames = self.panel._frame_index[row]
        at = bisect_left(frames, self.x_frame(x))
        candidates = frames[max(0, at - 1):at + 2]
        nearest = min(candidates, key=lambda f: abs(self.frame_x(f) - x), default=None)
        return (self.panel._aggregate[row][nearest]
                if nearest is not None and abs(self.frame_x(nearest) - x) <= 6 else ())

    def _frames_between(self, row: str, left: int, right: int) -> list[int]:
        frames = self.panel._frame_index[row]
        return frames[bisect_left(frames, left):bisect_right(frames, right)]

    def paintEvent(self, event: QPaintEvent | None) -> None:
        dpr = self.devicePixelRatioF()
        if self._surface is None or self._surface.devicePixelRatio() != dpr:
            surface = QPixmap(ceil(self.width() * dpr), ceil(self.height() * dpr))
            surface.setDevicePixelRatio(dpr)
            with QPainter(surface) as cache:
                self._paint_surface(cache)
            self._surface = surface
        with QPainter(self) as p:
            p.drawPixmap(0, 0, self._surface)
            if self.panel.snapshot.frame_count:
                self._paint_overlays(p)

    def _paint_surface(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor("#2c2d31"))
        p.setFont(QFont(self.font().family(), 10))
        if not self.panel.snapshot.frame_count:
            p.setPen(QColor("#9a9aa0"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("empty", "No clip selected"))
            return

        left = self.divider
        rows = self._visible_rows()
        p.save()
        p.setClipRect(0, _RULER, self.width(), max(0, self.height() - _RULER))
        p.fillRect(0, _RULER, left, self.height(), QColor("#25252a"))
        colors = {"all": "#6e737f", "color": "#3f6551", "sizing": "#3c566f", "matte": "#655271"}
        for row, depth, y in rows:
            active = row == self.panel.active_row
            p.fillRect(0, y, left, _ROW, QColor("#303035" if active else "#25252a"))
            bg = colors[self.panel.scope] if row == self.panel.scope_row() else "#33343a" if active else "#27282d"
            p.fillRect(left, y, self.width() - left, _ROW, QColor(bg))
            p.setPen(QColor("#17171b"))
            p.drawLine(0, y + _ROW - 1, self.width(), y + _ROW - 1)
            self._paint_header(p, row, depth, y)
        p.restore()

        p.fillRect(0, 0, self.width(), _RULER, QColor("#202024"))
        p.setPen(QColor("#121214"))
        p.drawLine(0, _RULER - 1, self.width(), _RULER - 1)
        p.drawLine(left - 1, 0, left - 1, self.height())
        p.save()
        p.setClipRect(left, 0, self.width() - left, self.height())
        for frame in self._ruler_frames():
            x = self.frame_x(frame)
            p.setPen(QColor("#404147"))
            p.drawLine(x, 0, x, self.height())
            p.setPen(QColor("#98989f"))
            p.drawText(x + 4, 16, self._timecode(frame))
        p.save()
        p.setClipRect(left, _RULER, self.width() - left, max(0, self.height() - _RULER))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for row, _, y in rows:
            p.setOpacity(1.0 if self.panel.can_edit(row) else 0.48)
            self._paint_markers(p, row, y + _ROW // 2)
        p.restore()

        p.restore()

    def _paint_overlays(self, p: QPainter) -> None:
        p.setFont(QFont(self.font().family(), 10))
        p.setPen(QColor("#adadb0"))
        p.drawText(QRect(0, 0, self.divider, _RULER), Qt.AlignmentFlag.AlignCenter,
                   self._timecode(self.panel.playhead))
        p.save()
        p.setClipRect(self.divider, 0, self.width() - self.divider, self.height())
        head = self.frame_x(self.panel.playhead)
        p.setPen(QColor(_RED))
        p.drawLine(head, 11, head, self.height())
        p.setBrush(QColor(_RED))
        p.drawPolygon(QPolygon([QPoint(head - 6, 2), QPoint(head + 6, 2),
                                QPoint(head + 6, 7), QPoint(head, 13), QPoint(head - 6, 7)]))
        if self._gesture == "box" and self._origin is not None:
            rect = QRect(self._origin, self._cursor).normalized()
            p.fillRect(rect, QColor(210, 213, 230, 28))
            p.setPen(QColor("#a6a8b2"))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
        p.restore()

    def _paint_header(self, p: QPainter, row: str, depth: int, y: int) -> None:
        track = self.panel._tracks.get(row)
        if track is None:
            p.setPen(QColor("#ededee"))
            p.drawText(QRect(0, y, self.divider, _ROW), Qt.AlignmentFlag.AlignCenter, tr("master", "Master"))
            return
        mid = y + _ROW // 2
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if track.row_controls:
            p.setPen(QColor("#151518"))
            color = "#ededee" if track.enabled and track.available else "#56565d"
            p.setBrush(QColor(color))
            p.drawEllipse(QPoint(12, mid), 4, 4)
            inherited_lock = not self.panel.can_edit(self.panel._parents[row])
            lock = track.locked or inherited_lock
            p.setPen(QColor("#d1d1d7" if lock else "#5b5b62"))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRect(25 + (0 if lock else 2), mid - 6, 6, 8), 0, 180 * 16)
            p.drawRoundedRect(QRect(24, mid - 1, 9, 6), 1, 1)
            p.setPen(QColor("#151518"))
            p.setBrush(QColor(_RED if track.auto else "#888990"))
            p.drawPolygon(self._diamond(45, mid, 5))
        x = self._disclosure_x(depth)
        if track.children:
            p.setPen(QColor("#a4a4a8"))
            if row in self.panel.expanded:
                points = [QPoint(x - 4, mid - 2), QPoint(x, mid + 2), QPoint(x + 4, mid - 2)]
            else:
                points = [QPoint(x - 2, mid - 4), QPoint(x + 2, mid), QPoint(x - 2, mid + 4)]
            p.drawPolyline(QPolygon(points))
        p.restore()
        p.setPen(QColor("#c6c6ca" if track.available else "#75757d"))
        name = p.fontMetrics().elidedText(track.label, Qt.TextElideMode.ElideRight, self.divider - x - 13)
        p.drawText(QRect(x + 10, y, self.divider - x - 13, _ROW),
                   Qt.AlignmentFlag.AlignVCenter, name)

    def _disclosure_x(self, depth: int) -> int:
        return min(self.divider - 29, 60 + depth * 9)

    @staticmethod
    def _diamond(x: int, y: int, radius: int) -> QPolygon:
        return QPolygon([QPoint(x, y - radius), QPoint(x + radius, y),
                         QPoint(x, y + radius), QPoint(x - radius, y)])

    def _paint_markers(self, p: QPainter, row: str, y: int) -> None:
        panel = self.panel
        lo = self.x_frame(self.divider - 6)
        hi = self.x_frame(self.width() + 6)
        aggregate = panel._aggregate[row]
        moved: dict[str, set[int]] = {}
        for track, frame in self._move_refs if self._delta else ():
            moved.setdefault(track, set()).add(frame)

        segments: set[tuple[int, int]] = set()
        for leaf in panel._leaves[row]:
            keys = panel._frame_index[leaf]
            dynamic = panel._dynamic_frames[leaf]
            moving = moved.get(leaf)
            if moving:
                keys = sorted(f + self._delta if f in moving else f for f in keys)
                dynamic = {f + self._delta if f in moving else f for f in dynamic}
            start = max(0, bisect_left(keys, lo) - 1)
            stop = bisect_right(keys, hi)
            segments.update((keys[i], keys[i + 1]) for i in range(start, min(stop, len(keys) - 1))
                            if keys[i] in dynamic)
        for left, right in sorted(segments):
            x1, x2 = self.frame_x(left), self.frame_x(right)
            if x2 - x1 > 8 and x2 >= self.divider and x1 < self.width():
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#585960"))
                p.drawPolygon(QPolygon([QPoint(x1, y), QPoint(x2, y - 4), QPoint(x2, y + 4)]))

        if moved:
            frames: dict[int, list[KeyRef]] = {}
            left, right = min(lo, lo - self._delta), max(hi, hi - self._delta)
            for frame in self._frames_between(row, left, right):
                for ref in aggregate[frame]:
                    pos = frame + self._delta if ref in self._move_refs else frame
                    if lo <= pos <= hi:
                        frames.setdefault(pos, []).append(ref)
            marks = ((f, any(panel._markers[ref].kind == "dynamic" for ref in refs),
                      bool(panel.selected.intersection(refs))) for f, refs in frames.items())
        else:
            dynamic_frames = panel._dynamic_frames[row]
            marks = ((f, f in dynamic_frames, bool(panel.selected.intersection(aggregate[f])))
                     for f in self._frames_between(row, lo, hi))
        # At fit zoom, several frames may project onto the same pixel.
        pixels: dict[int, tuple[bool, bool]] = {}
        for frame, dynamic, selected in marks:
            x = self.frame_x(frame)
            old_dynamic, old_selected = pixels.get(x, (False, False))
            pixels[x] = (dynamic or old_dynamic, selected or old_selected)
        for x, (dynamic, selected) in pixels.items():
            p.setPen(QColor("#161619"))
            p.setBrush(QColor(_RED if selected else "#e9e9ea"))
            if dynamic:
                p.drawPolygon(self._diamond(x, y, 5))
            else:
                p.drawEllipse(QPoint(x, y), 4, 4)

    def _timecode(self, frame: int) -> str:
        seconds, f = divmod(frame, max(1, self.panel.snapshot.timecode_fps))
        minutes, s = divmod(seconds, 60)
        h, m = divmod(minutes, 60)
        return f"{h:02}:{m:02}:{s:02}:{f:02}"

    def _ruler_frames(self) -> range:
        step = max(1, ceil(106 / self.scale))
        base = 10 ** int(log10(step))
        spacing = next(n * base for n in (1, 2, 5, 10) if n * base >= step)
        first = self.x_frame(self.divider) // spacing * spacing
        last = self.x_frame(self.width())
        return range(first, last + 1, spacing)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None or not self.panel.snapshot.frame_count:
            return
        point = event.position().toPoint()
        x, y = point.x(), point.y()
        row = self._row_at(y)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._prior_selection = self.panel.selected.copy()
        if event.button() == _MIDDLE and x >= self.divider:
            self._gesture, self._origin = "pan", point
            self._pan_value = self.panel.horizontal.value()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == _LEFT:
            if y < _RULER and x >= self.divider:
                self._gesture, self._origin = "seek", point
                self.panel.scrub_started.emit()
                self.panel.seek(self.x_frame(x))
            elif row is not None and x < self.divider:
                self.panel.activate_row(row)
                if x < 53:
                    self.panel.row_action(row, 0 if x < 20 else 1 if x < 37 else 2)
                else:
                    index = (y - _RULER + self.panel.vertical.value()) // _ROW
                    if abs(x - self._disclosure_x(self.panel._rows[index][1])) <= 8:
                        self.panel.toggle_row(row)
                self.invalidate()
            elif x >= self.divider and y >= _RULER:
                refs = set(self._marker_at(row, x)) if row is not None else set()
                self._origin = self._cursor = point
                extend = bool(event.modifiers() & _ADD_MOD)
                self._base_selection = self._prior_selection if extend else set()
                if row is not None:
                    self.panel.activate_row(row)
                if refs:
                    if extend:
                        self.panel.selected.symmetric_difference_update(refs)
                    elif not refs.issubset(self.panel.selected):
                        self.panel.selected = refs
                    if refs.intersection(self.panel.selected) and any(self.panel.can_edit(ref[0]) for ref in refs):
                        self._move_refs = {ref for ref in self.panel.selected if self.panel.can_edit(ref[0])}
                    self._gesture = "move"
                else:
                    self.panel.selected = self._base_selection.copy()
                    self._gesture = "box"
                self.invalidate()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._origin is None:
            return
        self._cursor = event.position().toPoint()
        x = self._cursor.x()
        if self._gesture == "seek":
            self.panel.seek(self.x_frame(x))
            event.accept()
            return
        elif self._gesture == "pan":
            self.panel.horizontal.setValue(self._pan_value + self._origin.x() - x)
        elif self._gesture == "move":
            delta = round((x - self._origin.x()) / self.scale)
            self._delta = delta if self.panel.move_allowed(self._move_refs, delta) else 0
        elif self._gesture == "box":
            rect = QRect(self._origin, self._cursor).normalized()
            lo = self.x_frame(max(self.divider, rect.left()))
            hi = self.x_frame(min(self.width(), rect.right()))
            self.panel.selected = self._base_selection | {
                ref for row, _, y in self._visible_rows() for frame in self._frames_between(row, lo, hi)
                if rect.contains(QPoint(self.frame_x(frame), y + _ROW // 2))
                for ref in self.panel._aggregate[row][frame]
            }
        self.invalidate()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None or not self._gesture:
            return
        if event.button() != (_MIDDLE if self._gesture == "pan" else _LEFT):
            return
        if self._gesture == "seek":
            self.panel.seek(self.x_frame(event.position().toPoint().x()))
        delta = 0
        if self._gesture == "move" and self._origin is not None:
            delta = round((event.position().toPoint().x() - self._origin.x()) / self.scale)
        refs = tuple(sorted(self._move_refs))
        moved = self._gesture == "move" and delta != 0 and self.panel.move_allowed(self._move_refs, delta)
        self.cancel_gesture(settle=True)
        if moved:
            self.panel.request("move", refs, delta=delta)
        event.accept()

    def cancel_gesture(self, *, settle: bool = False) -> None:
        was_seek = self._gesture == "seek"
        self._origin = None
        self._gesture = ""
        self._delta = 0
        self._move_refs.clear()
        self.unsetCursor()
        self.invalidate()
        if was_seek:
            signal = self.panel.scrub_finished if settle else self.panel.scrub_cancelled
            signal.emit()

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        if event is None:
            return
        delta = event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.panel.zoom.setValue(self.panel.zoom.value() + (5 if delta.y() > 0 else -5))
        elif abs(delta.x()) > abs(delta.y()) or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            bar = self.panel.horizontal
            bar.setValue(bar.value() - (delta.x() or delta.y()) // 2)
        else:
            bar = self.panel.vertical
            bar.setValue(bar.value() - delta.y() // 3)
        event.accept()

    def event(self, event: QEvent | None) -> bool:
        if isinstance(event, QHelpEvent) and event.type() == QEvent.Type.ToolTip:
            p = event.pos()
            row = self._row_at(p.y())
            track = self.panel._tracks.get(row)
            if track is not None and (p.x() < self.divider or track.unavailable_reason):
                if track.unavailable_reason:
                    tip = track.unavailable_reason
                elif track.row_controls and p.x() < 20:
                    tip = tr("enable", "Enable / Disable")
                elif track.row_controls and p.x() < 37:
                    tip = tr("lock", "Lock / Unlock")
                elif track.row_controls and p.x() < 53:
                    tip = tr("auto", "Auto Keyframe")
                else:
                    tip = track.label
                QToolTip.showText(event.globalPos(), tip, self)
                return True
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.ShortcutOverride:
            if self.panel.snapshot.frame_count and (
                event.key() in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight,
                                Qt.Key.Key_Delete, Qt.Key.Key_Backspace, Qt.Key.Key_Escape)
                or event.matches(QKeySequence.StandardKey.Copy)
                or event.matches(QKeySequence.StandardKey.Paste)
            ):
                event.accept()
                return True
        if event is not None and event.type() in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            self.cancel_gesture()
        return super().event(event)

    def _selected_editable(self) -> tuple[KeyRef, ...]:
        return tuple(sorted(ref for ref in self.panel.selected if self.panel.can_edit(ref[0])))

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:
        if event is None or not self.panel.snapshot.frame_count:
            return
        pos = event.pos()
        keyboard = event.reason() == QContextMenuEvent.Reason.Keyboard
        if pos.x() < self.divider and not keyboard:
            return
        panel = self.panel
        row = panel.active_row if keyboard else self._row_at(pos.y())
        frame = panel.playhead
        refs = set(self._marker_at(row, pos.x())) if not keyboard and row is not None else set()
        if refs and not refs.intersection(panel.selected):
            panel.selected = refs
        if row is not None:
            panel.activate_row(row)
            self.invalidate()
        chosen = self._selected_editable() if refs or keyboard else ()
        dynamics = tuple(ref for ref in chosen if panel._markers[ref].kind == "dynamic")
        targets = panel.targets(row, frame) if row is not None else ()
        add_targets = tuple(ref for ref in targets if ref not in panel._markers)
        menu = QMenu(self)
        original = panel.snapshot

        def apply(action: EditKind, targets: tuple[KeyRef, ...],
                  kind: KeyKind = "static", *, delta: int = 0) -> None:
            if panel.snapshot is original:
                panel.request(action, targets, kind=kind, delta=delta)

        def edit_attributes() -> None:
            if panel.snapshot is original:
                panel.edit_dynamics(dynamics)

        if row is not None:
            menu.addAction(tr("add_static", "Add Static Keyframe"),
                           lambda: apply("add", add_targets)).setEnabled(bool(add_targets))
            menu.addAction(tr("add_dynamic", "Add Dynamic Keyframe"),
                           lambda: apply("add", add_targets, "dynamic")).setEnabled(bool(add_targets))
            menu.addAction(tr("to_static", "Change to Static Keyframe"),
                           lambda: apply("convert", chosen)).setEnabled(bool(dynamics))
            menu.addAction(tr("to_dynamic", "Change to Dynamic Keyframe"),
                           lambda: apply("convert", chosen, "dynamic")).setEnabled(len(dynamics) < len(chosen))
            menu.addAction(tr("delete", "Delete Selected Keyframe"),
                           lambda: apply("delete", chosen)).setEnabled(bool(chosen))
            menu.addAction(tr("attributes", "Change Dynamic Attributes…"),
                           edit_attributes).setEnabled(bool(dynamics))
            menu.addSeparator()
            selection = set(chosen)
            menu.addAction(tr("move_left", "Move Selected Keyframes Left"),
                           lambda: apply("move", chosen, delta=-1)).setEnabled(panel.move_allowed(selection, -1))
            menu.addAction(tr("move_right", "Move Selected Keyframes Right"),
                           lambda: apply("move", chosen, delta=1)).setEnabled(panel.move_allowed(selection, 1))
            if row == _MASTER:
                keys = tuple(sorted(panel._markers))
                menu.addAction(tr("delete_visible", "Delete All Visible Keyframes"),
                               lambda: apply("delete", keys)).setEnabled(
                                   bool(keys) and all(panel.can_edit(track) for track, _ in keys))
            menu.addSeparator()
        menu.addAction(tr("max_zoom", "Maximum Zoom"), lambda: panel.zoom.setValue(100))
        menu.addAction(tr("reset_zoom", "Reset Zoom"), lambda: panel.zoom.setValue(0))
        menu.exec(event.globalPos())
        menu.deleteLater()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is None:
            return
        panel = self.panel
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key.Key_Escape:
            if self._gesture:
                panel.selected = self._prior_selection.copy()
                self.cancel_gesture()
            else:
                panel.selected.clear()
        elif not panel.snapshot.frame_count:
            super().keyPressEvent(event)
            return
        elif key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight):
            right = key == Qt.Key.Key_BracketRight
            if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                targets = tuple(ref for ref in panel.scope_targets(panel.playhead) if ref not in panel._markers)
                panel.request("add", targets, kind="static" if right else "dynamic")
            elif right and mods & Qt.KeyboardModifier.AltModifier:
                panel.request("delete", tuple(ref for ref in panel.targets(_MASTER, panel.playhead)
                                              if ref in panel._markers))
            else:
                panel.jump(1 if right else -1)
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            panel.request("delete", self._selected_editable())
        elif event.matches(QKeySequence.StandardKey.Copy):
            panel.copy()
        elif event.matches(QKeySequence.StandardKey.Paste):
            panel.paste(panel.playhead, panel.active_row)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            panel.seek(panel.playhead + (1 if key == Qt.Key.Key_Right else -1))
            self.reveal_playhead()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            visible_ids = [row for row, _ in panel._rows]
            index = visible_ids.index(panel.active_row) if panel.active_row in visible_ids else 0
            index = max(0, min(len(visible_ids) - 1, index + (1 if key == Qt.Key.Key_Down else -1)))
            panel.activate_row(visible_ids[index])
            top = index * _ROW
            bar = panel.vertical
            bar.setValue(min(top, max(bar.value(), top + _ROW - bar.pageStep())))
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            panel.toggle_row(panel.active_row)
        elif key == Qt.Key.Key_Space:
            panel.row_action(panel.active_row, 2)
        else:
            super().keyPressEvent(event)
            return
        self.invalidate()
        event.accept()
