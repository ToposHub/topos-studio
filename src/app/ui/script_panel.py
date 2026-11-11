"""
Script Editor Panel for Topos Color.

Provides a dockable panel with a Python code editor (syntax highlighted)
and an output console for running user scripts.
"""

import os
import re
import logging

logger = logging.getLogger(__name__)

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton,
    QFileDialog, QMenu, QApplication,
    QMessageBox, QLineEdit, QLabel, QToolButton, QCompleter,
)
from PyQt6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont, QTextDocument,
    QIcon, QPixmap, QPainter, QPen, QAction, QKeySequence, QTextCursor,
    QPolygonF,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QSettings, QRect, QPointF, QObject

from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.ui.style_sheet import (
    C,
    SCRIPT_TOOLBAR_BTN_STYLE,
    SCRIPT_RUN_BTN_STYLE,
    SCRIPT_STOP_BTN_STYLE,
    SCRIPT_EDITOR_STYLE,
    SCRIPT_CONSOLE_STYLE,
    SCRIPT_STATUS_STYLE,
    CONSOLE_LABEL_STYLE,
    CONSOLE_CLEAR_BTN_STYLE,
    COMPLETER_POPUP_STYLE,
)


# ── Topos API completion list ─────────────────────────────────────

_TOPOS_API_METHODS = [
    "open", "save", "save_project",
    "add_layer", "remove_layer", "select_layer",
    "set_layer_visible", "set_layer_opacity", "set_layer_blend_mode",
    "duplicate_layer", "reset_layer",
    "grade",
    "set_exposure", "set_contrast", "set_saturation", "set_vibrance",
    "set_temperature", "set_tint", "set_highlights", "set_shadows",
    "set_hue_shift", "set_mid_detail", "set_sharpness",
    "set_lift", "set_gamma", "set_gain",
    "set_curves", "reset_curves",
    "set_hsl_curve", "reset_hsl_curves",
    "apply_lut", "set_lut_intensity", "remove_lut",
    "set_qualifier", "enable_qualifier", "reset_qualifier",
    "set_soft_clip", "reset_soft_clip",
    "set_film_grain", "reset_film_grain",
    "set_mixer", "reset_mixer",
    "set_blur_sharpen",
    "crop", "rotate", "flip",
    "batch", "list_files",
    "get_image_size", "get_layer_count", "get_active_layer_index",
    "get_grading_params",
    "print",
    "begin_undo", "end_undo",
    "set_progress", "set_status",
    "input_text", "input_number", "input_choice",
    "input_file", "input_folder",
    "confirm", "register_action",
    "capture_still", "apply_preset", "list_presets",
]


# ── Python Syntax Highlighter (State Machine) ─────────────────────

_NORMAL = 0
_TRIPLE_DQ = 1   # """
_TRIPLE_SQ = 2   # '''


class PythonHighlighter(QSyntaxHighlighter):
    """Python syntax highlighter using a state machine for multi-line strings."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)

        # Format definitions
        self._keyword_fmt = QTextCharFormat()
        self._keyword_fmt.setForeground(QColor("#ff8800"))
        self._keyword_fmt.setFontWeight(QFont.Weight.Bold)

        self._builtin_fmt = QTextCharFormat()
        self._builtin_fmt.setForeground(QColor("#569cd6"))

        self._string_fmt = QTextCharFormat()
        self._string_fmt.setForeground(QColor("#ce9178"))

        self._comment_fmt = QTextCharFormat()
        self._comment_fmt.setForeground(QColor("#6a9955"))

        self._number_fmt = QTextCharFormat()
        self._number_fmt.setForeground(QColor("#b5cea8"))

        self._decorator_fmt = QTextCharFormat()
        self._decorator_fmt.setForeground(QColor("#dcdcaa"))

        self._topos_fmt = QTextCharFormat()
        self._topos_fmt.setForeground(QColor("#4ec9b0"))

        self._keywords = (
            "False", "None", "True", "and", "as", "assert", "async", "await",
            "break", "class", "continue", "def", "del", "elif", "else",
            "except", "finally", "for", "from", "global", "if", "import",
            "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
            "return", "try", "while", "with", "yield",
        )
        self._builtins = (
            "print", "range", "len", "int", "float", "str", "list", "dict",
            "tuple", "set", "bool", "type", "isinstance", "enumerate", "zip",
            "map", "filter", "sorted", "abs", "min", "max", "sum", "round",
            "open", "os", "sys",
        )

    def highlightBlock(self, text: str):
        state = self.previousBlockState()
        if state < 0:
            state = _NORMAL

        if state == _TRIPLE_DQ:
            end = text.find('"""')
            if end >= 0:
                length = end + 3
                self.setFormat(0, length, self._string_fmt)
                self._highlight_normal(text, length)
                self.setCurrentBlockState(_NORMAL)
            else:
                self.setFormat(0, len(text), self._string_fmt)
                self.setCurrentBlockState(_TRIPLE_DQ)
            return

        if state == _TRIPLE_SQ:
            end = text.find("'''")
            if end >= 0:
                length = end + 3
                self.setFormat(0, length, self._string_fmt)
                self._highlight_normal(text, length)
                self.setCurrentBlockState(_NORMAL)
            else:
                self.setFormat(0, len(text), self._string_fmt)
                self.setCurrentBlockState(_TRIPLE_SQ)
            return

        # NORMAL state
        self.setCurrentBlockState(_NORMAL)
        self._highlight_normal(text, 0)

    def _highlight_normal(self, text: str, start: int):
        """Highlight a portion of text that is in NORMAL state, starting from *start*."""
        i = start
        n = len(text)
        while i < n:
            ch = text[i]

            # Triple double-quote
            if ch == '"' and i + 2 < n and text[i+1] == '"' and text[i+2] == '"':
                end = text.find('"""', i + 3)
                if end >= 0:
                    self.setFormat(i, end + 3 - i, self._string_fmt)
                    i = end + 3
                else:
                    self.setFormat(i, n - i, self._string_fmt)
                    self.setCurrentBlockState(_TRIPLE_DQ)
                    return
                continue

            # Triple single-quote
            if ch == "'" and i + 2 < n and text[i+1] == "'" and text[i+2] == "'":
                end = text.find("'''", i + 3)
                if end >= 0:
                    self.setFormat(i, end + 3 - i, self._string_fmt)
                    i = end + 3
                else:
                    self.setFormat(i, n - i, self._string_fmt)
                    self.setCurrentBlockState(_TRIPLE_SQ)
                    return
                continue

            # Single-line double-quote string
            if ch == '"':
                j = i + 1
                while j < n:
                    if text[j] == '\\':
                        j += 2
                        if j > n:
                            j = n
                        continue
                    if text[j] == '"':
                        j += 1
                        break
                    j += 1
                self.setFormat(i, min(j, n) - i, self._string_fmt)
                i = j
                continue

            # Single-line single-quote string
            if ch == "'":
                j = i + 1
                while j < n:
                    if text[j] == '\\':
                        j += 2
                        if j > n:
                            j = n
                        continue
                    if text[j] == "'":
                        j += 1
                        break
                    j += 1
                self.setFormat(i, min(j, n) - i, self._string_fmt)
                i = j
                continue

            # Comment
            if ch == '#':
                self.setFormat(i, n - i, self._comment_fmt)
                return  # rest of line is comment

            # Decorator
            if ch == '@' and (i == 0 or not text[i-1].isalnum()):
                m = re.match(r'@\w+', text[i:])
                if m:
                    self.setFormat(i, m.end(), self._decorator_fmt)
                    i += m.end()
                    continue

            # Word
            if ch.isalpha() or ch == '_':
                m = re.match(r'\w+', text[i:])
                word = m.group()
                if word in self._keywords:
                    self.setFormat(i, len(word), self._keyword_fmt)
                elif word in self._builtins:
                    self.setFormat(i, len(word), self._builtin_fmt)
                elif word == "topos":
                    self.setFormat(i, len(word), self._topos_fmt)
                i += len(word)
                continue

            # Number
            if ch.isdigit():
                m = re.match(r'\d+\.?\d*', text[i:])
                if m:
                    self.setFormat(i, m.end(), self._number_fmt)
                    i += m.end()
                    continue

            i += 1


