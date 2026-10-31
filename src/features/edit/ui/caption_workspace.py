"""SUB-P2-03: Caption Workspace — virtualized cue table + editing tools.

A dockable panel that gives subtitle editors a spreadsheet-style view of every
cue on every subtitle track, with:

* a virtualized :class:`CaptionCueModel` (``QAbstractTableModel``) — the table
  asks the model for rows on demand, so 1000+ cues scroll smoothly without
  pre-building 1000 widgets.
* editable columns: index, in (timecode), out (timecode), duration, text,
  speaker, status. Timecode is displayed read-only; editing happens via the
  properties panel / double-click (keeps one editing surface, matches the
  project's single-source-of-truth convention).
* find/replace across the selected track or all cues.
* split / merge / batch-style commands, emitted as ``command_requested`` for
  the controller to submit through the unified command path.

The panel is a *view* — it never mutates ``edit_page._current_timeline`` or the
command stack directly. Every change is emitted as a signal that the
:class:`EditController` wires to ``_execute_subtitle_command`` /
``_execute_batch_macro``. Selection syncs bidirectionally with the timeline
(``cue_selected`` ↔ controller's ``on_subtitle_clip_clicked``).

Headless-testable: the model and the pure-logic helpers (find/replace, split/
merge command builders) are exercised without a display; the QWidget parts are
only instantiated when a QApplication exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.core.captions.commands import (
    MergeSubtitleClipsCommand,
    SplitSubtitleClipCommand,
)
from src.features.edit.core.timeline_v2 import SubtitleClip, TimelineSequence
from src.shared.i18n.i18n import I18N


# ──────────────────────────────────────────────────────────────────────────
# Column definitions
# ──────────────────────────────────────────────────────────────────────────

class CueColumn:
    """Column indices in the cue table (stable contract for the model)."""
    INDEX = 0       # 1-based row number
    IN_TC = 1       # timeline_start as timecode (read-only display)
    OUT_TC = 2      # timeline_end as timecode
    DURATION = 3    # timeline_duration as timecode
    TEXT = 4        # cue text (editable via double-click → properties)
    SPEAKER = 5     # caption_metadata.speaker
    STATUS = 6      # caption_metadata.status

COLUMN_COUNT = 7
COLUMN_HEADERS = ("#", "In", "Out", "Duration", "Text", "Speaker", "Status")


# ──────────────────────────────────────────────────────────────────────────
# Timecode formatting (shared with edit_page, kept simple / NDF for now)
# ──────────────────────────────────────────────────────────────────────────

def frame_to_timecode(frame: int, fps: float) -> str:
    """Format a frame index as ``HH:MM:SS:FF`` (non-drop-frame).

    Mirrors :meth:`EditPage._frame_to_timecode` so the workspace and the
    program viewer show the same string. Drop-frame support arrives with
    SUB-P2-09 (TimebaseConverter); until then NDF is correct for 24/25/30.
    """
    if fps <= 0:
        fps = 24.0
    # FE-B13：统一走 interchange.frames_to_timecode——29.97 下 frame29
    # 显示 :28（NDF 应 :29）、1799 显示 00:01:00:00（应 00:00:59:29）
    # 的浮点漂移来自旧的整除/取模。
    from src.features.edit.core.interchange import frames_to_timecode as _smpte

    return _smpte(int(frame), float(fps))


# ──────────────────────────────────────────────────────────────────────────
# CaptionCueModel — virtualized table model
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CueRow:
    """One flattened cue row: the cue + its owning track id."""
    clip: SubtitleClip
    track_id: str
    row_index: int  # 1-based display index


class CaptionCueModel(QAbstractTableModel):
    """Table model exposing every cue across all subtitle tracks.

    The model holds a flat list of :class:`CueRow` (cue + owner track), built
    by :meth:`reload_from_sequence`. Because Qt's table view only calls
    :meth:`data` for visible rows, this is effectively virtualized — 1000 cues
    cost no more widgets than fit on screen.

    The model is read-only at the Qt level (no ``setData``); edits go through
    command-emitted signals so the command stack stays the single mutation
    path. This avoids the dual-write bug where a table cell edit and a command
    both try to update the same cue.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._rows: List[CueRow] = []
        self._fps: float = 24.0

    # ── Qt model API ──

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else COLUMN_COUNT

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < COLUMN_COUNT:
            return COLUMN_HEADERS[section]
        if orientation == Qt.Orientation.Vertical:
            return None  # row numbers come from the INDEX column
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.EditRole:
            row = self._rows[index.row()]
            col = index.column()
            clip = row.clip
            if col == CueColumn.INDEX:
                return str(row.row_index)
            if col == CueColumn.IN_TC:
                return frame_to_timecode(clip.timeline_start, self._fps)
            if col == CueColumn.OUT_TC:
                return frame_to_timecode(clip.timeline_end, self._fps)
            if col == CueColumn.DURATION:
                return frame_to_timecode(clip.timeline_duration, self._fps)
            if col == CueColumn.TEXT:
                return clip.text
            if col == CueColumn.SPEAKER:
                return clip.caption_metadata.speaker
            if col == CueColumn.STATUS:
                return clip.caption_metadata.status
        if role == Qt.ItemDataRole.UserRole:
            # Carry the clip_id + track_id so the view can request selection
            # without re-looking-up by row.
            row = self._rows[index.row()]
            return (row.clip.clip_id, row.track_id)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            col = index.column()
            if col in (CueColumn.INDEX, CueColumn.IN_TC, CueColumn.OUT_TC,
                       CueColumn.DURATION):
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        # Text + speaker are nominally editable-looking but we route edits via
        # double-click signals, not inline setData, to keep the command path.
        return base

    # ── Data loading ──

    def reload_from_sequence(self, sequence: Optional[TimelineSequence],
                             fps: float = 24.0) -> None:
        """Rebuild the row list from a timeline sequence.

        Call after any ``timeline_changed`` so the table reflects the current
        state. Cues are ordered by track index then timeline_start. Resets the
        model (emits ``beginResetModel``/``endResetModel``) so attached views
        refresh.
        """
        self.beginResetModel()
        self._fps = fps
        self._rows = []
        if sequence is None:
            self.endResetModel()
            return
        idx = 0
        for track in sequence.subtitle_tracks:
            for clip in sorted(track.subtitle_clips, key=lambda c: c.timeline_start):
                idx += 1
                self._rows.append(CueRow(clip=clip, track_id=track.track_id,
                                         row_index=idx))
        self.endResetModel()

    # ── Accessors ──

    def row_count(self) -> int:
        return len(self._rows)

    def row_at(self, row: int) -> Optional[CueRow]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def find_row_by_clip_id(self, clip_id: str) -> int:
        """Return the model row index for ``clip_id``, or -1 if absent."""
        for i, r in enumerate(self._rows):
            if r.clip.clip_id == clip_id:
                return i
        return -1

    def all_clips(self) -> List[SubtitleClip]:
        return [r.clip for r in self._rows]


