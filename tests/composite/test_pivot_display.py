"""Tests for the Transform node pivot display in the Properties panel.

The Transform node's rotation center is a *semantic* parameter: when
``pivot_mode="center"`` the renderer uses the image center (w/2, h/2) and the
``pivot_x`` / ``pivot_y`` parameters (default 0.0) are ignored. The panel must
reflect this so the user isn't misled into thinking the pivot is at (0, 0):

- ``pivot_mode="center"`` + known image size → panel shows the image center
  (e.g. 960, 540 for 1920×1080) and the value boxes / sliders are read-only.
- ``pivot_mode="custom"`` → boxes are editable and show the real parameter value.
- No image size (multi-select / not yet rendered) → fallback to the raw param
  value, not disabled (avoids showing a misleading read-only value).

Switching ``pivot_mode`` via the combo (or by dragging the viewer handle, which
writes ``pivot_mode="custom"`` + new pivot) must re-sync the row.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def panel(qapp):
    p = NodePropertiesPanel()
    p.resize(320, 700)
    p.show()
    qapp.processEvents()
    return p


def _transform_data(image_size=None, **param_overrides):
    """Build the data dict set_node expects for a Transform node."""
    node = TransformNode("n1")
    params = dict(node.parameters)
    params.update(param_overrides)
    param_options = node.get_param_options() if hasattr(node, "get_param_options") else {}
    param_defs = node.get_param_defs() if hasattr(node, "get_param_defs") else {}
    return dict(
        node_id="n1", node_type="transform", node_title="Transform",
        parameters=params, param_options=param_options, param_defs=param_defs,
        image_size=image_size,
    )


def _pivot_widgets(panel):
    section = panel._sections["n1"]
    return (
        section,
        section._param_widgets["pivot_x"],
        section._param_widgets["pivot_y"],
    )


# ── center mode shows image center, read-only ───────────────────────────────

def test_center_mode_shows_image_center(panel):
    panel.set_node("n1", "transform", "Transform",
                   dict(TransformNode("n1").parameters),
                   TransformNode("n1").get_param_options(),
                   TransformNode("n1").get_param_defs(),
                   image_size=(1920, 1080))
    panel.show()  # ensure widgets realized for slider visibility logic
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 960.0
    assert wy["widget"].value() == 540.0
    assert wx["widget"].isEnabled() is False
    assert wy["widget"].isEnabled() is False
    assert wx.get("pivot_center_readonly") is True


def test_center_mode_slider_also_disabled(panel):
    panel.set_node(**_transform_data(image_size=(1920, 1080)))
    _, wx, wy = _pivot_widgets(panel)
    # Sliders are created hidden but should still flip to disabled in center mode.
    if "slider" in wx:
        assert wx["slider"].isEnabled() is False
    if "slider" in wy:
        assert wy["slider"].isEnabled() is False


# ── custom mode shows the real param value, editable ────────────────────────

def test_custom_mode_shows_param_value(panel):
    panel.set_node(**_transform_data(
        image_size=(1920, 1080),
        pivot_mode="custom", pivot_x=123.0, pivot_y=456.0,
    ))
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 123.0
    assert wy["widget"].value() == 456.0
    assert wx["widget"].isEnabled() is True
    assert wy["widget"].isEnabled() is True
    assert not wx.get("pivot_center_readonly", False)


# ── fallback when image size unknown ────────────────────────────────────────

def test_center_mode_without_image_size_falls_back(panel):
    """No image size → don't pretend to know the center; show raw param value,
    keep editable so the user isn't locked out."""
    panel.set_node(**_transform_data(image_size=None))  # multi-select path
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 0.0          # raw default param
    assert wx["widget"].isEnabled() is True


def test_center_mode_zero_size_falls_back(panel):
    panel.set_node(**_transform_data(image_size=(0, 0)))  # not yet rendered
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 0.0
    assert wx["widget"].isEnabled() is True


# ── switching pivot_mode via combo re-syncs the row ─────────────────────────

def test_switching_combo_to_custom_enables_and_shows_param(panel):
    panel.set_node(**_transform_data(image_size=(1920, 1080)))  # center, readonly
    section, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 960.0       # starting point: center shown

    # User picks "custom" in the pivot_mode combo.  The panel shows translated
    # display labels ("Center"/"Custom") while `options` keeps the raw keys;
    # selection therefore goes through setCurrentIndex, like a real click.
    mode_info = section._param_widgets["pivot_mode"]
    mode_info["widget"].setCurrentIndex(mode_info["options"].index("custom"))
    panel._on_param_changed  # sanity: handler exists
    # currentIndexChanged wired to _on_param_changed at build time
    assert wx["widget"].isEnabled() is True
    assert wy["widget"].isEnabled() is True
    # Param value was 0.0 (never written), so custom shows 0.0 not the center.
    assert wx["widget"].value() == 0.0


def test_switching_combo_back_to_center_disables_and_shows_center(panel):
    panel.set_node(**_transform_data(
        image_size=(1920, 1080),
        pivot_mode="custom", pivot_x=100.0, pivot_y=200.0,
    ))
    section, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 100.0       # custom shows param value

    # Switch back to center via the combo (index-based, see note above).
    mode_info = section._param_widgets["pivot_mode"]
    mode_info["widget"].setCurrentIndex(mode_info["options"].index("center"))
    assert wx["widget"].value() == 960.0       # now shows image center
    assert wy["widget"].value() == 540.0
    assert wx["widget"].isEnabled() is False


# ── simulating a viewer handle drag (external param update) ─────────────────

def test_viewer_drag_handle_updates_display(panel):
    """Dragging the pivot handle writes pivot_mode=custom + new pivot values
    via CompositePage, which re-calls set_node with the new params."""
    # Start in center mode.
    panel.set_node(**_transform_data(image_size=(1920, 1080)))
    _, wx, _ = _pivot_widgets(panel)
    assert wx["widget"].value() == 960.0

    # Simulate the handle drag: new params pushed back.
    panel.set_node(**_transform_data(
        image_size=(1920, 1080),
        pivot_mode="custom", pivot_x=700.0, pivot_y=300.0,
    ))
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 700.0
    assert wy["widget"].value() == 300.0
    assert wx["widget"].isEnabled() is True


# ── image_size changing alone re-syncs the center display ───────────────────

def test_image_size_change_refreshes_center(panel):
    """If the user loads a different-resolution image, the center value must
    update even though pivot_mode (and the param keys) didn't change."""
    panel.set_node(**_transform_data(image_size=(1920, 1080)))
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 960.0

    # New image: 3840x2160 → center 1920, 1080
    panel.set_node(**_transform_data(image_size=(3840, 2160)))
    _, wx, wy = _pivot_widgets(panel)
    assert wx["widget"].value() == 1920.0
    assert wy["widget"].value() == 1080.0
    assert wx["widget"].isEnabled() is False
