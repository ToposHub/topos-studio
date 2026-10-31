"""Tests for the X/Y parameter-pairing layout helper.

``group_xy_pairs`` decides which numeric parameters share a row (X/Y on one line,
Fusion-style) vs. stand alone. It's a pure function in node_properties_panel so it
can be tested without instantiating the panel.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from src.features.composite.ui.node_properties_panel import group_xy_pairs


def _pd(ptype="float", group=None):
    return SimpleNamespace(ptype=ptype, group=group)


# ── Transform ordering ───────────────────────────────────────────────────────

def test_transform_keys_pair_translate_scale_pivot():
    keys = [
        "translate_x", "translate_y",
        "scale_x", "scale_y",
        "rotation",
        "pivot_mode",
        "pivot_x", "pivot_y",
        "filter",
    ]
    out = group_xy_pairs(keys)
    assert out == [
        ("pair", "translate", "translate_x", "translate_y"),
        ("pair", "scale", "scale_x", "scale_y"),
        ("single", "rotation"),
        ("single", "pivot_mode"),
        ("pair", "pivot", "pivot_x", "pivot_y"),
        ("single", "filter"),
    ]


# ── edge cases ───────────────────────────────────────────────────────────────

def test_unpaired_trailing_x_stays_single():
    """A *_x with no following *_y is its own row."""
    keys = ["alpha_x", "rotation"]
    out = group_xy_pairs(keys)
    assert out == [("single", "alpha_x"), ("single", "rotation")]


def test_non_consecutive_x_y_do_not_pair():
    keys = ["a_x", "middle", "a_y"]
    out = group_xy_pairs(keys)
    assert out == [("single", "a_x"), ("single", "middle"), ("single", "a_y")]


def test_different_base_names_do_not_pair():
    keys = ["a_x", "b_y"]
    out = group_xy_pairs(keys)
    assert out == [("single", "a_x"), ("single", "b_y")]


def test_non_float_pair_does_not_pair():
    """If either side isn't a float param, no pair (e.g. an option _x)."""
    keys = ["mode_x", "mode_y"]
    defs = {"mode_x": _pd("option"), "mode_y": _pd("option")}
    out = group_xy_pairs(keys, defs)
    assert out == [("single", "mode_x"), ("single", "mode_y")]


def test_pair_respects_group_boundary():
    """_x and _y in different groups must NOT pair (group headers separate them)."""
    keys = ["start_x", "start_y"]
    defs = {"start_x": _pd("float", group="A"), "start_y": _pd("float", group="B")}
    out = group_xy_pairs(keys, defs)
    assert out == [("single", "start_x"), ("single", "start_y")]


def test_pair_allowed_within_same_group():
    keys = ["start_x", "start_y"]
    defs = {"start_x": _pd("float", group="A"), "start_y": _pd("float", group="A")}
    out = group_xy_pairs(keys, defs)
    assert out == [("pair", "start", "start_x", "start_y")]


def test_no_defs_pairs_by_suffix_only():
    """Without param_defs, pairing is by suffix alone (assume float)."""
    keys = ["foo_x", "foo_y", "bar"]
    out = group_xy_pairs(keys)
    assert out == [("pair", "foo", "foo_x", "foo_y"), ("single", "bar")]


def test_three_xs_pair_first_two_leave_third():
    # 三个连续 _x/_y/_z 现在合并成 triple（XYZ 三合一），而非 pair + single。
    keys = ["w_x", "w_y", "w_z"]
    out = group_xy_pairs(keys)
    assert out == [("triple", "w", "w_x", "w_y", "w_z")]


# ── Integration: a real Transform section built through the panel ────────────

import pytest  # noqa: E402


@pytest.fixture
def transform_section(qapp):
    from src.features.composite.ui.node_properties_panel import (
        NodePropertiesPanel, NodeSectionWidget)
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.ui.widgets.drag_value import DragValue

    panel = NodePropertiesPanel()
    node = TransformNode()
    pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
    params = {pd.name: pd.default for pd in node.PARAM_DEFS}
    section = NodeSectionWidget(
        node_id="n1", node_type="transform", node_title="Transform",
        parameters=params, param_options={}, param_defs=pdefs,
        enabled=True, node_color="#666", parent_panel=panel)
    return section, DragValue


@pytest.fixture
def transform_panel(qapp):
    """A panel + section with the section registered in ``panel._sections`` so
    the Separate/Combine rebuild path (which looks up the section by node id)
    works end-to-end."""
    from src.features.composite.ui.node_properties_panel import (
        NodePropertiesPanel, NodeSectionWidget)
    from src.features.composite.nodes.transform_node import TransformNode

    panel = NodePropertiesPanel()
    node = TransformNode()
    pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
    params = {pd.name: pd.default for pd in node.PARAM_DEFS}
    section = NodeSectionWidget(
        node_id="n1", node_type="transform", node_title="Transform",
        parameters=params, param_options={}, param_defs=pdefs,
        enabled=True, node_color="#666", parent_panel=panel)
    panel._sections["n1"] = section
    return panel, section


