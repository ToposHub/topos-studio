"""Audio effects browser for the project-backed Mixer workspace."""

from __future__ import annotations

from collections.abc import Iterable

from PyQt6.QtCore import QMimeData, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QShortcut, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QLabel, QLineEdit, QStackedWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.features.edit.ui.edit_effects_panel import MIME_TYPE_EFFECT
from src.features.mixer.effects import EffectInstance, EffectWindowManager, effect_catalog
from src.features.mixer.effects.browser_catalog import audio_effect_groups, audio_effect_name
from src.features.mixer.effects.production_dialog import PRODUCTION_BINDINGS
from src.shared.assets.icon_manager import icon_fx, icon_search
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
from src.shared.ui.style_sheet import C


_EFFECT_ROLE = Qt.ItemDataRole.UserRole
_SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1
_PANEL_ROLE = Qt.ItemDataRole.UserRole + 2
_STYLE = f"""
QWidget#MixerEffectsPanel {{
    background: {C.BG_PANEL};
}}
QLineEdit#MixerEffectsSearch {{
    background: {C.BG_INPUT_DARK}; color: {C.TEXT_PRIMARY};
    border: 1px solid {C.BORDER}; border-radius: 4px;
    min-height: 22px; padding: 2px 6px; font-size: 12px;
}}
QLineEdit#MixerEffectsSearch:focus {{ border-color: {C.ORANGE}; }}
QTreeWidget#MixerEffectsTree {{
    background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; border: none;
    outline: none; font-size: 12px;
}}
QTreeWidget#MixerEffectsTree::item {{ padding: 0 5px; }}
QTreeWidget#MixerEffectsTree::item:hover {{ background: {C.BG_BUTTON}; }}
QTreeWidget#MixerEffectsTree::item:selected {{
    background: {C.ACCENT_SURFACE}; color: white;
}}
QLabel#MixerEffectsEmpty {{
    background: transparent; color: {C.TEXT_SECONDARY};
    padding: 12px;
}}
"""


def _tr(suffix: str, default: str) -> str:
    return I18N.tr(f"mixer.effects_browser.{suffix}", default)


class _MixerEffectsTree(QTreeWidget):
    def mimeTypes(self) -> list[str]:  # noqa: N802 - Qt API
        return [MIME_TYPE_EFFECT]

    def mimeData(self, items: Iterable[QTreeWidgetItem]) -> QMimeData:  # noqa: N802 - Qt API
        payload = QMimeData()
        for item in items:
            effect_type = item.data(0, _EFFECT_ROLE)
            if (isinstance(effect_type, str) and effect_type and not item.isHidden()
                    and item.flags() & Qt.ItemFlag.ItemIsDragEnabled):
                payload.setData(MIME_TYPE_EFFECT, effect_type.encode("utf-8"))
                break
        return payload