# ── Icon Helpers ────────────────────────────────────────────────────

def _make_icon(draw_func, size=16):
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw_func(painter)
    painter.end()
    return QIcon(pixmap)


def _play_icon():
    def draw(p):

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#4CAF50"))
        tri = QPolygonF([QPointF(4, 2), QPointF(14, 8), QPointF(4, 14)])
        p.drawPolygon(tri)
    return _make_icon(draw)


def _stop_icon():
    def draw(p):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#f44336"))
        p.drawRect(3, 3, 10, 10)
    return _make_icon(draw)


def _new_icon():
    def draw(p):
        pen = QPen(QColor("#cccccc"), 1.5)
        p.setPen(pen)
        p.setBrush(QColor(0, 0, 0, 0))
        # Page outline

        path_points = [
            QPointF(3, 2), QPointF(10, 2), QPointF(13, 5),
            QPointF(13, 14), QPointF(3, 14),
        ]
        from PyQt6.QtGui import QPolygonF
        p.drawPolygon(QPolygonF(path_points))
        # Fold corner
        p.drawLine(QPointF(10, 2), QPointF(10, 5))
        p.drawLine(QPointF(10, 5), QPointF(13, 5))
        # Plus sign
        p.drawLine(QPointF(8, 7), QPointF(8, 12))
        p.drawLine(QPointF(5.5, 9.5), QPointF(10.5, 9.5))
    return _make_icon(draw)


def _open_icon():
    def draw(p):
        pen = QPen(QColor("#cccccc"), 1.5)
        p.setPen(pen)
        p.setBrush(QColor(0, 0, 0, 0))
        # Folder back

        from PyQt6.QtGui import QPolygonF
        p.drawPolygon(QPolygonF([
            QPointF(1, 4), QPointF(1, 13), QPointF(15, 13), QPointF(15, 4),
        ]))
        # Folder tab
        p.drawPolygon(QPolygonF([
            QPointF(1, 4), QPointF(1, 3), QPointF(6, 3), QPointF(7, 4),
        ]))
    return _make_icon(draw)


def _save_icon():
    def draw(p):
        pen = QPen(QColor("#cccccc"), 1.5)
        p.setPen(pen)
        p.setBrush(QColor(0, 0, 0, 0))

        from PyQt6.QtGui import QPolygonF
        # Floppy disk outline
        p.drawPolygon(QPolygonF([
            QPointF(2, 2), QPointF(11, 2), QPointF(14, 5),
            QPointF(14, 14), QPointF(2, 14),
        ]))
        # Label area
        p.drawRect(5, 2, 5, 5)
        # Disk window
        p.drawRect(4, 9, 8, 5)
    return _make_icon(draw)


def _clear_icon():
    def draw(p):
        pen = QPen(QColor("#888888"), 1.5)
        p.setPen(pen)
        p.setBrush(QColor(0, 0, 0, 0))

        from PyQt6.QtGui import QPolygonF
        # Trash can body
        p.drawPolygon(QPolygonF([
            QPointF(4, 5), QPointF(5, 14), QPointF(11, 14), QPointF(12, 5),
        ]))
        # Lid
        p.drawLine(QPointF(3, 5), QPointF(13, 5))
        # Handle
        p.drawLine(QPointF(6, 3), QPointF(10, 3))
        p.drawLine(QPointF(6, 3), QPointF(6, 5))
        p.drawLine(QPointF(10, 3), QPointF(10, 5))
        # Lines
        p.drawLine(QPointF(6.5, 7), QPointF(7, 12))
        p.drawLine(QPointF(8, 7), QPointF(8, 12))
        p.drawLine(QPointF(9.5, 7), QPointF(9, 12))
    return _make_icon(draw)


def _find_prev_icon():
    def draw(p):
        pen = QPen(QColor("#cccccc"), 1.5)
        p.setPen(pen)
        p.drawLine(QPointF(8, 13), QPointF(8, 4))
        p.drawLine(QPointF(4, 8), QPointF(8, 4))
        p.drawLine(QPointF(12, 8), QPointF(8, 4))
    return _make_icon(draw, size=14)


def _find_next_icon():
    def draw(p):
        pen = QPen(QColor("#cccccc"), 1.5)
        p.setPen(pen)
        p.drawLine(QPointF(7, 3), QPointF(7, 12))
        p.drawLine(QPointF(3, 8), QPointF(7, 12))
        p.drawLine(QPointF(11, 8), QPointF(7, 12))
    return _make_icon(draw, size=14)


def _close_icon():
    def draw(p):
        pen = QPen(QColor("#cccccc"), 1.5)
        p.setPen(pen)
        p.drawLine(QPointF(3, 3), QPointF(11, 11))
        p.drawLine(QPointF(11, 3), QPointF(3, 11))
    return _make_icon(draw, size=14)


