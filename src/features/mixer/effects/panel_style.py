"""Apply the application input styles to the supplied effect designs."""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDialog, QFrame, QLabel, QLayout, QLineEdit, QWidget,
)

from src.shared.ui.style_sheet import GLOBAL_LINE_EDIT_STYLE, GLOBAL_SPINBOX_STYLE


def style_panel_inputs(panel: QWidget) -> None:
    for combo in panel.findChildren(QComboBox):
        # Some supplied panels use shorter fixed fields than ChevronComboBox.
        if combo.maximumHeight() < 24:
            combo.setFixedHeight(24)
    for spin in panel.findChildren(QAbstractSpinBox):
        spin.setStyleSheet(GLOBAL_SPINBOX_STYLE)
    for edit in panel.findChildren(QLineEdit):
        if isinstance(edit.parentWidget(), QAbstractSpinBox):
            continue
        compact = edit.parentWidget().metaObject().className() == "EditableValuePill"
        edit.setStyleSheet(GLOBAL_LINE_EDIT_STYLE + ("QLineEdit { padding: 0 3px; }" if compact else ""))


def style_reference_panel(panel: QWidget) -> None:
    # Preserve room for the factory preset, A/B and reset glyphs in the
    # design's fixed-width toolbar buttons.
    panel.setStyleSheet(panel.styleSheet() + "\nQPushButton { padding: 0; }")
    style_panel_inputs(panel)


def fit_effect_window(window: QDialog, panel: QWidget) -> None:
    """Keep the complete effect layout visible as the window is resized."""
    toolbar = panel.findChild(QFrame, "TopBar")
    if toolbar is not None:
        row = toolbar.layout()
        margins = row.contentsMargins()
        row.setContentsMargins(8, margins.top(), 8, margins.bottom())
        row.setSpacing(4)
        for label in toolbar.findChildren(QLabel, options=Qt.FindChildOption.FindDirectChildrenOnly):
            name, separator, _owner = label.text().partition("  ·  ")
            if separator:
                # The dialog's title bar already identifies the track or clip.
                label.setText(name)
                break
        preset = getattr(panel, "preset_combo", None)
        if isinstance(preset, QComboBox):
            preset.setMinimumWidth(132)
            preset.setMaximumWidth(206)
            preset.setMinimumContentsLength(14)
            preset.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            preset.setToolTip(preset.currentText() or preset.placeholderText())
            preset.currentTextChanged.connect(preset.setToolTip)
        toolbar.ensurePolished()
        toolbar.setFixedHeight(max(toolbar.height(), row.minimumSize().height()))

    # Standalone design previews had hard-coded minimums intended for large
    # demo windows. Qt can now compute the real minimum from the visible controls.
    panel.setMinimumSize(0, 0)
    window.ensurePolished()
    layout = window.layout()
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    minimum = layout.minimumSize()
    preferred = layout.sizeHint().expandedTo(minimum + QSize(24, 0))
    available = window.screen().availableSize() - QSize(24, 52)
    window.resize(preferred.boundedTo(available).expandedTo(minimum))