# ──────────────────────────────────────────────────────────────────────────
# Find / Replace (pure logic, headless-testable)
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FindMatch:
    """A single find/replace match location."""
    clip_id: str
    track_id: str
    text_occurrence_index: int  # 0-based, which match within the cue's text
    start: int  # char offset into cue.text
    end: int    # exclusive end offset


def find_in_clips(clips: Sequence[SubtitleClip],
                  needle: str,
                  case_sensitive: bool = False,
                  track_id_filter: Optional[str] = None) -> List[FindMatch]:
    """Find all occurrences of ``needle`` across the given clips.

    Args:
        clips: cues to search (typically ``model.all_clips()`` or a track slice).
        needle: substring to find. Empty needle → no matches (avoids matching
            everywhere).
        case_sensitive: default False (case-insensitive search).
        track_id_filter: if set, only search cues on this track.

    Returns:
        List of :class:`FindMatch`, one per occurrence, in clip-then-offset
        order. Multi-occurrence within one cue produces multiple matches with
        incrementing ``text_occurrence_index``.
    """
    if not needle:
        return []
    matches: list[FindMatch] = []
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(needle), flags)
    for clip in clips:
        if track_id_filter is not None and clip.track_id != track_id_filter:
            continue
        text = clip.text or ""
        for occ, found in enumerate(pattern.finditer(text)):
            matches.append(FindMatch(
                clip_id=clip.clip_id,
                track_id=clip.track_id,
                text_occurrence_index=occ,
                start=found.start(),
                end=found.end(),
            ))
    return matches


def build_replace_text(original: str, match: FindMatch, replacement: str) -> str:
    """Return ``original`` with the matched span replaced by ``replacement``.

    Pure helper — does not touch the model or commands. The caller wraps the
    result in an :class:`UpdateSubtitleClipCommand` (text field) and emits it.
    """
    return original[:match.start] + replacement + original[match.end:]


def build_replace_all_text(
    original: str,
    matches: Sequence[FindMatch],
    replacement: str,
) -> str:
    """Apply original-offset matches safely, regardless of length changes.

    ``find_in_clips`` records offsets against the original cue text. Replacing
    from the end keeps every earlier offset valid when replacement text is
    longer or shorter than the search term.
    """
    result = original
    for match in sorted(matches, key=lambda item: item.start, reverse=True):
        result = build_replace_text(result, match, replacement)
    return result


# ──────────────────────────────────────────────────────────────────────────
# CaptionWorkspacePanel — the dockable widget
# ──────────────────────────────────────────────────────────────────────────