def test_transform_section_uses_drag_values(transform_section):
    section, DragValue = transform_section
    for k in ("translate_x", "translate_y", "scale_x", "scale_y",
              "pivot_x", "pivot_y", "rotation"):
        assert k in section._param_widgets, f"{k} missing from panel widgets"
        assert isinstance(section._param_widgets[k]["widget"], DragValue), (
            f"{k} should be a DragValue, got {type(section._param_widgets[k]['widget'])}")


def _chain_button(section, base):
    from PyQt6.QtWidgets import QToolButton
    # The chain button is the only checkable QToolButton whose row holds the
    # scale_x/scale_y drag values. Find it by scanning the section's buttons.
    btns = section.findChildren(QToolButton)
    pairs = {
        "scale": ("scale_x", "scale_y"),
        "translate": ("translate_x", "translate_y"),
        "pivot": ("pivot_x", "pivot_y"),
    }
    xk, yk = pairs[base]
    drag_x = section._param_widgets[xk]["widget"]
    # The chain button is a sibling QToolButton in the same row widget as drag_x.
    # Identify it by the dynamic `link_button` property (set by
    # icon_manager.make_link_button); fall back to the legacy ⛓ text for
    # older code paths.
    row = drag_x.parentWidget()
    def _is_link(b):
        if not b.isCheckable():
            return False
        return b.property("link_button") is True or b.text() == "⛓"
    cand = [b for b in btns if b.parentWidget() is row and _is_link(b)]
    return cand[0] if cand else None


def test_chain_only_on_scale(transform_section):
    """Only scale gets a link button (default on); translate/pivot have none."""
    section, _ = transform_section
    scale_chain = _chain_button(section, "scale")
    assert scale_chain is not None and scale_chain.isChecked(), "scale chain must exist and default ON"
    assert _chain_button(section, "translate") is None, "translate must NOT have a chain"
    assert _chain_button(section, "pivot") is None, "pivot must NOT have a chain"


def test_chain_links_x_to_y(transform_section):
    section, _ = transform_section
    drag_x = section._param_widgets["scale_x"]["widget"]
    drag_y = section._param_widgets["scale_y"]["widget"]
    assert drag_y.value() == 1.0
    drag_x.setValue(2.0)            # chain is on by default → Y follows
    assert drag_y.value() == 2.0


def test_unlinked_does_not_propagate(transform_section):
    section, _ = transform_section
    chain = _chain_button(section, "scale")
    chain.setChecked(False)
    drag_x = section._param_widgets["scale_x"]["widget"]
    drag_y = section._param_widgets["scale_y"]["widget"]
    drag_x.setValue(3.0)
    assert drag_y.value() == 1.0   # unchanged after unlink


def test_keyframe_buttons_on_numeric_axes(transform_section):
    """Every numeric parameter (single + each X/Y axis) has its ◆ keyframe button."""
    section, _ = transform_section
    for k in ("translate_x", "translate_y", "scale_x", "scale_y",
              "pivot_x", "pivot_y", "rotation"):
        info = section._param_widgets[k]
        assert "keyframe_btn" in info, f"{k} is missing its keyframe button"


def test_all_numeric_rows_register_sliders(transform_section):
    """Sliders are restored on every numeric row (compact pair/single model)."""
    section, _ = transform_section
    # rotation (single) → _slidable_sliders; translate/scale/pivot XY pairs
    # (compact rows since 9b9be8fa) → _pair_sliders. 1 + 3×2 = 7 sliders.
    # A5 added shutter / samples / shutter_offset under Motion Blur → +3.
    total = (len(section._slidable_sliders) + len(section._pair_sliders)
             + len(section._triple_sliders))
    assert total == 10
    for k in ("scale_x", "scale_y", "rotation"):
        assert "slider" in section._param_widgets[k], f"{k} should have a slider"


def test_sliders_use_global_style(transform_section):
    """Sliders must use the global stylesheet (ToposClickableSlider, no inline QSS)
    so they match the color-page sliders."""
    from src.shared.ui.widgets.topos_controls import ToposClickableSlider
    section, _ = transform_section
    for slider in (list(section._slidable_sliders) + list(section._pair_sliders)):
        assert isinstance(slider, ToposClickableSlider)
        assert slider.styleSheet() == "", "slider must not override the global QSS"


def test_slider_visibility_toggles_by_width(transform_section):
    """Single sliders always show; XY-pair sliders toggle with panel width."""
    section, _ = transform_section
    single = section._slidable_sliders[0]
    pair = section._pair_sliders[0]
    section.resize(200, 200)       # narrow → pair hidden, single stays
    section._apply_slider_visibility()
    assert pair.isVisibleTo(section) is False
    assert single.isVisibleTo(section) is True
    section.resize(500, 200)       # wide → both shown
    section._apply_slider_visibility()
    assert pair.isVisibleTo(section) is True
    assert single.isVisibleTo(section) is True


