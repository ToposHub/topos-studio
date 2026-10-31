"""Deliver module's Export preference page.

Self-contained preference page for export-related settings:
- Default export format, quality, path
- Metadata preservation, color profile, ICC embedding
- Post-export behavior

This page is only shown when the Deliver feature is enabled.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QCheckBox, QLineEdit,
    QScrollArea, QGroupBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.widgets.pref_form_layout import make_pref_form, make_pref_label
from src.shared.export.export_panel import (
    _apply_text_input_style,
    _apply_compact_button_style,
)


class ExportPreferencePage:
    """Deliver module's Export preference page.

    Manages widgets: default_export_format, default_export_quality,
    default_export_path, preserve_metadata, export_color_profile,
    embed_icc_profile, open_folder_after_export.
    """

    page_id = "deliver.export"

    def __init__(self):
        self._widgets: dict[str, QWidget] = {}
        self._dialog_ref: Any = None  # Weak reference to PreferencesDialog for _browse_path

    # ── Widget Creation ─────────────────────────────────────────────

    def create_widget(self, parent: QWidget) -> QWidget:
        """Create the Export preference page UI."""
        self._dialog_ref = parent

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("PrefPageContent")
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        group = QGroupBox(I18N.tr("preferences.export.section_title",
                                    "Default Export Settings"))
        group.setObjectName("PrefSettingsGroup")
        form = make_pref_form(group)

        # Default format
        fmt_combo = ChevronComboBox()
        fmt_combo.setMinimumWidth(120)
        formats = ["JPEG", "PNG", "TIFF", "BMP", "WebP"]
        for fmt in formats:
            fmt_combo.addItem(fmt)
        self._widgets['default_export_format'] = fmt_combo
        fmt_label = make_pref_label(I18N.tr("preferences.export.default_format",
                                             "Default Format:"))
        form.addRow(fmt_label, fmt_combo)

        # Default quality
        quality_widget = QWidget()
        quality_widget.setStyleSheet("background: transparent;")
        quality_layout = QHBoxLayout(quality_widget)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        quality_layout.setSpacing(8)
        quality_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        quality_slider.setRange(1, 100)
        quality_slider.setObjectName("PrefSlider")
        quality_value = QLabel("95")
        quality_value.setObjectName("PrefSliderValue")
        quality_value.setFixedWidth(32)
        mono_font = QFont("Menlo", 11)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier", 11)
        quality_value.setFont(mono_font)
        quality_slider.valueChanged.connect(
            lambda v: quality_value.setText(str(v))
        )
        quality_layout.addWidget(quality_slider, 1)
        quality_layout.addWidget(quality_value)
        self._widgets['default_export_quality'] = quality_slider
        q_label = make_pref_label(I18N.tr("preferences.export.default_quality",
                                           "Default Quality:"))
        form.addRow(q_label, quality_widget)

        # Default export path
        path_widget = QWidget()
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)
        path_edit = QLineEdit()
        path_edit.setObjectName("PrefPathEdit")
        path_edit.setPlaceholderText(
            I18N.tr("export.select_dir_placeholder", "Select export directory...")
        )
        _apply_text_input_style(path_edit)
        browse_btn = QPushButton(
            I18N.tr("preferences.export.browse", "Browse")
        )
        browse_btn.setObjectName("PrefBrowseBtn")
        browse_btn.setFixedWidth(80)
        _apply_compact_button_style(browse_btn)
        browse_btn.clicked.connect(
            lambda: self._browse_path(path_edit, "Select Export Directory")
        )
        path_layout.addWidget(path_edit, 1)
        path_layout.addWidget(browse_btn)
        self._widgets['default_export_path'] = path_edit
        path_label = make_pref_label(I18N.tr("preferences.export.default_path",
                                              "Default Export Path:"))
        form.addRow(path_label, path_widget)

        # Preserve metadata
        preserve = QCheckBox(
            I18N.tr("preferences.export.preserve_metadata",
                    "Preserve metadata")
        )
        self._widgets['preserve_metadata'] = preserve
        form.addRow("", preserve)

        # Export color profile
        profile_combo = ChevronComboBox()
        profile_combo.setMinimumWidth(120)
        profiles = ["sRGB", "Adobe RGB", "Rec.709", "DCI-P3", "None"]
        for profile in profiles:
            profile_combo.addItem(profile)
        self._widgets['export_color_profile'] = profile_combo
        profile_label = make_pref_label(I18N.tr("preferences.export.color_profile",
                                                 "Export Color Profile:"))
        form.addRow(profile_label, profile_combo)

        # Embed ICC profile
        embed_icc = QCheckBox(
            I18N.tr("preferences.export.embed_icc",
                    "Embed ICC profile in exported file")
        )
        self._widgets['embed_icc_profile'] = embed_icc
        form.addRow("", embed_icc)

        # Open folder after export
        open_folder = QCheckBox(
            I18N.tr("preferences.export.open_folder_after",
                    "Open folder after export")
        )
        self._widgets['open_folder_after_export'] = open_folder
        form.addRow("", open_folder)

        layout.addWidget(group)
        layout.addStretch()
        return scroll

    # ── Load / Apply / Defaults ──────────────────────────────────────

    def load(self, config: dict) -> None:
        """Load export settings from config into widgets."""
        if 'default_export_format' not in self._widgets:
            return

        fmt = config.get('default_export_format', 'JPEG')
        fmt_combo = self._widgets['default_export_format']
        idx = fmt_combo.findText(fmt)
        if idx >= 0:
            fmt_combo.setCurrentIndex(idx)

        self._widgets['default_export_quality'].setValue(
            config.get('default_export_quality', 95)
        )
        self._widgets['default_export_path'].setText(
            config.get('default_export_path', '')
        )
        self._widgets['preserve_metadata'].setChecked(
            config.get('preserve_metadata', True)
        )

        profile = config.get('export_color_profile', 'sRGB')
        profile_combo = self._widgets['export_color_profile']
        idx = profile_combo.findText(profile)
        if idx >= 0:
            profile_combo.setCurrentIndex(idx)

        self._widgets['embed_icc_profile'].setChecked(
            config.get('embed_icc_profile', True)
        )
        self._widgets['open_folder_after_export'].setChecked(
            config.get('open_folder_after_export', False)
        )

    def apply(self, config_manager: Any) -> None:
        """Read widget values and write to config manager."""
        if 'default_export_format' not in self._widgets:
            return

        config_manager.set('default_export_format',
                           self._widgets['default_export_format'].currentText(),
                           auto_save=False)
        config_manager.set('default_export_quality',
                           self._widgets['default_export_quality'].value(),
                           auto_save=False)
        config_manager.set('default_export_path',
                           self._widgets['default_export_path'].text(),
                           auto_save=False)
        config_manager.set('preserve_metadata',
                           self._widgets['preserve_metadata'].isChecked(),
                           auto_save=False)
        config_manager.set('export_color_profile',
                           self._widgets['export_color_profile'].currentText(),
                           auto_save=False)
        config_manager.set('embed_icc_profile',
                           self._widgets['embed_icc_profile'].isChecked(),
                           auto_save=False)
        config_manager.set('open_folder_after_export',
                           self._widgets['open_folder_after_export'].isChecked(),
                           auto_save=False)

    def restore_defaults(self, defaults: dict) -> None:
        """Reset export widgets to default values."""
        if 'default_export_format' not in self._widgets:
            return

        fmt_combo = self._widgets['default_export_format']
        idx = fmt_combo.findText(defaults['default_export_format'])
        if idx >= 0:
            fmt_combo.setCurrentIndex(idx)

        self._widgets['default_export_quality'].setValue(
            defaults['default_export_quality']
        )
        self._widgets['default_export_path'].setText(
            defaults['default_export_path']
        )
        self._widgets['preserve_metadata'].setChecked(
            defaults['preserve_metadata']
        )

        profile_combo = self._widgets['export_color_profile']
        idx = profile_combo.findText(defaults.get('export_color_profile', 'sRGB'))
        if idx >= 0:
            profile_combo.setCurrentIndex(idx)

        self._widgets['embed_icc_profile'].setChecked(
            defaults.get('embed_icc_profile', True)
        )
        self._widgets['open_folder_after_export'].setChecked(
            defaults.get('open_folder_after_export', False)
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _browse_path(self, line_edit: QLineEdit, title: str) -> None:
        """Open directory picker for path input."""
        from PyQt6.QtWidgets import QFileDialog
        current = line_edit.text()
        path = QFileDialog.getExistingDirectory(
            self._dialog_ref, title, current if current else ""
        )
        if path:
            line_edit.setText(path)
