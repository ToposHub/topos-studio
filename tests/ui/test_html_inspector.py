"""FH-06 HTML inspector — text-slot + token editing via patch (Qt widget test)."""

from __future__ import annotations

from src.features.teaching_animation.core.html_layer import (
    HtmlEditSlot,
    build_html_layer,
)
from src.features.teaching_animation.ui.html_inspector import HtmlInspector

# A layer with one text slot ("title") and two CSS tokens (brand, gap).
HTML = (
    '<style>:root{--brand:#06c;--gap:8px}</style>'
    '<div data-topos-node-id="title"><h1>Hello</h1></div>'
)


def _layer():
    return build_html_layer(
        layer_id="L", sanitized_html=HTML, authored_source=HTML,
        viewport_width=320, viewport_height=200, fps_num=24, fps_den=1, duration_frames=6,
        editable_slots=(HtmlEditSlot(node_id="title", edit_mode="content", kind="text", label="Title"),),
        editable_tokens=("brand", "gap"),
    )


def test_html_inspector_builds_fields_for_text_slots_and_tokens(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = HtmlInspector()
    panel.set_layer(_layer())
    # Text tokens remain text fields; the colour token is now a dedicated
    # project colour picker rather than a generic QLineEdit.
    from src.shared.ui.widgets import ColorPickerControl
    assert panel._form_host.findChild(type(panel._node_fields["title"]), "htmlInspectorField:title")
    assert panel._form_host.findChild(ColorPickerControl) is not None
    assert panel._form_host.findChild(type(panel._node_fields["title"]), "htmlInspectorField:gap")


def test_html_inspector_edit_text_slot_emits_replace_text(qapp) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtWidgets import QLineEdit
    panel = HtmlInspector()
    panel.set_layer(_layer())
    received: list[tuple[str, str, str]] = []
    panel.editRequested.connect(lambda op, tgt, val: received.append((op, tgt, val)))
    # The first field is the "title" text slot.
    title_field = panel._node_fields["title"]
    title_field.setText("World")
    title_field.editingFinished.emit()
    assert received == [("replace_text", "title", "World")]


def test_html_inspector_can_focus_browser_selected_node(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = HtmlInspector()
    panel.set_layer(_layer())
    assert panel.select_node("title") is True
    assert panel.select_node("missing") is False


def test_html_inspector_token_field_seed_with_current_value(qapp) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtWidgets import QLineEdit
    panel = HtmlInspector()
    panel.set_layer(_layer())
    gap = panel._form_host.findChild(QLineEdit, "htmlInspectorField:gap")
    from src.shared.ui.widgets import ColorPickerControl
    color = panel._form_host.findChild(ColorPickerControl)
    assert gap is not None and gap.text() == "8px"
    assert color is not None and color.hex_label.text().lower().endswith("0066cc")


def test_html_inspector_set_layer_none_clears_form(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = HtmlInspector()
    panel.set_layer(_layer())
    assert panel._node_fields
    panel.set_layer(None)
    assert not panel._node_fields


def test_html_inspector_does_not_rebuild_an_unchanged_layer(qapp) -> None:  # type: ignore[no-untyped-def]
    """Selection sync must not delete controls awaiting their Qt polish event."""
    panel = HtmlInspector()
    layer = _layer()
    panel.set_layer(layer)
    original_field = panel._node_fields["title"]

    panel.set_layer(layer)

    assert panel._node_fields["title"] is original_field


def test_html_inspector_excludes_page_timing_owned_by_page_panel(qapp) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtWidgets import QLineEdit
    from src.shared.ui.widgets.topos_controls import ToposClickableSlider

    panel = HtmlInspector()
    panel.set_layer(_layer())

    assert panel._form_host.findChild(QLineEdit, "pageDurationsSeconds") is None
    assert panel._form_host.findChild(ToposClickableSlider, "pageDefaultSeconds") is None


def test_html_inspector_uses_properties_controls_for_typed_generator_tokens(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.shared.ui.widgets import ColorPickerControl, DragValue
    from src.shared.ui.widgets.topos_controls import ToposClickableSlider

    html = '<style>:root{--accent:#22d9ff;--motion-speed:0.72}</style>'
    layer = build_html_layer(
        layer_id="typed", sanitized_html=html, authored_source=html,
        viewport_width=320, viewport_height=200, fps_num=24, fps_den=1, duration_frames=24,
        editable_tokens=("accent", "motion-speed"),
        metadata={
            "motion_graphic_parameter_schema": [
                {"name": "accent", "type": "color"},
                {"name": "motion-speed", "type": "number"},
            ],
            "parameter_ui": {"motion-speed": {"min": 0.1, "max": 2.5, "step": 0.01, "decimals": 2}},
        },
    )
    panel = HtmlInspector()
    received: list[tuple[str, str, str]] = []
    panel.editRequested.connect(lambda op, target, value: received.append((op, target, value)))
    panel.set_layer(layer)

    assert panel._form_host.findChild(ColorPickerControl) is not None
    sliders = panel._form_host.findChildren(ToposClickableSlider)
    values = panel._form_host.findChildren(DragValue)
    assert sliders and values
    # Intermediate slider positions only update the visible control.  One
    # completed gesture creates one undoable patch intent.
    sliders[-1].setValue(110)
    assert received == []
    sliders[-1].sliderReleased.emit()
    assert received[-1] == ("set_token", "motion-speed", "1.1")