# ── AE-style: one keyframe per X/Y pair + Separate/Combine dimensions ────────


def _kf_buttons(row):
    from PyQt6.QtWidgets import QToolButton
    return [b for b in row.findChildren(QToolButton) if b.text() == "◆"]


def test_xy_pair_has_single_keyframe_button(transform_panel):
    """An X/Y pair shares ONE ◆ button (AE: a multi-dim property = one stopwatch)."""
    panel, section = transform_panel
    drag_x = section._param_widgets["scale_x"]["widget"]
    row = drag_x.parentWidget()
    assert len(_kf_buttons(row)) == 1
    # the same button is reached from either axis
    assert (section._param_widgets["scale_x"]["keyframe_btn"]
            is section._param_widgets["scale_y"]["keyframe_btn"])


def test_pair_keyframe_button_mirrors_to_both_axes(transform_panel):
    """Applying keyframes updates both axes and the shared button."""
    panel, section = transform_panel
    pdef_x = section.param_defs["scale_x"]
    pdef_y = section.param_defs["scale_y"]
    btn = section._param_widgets["scale_x"]["keyframe_btn"]
    assert btn.isChecked() is False
    panel._current_node_id = "n1"
    kfs = [{"frame": 0, "value": 1.0}, {"frame": 10, "value": 2.0}]
    panel._on_keyframes_changed("scale_x", pdef_x, kfs)
    panel._on_keyframes_changed("scale_y", pdef_y, kfs)
    assert pdef_x.animated and pdef_x.keyframes == kfs
    assert pdef_y.animated and pdef_y.keyframes == kfs
    assert btn.isChecked() is True


def test_separate_dims_splits_pair_into_two_rows(transform_panel):
    """Right-click 'Separate' rebuilds the pair as two single rows."""
    panel, section = transform_panel
    dx = section._param_widgets["scale_x"]["widget"]
    dy = section._param_widgets["scale_y"]["widget"]
    assert dx.parentWidget() is dy.parentWidget()      # combined initially

    panel._toggle_dim_split("n1", "scale", True)

    dx = section._param_widgets["scale_x"]["widget"]
    dy = section._param_widgets["scale_y"]["widget"]
    assert dx.parentWidget() is not dy.parentWidget()  # now two rows
    # each half gets its own keyframe button
    assert (section._param_widgets["scale_x"]["keyframe_btn"]
            is not section._param_widgets["scale_y"]["keyframe_btn"])
    assert len(_kf_buttons(dx.parentWidget())) == 1
    assert len(_kf_buttons(dy.parentWidget())) == 1
    # other pairs stay combined
    tx = section._param_widgets["translate_x"]["widget"]
    ty = section._param_widgets["translate_y"]["widget"]
    assert tx.parentWidget() is ty.parentWidget()


def test_combine_dims_merges_back(transform_panel):
    """'Combine' rejoins the two halves onto one row with a shared keyframe."""
    panel, section = transform_panel
    panel._toggle_dim_split("n1", "scale", True)
    panel._toggle_dim_split("n1", "scale", False)
    dx = section._param_widgets["scale_x"]["widget"]
    dy = section._param_widgets["scale_y"]["widget"]
    assert dx.parentWidget() is dy.parentWidget()
    assert (section._param_widgets["scale_x"]["keyframe_btn"]
            is section._param_widgets["scale_y"]["keyframe_btn"])


def test_split_state_persists_on_panel(transform_panel):
    """Split state lives on the panel keyed by node id and survives rebuilds."""
    panel, section = transform_panel
    assert panel.is_dim_split("n1", "scale") is False
    panel.set_dim_split("n1", "translate", True)
    assert panel.is_dim_split("n1", "translate") is True
    assert panel.is_dim_split("n1", "scale") is False
    # emptying the set cleans up the node entry
    panel.set_dim_split("n1", "translate", False)
    assert "n1" not in panel._split_dims


def test_param_label_has_context_menu_builder(transform_panel):
    """The param-name label carries a right-click menu builder so the user can
    Separate (combined) or Combine (split half) from the label."""
    from src.features.composite.ui.node_properties_panel import _ParamLabel
    panel, section = transform_panel

    # combined pair → label offers 'Separate'
    drag_x = section._param_widgets["scale_x"]["widget"]
    label = drag_x.parentWidget().findChild(_ParamLabel)
    assert label is not None and label._context_menu_builder is not None

    # split half → label offers 'Combine'
    panel._toggle_dim_split("n1", "scale", True)
    drag_x = section._param_widgets["scale_x"]["widget"]
    label = drag_x.parentWidget().findChild(_ParamLabel)
    assert label is not None and label._context_menu_builder is not None