class CaptionWorkspacePanel(QWidget):
    """Dockable caption-editing workspace (SUB-P2-03).

    Signals:
        cue_selected(clip_id: str, track_id: str):
            Emitted when the user clicks a row. The controller wires this to
            ``on_subtitle_clip_clicked`` to sync timeline + viewer + properties.
        command_requested(command):
            Emitted for every mutating action (metadata edit, split, merge,
            batch style). The controller submits it via
            ``_execute_subtitle_command`` / ``_execute_batch_macro``.
        find_requested(needle, replacement, scope):
            Emitted when the user invokes find/replace-all. The controller
            builds the batch UpdateSubtitleClipCommand list and submits.

    The panel owns a :class:`CaptionCueModel`; call :meth:`set_sequence` on
    timeline changes to keep it in sync.
    """

    cue_selected = pyqtSignal(str, str)          # clip_id, track_id
    command_requested = pyqtSignal(object)       # an EditCommand
    find_requested = pyqtSignal(str, str, str)   # needle, replacement, scope
    # U05（第六轮审计）：Find 命中是用户主动定位动作——宿主应把播放头/画面
    # seek 到命中 cue 开始；普通行选择仍只做选择（是否跟随由宿主决定）。
    find_hit_selected = pyqtSignal(str, str)     # clip_id, track_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("captionWorkspacePanel")
        self._model = CaptionCueModel(self)
        self._current_sequence: Optional[TimelineSequence] = None
        self._current_fps: float = 24.0
        self._building_ui = False
        self._build_ui()

    # ── UI construction ──

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Toolbar: find / replace / split / merge
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self._find_edit = QLineEdit()
        self._find_edit.setPlaceholderText(I18N.tr("caption.find_placeholder", "Find…"))
        self._replace_edit = QLineEdit()
        self._replace_edit.setPlaceholderText(I18N.tr("caption.replace_placeholder", "Replace…"))
        self._find_btn = QPushButton(I18N.tr("caption.find", "Find"))
        self._replace_btn = QPushButton(I18N.tr("caption.replace_all", "Replace All"))
        self._split_btn = QPushButton(I18N.tr("caption.split", "Split"))
        self._merge_btn = QPushButton(I18N.tr("caption.merge", "Merge"))
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888;")

        toolbar.addWidget(QLabel(I18N.tr("script.find", "Find:")))
        toolbar.addWidget(self._find_edit, 1)
        toolbar.addWidget(QLabel(I18N.tr("caption.replace_label", "Repl:")))
        toolbar.addWidget(self._replace_edit, 1)
        toolbar.addWidget(self._find_btn)
        toolbar.addWidget(self._replace_btn)
        toolbar.addSpacing(12)
        toolbar.addWidget(self._split_btn)
        toolbar.addWidget(self._merge_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # The table
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            CueColumn.TEXT, QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        layout.addWidget(self._table, 1)

        layout.addWidget(self._status_label)

        # Wire signals
        self._find_btn.clicked.connect(self._on_find)
        self._replace_btn.clicked.connect(self._on_replace_all)
        self._split_btn.clicked.connect(self._on_split_selected)
        self._merge_btn.clicked.connect(self._on_merge_selected)
        sel_model = self._table.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(self._on_selection_changed)

    # ── Public API (called by controller) ──

    def set_sequence(self, sequence: Optional[TimelineSequence],
                     fps: float = 24.0) -> None:
        """Reload the table from a (possibly new) timeline sequence.

        Preserves the current selection by clip_id across the reload when
        possible (so a command that changes one cue doesn't lose focus).
        """
        self._current_sequence = sequence
        self._current_fps = fps
        selected_ids = self._selected_clip_ids()
        self._model.reload_from_sequence(sequence, fps)
        # Restore selection.
        if selected_ids:
            self._select_clip_ids(selected_ids)
        self._update_status()

    def select_clip(self, clip_id: str) -> None:
        """Select the row for ``clip_id`` and scroll to it (programmatic select)."""
        row = self._model.find_row_by_clip_id(clip_id)
        if row < 0:
            return
        self._building_ui = True
        try:
            sm = self._table.selectionModel()
            if sm is not None:
                idx = self._model.index(row, 0)
                sm.select(idx, sm.SelectionFlag.ClearAndSelect | sm.SelectionFlag.Rows)
            self._table.scrollTo(self._model.index(row, 0))
        finally:
            self._building_ui = False

    # ── Selection helpers ──

    def _selected_clip_ids(self) -> List[str]:
        sm = self._table.selectionModel()
        if sm is None:
            return []
        ids: list[str] = []
        for idx in sm.selectedRows():
            row = self._model.row_at(idx.row())
            if row is not None:
                ids.append(row.clip.clip_id)
        return ids

    def _select_clip_ids(self, clip_ids: Iterable[str]) -> None:
        sm = self._table.selectionModel()
        if sm is None:
            return
        id_set = set(clip_ids)
        self._building_ui = True
        try:
            sm.clear()
            for i in range(self._model.row_count()):
                row = self._model.row_at(i)
                if row is not None and row.clip.clip_id in id_set:
                    sm.select(self._model.index(i, 0),
                              sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
        finally:
            self._building_ui = False

    def _on_selection_changed(self, *args: Any) -> None:
        if self._building_ui:
            return
        ids = self._selected_clip_ids()
        if not ids:
            return
        # Emit the first selected cue (single-cue-driven sync); the controller
        # can extend to multi-cue properties later.
        clip_id = ids[0]
        row = self._model.row_at(self._model.find_row_by_clip_id(clip_id))
        if row is not None:
            self.cue_selected.emit(clip_id, row.track_id)
        self._update_status()

    # ── Find / Replace ──

    def _on_find(self) -> None:
        needle = self._find_edit.text()
        if not needle:
            self._status_label.setText(I18N.tr("caption.empty_search", "Empty search."))
            return
        clips = self._model.all_clips()
        matches = find_in_clips(clips, needle)
        if not matches:
            self._status_label.setText(f"No matches for '{needle}'.")
            return
        # Select the first match's row so the editor sees it in the timeline.
        self.select_clip(matches[0].clip_id)
        # AUD-19700: select_clip 以 _building_ui 防递归锁抑制了
        # selectionChanged → cue_selected 不会发射，时间线/查看器不会跳转到
        # 命中 cue。Find 是用户主动动作（与控制器驱动的 select_clip 反向同步
        # 不同，不存在回环），这里显式补发一次 cue_selected。
        hit_row = self._model.row_at(
            self._model.find_row_by_clip_id(matches[0].clip_id)
        )
        if hit_row is not None:
            self.cue_selected.emit(matches[0].clip_id, hit_row.track_id)
            # U05：Find 命中同时请求宿主定位播放头/画面到 cue 开始
            self.find_hit_selected.emit(matches[0].clip_id, hit_row.track_id)
        self._status_label.setText(f"{len(matches)} match(es) for '{needle}'.")

    def _on_replace_all(self) -> None:
        needle = self._find_edit.text()
        replacement = self._replace_edit.text()
        if not needle:
            self._status_label.setText(I18N.tr("caption.empty_search", "Empty search."))
            return
        # Emit a find_requested so the controller builds + submits the batch.
        # Scope "all" for now; per-track scope is a future toolbar option.
        self.find_requested.emit(needle, replacement, "all")
        self._status_label.setText(f"Replace-all '{needle}'→'{replacement}' requested.")

    # ── Split / Merge ──

    def _selected_row(self) -> Optional[CueRow]:
        ids = self._selected_clip_ids()
        if len(ids) != 1:
            return None
        row = self._model.row_at(self._model.find_row_by_clip_id(ids[0]))
        return row

    def _on_split_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            self._status_label.setText(
                I18N.tr("caption.split_need_one", "Select exactly one cue to split."))
            return
        clip = row.clip
        mid = clip.timeline_start + clip.timeline_duration // 2
        if mid <= clip.timeline_start or mid >= clip.timeline_end:
            self._status_label.setText(
                I18N.tr("caption.cue_too_short", "Cue too short to split."))
            return
        cmd = SplitSubtitleClipCommand.create(clip.clip_id, mid)
        self.command_requested.emit(cmd)
        self._status_label.setText(f"Split cue at frame {mid}.")

    def _on_merge_selected(self) -> None:
        ids = self._selected_clip_ids()
        if len(ids) != 2:
            self._status_label.setText(
                I18N.tr("caption.merge_need_two", "Select exactly two cues to merge."))
            return
        # Order by timeline_start via the model to pick first/second correctly.
        rows = [self._model.row_at(self._model.find_row_by_clip_id(cid))
                for cid in ids]
        rows = [r for r in rows if r is not None]
        if len(rows) != 2:
            return
        rows.sort(key=lambda r: r.clip.timeline_start)
        cmd = MergeSubtitleClipsCommand.create(
            rows[0].clip.clip_id, rows[1].clip.clip_id
        )
        self.command_requested.emit(cmd)
        self._status_label.setText(I18N.tr("caption.merge_requested", "Merge requested."))

    # ── Status ──

    def _update_status(self) -> None:
        n = self._model.row_count()
        sel = len(self._selected_clip_ids())
        if n == 0:
            self._status_label.setText(I18N.tr("caption.no_cues", "No cues."))
        else:
            self._status_label.setText(f"{n} cue(s); {sel} selected.")