# ── ScriptEditor (QPlainTextEdit with line numbers, auto-indent, etc.) ──

class LineNumberArea(QWidget):
    """Widget that draws line numbers for a ScriptEditor."""

    def __init__(self, editor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QApplication.fontMetrics().size(Qt.SizeFlag(0), "99999")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#1e1e1e"))

        block = self._editor.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self._editor.contentOffset()
        top = self._editor.blockBoundingGeometry(block).translated(offset).top()
        bottom = top + self._editor.blockBoundingRect(block).height()

        painter.setPen(QColor("#555555"))
        font = painter.font()
        font.setFamily("Menlo" if os.name != "nt" else "Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(font)

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.drawText(
                    0, int(top), self.width() - 4, int(self._editor.blockBoundingRect(block).height()),
                    Qt.AlignmentFlag.AlignRight, number,
                )
            block = block.next()
            top = bottom
            bottom = top + self._editor.blockBoundingRect(block).height()
            block_number += 1

        painter.end()


class _ConsoleDoubleClickFilter(QObject):
    """Event filter that detects double-clicks on traceback lines in the console."""

    def __init__(self, script_panel) -> None:
        super().__init__(script_panel)
        self._script_panel = script_panel

    def eventFilter(self, obj, event):
        if event.type() == event.Type.MouseButtonDblClick:
            cursor = self._script_panel.console.cursorForPosition(event.position().toPoint())
            line_text = cursor.block().text()
            match = re.search(r'line\s+(\d+)', line_text)
            if match:
                line_no = int(match.group(1))
                self._script_panel._jump_to_editor_line(line_no)
        return super().eventFilter(obj, event)


class ScriptEditor(QPlainTextEdit):
    """Code editor with line numbers, auto-indent, Tab/Shift+Tab, Ctrl+/, and Smart Home."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width(0)

        # Keyboard shortcuts
        comment_action = QAction(self)
        comment_action.setShortcut(QKeySequence("Ctrl+/"))
        comment_action.triggered.connect(self._toggle_comment)
        self.addAction(comment_action)

        # Code completion for topos. API
        self._completer = QCompleter(_TOPOS_API_METHODS, self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.activated.connect(self._insert_completion)
        self._completer.popup().setStyleSheet(COMPLETER_POPUP_STYLE)

    def _line_number_area_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 12 + digits * self.fontMetrics().horizontalAdvance("9")

    def _update_line_number_area_width(self, new_block_count):
        self.setViewportMargins(self._line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect, dy):
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self._line_number_area_width(), cr.height())
        )

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        # Tab / Shift+Tab
        if key == Qt.Key.Key_Tab:
            if modifiers == Qt.KeyboardModifier.NoModifier:
                self._handle_tab()
                return
        if key == Qt.Key.Key_Backtab:
            self._handle_shift_tab()
            return

        # Enter: auto-indent
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.NoModifier:
            self._handle_enter()
            return

        # Smart Home
        if key == Qt.Key.Key_Home:
            self._handle_home(modifiers)
            return

        super().keyPressEvent(event)
        self._check_completion()

    def _handle_tab(self):
        cursor = self.textCursor()
        if cursor.hasSelection():
            # Indent all selected lines
            start = cursor.selectionStart()
            end = cursor.selectionEnd()
            cursor.setPosition(start)
            cursor.beginEditBlock()
            first_line = cursor.blockNumber()
            cursor.setPosition(end)
            last_line = cursor.blockNumber()
            # Move to start of first selected line
            cursor.setPosition(start)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
            for _ in range(first_line, last_line + 1):
                cursor.insertText("    ")
                cursor.movePosition(QTextCursor.MoveOperation.Down)
            cursor.endEditBlock()
        else:
            cursor.insertText("    ")

    def _handle_shift_tab(self):
        cursor = self.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        cursor.setPosition(start)
        cursor.beginEditBlock()
        first_line = cursor.blockNumber()
        cursor.setPosition(end)
        last_line = cursor.blockNumber()
        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        for _ in range(first_line, last_line + 1):
            line_text = cursor.block().text()
            if line_text.startswith("\t"):
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 1)
                cursor.removeSelectedText()
            else:
                spaces = len(line_text) - len(line_text.lstrip(" "))
                remove = min(4, spaces)
                if remove > 0:
                    cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, remove)
                    cursor.removeSelectedText()
            cursor.movePosition(QTextCursor.MoveOperation.Down)
        cursor.endEditBlock()

    def _handle_enter(self):
        cursor = self.textCursor()
        line = cursor.block().text()
        indent = ""
        for ch in line:
            if ch in (" ", "\t"):
                indent += ch
            else:
                break
        # Extra indent after colon
        stripped = line.rstrip()
        if stripped.endswith(":"):
            indent += "    "
        cursor.insertText("\n" + indent)
        self.setTextCursor(cursor)

    def _handle_home(self, modifiers):
        cursor = self.textCursor()
        line = cursor.block().text()
        col = cursor.positionInBlock()

        # Find first non-space character
        first_non_space = 0
        for i, ch in enumerate(line):
            if ch != " " and ch != "\t":
                first_non_space = i
                break

        if col != first_non_space:
            new_pos = cursor.block().position() + first_non_space
        else:
            new_pos = cursor.block().position()

        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            # Extend selection to new_pos while preserving the far end
            anchor = cursor.anchor()
            position = cursor.position()
            far_end = anchor if position < anchor else position
            cursor.setPosition(far_end)
            cursor.setPosition(new_pos, QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.setPosition(new_pos)
        self.setTextCursor(cursor)

    def _check_completion(self):
        """Check if cursor is after 'topos.' and trigger completion popup."""
        cursor = self.textCursor()
        pos = cursor.position()
        text = self.toPlainText()

        if pos >= 6 and text[pos - 6:pos] == "topos.":
            # Just typed "topos." — show all methods
            self._completer.setCompletionPrefix("")
            rect = self.cursorRect()
            popup = self._completer.popup()
            rect.setWidth(
                popup.sizeHintForColumn(0)
                + popup.verticalScrollBar().sizeHint().width()
            )
            self._completer.complete(rect)
        elif self._completer.popup().isVisible():
            # Filter as user continues typing after "topos."
            start = pos - 6
            while start > 0 and (text[start - 1].isalnum() or text[start - 1] == '_'):
                start -= 1
            if start >= 0 and text[start:start + 6] == "topos.":
                prefix = text[start + 6:pos]
                if all(c.isalnum() or c == '_' for c in prefix):
                    self._completer.setCompletionPrefix(prefix)
                    rect = self.cursorRect()
                    popup = self._completer.popup()
                    rect.setWidth(
                        popup.sizeHintForColumn(0)
                        + popup.verticalScrollBar().sizeHint().width()
                    )
                    self._completer.complete(rect)
                    return
            self._completer.popup().hide()

    def _insert_completion(self, completion: str):
        """Insert the selected completion into the editor."""
        cursor = self.textCursor()
        prefix_len = len(self._completer.completionPrefix())
        if prefix_len > 0:
            cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, prefix_len)
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def _toggle_comment(self):
        cursor = self.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()

        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        first_line = cursor.blockNumber()
        cursor.setPosition(end)
        last_line = cursor.blockNumber()

        # Collect lines
        lines = []
        block = self.document().findBlockByNumber(first_line)
        for _ in range(first_line, last_line + 1):
            lines.append(block.text())
            block = block.next()

        all_commented = all(line.lstrip().startswith("#") or line.strip() == "" for line in lines)

        cursor.setPosition(start)
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)

        for i in range(first_line, last_line + 1):
            block = self.document().findBlockByNumber(i)
            text = block.text()
            if all_commented:
                # Remove '#' (and optional following space)
                if text.lstrip().startswith("#"):
                    indent_len = len(text) - len(text.lstrip())
                    after_hash = text[indent_len + 1:]
                    if after_hash.startswith(" "):
                        after_hash = after_hash[1:]
                    new_text = text[:indent_len] + after_hash
                    cursor.setPosition(block.position())
                    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                    cursor.insertText(new_text)
            else:
                if text.strip() and not text.lstrip().startswith("#"):
                    cursor.setPosition(block.position())
                    # Find indent
                    stripped = len(text) - len(text.lstrip())
                    cursor.setPosition(block.position() + stripped)
                    cursor.insertText("# ")
            cursor.movePosition(QTextCursor.MoveOperation.Down)

        cursor.endEditBlock()


# ── Search/Replace Bar ─────────────────────────────────────────────

class SearchBar(QWidget):
    """Floating search/replace bar for the script editor."""

    search_requested = pyqtSignal(str, bool)   # text, forward
    replace_requested = pyqtSignal(str, str, bool)  # find, replace, replace_all
    close_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from src.shared.i18n.i18n import I18N

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        # Close button
        self.btn_close = QToolButton()
        self.btn_close.setIcon(_close_icon())
        self.btn_close.setToolTip(I18N.tr("script.close_search_tip", "Close search bar (Esc)"))
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.clicked.connect(self._on_close)
        layout.addWidget(self.btn_close)

        # Search input
        layout.addWidget(QLabel(I18N.tr("script.find", "Find:")))
        self.search_input = QLineEdit()
        self.search_input.setFixedWidth(200)
        self.search_input.setPlaceholderText(I18N.tr("script.search_placeholder", "Search..."))
        self.search_input.returnPressed.connect(self._on_find_next)
        layout.addWidget(self.search_input)

        self.btn_find_prev = QToolButton()
        self.btn_find_prev.setIcon(_find_prev_icon())
        self.btn_find_prev.setToolTip(I18N.tr("script.find_prev_tip", "Find previous (Shift+Enter)"))
        self.btn_find_prev.setFixedSize(24, 24)
        self.btn_find_prev.clicked.connect(self._on_find_prev)
        layout.addWidget(self.btn_find_prev)

        self.btn_find_next = QToolButton()
        self.btn_find_next.setIcon(_find_next_icon())
        self.btn_find_next.setToolTip(I18N.tr("script.find_next_tip", "Find next (Enter)"))
        self.btn_find_next.setFixedSize(24, 24)
        self.btn_find_next.clicked.connect(self._on_find_next)
        layout.addWidget(self.btn_find_next)

        self.match_label = QLabel("")
        layout.addWidget(self.match_label)

        layout.addStretch()

        # Replace section (hidden by default)
        self.replace_label = QLabel(I18N.tr("script.replace", "Replace:"))
        layout.addWidget(self.replace_label)
        self.replace_label.hide()

        self.replace_input = QLineEdit()
        self.replace_input.setFixedWidth(200)
        self.replace_input.setPlaceholderText(I18N.tr("script.replace_placeholder", "Replace with..."))
        layout.addWidget(self.replace_input)
        self.replace_input.hide()

        self.btn_replace = QPushButton(I18N.tr("script.replace_btn", "Replace"))
        self.btn_replace.setFixedWidth(55)
        self.btn_replace.setToolTip(I18N.tr("script.replace_tip", "Replace current match"))
        self.btn_replace.clicked.connect(self._on_replace)
        layout.addWidget(self.btn_replace)
        self.btn_replace.hide()

        self.btn_replace_all = QPushButton(I18N.tr("script.replace_all", "All"))
        self.btn_replace_all.setFixedWidth(30)
        self.btn_replace_all.setToolTip(I18N.tr("script.replace_all_tip", "Replace all matches"))
        self.btn_replace_all.clicked.connect(self._on_replace_all)
        layout.addWidget(self.btn_replace_all)
        self.btn_replace_all.hide()

        self.btn_toggle_replace = QToolButton()
        self.btn_toggle_replace.setText("\u2261")
        self.btn_toggle_replace.setToolTip(I18N.tr("script.toggle_replace_tip", "Toggle replace mode"))
        self.btn_toggle_replace.setFixedSize(22, 22)
        self.btn_toggle_replace.clicked.connect(self._toggle_replace)
        layout.addWidget(self.btn_toggle_replace)

        self._replace_visible = False

    def _toggle_replace(self):
        self._replace_visible = not self._replace_visible
        self.replace_label.setVisible(self._replace_visible)
        self.replace_input.setVisible(self._replace_visible)
        self.btn_replace.setVisible(self._replace_visible)
        self.btn_replace_all.setVisible(self._replace_visible)
        self.btn_toggle_replace.setText("v" if self._replace_visible else ">")

    def _on_find_next(self):
        text = self.search_input.text()
        if text:
            self.search_requested.emit(text, True)

    def _on_find_prev(self):
        text = self.search_input.text()
        if text:
            self.search_requested.emit(text, False)

    def _on_replace(self):
        find_text = self.search_input.text()
        replace_text = self.replace_input.text()
        if find_text:
            self.replace_requested.emit(find_text, replace_text, False)

    def _on_replace_all(self):
        find_text = self.search_input.text()
        replace_text = self.replace_input.text()
        if find_text:
            self.replace_requested.emit(find_text, replace_text, True)

    def _on_close(self):
        self.hide()
        self.close_requested.emit()

    def show_search(self, text=""):
        if text:
            self.search_input.setText(text)
        self.show()
        self.search_input.setFocus()
        self.search_input.selectAll()

    def show_replace(self, text=""):
        if not self._replace_visible:
            self._toggle_replace()
        self.show_search(text)

    def update_match_count(self, current, total):
        self.match_label.setText(f"{current}/{total}")


# ── Script Panel ────────────────────────────────────────────────────

# Default script template
_DEFAULT_SCRIPT = """\
# Topos Color Script
# Use the 'topos' object to control the application.

# Example:
# topos.open("/path/to/image.jpg")
# topos.grade(exposure=0.5, contrast=15)
# topos.save("/output/graded.jpg")
"""


def _get_project_root():
    """Get the project root directory (where run_topos.command lives)."""
    # src/app/ui/script_panel.py -> src/app/ui -> src/app -> src -> project root
    # AUD-40014：此前只回退两级（落在 <root>/src），用户 Scripts 目录被
    # 错误写进源码树 src/Scripts；此处需要三级回退到仓库根。
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def _get_scripts_dir():
    """Get the user Scripts directory path, creating it if needed."""
    scripts_dir = os.path.join(_get_project_root(), "Scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    return scripts_dir


def _discover_builtin_examples():
    """Discover built-in example scripts from src/features/scripting/examples/.

    AUD-40013：此前相对本文件只回退一级，拼出的 ``src/app/scripting/examples``
    不存在（Scripts 菜单 "Examples" 永远为空，backward-compat 入口
    _load_example 同样失效）。真实示例目录在
    ``src/features/scripting/examples``——本文件（src/app/ui/）向上两级
    后跨包定位。"""
    examples_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "features", "scripting", "examples")
    examples_dir = os.path.normpath(examples_dir)
    result = {}
    if os.path.isdir(examples_dir):
        for root, _dirs, files in os.walk(examples_dir):
            for fname in sorted(files):
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, examples_dir)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            result[rel] = f.read()
                    except (IOError, OSError, UnicodeDecodeError):
                        pass
    return result


def _discover_user_scripts():
    """Discover user scripts from the project root Scripts/ directory."""
    scripts_dir = _get_scripts_dir()
    result = {}
    if os.path.isdir(scripts_dir):
        for root, _dirs, files in os.walk(scripts_dir):
            for fname in sorted(files):
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, scripts_dir)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            result[rel] = f.read()
                    except (IOError, OSError, UnicodeDecodeError):
                        pass
    return result


class ScriptPanel(QWidget):
    """Dockable script editor panel with run controls and output console."""

    run_clicked = pyqtSignal(str)       # script code
    stop_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_file = None
        self._recent_files = []
        self._settings = QSettings("ToposColor", "ScriptPanel")
        self._load_recent_files()
        self._setup_ui()
        self._setup_context_menu()

    def _load_recent_files(self):
        recent = self._settings.value("recent_files", [])
        if isinstance(recent, list):
            self._recent_files = [f for f in recent if os.path.isfile(f)][:10]

    def _save_recent_files(self):
        self._settings.setValue("recent_files", self._recent_files[:10])

    def _add_recent_file(self, path):
        path = os.path.abspath(path)
        if path in self._recent_files:
            self._recent_files.remove(path)
        self._recent_files.insert(0, path)
        self._recent_files = self._recent_files[:10]
        self._save_recent_files()
        self._update_recent_menu()

    def _setup_context_menu(self):
        """Set up context menu for the editor."""
        self.editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self._show_context_menu)

        self.console.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.console.customContextMenuRequested.connect(self._show_console_context_menu)

        # Main widget context menu (for toolbar area)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos):
        """Show context menu with docking/undocking options."""
        from src.shared.i18n.i18n import I18N

        menu = QMenu(self)

        # Docking options
        float_action = QAction(I18N.tr("dock.float", "Float"), self)
        float_action.triggered.connect(self._toggle_float)
        menu.addAction(float_action)

        dock_action = QAction(I18N.tr("dock.dock", "Dock"), self)
        dock_action.triggered.connect(self._dock_panel)
        menu.addAction(dock_action)

        menu.addSeparator()

        # Standard edit actions
        copy_action = QAction(I18N.tr("script.copy", "Copy"), self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self.editor.copy)
        menu.addAction(copy_action)

        paste_action = QAction(I18N.tr("script.paste", "Paste"), self)
        paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        paste_action.triggered.connect(self.editor.paste)
        menu.addAction(paste_action)

        cut_action = QAction(I18N.tr("script.cut", "Cut"), self)
        cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        cut_action.triggered.connect(self.editor.cut)
        menu.addAction(cut_action)

        menu.addSeparator()

        select_all_action = QAction(I18N.tr("script.select_all", "Select All"), self)
        select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all_action.triggered.connect(self.editor.selectAll)
        menu.addAction(select_all_action)

        menu.exec(self.mapToGlobal(pos))

    def _show_console_context_menu(self, pos):
        """Show context menu for the console."""
        from src.shared.i18n.i18n import I18N

        menu = QMenu(self)

        copy_action = QAction(I18N.tr("script.copy", "Copy"), self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self.console.copy)
        menu.addAction(copy_action)

        select_all_action = QAction(I18N.tr("script.select_all", "Select All"), self)
        select_all_action.triggered.connect(self.console.selectAll)
        menu.addAction(select_all_action)

        menu.addSeparator()
        clear_action = QAction(I18N.tr("script.menu_clear", "Clear"), self)
        clear_action.triggered.connect(self.clear_console)
        menu.addAction(clear_action)

        menu.exec(self.console.mapToGlobal(pos))

    def _toggle_float(self):
        """Toggle the panel to floating mode."""
        parent = self.parent()
        while parent is not None:
            from PyQt6.QtWidgets import QDockWidget
            if isinstance(parent, QDockWidget):
                if not parent.isFloating():
                    parent.setFloating(True)
                return
            parent = parent.parent()

    def _dock_panel(self):
        """Dock the panel back to main window."""
        parent = self.parent()
        while parent is not None:
            from PyQt6.QtWidgets import QDockWidget
            if isinstance(parent, QDockWidget):
                if parent.isFloating():
                    parent.setFloating(False)
                return
            parent = parent.parent()

    def _setup_ui(self):
        from src.shared.i18n.i18n import I18N

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Toolbar ──
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 4)
        toolbar.setSpacing(4)

        self.btn_new = QPushButton()
        self.btn_new.setIcon(_new_icon())
        self.btn_new.setToolTip(I18N.tr("script.new_tooltip", "New script (Ctrl+N)"))
        self.btn_new.setFixedSize(30, 28)
        self.btn_new.setStyleSheet(SCRIPT_TOOLBAR_BTN_STYLE)
        self.btn_new.clicked.connect(self._on_new)
        toolbar.addWidget(self.btn_new)

        self.btn_open = QPushButton()
        self.btn_open.setIcon(_open_icon())
        self.btn_open.setToolTip(I18N.tr("script.open_tooltip", "Open script (Ctrl+O)"))
        self.btn_open.setFixedSize(30, 28)
        self.btn_open.setStyleSheet(SCRIPT_TOOLBAR_BTN_STYLE)
        self.btn_open.clicked.connect(self._on_open)
        toolbar.addWidget(self.btn_open)

        self.btn_save = QPushButton()
        self.btn_save.setIcon(_save_icon())
        self.btn_save.setToolTip(I18N.tr("script.save_tooltip", "Save script"))
        self.btn_save.setFixedSize(30, 28)
        self.btn_save.setStyleSheet(SCRIPT_TOOLBAR_BTN_STYLE)
        self.btn_save.clicked.connect(self._on_save)
        toolbar.addWidget(self.btn_save)

        # Separator
        sep = QLabel("|")
        sep.setStyleSheet(f"color: {C.BORDER_MID};")
        toolbar.addWidget(sep)

        # Scripts dropdown (unified: Examples + My Scripts + Recent)
        self.btn_scripts = QPushButton(I18N.tr("script.scripts_btn", "Scripts"))
        self.btn_scripts.setFixedHeight(28)
        self.btn_scripts.setToolTip(I18N.tr("script.scripts_tip", "Browse and manage scripts"))
        self.btn_scripts.setStyleSheet(SCRIPT_TOOLBAR_BTN_STYLE)
        # [AUD-40015] 全生命周期只建一个 QMenu：此前每次打开都 new 一个
        # QMenu(self) 再 setMenu 覆盖，旧菜单（含全部 action 与其捕获的
        # 脚本内容 lambda）不销毁，随打开次数无上限累积（实测复现 5 次
        # 触发后 QMenu 1→6）。aboutToShow 时对同一对象 clear+refill，
        # 本次打开即见最新内容（一并消除 AUD-40016 的上一代内容滞后）。
        scripts_menu = QMenu(self)
        self.btn_scripts.setMenu(scripts_menu)
        scripts_menu.aboutToShow.connect(self._update_scripts_menu)
        self._fill_scripts_menu(scripts_menu)
        toolbar.addWidget(self.btn_scripts)

        toolbar.addStretch()

        self.btn_run = QPushButton(f" {I18N.tr('script.run', 'Run')}")
        self.btn_run.setIcon(_play_icon())
        self.btn_run.setFixedHeight(28)
        self.btn_run.setToolTip(I18N.tr("script.run_tooltip", "Run script (F5)"))
        self.btn_run.setStyleSheet(SCRIPT_RUN_BTN_STYLE)
        self.btn_run.clicked.connect(self._on_run)
        toolbar.addWidget(self.btn_run)

        self.btn_stop = QPushButton(f" {I18N.tr('script.stop', 'Stop')}")
        self.btn_stop.setIcon(_stop_icon())
        self.btn_stop.setFixedHeight(28)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip(I18N.tr("script.stop_tooltip", "Stop running script"))
        self.btn_stop.setStyleSheet(SCRIPT_STOP_BTN_STYLE)
        self.btn_stop.clicked.connect(self._on_stop)
        toolbar.addWidget(self.btn_stop)

        toolbar.addSpacing(12)
        self._status_label = QLabel(I18N.tr("script.ready", "Ready"))
        self._status_label.setStyleSheet(SCRIPT_STATUS_STYLE)
        toolbar.addWidget(self._status_label)

        layout.addLayout(toolbar)

        # ── Splitter: Editor + Console ──
        self.splitter = PanelSplitter(Qt.Orientation.Vertical)

        # Editor container (editor + search bar)
        editor_container = QWidget()
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        # Code editor (ScriptEditor)
        self.editor = ScriptEditor()
        self.editor.setPlainText(_DEFAULT_SCRIPT)
        font = QFont("Menlo", 12) if os.name != "nt" else QFont("Consolas", 12)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.editor.setFont(font)
        self.editor.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setStyleSheet(SCRIPT_EDITOR_STYLE)
        self._highlighter = PythonHighlighter(self.editor.document())
        self.editor.document().setModified(False)

        # Project saving owns Cmd+S globally. Keep script saving available from
        # the Save button, but do not install a second Cmd+S action on the
        # editor, otherwise it competes with the application's project save.
        open_action = QAction(self)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self._on_open)
        self.editor.addAction(open_action)

        # Search/replace bar
        self.search_bar = SearchBar(self)
        self.search_bar.hide()
        self.search_bar.search_requested.connect(self._do_search)
        self.search_bar.replace_requested.connect(self._do_replace)
        self.search_bar.close_requested.connect(self._search_closed)

        find_action = QAction(self)
        find_action.setShortcut(QKeySequence("Ctrl+F"))
        find_action.triggered.connect(self._show_search)
        self.editor.addAction(find_action)

        replace_action = QAction(self)
        replace_action.setShortcut(QKeySequence("Ctrl+H"))
        replace_action.triggered.connect(self._show_replace)
        self.editor.addAction(replace_action)

        editor_layout.addWidget(self.editor)
        editor_layout.addWidget(self.search_bar)

        self.splitter.addWidget(editor_container)

        # Output console section
        console_container = QWidget()
        console_layout = QVBoxLayout(console_container)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(0)

        # Console toolbar
        console_toolbar = QHBoxLayout()
        console_toolbar.setContentsMargins(4, 2, 4, 2)
        console_toolbar.setSpacing(4)

        from src.shared.i18n.i18n import I18N

        console_label = QLabel(I18N.tr("script.output", "Output"))
        console_label.setStyleSheet(CONSOLE_LABEL_STYLE)
        console_toolbar.addWidget(console_label)

        console_toolbar.addStretch()

        self.btn_clear_console = QPushButton()
        self.btn_clear_console.setIcon(_clear_icon())
        self.btn_clear_console.setToolTip(I18N.tr("script.clear", "Clear console output"))
        self.btn_clear_console.setFixedSize(24, 20)
        self.btn_clear_console.setStyleSheet(CONSOLE_CLEAR_BTN_STYLE)
        self.btn_clear_console.clicked.connect(self.clear_console)
        console_toolbar.addWidget(self.btn_clear_console)

        console_toolbar.addWidget(QLabel(I18N.tr("script.console_search", "Search:")))
        self.console_search = QLineEdit()
        self.console_search.setFixedWidth(150)
        self.console_search.setFixedHeight(20)
        self.console_search.setPlaceholderText(I18N.tr("script.find_in_output", "Find in output..."))
        self.console_search.textChanged.connect(self._on_console_search_changed)
        self._console_search_timer = QTimer(self)
        self._console_search_timer.setSingleShot(True)
        self._console_search_timer.setInterval(200)
        self._console_search_timer.timeout.connect(self._search_console)
        console_toolbar.addWidget(self.console_search)

        console_layout.addLayout(console_toolbar)

        # Output console
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        console_font = QFont("Menlo", 11) if os.name != "nt" else QFont("Consolas", 11)
        console_font.setStyleHint(QFont.StyleHint.Monospace)
        self.console.setFont(console_font)
        self.console.setPlaceholderText(I18N.tr("script.console_placeholder", "Output console"))
        self.console.setStyleSheet(SCRIPT_CONSOLE_STYLE)
        console_layout.addWidget(self.console)

        # Enable double-click traceback jump
        self._console_dblclick_filter = _ConsoleDoubleClickFilter(self)
        self.console.installEventFilter(self._console_dblclick_filter)

        self.splitter.addWidget(console_container)

        self.splitter.setSizes([400, 150])
        layout.addWidget(self.splitter)

    def _fill_scripts_menu(self, menu):
        """Fill the given Scripts menu with its three sections.

        [AUD-40015] 由 _build_scripts_menu（每次 new QMenu）改为向调用方
        提供的菜单对象填充内容，配合单一菜单 clear+refill 复用。
        """
        from src.shared.i18n.i18n import I18N

        # Section 1: Built-in Examples
        menu_label = menu.addAction(I18N.tr("script.examples", "Examples"))
        menu_label.setEnabled(False)
        font = menu_label.font()
        font.setBold(True)
        menu_label.setFont(font)

        examples = _discover_builtin_examples()
        if examples:
            for name, code in examples.items():
                display = os.path.splitext(os.path.basename(name))[0].replace("_", " ").title()
                act = menu.addAction(display, lambda checked=False, c=code: self._load_example_code(c))
        else:
            act = menu.addAction(I18N.tr("script.no_examples", "(No examples)"))
            act.setEnabled(False)

        menu.addSeparator()

        # Section 2: My Scripts (from Scripts/ directory)
        menu_label2 = menu.addAction(I18N.tr("script.my_scripts", "My Scripts"))
        menu_label2.setEnabled(False)
        menu_label2.setFont(font)

        user_scripts = _discover_user_scripts()
        if user_scripts:
            for name, code in user_scripts.items():
                display = os.path.splitext(os.path.basename(name))[0].replace("_", " ").title()
                scripts_dir = _get_scripts_dir()
                fpath = os.path.join(scripts_dir, name)
                act = menu.addAction(display, lambda checked=False, p=fpath: self._open_script_file(p))
        else:
            act = menu.addAction(I18N.tr("script.my_scripts_empty", "(Empty — Save scripts here)"))
            act.setEnabled(False)

        menu.addSeparator()

        # Quick save to Scripts/
        menu.addAction(I18N.tr("script.save_to_scripts", "Save to Scripts..."), self._save_to_scripts)
        menu.addSeparator()

        # Section 3: Recent Files
        menu_label3 = menu.addAction(I18N.tr("script.recent_files", "Recent Files"))
        menu_label3.setEnabled(False)
        menu_label3.setFont(font)

        if self._recent_files:
            for path in self._recent_files:
                display = os.path.basename(path)
                menu.addAction(display, lambda checked=False, p=path: self._open_recent(p))
        else:
            act = menu.addAction(I18N.tr("script.no_recent_files", "(No recent files)"))
            act.setEnabled(False)

        return menu

    def _update_scripts_menu(self):
        """Rebuild the Scripts menu in place before showing.

        [AUD-40015] 复用 btn_scripts 上唯一的 QMenu：clear 后重新填充，
        不再新建菜单（旧对象不销毁导致无限累积）。aboutToShow 在弹出动作
        发生前触发，清空重填同一菜单即生效于本次打开。
        """
        menu = self.btn_scripts.menu()
        if menu is None:  # defensive: button always gets its menu in _setup_ui
            menu = QMenu(self)
            self.btn_scripts.setMenu(menu)
            menu.aboutToShow.connect(self._update_scripts_menu)
        menu.clear()
        self._fill_scripts_menu(menu)

    def _update_recent_menu(self):
        """Recent files are now part of the unified Scripts menu."""
        # No-op: the Scripts menu is rebuilt each time it opens
        pass

    def _open_recent(self, path):
        if not self._confirm_discard_changes():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.editor.setPlainText(f.read())
            self._current_file = path
            self.editor.document().setModified(False)
            self._add_recent_file(path)
        except (IOError, OSError, UnicodeDecodeError) as e:
            QMessageBox.critical(self, "Open Error", f"Failed to open file:\n{e}")

    # ── Search/Replace ──

    def _show_search(self):
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        self.search_bar.show_search(selected if selected else "")

    def _show_replace(self):
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        self.search_bar.show_replace(selected if selected else "")

    def _search_closed(self):
        self.editor.setFocus()

    def _do_search(self, text, forward):
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags = QTextDocument.FindFlag.FindBackward
        found = self.editor.find(text, flags)
        if not found:
            # Wrap around
            if forward:
                cursor = self.editor.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                self.editor.setTextCursor(cursor)
            else:
                cursor = self.editor.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                self.editor.setTextCursor(cursor)
            self.editor.find(text, flags)

        # Update match count
        full_text = self.editor.toPlainText()
        total = full_text.count(text)
        if total > 0 and text:
            cursor_pos = self.editor.textCursor().position()
            current = full_text[:cursor_pos].count(text) + 1
            if current > total:
                current = total
        else:
            current = 0
        self.search_bar.update_match_count(current, total)

    def _do_replace(self, find_text, replace_text, replace_all):
        cursor = self.editor.textCursor()
        if replace_all:
            cursor.beginEditBlock()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            count = 0
            while self.editor.find(find_text):
                cursor = self.editor.textCursor()
                cursor.insertText(replace_text)
                count += 1
            cursor.endEditBlock()
        else:
            if cursor.hasSelection() and cursor.selectedText() == find_text:
                cursor.insertText(replace_text)
            self.editor.find(find_text)

    def _on_console_search_changed(self):
        """Debounce console search input."""
        self._console_search_timer.start()

    def _search_console(self):
        """Highlight matches in the console."""
        text = self.console_search.text()
        if not text:
            self.console.setTextCursor(QTextCursor(self.console.document()))
            return
        # Start search from current cursor position
        current = self.console.textCursor()
        cursor = self.console.document().find(text, current)
        if cursor.isNull():
            # Wrap around from the beginning
            cursor = self.console.document().find(text, 0)
        if not cursor.isNull():
            self.console.setTextCursor(cursor)
            self.console.ensureCursorVisible()

    # ── Public API ──

    def append_output(self, text: str, color: str = "#cccccc"):
        """Append text to the output console with optional color."""
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    def append_error(self, text: str):
        """Append error text to the output console in red."""
        self.append_output(text, "#f44336")

    def clear_console(self):
        """Clear the output console."""
        self.console.clear()

    def set_running(self, running: bool):
        """Update button states based on running status."""
        self.btn_run.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.editor.setReadOnly(running)
        if running:
            self.set_status("Running...", "#4CAF50")

    def set_status(self, text: str, color: str = "#888"):
        """Update the execution status label in the toolbar."""
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"QLabel {{ color: {color}; font-size: 11px; padding: 0 4px; }}"
        )

    def _jump_to_editor_line(self, line_number: int):
        """Move the editor cursor to the given line number (1-based) and center it."""
        if line_number < 1:
            return
        block = self.editor.document().findBlockByNumber(line_number - 1)
        if block.isValid():
            cursor = self.editor.textCursor()
            cursor.setPosition(block.position())
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            self.editor.setTextCursor(cursor)
            self.editor.setFocus()
            self.editor.centerCursor()

    def highlight_line(self, line_number: int):
        """Highlight a specific line in the editor (called on script error)."""
        self._jump_to_editor_line(line_number)

    def get_script_code(self) -> str:
        """Return the current editor content."""
        return self.editor.toPlainText()

    # ── Slots ──

    def _on_run(self):
        code = self.editor.toPlainText()
        logger.debug(f"_on_run called, code length={len(code)}, empty={not bool(code.strip())}")
        if code.strip():
            self.clear_console()
            self.set_running(True)
            logger.debug("emitting run_clicked signal")
            self.run_clicked.emit(code)
        else:
            logger.debug("code is empty, skipping")

    def _on_stop(self):
        self.btn_stop.setEnabled(False)  # Prevent repeated clicks
        self.stop_clicked.emit()

    def _confirm_discard_changes(self) -> bool:
        """Ask user to confirm discarding unsaved changes.

        Returns True if it is safe to proceed (no changes or user confirmed).
        """
        if self.editor.document().isModified():
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "The current script has unsaved changes.\n"
                "Discard and continue?",
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            return reply == QMessageBox.StandardButton.Discard
        return True

    def _on_new(self):
        if not self._confirm_discard_changes():
            return
        self.editor.setPlainText(_DEFAULT_SCRIPT)
        self._current_file = None
        self.editor.document().setModified(False)

    def _on_open(self):
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Script", "",
            "Python Scripts (*.py);;All Files (*)"
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.editor.setPlainText(f.read())
                self._current_file = path
                self.editor.document().setModified(False)
                self._add_recent_file(path)
            except (IOError, OSError, UnicodeDecodeError) as e:
                QMessageBox.critical(self, "Open Error", f"Failed to open file:\n{e}")

    def _on_save(self):
        if self._current_file:
            path = self._current_file
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Script", "",
                "Python Scripts (*.py);;All Files (*)"
            )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.toPlainText())
                self._current_file = path
                self.editor.document().setModified(False)
                self._add_recent_file(path)
            except (IOError, OSError) as e:
                QMessageBox.critical(self, "Save Error", f"Failed to save file:\n{e}")

    def _load_example(self, name: str):
        """Load an example by name (kept for backward compat)."""
        examples = _discover_builtin_examples()
        if not self._confirm_discard_changes():
            return
        code = examples.get(name, "")
        if code:
            self.editor.setPlainText(code)
            self._current_file = None
            self.editor.document().setModified(False)

    def _load_example_code(self, code: str):
        """Load example code directly."""
        if not self._confirm_discard_changes():
            return
        self.editor.setPlainText(code)
        self._current_file = None
        self.editor.document().setModified(False)

    def _open_script_file(self, path: str):
        """Open a script file from the Scripts directory."""
        if not self._confirm_discard_changes():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.editor.setPlainText(f.read())
            self._current_file = path
            self.editor.document().setModified(False)
            self._add_recent_file(path)
        except (IOError, OSError, UnicodeDecodeError) as e:
            QMessageBox.critical(self, "Open Error", f"Failed to open file:\n{e}")

    def _save_to_scripts(self):
        """Save current script to the Scripts directory."""
        scripts_dir = _get_scripts_dir()
        if self._current_file and os.path.dirname(self._current_file) == scripts_dir:
            # Already in Scripts dir, just save
            path = self._current_file
        else:
            # Suggest a name based on current file or default
            default_name = ""
            if self._current_file:
                default_name = os.path.basename(self._current_file)
            if not default_name.endswith(".py"):
                default_name = ""
            path, _ = QFileDialog.getSaveFileName(
                self, "Save to Scripts", os.path.join(scripts_dir, default_name),
                "Python Scripts (*.py);;All Files (*)"
            )
        if path:
            if not path.endswith(".py"):
                path += ".py"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.toPlainText())
                self._current_file = path
                self.editor.document().setModified(False)
                self._add_recent_file(path)
            except (IOError, OSError) as e:
                QMessageBox.critical(self, "Save Error", f"Failed to save file:\n{e}")
