"""NodeHelpDialog — read-only Markdown viewer for node usage documents.

Opened from the node-section header "?" button. Renders the node's usage
document in a dark-themed QTextBrowser. The initial language follows the
app locale; a language selector in the header lets the user switch between
the available document languages (e.g. 中文 / English) at any time and the
content refreshes immediately.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N, tr
from src.shared.node_help import available_langs, get_node_help
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

# Document typography — a visual hierarchy separated by color AND size
# (all left-aligned):
#   level-1 heading -> light red, largest
#   level-2 heading -> light red, smaller
#   level-3+ heading -> off-white (white with a touch of gray)
#   body / code     -> light gray
_H1_COLOR = QColor("#e57373")
_H2_COLOR = QColor("#e57373")
_H3_COLOR = QColor("#e0e0e0")
_BODY_COLOR = QColor("#c8c8c8")
_H1_SIZE = 16.0
_H2_SIZE = 13.0
_H3_SIZE = 12.0

# Display names for document languages (shown in their own language by
# convention — recognizable regardless of the current UI language).
_LANG_DISPLAY = {
    "zh_CN": "中文",
    "en_US": "English",
}


class NodeHelpDialog(QDialog):
    """Help window showing a node's usage documentation with language switch."""

    _DIALOG_STYLE = (
        "QDialog { background-color: #2b2b2e; }"
        "QTextBrowser {"
        " background-color: #2b2b2e; color: #d8d8d8;"
        " border: none; font-size: 12px;"
        " selection-background-color: #4a6a8a;"
        "}"
        # Keep the label flush with the dialog background.
        "QLabel { color: #b0b0b0; background-color: #2b2b2e; }"
    )

    def __init__(self, node_type: str, node_title: str, parent=None):
        super().__init__(parent)
        self._node_type = node_type
        self.setWindowTitle(
            I18N.tr("composite.node_help.window_title",
                    "{title} — Help").format(title=node_title))
        self.setWindowFlags(self.windowFlags()
                            & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setMinimumSize(520, 420)
        self.resize(600, 520)
        self.setStyleSheet(self._DIALOG_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # Language selector — switch between the available document
        # languages without closing the dialog.
        header = QHBoxLayout()
        header.addWidget(QLabel(
            tr("composite.node_help.language", "Language"), self))
        self._lang_combo = ChevronComboBox(self)
        self._langs = available_langs(node_type)
        for lang in self._langs:
            self._lang_combo.addItem(
                _LANG_DISPLAY.get(lang, lang), lang)
        self._lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        header.addWidget(self._lang_combo)
        header.addStretch(1)
        layout.addLayout(header)

        # Widen the combo so the longest option (e.g. "English") is never
        # truncated: text width + left padding + chevron space + slack.
        fm = self._lang_combo.fontMetrics()
        text_w = max(
            (fm.horizontalAdvance(self._lang_combo.itemText(i))
             for i in range(self._lang_combo.count())),
            default=0)
        self._lang_combo.setMinimumWidth(text_w + 12 + 30 + 12)

        self._browser = QTextBrowser(self)
        self._browser.setOpenExternalLinks(True)
        layout.addWidget(self._browser)

        # Show the current language's document (combo index 0 = app locale).
        self._load_current_lang()

    def current_lang(self) -> str:
        """The language code currently displayed."""
        return self._lang_combo.currentData() or ""

    def set_language(self, lang: str) -> None:
        """Switch the displayed document language programmatically."""
        idx = self._lang_combo.findData(lang)
        if idx >= 0:
            self._lang_combo.setCurrentIndex(idx)
        else:
            self._load_lang(lang)

    def _on_lang_changed(self, _index: int) -> None:
        self._load_current_lang()

    def _load_current_lang(self) -> None:
        self._load_lang(self.current_lang())

    def _load_lang(self, lang: str) -> None:
        # Explicit locale_code targets that language directly; the combo is
        # built from available_langs(), so a chosen language always exists
        # and switching never silently shows another language.
        markdown = get_node_help(self._node_type, locale_code=lang or None)
        self._browser.setMarkdown(markdown or "")
        self._apply_typography()

    def _apply_typography(self) -> None:
        """Color/size the loaded document into the heading hierarchy."""
        doc = self._browser.document()
        block = doc.begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            fmt = QTextCharFormat()
            if level == 1:
                fmt.setForeground(_H1_COLOR)
                fmt.setFontPointSize(_H1_SIZE)
                fmt.setFontWeight(QFont.Weight.Bold)
            elif level == 2:
                fmt.setForeground(_H2_COLOR)
                fmt.setFontPointSize(_H2_SIZE)
                fmt.setFontWeight(QFont.Weight.Bold)
            elif level >= 3:
                fmt.setForeground(_H3_COLOR)
                fmt.setFontPointSize(_H3_SIZE)
                fmt.setFontWeight(QFont.Weight.Bold)
            else:
                fmt.setForeground(_BODY_COLOR)
            cursor = QTextCursor(doc)
            cursor.setPosition(block.position())
            cursor.setPosition(block.position() + block.length() - 1,
                               QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(fmt)
            block = block.next()