class MixerEffectsPanel(QWidget):
    """Browse project effects and open panels whose processors are pending."""

    effect_activated = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MixerEffectsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_STYLE)
        self._catalog_loaded = False
        self._search_expansion: tuple[bool, ...] | None = None
        self._groups: list[QTreeWidgetItem] = []
        self._panel_windows = EffectWindowManager(self)
        self._panel_instances: dict[str, EffectInstance] = {}

        self.search = QLineEdit(self)
        self.search.setObjectName("MixerEffectsSearch")
        search_label = _tr("search", "Search audio effects")
        self.search.setPlaceholderText(search_label)
        self.search.setAccessibleName(search_label)
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon_search(C.TEXT_HINT), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._filter)

        self.tree = _MixerEffectsTree(self)
        self.tree.setObjectName("MixerEffectsTree")
        self.tree.setAccessibleName(I18N.tr("panel.audio_effects", "Audio Effects"))
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(15)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setSupportedDragActions(Qt.DropAction.CopyAction)
        self.tree.itemDoubleClicked.connect(self._activate)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._activate_current)

        self._empty = QLabel(self)
        self._empty.setObjectName("MixerEffectsEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._content = QStackedWidget(self)
        self._content.addWidget(self.tree)
        self._content.addWidget(self._empty)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(6)
        layout.addWidget(self.search)
        layout.addWidget(self._content, 1)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self._catalog_loaded:
            self._catalog_loaded = True
            self._populate()
            self._filter(self.search.text())

    def _populate(self) -> None:
        row_icon = icon_fx(C.TEXT_HINT)
        for label, group_name, entries in audio_effect_groups(include_track_effects=True):
            heading = self._add_group(label)
            for effect in entries:
                effect_type = effect.effect_type
                name = audio_effect_name(effect)
                item = QTreeWidgetItem(heading, [name])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                              Qt.ItemFlag.ItemIsDragEnabled)
                item.setData(0, _EFFECT_ROLE, effect_type)
                item.setData(0, _SEARCH_ROLE, " ".join((
                    name, effect.name, effect_type, effect.description, label, group_name,
                )).casefold())
                item.setIcon(0, row_icon)
                item.setToolTip(0, f"{name}\n{self._target_hint(effect_type)}")

        live_panels = {panel_id for panel_id, _fields in PRODUCTION_BINDINGS.values()}
        tools = self._add_group(_tr("group.tools", "Clip Tools"))
        pending = self._add_group(_tr("group.pending", "Panels awaiting audio processing"))
        for descriptor in effect_catalog():
            if descriptor.effect_id in live_panels:
                continue
            clip_tool = descriptor.effect_id == "clip_editor"
            heading = tools if clip_tool else pending
            name = I18N.tr(f"mixer.effect_panel.name.{descriptor.effect_id}", descriptor.name_en)
            item = QTreeWidgetItem(heading, [name])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setData(0, _PANEL_ROLE, descriptor.effect_id)
            item.setData(0, _SEARCH_ROLE, " ".join((
                descriptor.name_en, descriptor.name_zh, descriptor.effect_id, heading.text(0),
            )).casefold())
            item.setIcon(0, row_icon)
            item.setToolTip(0, _tr(
                "open_clip_tool", "Double-click to open the selected audio clip in the editor.",
            ) if clip_tool else _tr(
                "open_pending_panel", "Double-click to open the panel and try its controls.",
            ))

    def _add_group(self, label: str) -> QTreeWidgetItem:
        heading = QTreeWidgetItem(self.tree, [label])
        heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
        heading.setForeground(0, QColor(C.TEXT_SECONDARY))
        font = self.tree.font()
        font.setBold(True)
        heading.setFont(0, font)
        heading.setSizeHint(0, QSize(0, 25))
        heading.setExpanded(True)
        self._groups.append(heading)
        return heading

    def pause_panel_windows(self) -> None:
        self._panel_windows.pause_all()

    @staticmethod
    def _target_hint(effect_type: str) -> str:
        if effect_type == "spatial_panner":
            return _tr(
                "target_track",
                "Drag onto an audio clip or double-click with one selected to configure its track.",
            )
        if effect_type in TrackMixerWidget._INSERT_EFFECT_TYPES:
            return _tr(
                "targets_clip_channel",
                "Drag onto an audio clip or a mixer channel. Double-click to add to the selected clip.",
            )
        return _tr("target_clip", "Drag onto an audio clip or double-click to add to the selected clip.")

    def _filter(self, query: str) -> None:
        if not self._catalog_loaded:
            return
        terms = query.casefold().split()
        if terms and self._search_expansion is None:
            self._search_expansion = tuple(group.isExpanded() for group in self._groups)
        any_results = False
        for index, group in enumerate(self._groups):
            visible = False
            for row in range(group.childCount()):
                item = group.child(row)
                matches = all(term in item.data(0, _SEARCH_ROLE) for term in terms)
                item.setHidden(not matches)
                visible |= matches
            group.setHidden(not visible)
            any_results |= visible
            if terms:
                group.setExpanded(visible)
            elif self._search_expansion is not None:
                group.setExpanded(self._search_expansion[index])
        if not terms:
            self._search_expansion = None
        current = self.tree.currentItem()
        if current is not None and current.isHidden():
            self.tree.setCurrentItem(None)
        if not any_results:
            self._empty.setText(
                _tr("no_results", "No matching audio effects.") if terms
                else _tr("unavailable", "No audio effects are currently available.")
            )
        self._content.setCurrentWidget(self.tree if any_results else self._empty)

    def _activate_current(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self._activate(item, 0)

    def _activate(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.isHidden():
            return
        effect_type = item.data(0, _EFFECT_ROLE)
        if isinstance(effect_type, str):
            self.effect_activated.emit(effect_type)
            return
        panel_id = item.data(0, _PANEL_ROLE)
        if panel_id == "clip_editor":
            self.effect_activated.emit(panel_id)
        elif isinstance(panel_id, str):
            instance = self._panel_instances.get(panel_id)
            if instance is None:
                instance = EffectInstance.create(panel_id, "effect-library")
                self._panel_instances[panel_id] = instance
            self._panel_windows.open(instance, _tr("library", "Audio Effects"))
