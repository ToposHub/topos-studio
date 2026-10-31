"""Properties-style inspector for an editable HTML Motion Graphic layer."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C, EDIT_PROPERTIES_ROW_STYLE, EDIT_PROPERTIES_SECTION_CONTENT_STYLE,
    EDIT_PROPERTIES_SECTION_DIVIDER_STYLE, EDIT_PROPERTIES_SECTION_HEADER_STYLE,
    EDIT_PROPERTIES_SECTION_STYLE, EDIT_PROPERTIES_SECTION_TITLE_STYLE,
)
from src.shared.ui.widgets import ChevronComboBox, ColorPickerControl, ParamSliderRow
from src.shared.ui.widgets.color_picker_control import qcolor_to_linear_rgba

from ..core.html_layer import FreeformHtmlMotionLayer


_VALUE_FIXED_WIDTH = 64
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class HtmlInspector(QWidget):
    """Declared HTML fields using the clip Properties control vocabulary.

    It only emits the existing patch intents. Therefore changes still flow
    through ``ApplyHtmlPatchCommand`` and preserve undo, preview and project
    persistence rather than becoming UI-local state.
    """

    editRequested = pyqtSignal(str, str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layer: FreeformHtmlMotionLayer | None = None
        self._updating = False
        self._node_fields: dict[str, QLineEdit] = {}
        self._persistent_sections: list[QWidget] = []
        self._form_host = QWidget(self)
        self._form_layout = QVBoxLayout(self._form_host)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._form_layout.setSpacing(0)
        self._form_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidget(self._form_host)
        scroll.setWidgetResizable(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def add_persistent_section(self, section: QWidget) -> None:
        """Append a non-token parameter group after the generated HTML fields.

        Page timing is a scene parameter rather than a token, but it should
        live in the same scrollable Properties flow.  Persistent sections are
        deliberately kept while ``set_layer`` rebuilds only generated fields.
        """
        section.setParent(self._form_host)
        self._persistent_sections.append(section)
        self._form_layout.insertWidget(self._form_layout.count() - 1, section)

    def set_layer(self, layer: FreeformHtmlMotionLayer | None) -> None:
        # ``MotionGraphicEditorWindow._refresh_scene`` populates this inspector
        # before selecting the synthetic HTML-layer row.  That selection then
        # arrives here once more with the *same* immutable layer.  Rebuilding
        # twice in the same Qt event turn used to delete newly-created combo
        # boxes with ``deleteLater`` before their deferred Polish event ran;
        # macOS subsequently crashed inside QComboBox::event.  A changed edit
        # always produces a new layer instance, so identity is the precise
        # no-op check and retains normal refresh behaviour.
        if layer is self._layer:
            return
        self._layer = layer
        self._node_fields = {}
        self._updating = True
        try:
            self._clear_form()
            if layer is None:
                return
            text_slots = [slot for slot in layer.editable_slots if slot.kind == "text"]
            if text_slots:
                content = self._add_section(I18N.tr("motion_graphic.sections.text", "Text"))
                for slot in text_slots:
                    self._add_text_row(content, slot.label or slot.node_id, slot.node_id,
                                       "replace_text")
            if layer.editable_tokens:
                content = self._add_section(I18N.tr("motion_graphic.sections.color_tokens", "Color / Token"))
                specs = self._token_specs(layer)
                for token in layer.editable_tokens:
                    self._add_token_row(content, token, self._token_initial_value(layer, token),
                                        specs.get(token, {}))
        finally:
            self._updating = False

    def select_node(self, node_id: str) -> bool:
        field = self._node_fields.get(str(node_id))
        if field is None:
            return False
        field.setFocus(Qt.FocusReason.OtherFocusReason)
        field.selectAll()
        return True

    # Properties sections: copied from the clip Properties visual hierarchy,
    # but without clip-specific enable/reset state.
    def _add_section(self, title: str) -> QVBoxLayout:
        section = QFrame(self._form_host)
        section.setObjectName("editPropertiesSection")
        section.setStyleSheet(EDIT_PROPERTIES_SECTION_STYLE)
        outer = QVBoxLayout(section)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        for _ in range(1):
            divider = QFrame(section)
            divider.setFixedHeight(2)
            divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
            outer.addWidget(divider)
        header = QWidget(section)
        header.setObjectName("editPropertiesSectionHeader")
        header.setStyleSheet(EDIT_PROPERTIES_SECTION_HEADER_STYLE)
        header.setFixedHeight(24)
        h = QHBoxLayout(header)
        h.setContentsMargins(7, 0, 7, 0)
        h.setSpacing(4)
        collapse = QToolButton(header)
        collapse.setText("▼")
        collapse.setFixedSize(18, 18)
        collapse.setStyleSheet("QToolButton { border: none; color: #9a9a9a; background: transparent; }")
        dot = QLabel("●", header)
        dot.setStyleSheet(f"color: {C.ORANGE}; font-size: 11px;")
        label = QLabel(title, header)
        label.setStyleSheet(EDIT_PROPERTIES_SECTION_TITLE_STYLE)
        h.addWidget(collapse)
        h.addWidget(dot)
        h.addWidget(label)
        h.addStretch(1)
        outer.addWidget(header)
        divider = QFrame(section)
        divider.setFixedHeight(2)
        divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        outer.addWidget(divider)
        content = QWidget(section)
        content.setObjectName("editPropertiesSectionContent")
        content.setStyleSheet(EDIT_PROPERTIES_SECTION_CONTENT_STYLE)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 5, 8, 5)
        content_layout.setSpacing(3)
        outer.addWidget(content)
        collapse.clicked.connect(lambda: self._toggle_section(content, collapse))
        first_persistent = (
            self._form_layout.indexOf(self._persistent_sections[0])
            if self._persistent_sections else self._form_layout.count() - 1
        )
        self._form_layout.insertWidget(first_persistent, section)
        return content_layout

    @staticmethod
    def _toggle_section(content: QWidget, button: QToolButton) -> None:
        content.setVisible(not content.isVisible())
        button.setText("▼" if content.isVisible() else "▶")

    def _make_row(self) -> tuple[QWidget, QHBoxLayout]:
        row = QWidget(self._form_host)
        row.setObjectName("editPropertiesRow")
        row.setStyleSheet(EDIT_PROPERTIES_ROW_STYLE)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)
        return row, layout

    @staticmethod
    def _label(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(90)
        return label

    def _add_text_row(self, content: QVBoxLayout, label: str, target: str,
                      operation: str, initial: str = "") -> None:
        row, layout = self._make_row()
        layout.addWidget(self._label(label, row))
        field = QLineEdit(initial, row)
        field.setObjectName(f"htmlInspectorField:{target}")
        field.setToolTip(target)
        if operation == "replace_text":
            self._node_fields[target] = field
        field.editingFinished.connect(
            lambda f=field, op=operation, tgt=target: self._emit(op, tgt, f.text()))
        layout.addWidget(field, 1)
        content.addWidget(row)

    def _add_token_row(self, content: QVBoxLayout, token: str, value: str,
                       spec: Mapping[str, Any]) -> None:
        kind = str(spec.get("type", "")).lower()
        label = token.replace("_", " ").replace("-", " ").title()
        if kind == "color" or (not kind and _HEX_COLOR_RE.fullmatch(value.strip())):
            self._add_color_row(content, label, token, value)
        elif kind in {"integer", "number"} or (
            not kind and re.fullmatch(r"[-+]?(?:\d+|\d*\.\d+)", value.strip())
        ):
            inferred_kind = kind or ("integer" if re.fullmatch(r"[-+]?\d+", value.strip()) else "number")
            self._add_numeric_row(content, label, token, value, inferred_kind)
        elif kind in {"boolean", "bool"}:
            self._add_bool_row(content, label, token, value)
        elif spec.get("enum"):
            self._add_choice_row(content, label, token, value, spec)
        else:
            self._add_text_row(content, label, token, "set_token", value)

    def _add_color_row(self, content: QVBoxLayout, label: str, token: str, value: str) -> None:
        color = QColor(value.strip())
        if not color.isValid():
            self._add_text_row(content, label, token, "set_token", value)
            return
        row, layout = self._make_row()
        layout.addWidget(self._label(label, row))
        # ColorPickerControl is the shared Solid Color/Project Settings picker:
        # it supplies the same Color Studio plus the screen eyedropper.
        picker = ColorPickerControl(qcolor_to_linear_rgba(color), row)
        picker.color_changed.connect(
            lambda selected, target=token: self._emit(
                "set_token", target, selected.name(QColor.NameFormat.HexRgb)))
        layout.addWidget(picker)
        layout.addStretch(1)
        content.addWidget(row)

    @staticmethod
    def _ui_number(ui: Mapping[str, Any], key: str, fallback: float) -> float:
        """Read one numeric ``parameter_ui`` field, degrading malformed data.

        AUD-30640: ``parameter_ui`` originates from template/AI-authored layer
        metadata.  A ``"min": "abc"`` or ``"step": None`` entry must fall back
        to the declared default instead of raising ValueError/TypeError
        through ``set_layer`` → the window's scene refresh.  ``bool`` is
        rejected on purpose (an ``int`` subclass, but never a real bound) and
        so are non-finite values.
        """
        raw = ui.get(key, fallback)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return float(fallback)
        value = float(raw)
        if not math.isfinite(value):
            return float(fallback)
        return value

    @staticmethod
    def _ui_int(ui: Mapping[str, Any], key: str, fallback: int) -> int:
        """Read one integer ``parameter_ui`` field with the AUD-30640 guards."""
        raw = ui.get(key, fallback)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return int(fallback)
        value = float(raw)
        if not math.isfinite(value) or value != int(value):
            return int(fallback)
        return max(0, int(value))

    def _add_numeric_row(self, content: QVBoxLayout, label: str, token: str,
                         raw_value: str, kind: str) -> None:
        try:
            value = float(raw_value)
        except ValueError:
            self._add_text_row(content, label, token, "set_token", raw_value)
            return
        ui = self._parameter_ui(token)
        integer = kind == "integer"
        # Schemas provide the exact meaningful limits.  For undeclared tokens,
        # keep a deliberately modest range so a normal horizontal drag remains
        # controllable instead of sweeping through orders of magnitude.
        # AUD-30640: every metadata field is validated before the cast; a
        # malformed template entry degrades to these defaults instead of
        # aborting the whole inspector refresh.
        minimum = self._ui_number(ui, "min", min(0.0, value))
        fallback_maximum = max(100.0 if integer else 2.0, value * 2.0)
        maximum = self._ui_number(ui, "max", fallback_maximum)
        step = self._ui_number(ui, "step", 1.0 if integer else 0.01)
        if step <= 0.0:
            # A zero/negative step would divide by zero two lines below; it is
            # the same malformed-metadata class as a non-numeric one.
            step = 1.0 if integer else 0.01
        decimals = self._ui_int(ui, "decimals", 0 if integer else 2)
        maximum = max(maximum, minimum + step)
        row, layout = self._make_row()
        control = ParamSliderRow(
            label,
            minimum,
            maximum,
            value,
            step=step,
            decimals=decimals,
            label_width=90,
            slider_auto_hide=False,
            value_fixed_width=_VALUE_FIXED_WIDTH,
            parent=row,
        )
        layout.addWidget(control, 1)
        pending_value: list[float | None] = [None]

        def emit_value(number: float) -> None:
            normalized: int | float = int(round(number)) if integer else round(number, decimals)
            self._emit("set_token", token, str(normalized))

        def defer_value(number: float) -> None:
            # Moving a slider produces many valueChanged signals.  Keep the
            # visual control responsive but write exactly once when the user
            # releases it, so one gesture equals one undo step.
            pending_value[0] = float(number)

        def commit_value() -> None:
            if pending_value[0] is None:
                return
            number = pending_value[0]
            pending_value[0] = None
            emit_value(number)

        control.valueChanged.connect(defer_value)
        control.interactionEnded.connect(commit_value)
        content.addWidget(row)

    def _add_bool_row(self, content: QVBoxLayout, label: str, token: str, value: str) -> None:
        row, layout = self._make_row()
        layout.addWidget(self._label(label, row))
        check = QCheckBox(row)
        check.setChecked(value.strip().lower() in {"1", "true", "yes", "on"})
        check.toggled.connect(lambda checked: self._emit("set_token", token, "true" if checked else "false"))
        layout.addWidget(check)
        layout.addStretch(1)
        content.addWidget(row)

    def _add_choice_row(self, content: QVBoxLayout, label: str, token: str,
                        value: str, spec: Mapping[str, Any]) -> None:
        raw_enum = spec.get("enum", ())
        if not isinstance(raw_enum, (list, tuple)):
            # AUD-30640: a non-sequence enum (a plain string would be iterated
            # character by character) degrades to a plain text field instead
            # of producing a broken combo row during the scene refresh.
            self._add_text_row(content, label, token, "set_token", value)
            return
        choices = [str(item) for item in raw_enum]
        row, layout = self._make_row()
        layout.addWidget(self._label(label, row))
        combo = ChevronComboBox(row)
        combo.addItems(choices)
        combo.setCurrentIndex(max(0, combo.findText(value)))
        combo.currentTextChanged.connect(lambda selected: self._emit("set_token", token, selected))
        layout.addWidget(combo, 1)
        content.addWidget(row)

    def _clear_form(self) -> None:
        while self._form_layout.count() > 1:
            first = self._form_layout.itemAt(0)
            if first.widget() in self._persistent_sections:
                break
            item = self._form_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _emit(self, operation: str, target: str, value: str) -> None:
        if not self._updating:
            self.editRequested.emit(operation, target, value)

    @staticmethod
    def _token_initial_value(layer: FreeformHtmlMotionLayer, token: str) -> str:
        match = re.search(rf"--{re.escape(token)}\s*:\s*([^;}}]+)", layer.sanitized_html)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _token_specs(layer: FreeformHtmlMotionLayer) -> dict[str, Mapping[str, Any]]:
        raw = layer.metadata.get("motion_graphic_parameter_schema", ())
        if not isinstance(raw, (list, tuple)):
            return {}
        return {str(item["name"]): item for item in raw if isinstance(item, Mapping) and item.get("name")}

    def _parameter_ui(self, token: str) -> Mapping[str, Any]:
        """Return the token's ``parameter_ui`` entry, or {} for any bad shape.

        Only the Mapping shape is checked here; the numeric fields inside the
        entry are validated when read (:meth:`_ui_number` / :meth:`_ui_int`)
        so malformed template metadata degrades per-field instead of raising
        (AUD-30640).
        """
        if self._layer is None:
            return {}
        raw = self._layer.metadata.get("parameter_ui", {})
        if not isinstance(raw, Mapping):
            return {}
        entry = raw.get(token, {})
        return entry if isinstance(entry, Mapping) else {}
