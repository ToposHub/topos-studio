"""TimelineTimeDisplay — the current-frame readout + jump box (T25/T34 view).

A compact QWidget showing the current frame in two lines, AE-style (plan
§16.3 B1):

* top line — large blue timecode (``H:MM:SS:FF``) or seconds, depending on
  the active display mode;
* bottom line — small blue ``frame (fps)`` readout.

The whole widget is read-only by default but switches to an editable
QLineEdit when clicked (plan §16.3 B1: "可点击进入编辑状态，输入合法时间
码或帧号后回车跳转"). Invalid input flashes an error border and does NOT
move the playhead (plan §16.3 B1: "时间码无效时使用 C.ERROR 边框，不修改
当前帧").

Why a dedicated widget:

* the spec demands a *fixed width* (104-112 px) so the layout doesn't jump
  when the frame number changes digit count — a bare QLabel can't enforce
  that without per-digit measurement;
* the edit/validate/commit cycle is non-trivial (timecode parse, frame
  parse, seconds parse, error feedback) and belongs in one tested place;
* the widget is used directly in the Keyframes property header, where the
  right-click menu keeps display-unit selection close to the readout.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QLabel, QLineEdit, QMenu, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C, timeline_timecode_font
from . import timeline_layout


# Fixed widget width per plan §16.3 B1 (104-112 px). Picking the midpoint
# so single-digit → four-digit frame numbers don't shift neighbouring
# controls.
_FIXED_WIDTH_PX = 108
_COMPACT_WIDTH_PX = 96
_LAYER_TIMECODE_PX = 14
_LAYER_DETAILS_PX = 10


class TimelineTimeDisplay(QWidget):
    """Two-line current-frame readout with click-to-edit jump.

    Signals:
        frame_committed(int) — emitted when the user types a valid frame
            (or timecode, or seconds) and presses Enter. The panel wires
            this to seek_to_frame(). Not emitted while the user is still
            typing.
    """

    frame_committed = pyqtSignal(int)
    display_mode_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None,
                 fps: float = 24.0, compact: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("TimelineTimeDisplay")
        self.setFixedWidth(_COMPACT_WIDTH_PX if compact else _FIXED_WIDTH_PX)
        self._fps = max(1.0, float(fps))
        self._display_mode = "frame"   # frame | seconds | timecode
        self._current_frame = 0
        self._editing = False
        self._compact = bool(compact)
        self._layer_header = False
        self._menu_display_mode = "frame"
        # AUD-11110: Qt emits editingFinished immediately after returnPressed
        # when Enter commits the edit. This flag marks "a commit attempt is
        # being processed this event round" so the trailing editingFinished
        # (which means cancel) can be told apart from a real focus-loss cancel.
        self._commit_round = False

        self._build_ui()
        self._refresh_labels()
        self.setStyleSheet(self._stylesheet())

    # ── public API ──

    def set_fps(self, fps: float) -> None:
        fps = max(1.0, float(fps))
        if abs(fps - self._fps) < 1e-9:
            return
        self._fps = fps
        self._refresh_labels()

    def set_layer_header(self, enabled: bool) -> None:
        """Group the layer timecode and frame count in a compact left column."""
        if enabled == self._layer_header:
            return
        self._exit_edit()
        self._layer_header = enabled
        layout = self.layout()
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                            if enabled else Qt.AlignmentFlag(0))
        layout.setContentsMargins(6, 0 if self._compact or enabled else 2,
                                  6, 0 if self._compact or enabled else 2)
        if enabled:
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            font = timeline_timecode_font()
            font.setPixelSize(_LAYER_TIMECODE_PX)
            details = QFont(font)
            details.setPixelSize(_LAYER_DETAILS_PX)
            details.setWeight(QFont.Weight.Normal)
            self._top_label.setFont(font)
            self._bottom_label.setFont(details)
            self._edit.setFont(font)
        else:
            self.setFixedWidth(_COMPACT_WIDTH_PX if self._compact else _FIXED_WIDTH_PX)
            self._top_label.setFont(self._legacy_main_font)
            self._bottom_label.setFont(self._legacy_details_font)
            self._edit.setFont(self._legacy_edit_font)
        self.setStyleSheet(self._stylesheet())
        self._refresh_labels()

    def set_display_mode(self, mode: str) -> None:
        """Switch the top line between frame / seconds / timecode.

        The bottom line always shows the raw frame + fps; only the top line
        follows the mode. The context menu uses the same panel state.
        """
        if mode not in ("frame", "seconds", "timecode"):
            mode = "frame"
        if mode == self._display_mode:
            return
        self._display_mode = mode
        self._refresh_labels()

    def set_menu_display_mode(self, mode: str) -> None:
        """Remember the panel/ruler mode shown as checked in the menu."""
        if mode in ("time", "frame", "seconds", "timecode"):
            self._menu_display_mode = mode

    def set_current_frame(self, frame: int) -> None:
        frame = int(frame)
        if frame == self._current_frame and not self._editing:
            return
        self._current_frame = frame
        if not self._editing:
            self._refresh_labels()

    @property
    def current_frame(self) -> int:
        return self._current_frame

    # ── UI build ──

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        if self._compact:
            outer.setContentsMargins(6, 0, 6, 0)
        else:
            outer.setContentsMargins(6, 2, 6, 2)
        outer.setSpacing(0)

        # Top line: large blue timecode/seconds/frame.
        self._top_label = QLabel(self)
        self._top_label.setObjectName("TimelineTimecode")
        f = QFont(self._top_label.font())
        f.setPointSize(9 if self._compact else 10)
        f.setBold(True)
        f.setFamily("Menlo, Consolas, Monaco, monospace")
        self._top_label.setFont(f)
        self._legacy_main_font = f
        self._top_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._top_label)

        # Bottom line: small blue "frame (fps)".
        self._bottom_label = QLabel(self)
        self._bottom_label.setObjectName("TimelineFrameDetails")
        f2 = QFont(self._bottom_label.font())
        f2.setPointSize(7 if self._compact else 8)
        f2.setFamily("Menlo, Consolas, Monaco, monospace")
        self._bottom_label.setFont(f2)
        self._legacy_details_font = f2
        self._bottom_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._bottom_label)

        # Edit overlay: hidden until the user clicks. Sits over the labels.
        self._edit = QLineEdit(self)
        self._edit.setObjectName("TimelineTimecodeEdit")
        self._legacy_edit_font = self._edit.font()
        self._edit.setFrame(False)
        self._edit.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._edit.returnPressed.connect(self._on_edit_commit)
        self._edit.editingFinished.connect(self._on_edit_cancel)
        self._edit.installEventFilter(self)
        self._edit.hide()

    def _stylesheet(self) -> str:
        blue = timeline_layout.resolve_token("accent_blue")
        error = timeline_layout.resolve_token("error")
        bg = C.TIMELINE_TIMECODE_BG if self._layer_header else timeline_layout.resolve_token("header_bg")
        text = C.TIMELINE_TIMECODE_TEXT if self._layer_header else blue
        details = C.TEXT_LABEL if self._layer_header else blue
        surface = "transparent" if self._layer_header else bg
        layer_typography = (
            "QLabel#TimelineTimecode, QLineEdit#TimelineTimecodeEdit "
            f"{{ font-size: {_LAYER_TIMECODE_PX}px; font-weight: 500; }}"
            f"QLabel#TimelineFrameDetails {{ font-size: {_LAYER_DETAILS_PX}px; font-weight: 400; }}"
        ) if self._layer_header else ""
        return (
            f"QWidget#TimelineTimeDisplay {{ background-color: {surface};"
            f" border: 1px solid transparent; border-radius: 2px; }}"
            f"QLabel {{ color: {text}; background: transparent; border: none; }}"
            f"QLabel#TimelineFrameDetails {{ color: {details}; }}"
            f"QLineEdit {{ background-color: {bg}; color: {text};"
            f" border: 1px solid {blue}; border-radius: 2px;"
            f" padding: 0 2px; selection-background-color: {blue};"
            f" selection-color: {bg}; }}"
            # AUD-11111: the invalid state is a dynamic *property*, so the
            # selector must be an attribute selector (objectName "#invalid"
            # never matched and the red border never showed).
            f"QLineEdit[invalid=\"true\"] {{ border: 1px solid {error}; }}"
            f"{layer_typography}"
        )

    # ── label rendering ──

    def _refresh_labels(self) -> None:
        self._top_label.setText(self._format_top(self._current_frame))
        self._bottom_label.setText(self._format_bottom(self._current_frame))

    def _format_top(self, frame: int) -> str:
        if self._display_mode == "timecode":
            return self._frame_to_timecode(frame)
        if self._display_mode == "seconds":
            return f"{frame / self._fps:7.3f}s"
        # frame mode: show the frame number, zero-padded to 5 digits so the
        # width doesn't jump as the value changes.
        return f"{frame:05d}"

    def _format_bottom(self, frame: int) -> str:
        # Always show raw frame + fps on the bottom line (plan §16.3 B1).
        fps_str = f"{self._fps:g}"
        if self._layer_header:
            return f"{frame:d} · {fps_str} fps"
        return f"{frame:d} ({fps_str})"

    def _frame_to_timecode(self, frame: int) -> str:
        """Convert a frame number to ``H:MM:SS:FF`` timecode.

        Frames are clamped to non-negative before formatting — AE's TC is
        undefined for negative frames, and the panel doesn't use them.
        """
        f = max(0, int(frame))
        if self._layer_header:
            total_seconds, ff = divmod(f, max(1, round(self._fps)))
            minutes, ss = divmod(total_seconds, 60)
            hh, mm = divmod(minutes, 60)
            return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
        ff = int(round(f % self._fps))
        total_seconds = int(f // self._fps)
        ss = total_seconds % 60
        mm = (total_seconds // 60) % 60
        hh = total_seconds // 3600
        return f"{hh:d}:{mm:02d}:{ss:02d}:{ff:02d}"

    # ── click-to-edit ──

    def _readout_rect(self) -> QRect:
        if not self._layer_header:
            return self.rect().adjusted(2, 2, -2, -2)
        return (self._top_label.geometry().united(self._bottom_label.geometry())
                .adjusted(-4, -3, 4, 3).intersected(self.rect()))

    def mouseDoubleClickEvent(self, event) -> None:
        if not self._layer_header or self._readout_rect().contains(event.position().toPoint()):
            self._enter_edit()

    def contextMenuEvent(self, event) -> None:
        """Offer display-unit choices without adding another footer row."""
        if self._layer_header and not self._readout_rect().contains(event.pos()):
            event.ignore()
            return
        menu = QMenu(self)
        choices = (
            ("time", I18N.tr("keyframes.ruler_automatic", "Automatic") if self._layer_header
             else I18N.tr("keyframes.unit_time", "Time")),
            ("frame", I18N.tr("keyframes.unit_frame", "Frames")),
            ("seconds", I18N.tr("keyframes.unit_seconds", "Seconds")),
            ("timecode", I18N.tr("keyframes.unit_timecode", "Timecode")),
        )
        actions = []
        for mode, label in choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._menu_display_mode == mode)
            actions.append((action, mode))
        chosen = menu.exec(event.globalPos())
        for action, mode in actions:
            if chosen is action:
                self._menu_display_mode = mode
                self.display_mode_requested.emit(mode)
                break

    def mousePressEvent(self, event) -> None:
        # Single click also enters edit (AE enters on single-click of the
        # time readout). Keep double-click too for discoverability.
        if (event.button() == Qt.MouseButton.LeftButton
                and (not self._layer_header or self._readout_rect().contains(event.position().toPoint()))):
            self._enter_edit()
        super().mousePressEvent(event)

    def _enter_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        self._commit_round = False
        # Pre-fill with the current top-line text so the user edits in the
        # active display mode (timecode edits stay as timecode, etc.).
        self._edit.setText(self._format_top(self._current_frame))
        self._edit.selectAll()
        self._edit.setProperty("invalid", False)
        self._edit.setGeometry(self._readout_rect())
        self._edit.show()
        self._edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_edit_commit(self) -> None:
        # AUD-11110: returnPressed is immediately followed by
        # editingFinished; the guard makes that trailing signal a no-op so
        # an invalid input can actually stay in edit mode with the red
        # border (the old code cancelled the edit one event later).
        self._commit_round = True
        text = self._edit.text().strip()
        frame = self._parse_user_input(text)
        if frame is None:
            # Invalid → red border, stay in edit mode so the user can fix it.
            self._edit.setProperty("invalid", True)
            # Property change alone doesn't re-evaluate the stylesheet;
            # unpolish/polish does (the old setStyleSheet("") cleared the
            # widget sheet instead of forcing a re-eval — AUD-11111).
            self._edit.style().unpolish(self._edit)
            self._edit.style().polish(self._edit)
            return
        self._exit_edit()
        if frame != self._current_frame:
            self.set_current_frame(frame)
            self.frame_committed.emit(frame)

    def _on_edit_cancel(self) -> None:
        if self._commit_round:
            # This editingFinished is the one Qt emits right after
            # returnPressed — the commit path already decided the outcome
            # (exited on success / stayed open with red border on invalid).
            self._commit_round = False
            return
        self._exit_edit()

    def _exit_edit(self) -> None:
        self._editing = False
        self._commit_round = False
        self._edit.hide()
        self._edit.setProperty("invalid", False)
        self._refresh_labels()

    def eventFilter(self, obj, event) -> bool:
        # Esc cancels the edit without committing (plan §16.3 B1).
        if obj is self._edit and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._exit_edit()
                return True
        return super().eventFilter(obj, event)

    # ── input parsing ──

    def _parse_user_input(self, text: str) -> Optional[int]:
        """Parse user input in any of the three display modes.

        Accepted forms:
          * integer (``"49"``) → frame number;
          * seconds (``"2.042s"`` or ``"2.042"`` in seconds mode) → frame;
          * timecode (``"0:00:02:01"`` or ``"00:00:02:01"``) → frame.

        Returns None for anything unparseable, so the caller can flash the
        error border without moving the playhead.
        """
        if not text:
            return None
        text = text.strip()

        # Pure integer → frame.
        try:
            return int(text)
        except ValueError:
            pass

        # Timecode H:MM:SS:FF (1-4 colon-separated parts).
        if ":" in text:
            return self._parse_timecode(text)

        # Seconds (with a decimal or trailing 's').
        if text.endswith("s"):
            text = text[:-1]
        try:
            seconds = float(text)
        except ValueError:
            return None
        return int(round(seconds * self._fps))

    def _parse_timecode(self, text: str) -> Optional[int]:
        """Parse an ``H:MM:SS:FF`` timecode, allowing partial forms.

        Partial inputs are **right-aligned**: the typed fields map to the
        lowest-order slots (S, F) and the higher-order slots (H, M) default
        to zero. So "2:01" → 0:0:2:1 (2s + 1f), and "1" → a single field
        that lands in the last slot (F). This matches AE's goto-time box.
        """
        parts = text.split(":")
        if not 1 <= len(parts) <= 4:
            return None
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        # Left-pad with zeros so the typed fields land in the lowest slots.
        while len(nums) < 4:
            nums.insert(0, 0)
        hh, mm, ss, ff = nums
        if not (0 <= mm < 60 and 0 <= ss < 60 and 0 <= ff < self._fps):
            return None
        total_frames = ((hh * 3600 + mm * 60 + ss) * int(round(self._fps))
                        + ff)
        return total_frames
