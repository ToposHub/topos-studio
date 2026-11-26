"""Preference page for the shared native Files and Library display settings."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from PyQt6.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QSpinBox, QWidget

from src.app.ui.native_files_gallery import FILE_CAPTION_FIELDS
from src.app.ui.native_files_prefs import (
    NativeFilesPreferences, THUMBNAIL_WIDTH_DEFAULT, THUMBNAIL_WIDTH_MAX, THUMBNAIL_WIDTH_MIN,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.pref_form_layout import make_pref_form, make_pref_label, make_pref_page


_CAPTION_LABELS = {
    "resolution": "Resolution",
    "size": "File size",
    "tags": "Tags",
    "rating": "Star rating",
}


def _tr(key: str, zh: str, en: str) -> str:
    fallback = zh if I18N.get_current_locale() == "zh_CN" else en
    return I18N.tr_preference(f"preferences.files_browser.{key}", fallback)


@dataclass(frozen=True)
class _BrowserDisplay:
    layout_mode: str = "adaptive"
    density: int = THUMBNAIL_WIDTH_DEFAULT
    grid_fill: bool = False
    sidebar_preference: bool | None = None
    inspector_preference: bool | None = None
    card_metadata: tuple[str, ...] = ()


class FilesBrowserPreferencePage:
    """Edit the same QSettings used by the Files and Library toolbars."""

    page_id = "app.files_browser"

    def __init__(self, preferences: NativeFilesPreferences) -> None:
        self._preferences = preferences
        self._baseline: _BrowserDisplay | None = None
        self._before_apply: dict[str, Any] = {}
        self._last_written: dict[str, Any] = {}

    def create_widget(self, parent: QWidget) -> QWidget:
        page, layout = make_pref_page(parent)
        group = QGroupBox(_tr("view", "浏览视图", "Browser View"))
        group.setObjectName("PrefSettingsGroup")
        form = make_pref_form(group)

        self._layout = ChevronComboBox()
        self._layout.setObjectName("filesBrowserLayout")
        for key, zh, en in (
            ("adaptive", "自适应", "Adaptive"),
            ("masonry", "瀑布流", "Masonry"),
            ("grid", "网格", "Grid"),
            ("list", "列表", "List"),
        ):
            self._layout.addItem(_tr(f"layout.{key}", zh, en), key)
        form.addRow(make_pref_label(_tr("layout.title", "浏览布局", "Browser layout")), self._layout)

        self._density = QSpinBox()
        self._density.setObjectName("filesBrowserThumbnailWidth")
        self._density.setRange(THUMBNAIL_WIDTH_MIN, THUMBNAIL_WIDTH_MAX)
        self._density.setSuffix(" px")
        form.addRow(make_pref_label(_tr("thumbnail_width", "缩略图宽度", "Thumbnail width")), self._density)

        self._grid_fill = ChevronComboBox()
        self._grid_fill.setObjectName("filesBrowserGridFill")
        self._grid_fill.addItem(_tr("grid.fit", "适应", "Fit"), False)
        self._grid_fill.addItem(_tr("grid.fill", "填充", "Fill"), True)
        form.addRow(make_pref_label(_tr("grid.title", "网格缩略图缩放", "Grid thumbnail scaling")), self._grid_fill)

        self._sidebar = self._add_visibility_field(
            form, "filesBrowserSidebar", _tr("sidebar", "位置侧栏", "Locations sidebar")
        )
        self._inspector = self._add_visibility_field(
            form, "filesBrowserInspector", _tr("inspector", "详情栏", "Details panel")
        )
        layout.addWidget(group)

        group = QGroupBox(_tr("card_details", "卡片信息", "Card Details"))
        group.setObjectName("PrefSettingsGroup")
        form = make_pref_form(group)
        self._metadata: dict[str, QCheckBox] = {}
        for key, zh in FILE_CAPTION_FIELDS:
            check = QCheckBox(_tr(f"card.{key}", zh, _CAPTION_LABELS[key]))
            check.setObjectName(f"filesBrowserCard_{key}")
            form.addRow("", check)
            self._metadata[key] = check
        layout.addWidget(group)
        layout.addStretch()
        return page

    @staticmethod
    def _add_visibility_field(form: QFormLayout, name: str, label: str) -> ChevronComboBox:
        combo = ChevronComboBox()
        combo.setObjectName(name)
        combo.setAccessibleName(label)
        combo.addItem(_tr("visibility.auto", "自动", "Automatic"), None)
        combo.addItem(_tr("visibility.show", "显示", "Show"), True)
        combo.addItem(_tr("visibility.hide", "隐藏", "Hide"), False)
        form.addRow(make_pref_label(label), combo)
        return combo

    @staticmethod
    def _select(combo: ChevronComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _stored_values(self) -> _BrowserDisplay:
        prefs = self._preferences
        return _BrowserDisplay(
            layout_mode=prefs.layout_mode,
            density=prefs.density,
            grid_fill=prefs.grid_fill,
            sidebar_preference=prefs.sidebar_preference,
            inspector_preference=prefs.inspector_preference,
            card_metadata=tuple(sorted(prefs.card_metadata)),
        )

    def _show_values(self, values: _BrowserDisplay) -> None:
        self._select(self._layout, values.layout_mode)
        self._density.setValue(values.density)
        self._select(self._grid_fill, values.grid_fill)
        self._select(self._sidebar, values.sidebar_preference)
        self._select(self._inspector, values.inspector_preference)
        for key, check in self._metadata.items():
            check.setChecked(key in values.card_metadata)

    def _form_values(self) -> _BrowserDisplay:
        return _BrowserDisplay(
            layout_mode=self._layout.currentData(),
            density=self._density.value(),
            grid_fill=self._grid_fill.currentData(),
            sidebar_preference=self._sidebar.currentData(),
            inspector_preference=self._inspector.currentData(),
            card_metadata=tuple(sorted(key for key, check in self._metadata.items() if check.isChecked())),
        )

    def load(self, config: Any) -> None:
        if not hasattr(self, "_layout"):
            return
        self._show_values(self._stored_values())
        self._baseline = self._form_values()

    def apply(self, config_manager: Any) -> None:
        if self._baseline is None:
            return
        baseline = asdict(self._baseline)
        current = asdict(self._stored_values())
        for key, value in asdict(self._form_values()).items():
            if value == current[key] or (value == baseline[key] and key not in self._before_apply):
                continue
            self._before_apply.setdefault(key, current[key])
            self._last_written[key] = value
            self._set_preference(key, value)

    def restore_defaults(self, defaults: dict) -> None:
        if hasattr(self, "_layout"):
            self._show_values(_BrowserDisplay())

    def rollback(self) -> None:
        current = asdict(self._stored_values())
        for key, value in self._before_apply.items():
            if key in self._last_written and current[key] == self._last_written[key]:
                self._set_preference(key, value)
        self._before_apply.clear()
        self._last_written.clear()

    def _set_preference(self, key: str, value: Any) -> None:
        if key == "card_metadata":
            self._preferences.card_metadata = list(value)
        else:
            setattr(self._preferences, key, value)
