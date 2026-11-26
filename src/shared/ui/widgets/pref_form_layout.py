"""Shared layout for preference pages and their two-column forms."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFormLayout, QLabel, QLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

PREF_PAGE_BACKGROUND = "#27272b"
PREF_PAGE_MARGINS = (20, 18, 20, 18)
PREF_LABEL_WIDTH = 220
PREF_COLUMN_SPACING = 12
PREF_FORM_MARGINS = (16, 16, 16, 14)
PREF_FORM_SPACING = 8


class _PreferenceForm(QFormLayout):
    def addRow(self, *args: str | QWidget | QLayout) -> None:
        if len(args) == 2 and isinstance(args[1], QLabel) and args[1].wordWrap():
            field = args[1]
            policy = field.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            field.setSizePolicy(policy)
        # A checkbox-only section needs the same label column as a mixed form.
        if len(args) == 2 and args[0] == "":
            super().addRow(make_pref_label(""), args[1])
        else:
            super().addRow(*args)


def make_pref_form(
    parent: QWidget | None = None,
    *,
    margins: tuple[int, int, int, int] = PREF_FORM_MARGINS,
    spacing: int = PREF_FORM_SPACING,
) -> QFormLayout:
    """Keep the same field origin across groups and pages."""
    form = _PreferenceForm(parent)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setContentsMargins(*margins)
    form.setHorizontalSpacing(PREF_COLUMN_SPACING)
    form.setVerticalSpacing(spacing)
    return form


def make_pref_label(text: str, tooltip: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("PrefFormLabel")
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    label.setWordWrap(True)
    label.setFixedWidth(PREF_LABEL_WIDTH)
    if tooltip:
        label.setToolTip(tooltip)
    return label


def make_pref_page(parent: QWidget | None = None) -> tuple[QScrollArea, QVBoxLayout]:
    scroll = QScrollArea(parent)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    content = QWidget()
    content.setObjectName("PrefPageContent")
    scroll.setWidget(content)

    layout = QVBoxLayout(content)
    layout.setContentsMargins(*PREF_PAGE_MARGINS)
    layout.setSpacing(14)
    return scroll, layout
