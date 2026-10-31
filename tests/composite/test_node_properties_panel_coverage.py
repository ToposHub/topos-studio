"""Coverage tests for ``src/features/composite/ui/node_properties_panel.py``.

Plan 02 (test-coverage backfill), agent B2. Focus areas:

- ParamDef → control mapping for every ptype (float/int/bool/option/str/
  color/file/channel/vec2/vec3/readonly) through the real ``set_node`` path.
- Parameter read/write + normalization helpers (``_numeric_meta``,
  ``_value_to_slider``, ``_rgba_to_hex``, ``_normalize_keyframe_value``).
- Multi-selection routing. Review flagged that edits made in a secondary
  node's section were routed to the FIRST selected node (BUG-B2-01 /
  AUD-10700). The routing is now fixed (row builders bind the owning node at
  build time and ``_on_param_changed`` accepts a routed ``node_id``), so the
  former xfail locks below are flipped to positive regression assertions.
- Signal anti-recursion: ``_block_signals`` and the per-widget ``blockSignals``
  refresh path must never re-emit ``parameter_changed``.
- Undo-facing payloads: ``parameter_changed`` / ``keyframes_changed`` /
  ``parameters_changed_batch`` signal shapes plus the before-snapshot
  (``_pending_keyframe_before``) that CompositePage turns into undo commands.
- ``set_node`` / ``set_nodes`` state switching (title, pin survival, reorder,
  incremental refresh, image-size driven pivot display).

Everything here is headless (QT_QPA_PLATFORM=offscreen); no GL, no rendering.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QLabel, QLineEdit,
    QPushButton, QToolButton,
)

from src.features.composite.nodes.base_node import ParamDef
from src.features.composite.ui.node_properties_panel import (
    CurveWidget, NodePropertiesPanel, NodeSectionWidget, TrimRangeEditor,
    TrimRangeSlider, _GradientPreviewWidget, group_xy_pairs,
)
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.topos_controls import ToposClickableSlider


# ── fixtures & builders ──────────────────────────────────────────────────────

@pytest.fixture
def panel(qapp) -> NodePropertiesPanel:
    """A fresh properties panel per test."""
    return NodePropertiesPanel()


def _pd(name: str, ptype: str = "float", default: Any = None, **kw: Any) -> ParamDef:
    return ParamDef(name=name, ptype=ptype, default=default, **kw)


MIXED_PARAMS: Dict[str, Any] = {
    "gain": 1.0,
    "samples": 16,
    "invert": True,
    "filter": "gaussian",
    "label": "hello",
    "tint": "#ff0000",
    "path": "/tmp/a.exr",
    "channel": "G",
    "offset": (1.0, 2.0),
    "position": (1.0, 2.0, 3.0),
    "lut": None,
}

MIXED_DEFS: Dict[str, ParamDef] = {
    "gain": _pd("gain", default=1.0, min_val=0.0, max_val=2.0, step=0.01),
    "samples": _pd("samples", "int", default=16, min_val=0, max_val=64),
    "invert": _pd("invert", "bool", default=True),
    "filter": _pd("filter", "option", default="gaussian"),
    "label": _pd("label", "str", default="hello"),
    "tint": _pd("tint", "color", default="#ff0000"),
    "path": _pd("path", "file", default="/tmp/a.exr"),
    "channel": _pd("channel", "channel", default="G"),
    "offset": _pd("offset", "vec2", default=(1.0, 2.0),
                  min_val=-100.0, max_val=100.0),
    "position": _pd("position", "vec3", default=(1.0, 2.0, 3.0),
                    min_val=-100.0, max_val=100.0),
}

MIXED_OPTIONS: Dict[str, List[str]] = {"filter": ["box", "gaussian", "lanczos"]}


def _node_data(node_id: str = "n1", node_type: str = "generic",
               node_title: str = "Generic",
               parameters: Optional[Dict[str, Any]] = None,
               param_defs: Optional[Dict[str, ParamDef]] = None,
               param_options: Optional[Dict[str, List[str]]] = None,
               image_size: Optional[Tuple[int, int]] = None,
               **overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "node_id": node_id,
        "node_type": node_type,
        "node_title": node_title,
        "parameters": dict(parameters if parameters is not None else MIXED_PARAMS),
        "param_options": dict(param_options if param_options is not None else MIXED_OPTIONS),
        "param_defs": dict(param_defs if param_defs is not None else MIXED_DEFS),
        "state": "dirty",
        "progress": 0.0,
        "error_message": "",
        "enabled": True,
        "node_color": "#666666",
        "image_size": image_size,
        "read_only": False,
    }
    data.update(overrides)
    return data


class _SignalLog:
    """Collects (args) tuples from a Qt signal."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(tuple(args))

    def __len__(self) -> int:
        return len(self.calls)


class _GraphStub:
    """Minimal NodeGraph stand-in: ``get_node`` by id, None otherwise."""

    def __init__(self, nodes: Dict[str, Any]) -> None:
        self._nodes = nodes

    def get_node(self, node_id: str) -> Optional[Any]:
        return self._nodes.get(node_id)


def _transform_defs() -> Dict[str, ParamDef]:
    """Per-test copies of TransformNode's ParamDefs (never mutate class state)."""
    from src.features.composite.nodes.transform_node import TransformNode
    return {pd.name: copy.deepcopy(pd) for pd in TransformNode.PARAM_DEFS}


def _transform_params(defs: Dict[str, ParamDef]) -> Dict[str, Any]:
    return {name: pd.default for name, pd in defs.items()}


def _mouse(event_type: QEvent.Type, pos: QPointF,
           button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> QMouseEvent:
    return QMouseEvent(
        event_type, QPointF(pos), QPointF(pos),
        button, button, Qt.KeyboardModifier.NoModifier)


# ── 1. group_xy_pairs — branches not covered by the xy test file ────────────

def test_group_xy_pairs_rgb_triple() -> None:
    keys = ["tint_r", "tint_g", "tint_b", "gain"]
    out = group_xy_pairs(keys)
    assert out == [("rgb", "tint", "tint_r", "tint_g", "tint_b"),
                   ("single", "gain")]


def test_group_xy_pairs_rgb_blocked_by_non_float() -> None:
    defs = {"tint_r": _pd("tint_r"), "tint_g": _pd("tint_g"),
            "tint_b": _pd("tint_b", "int")}
    out = group_xy_pairs(["tint_r", "tint_g", "tint_b"], defs)
    assert out == [("single", "tint_r"), ("single", "tint_g"),
                   ("single", "tint_b")]


def test_group_xy_pairs_rgb_respects_group_boundary() -> None:
    defs = {"tint_r": _pd("tint_r", group="A"),
            "tint_g": _pd("tint_g", group="A"),
            "tint_b": _pd("tint_b", group="B")}
    out = group_xy_pairs(["tint_r", "tint_g", "tint_b"], defs)
    assert ("rgb", "tint") not in out
    assert ("single", "tint_b") in out


def test_group_xy_pairs_triple_blocked_by_group_falls_back_to_pair() -> None:
    defs = {"p_x": _pd("p_x", group="A"), "p_y": _pd("p_y", group="A"),
            "p_z": _pd("p_z", group="B")}
    out = group_xy_pairs(["p_x", "p_y", "p_z"], defs)
    assert out == [("pair", "p", "p_x", "p_y"), ("single", "p_z")]


def test_group_xy_pairs_triple_z_non_float_falls_back_to_pair() -> None:
    defs = {"p_x": _pd("p_x"), "p_y": _pd("p_y"),
            "p_z": _pd("p_z", "option")}
    out = group_xy_pairs(["p_x", "p_y", "p_z"], defs)
    assert out == [("pair", "p", "p_x", "p_y"), ("single", "p_z")]


# ── 2. value mapping helpers ────────────────────────────────────────────────

def test_rgba_to_hex_rounding_and_clamping() -> None:
    assert NodePropertiesPanel._rgba_to_hex(1.0, 0.0, 0.5) == "#ff0080"
    assert NodePropertiesPanel._rgba_to_hex(0.0, 1.0, 0.0) == "#00ff00"
    assert NodePropertiesPanel._rgba_to_hex(-0.5, 2.0, 0.0) == "#00ff00"
    assert NodePropertiesPanel._rgba_to_hex(0.5, 0.5, 0.5) == "#808080"


def test_numeric_meta_defaults_and_pdef_override() -> None:
    is_int, mn, mx, step, dec = NodePropertiesPanel._numeric_meta(16, None)
    assert (is_int, mn, mx, step, dec) == (True, 0, 100, 1, 0)
    is_int, mn, mx, step, dec = NodePropertiesPanel._numeric_meta(0.5, None)
    assert (is_int, mn, mx, step, dec) == (False, 0.0, 1.0, 0.01, 2)
    pdef = _pd("gain", min_val=-2.0, max_val=4.0, step=0.05, decimals=3)
    _, mn, mx, step, dec = NodePropertiesPanel._numeric_meta(0.0, pdef)
    assert (mn, mx, step, dec) == (-2.0, 4.0, 0.05, 3)


def test_value_to_slider_int_and_float() -> None:
    assert NodePropertiesPanel._value_to_slider(7, {"type": "int"}) == 7
    assert NodePropertiesPanel._value_to_slider(
        2.0, {"type": "float", "min_val": 0.0, "step": 0.5}) == 4


def test_display_name_fallbacks(qapp, panel: NodePropertiesPanel) -> None:
    # Unknown keys have no i18n entry → mechanical fallbacks.
    assert panel._param_display_name("zz_my_param") == "Zz My Param"
    assert panel._option_display_label("", "zz_param", "gaussian") == "Gaussian"
    assert panel._group_display_name("zz_group_name") == "Zz Group Name"


# ── 3. TrimRangeSlider / TrimRangeEditor ────────────────────────────────────

def test_trim_slider_initial_clamp(qapp) -> None:
    s = TrimRangeSlider(0, 100, -10, 200)
    assert s.values() == (0, 100)
    s2 = TrimRangeSlider(10, 20, 30, 5)
    assert s2.values() == (20, 20)


def test_trim_slider_set_values_clamps_and_orders(qapp) -> None:
    s = TrimRangeSlider(0, 100, 0, 100)
    s.set_values(80, 20)      # end dragged before start → snapped to start
    assert s.values() == (80, 80)
    s.set_values(-5, 10)      # start below minimum → clamped to 0
    assert s.values() == (0, 10)


def test_trim_slider_set_bounds_retains_clamped_range(qapp) -> None:
    s = TrimRangeSlider(0, 100, 10, 90)
    s.set_bounds(20, 50)
    assert s.values() == (20, 50)


def test_trim_slider_x_value_roundtrip(qapp) -> None:
    s = TrimRangeSlider(0, 100, 0, 100)
    s.resize(200, 26)
    for v in (0, 25, 50, 75, 100):
        back = s._x_to_value(s._value_to_x(v))
        assert abs(back - v) <= 1


def test_trim_slider_set_from_x_requires_handle_and_emits(qapp) -> None:
    s = TrimRangeSlider(0, 100, 10, 90)
    s.resize(200, 26)
    log = _SignalLog()
    s.range_changed.connect(log)
    s._set_from_x(s._value_to_x(50))            # no active handle → no-op
    assert s.values() == (10, 90) and len(log) == 0
    s._active_handle = "in"
    s._set_from_x(s._value_to_x(30))            # in-handle moves start
    assert s.values() == (30, 90) and log.calls == [(30, 90)]
    s._set_from_x(s._value_to_x(30))            # unchanged → no duplicate emit
    assert len(log) == 1


def test_trim_slider_handles_cannot_cross(qapp) -> None:
    s = TrimRangeSlider(0, 100, 40, 60)
    s.resize(200, 26)
    s._active_handle = "in"
    s._set_from_x(s._value_to_x(80))            # in dragged past out → meet
    assert s.values() == (60, 60)
    s2 = TrimRangeSlider(0, 100, 40, 60)
    s2.resize(200, 26)
    s2._active_handle = "out"
    s2._set_from_x(s2._value_to_x(20))          # out dragged before in → meet
    assert s2.values() == (40, 40)


def test_trim_editor_set_values_is_silent(qapp) -> None:
    ed = TrimRangeEditor(0, 100, 10, 90)
    log = _SignalLog()
    ed.range_changed.connect(log)
    ed.set_values(20, 80)
    assert len(log) == 0
    assert ed.values() == (20, 80)
    assert ed._in_spin.value() == 20
    assert ed._out_spin.value() == 80


def test_trim_editor_in_changed_clamped_by_out(qapp) -> None:
    ed = TrimRangeEditor(0, 100, 10, 90)
    log = _SignalLog()
    ed.range_changed.connect(log)
    ed._on_in_changed(95.0)
    assert ed.values() == (90, 90)
    assert log.calls == [(90, 90)]


def test_trim_editor_out_changed_clamped_by_in(qapp) -> None:
    ed = TrimRangeEditor(0, 100, 10, 90)
    log = _SignalLog()
    ed.range_changed.connect(log)
    ed._on_out_changed(5.0)
    assert ed.values() == (10, 10)
    assert log.calls == [(10, 10)]


def test_trim_editor_slider_change_routes_and_syncs_spins(qapp) -> None:
    ed = TrimRangeEditor(0, 100, 10, 90)
    log = _SignalLog()
    ed.range_changed.connect(log)
    ed._slider._active_handle = "in"
    ed._slider._set_from_x(ed._slider._value_to_x(30))
    assert log.calls == [(30, 90)]
    assert ed._in_spin.value() == 30
    assert ed._out_spin.value() == 90


# ── 4. CurveWidget ──────────────────────────────────────────────────────────

def test_curve_default_and_set_sorts(qapp) -> None:
    w = CurveWidget()
    assert w.curve() == [(0.0, 0.0), (1.0, 1.0)]
    w.setCurve([(0.5, 0.25), (0.1, 0.9)])
    assert w.curve() == [(0.1, 0.9), (0.5, 0.25)]
    # setCurve returns a copy — mutating it must not touch the widget
    got = w.curve()
    got.append((9.0, 9.0))
    assert len(w.curve()) == 2


def test_curve_widget_coordinate_roundtrip(qapp) -> None:
    w = CurveWidget()
    w.resize(400, 300)
    for pt in ((0.0, 0.0), (0.25, 0.75), (1.0, 1.0)):
        back = w._from_widget(w._to_widget(pt))
        assert abs(back[0] - pt[0]) < 1e-6
        assert abs(back[1] - pt[1]) < 1e-6


def test_curve_find_point_at_hit_and_miss(qapp) -> None:
    w = CurveWidget([(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
    w.resize(400, 300)
    assert w._find_point_at(w._to_widget((0.5, 0.5))) == 1
    assert w._find_point_at(QPointF(200.0, 150.0)) in (0, 1, 2)  # dead centre hits
    assert w._find_point_at(QPointF(5.0, 295.0)) == 0            # near first point
    assert w._find_point_at(QPointF(100.0, 20.0)) == -1          # empty space


def test_curve_double_click_removes_middle_point_only(qapp) -> None:
    w = CurveWidget([(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
    w.resize(400, 300)
    log = _SignalLog()
    w.curveChanged.connect(log)
    w.mouseDoubleClickEvent(_mouse(QEvent.Type.MouseButtonDblClick,
                                   w._to_widget((0.5, 0.5))))
    assert w.curve() == [(0.0, 0.0), (1.0, 1.0)]
    assert len(log) == 1
    # endpoints are never removable
    w.mouseDoubleClickEvent(_mouse(QEvent.Type.MouseButtonDblClick,
                                   w._to_widget((0.0, 0.0))))
    w.mouseDoubleClickEvent(_mouse(QEvent.Type.MouseButtonDblClick,
                                   w._to_widget((1.0, 1.0))))
    assert w.curve() == [(0.0, 0.0), (1.0, 1.0)] and len(log) == 1


def test_curve_press_adds_point_and_emits(qapp) -> None:
    w = CurveWidget([(0.0, 0.0), (1.0, 1.0)])
    w.resize(400, 300)
    log = _SignalLog()
    w.curveChanged.connect(log)
    pos = QPointF(100.0, 150.0)
    expected = w._from_widget(pos)
    w.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, pos))
    assert len(w.curve()) == 3 and len(log) == 1
    assert expected in w.curve()


def test_curve_drag_clamped_between_neighbors(qapp) -> None:
    w = CurveWidget([(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
    w.resize(400, 300)
    w._dragging_index = 1
    w.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, QPointF(380.0, 20.0)))
    xs = [p[0] for p in w.curve()]
    assert xs == sorted(xs)
    assert xs[1] < 1.0 - 1e-4      # clamped below the right neighbour
    w.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, QPointF(5.0, 280.0)))
    xs = [p[0] for p in w.curve()]
    assert xs == sorted(xs)
    assert xs[1] > 1e-4            # clamped above the left neighbour


# ── 5. _GradientPreviewWidget ───────────────────────────────────────────────

def test_gradient_preview_state_and_paint_directions(qapp) -> None:
    w = _GradientPreviewWidget("#101010", "#efefef")
    w.set_colors("#123456", "#654321")
    w.set_direction("vertical")
    assert w._color1 == QColor("#123456")
    assert w._color2 == QColor("#654321")
    assert w._direction == "vertical"
    # paint smoke-test for every supported direction (offscreen raster)
    w.resize(80, 22)
    for direction in ("horizontal", "vertical",
                      "diagonal_tl_br", "diagonal_tr_bl", "bogus"):
        w.set_direction(direction)
        assert w.grab() is not None


# ── 6. NodeSectionWidget: ParamDef → control mapping ───────────────────────

def test_section_maps_param_types_to_controls(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    widgets = section._param_widgets

    assert widgets["gain"]["type"] == "float"
    assert isinstance(widgets["gain"]["widget"], DragValue)
    assert widgets["samples"]["type"] == "int"
    assert isinstance(widgets["samples"]["widget"], DragValue)
    assert widgets["invert"]["type"] == "bool"
    assert isinstance(widgets["invert"]["widget"], QCheckBox)
    assert widgets["filter"]["type"] == "combo"
    assert isinstance(widgets["filter"]["widget"], QComboBox)
    assert widgets["filter"]["options"] == MIXED_OPTIONS["filter"]
    assert widgets["label"]["type"] == "str"
    assert isinstance(widgets["label"]["widget"], QLineEdit)
    assert widgets["tint"]["type"] == "color"
    assert isinstance(widgets["tint"]["widget"], QPushButton)
    assert widgets["path"]["type"] == "file"
    assert widgets["channel"]["type"] == "channel"
    assert isinstance(widgets["channel"]["widget"], QComboBox)
    assert widgets["offset"]["type"] == "vec2" and widgets["offset"]["dim"] == 2
    assert widgets["position"]["type"] == "vec3" and widgets["position"]["dim"] == 3
    assert widgets["lut"]["type"] == "readonly"
    assert isinstance(widgets["lut"]["widget"], QLabel)
    # animatable numeric rows get a keyframe button, readonly rows do not
    assert "keyframe_btn" in widgets["gain"]
    assert "keyframe_btn" not in widgets["label"]
    assert "keyframe_btn" not in widgets["lut"]


def test_section_initial_widget_values(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    widgets = panel._sections["n1"]._param_widgets
    assert widgets["gain"]["widget"].value() == 1.0
    assert widgets["samples"]["widget"].value() == 16.0
    assert widgets["invert"]["widget"].isChecked() is True
    assert widgets["filter"]["widget"].currentIndex() == 1      # "gaussian"
    assert widgets["label"]["widget"].text() == "hello"
    assert widgets["tint"]["widget"].text() == "#ff0000"
    assert widgets["channel"]["widget"].currentIndex() == 1    # "G"
    spins = widgets["offset"]["widget"].findChildren(QDoubleSpinBox)
    assert [s.value() for s in spins] == [1.0, 2.0]


def test_numeric_row_registers_hidden_slider(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    info = section._param_widgets["gain"]
    slider = info.get("slider")
    assert isinstance(slider, ToposClickableSlider)
    assert slider in section._slidable_sliders
    assert slider.styleSheet() == ""      # global QSS, no inline override


def test_hidden_paramdef_not_built(qapp, panel: NodePropertiesPanel) -> None:
    defs = dict(MIXED_DEFS)
    defs["gain"] = replace(MIXED_DEFS["gain"], hidden=True)
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=defs)
    assert "gain" not in panel._sections["n1"]._param_widgets
    assert "samples" in panel._sections["n1"]._param_widgets


def test_grouped_params_land_in_group_box(qapp, panel: NodePropertiesPanel) -> None:
    defs = dict(MIXED_DEFS)
    defs["gain"] = replace(MIXED_DEFS["gain"], group="zz_quality")
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=defs)
    section = panel._sections["n1"]
    boxes = section.findChildren(QGroupBox)
    assert any("Zz Quality" in b.title() for b in boxes)
    assert "gain" in section._param_widgets


def test_section_search_filter_rebuilds(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    section.set_search_filter("gain")
    assert set(section._param_widgets) == {"gain"}
    section.set_search_filter("")
    assert "samples" in section._param_widgets


# ── 7. parameter edit routing (single selection) ────────────────────────────

def test_drag_edit_emits_and_syncs_caches(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    drag = panel._sections["n1"]._param_widgets["gain"]["widget"]
    drag.setValue(0.5)
    assert log.calls == [("n1", "gain", 0.5)]
    assert panel._current_parameters["gain"] == 0.5
    assert panel._sections["n1"].parameters["gain"] == 0.5
    # setting the same value again is suppressed by the equality guard
    drag.setValue(0.5)
    assert len(log) == 1


def test_combo_edit_emits_raw_option_value(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    combo = panel._sections["n1"]._param_widgets["filter"]["widget"]
    combo.setCurrentIndex(0)                 # display label may be "Box"…
    assert log.calls == [("n1", "filter", "box")]   # …but raw value is stored
    assert panel._sections["n1"].parameters["filter"] == "box"


def test_bool_toggle_emits(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    checkbox = panel._sections["n1"]._param_widgets["invert"]["widget"]
    checkbox.setChecked(False)
    assert log.calls == [("n1", "invert", False)]


def test_str_commit_emits(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    line = panel._sections["n1"]._param_widgets["label"]["widget"]
    line.setText("world")
    line.editingFinished.emit()
    assert log.calls == [("n1", "label", "world")]


def test_channel_change_emits(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    combo = panel._sections["n1"]._param_widgets["channel"]["widget"]
    combo.setCurrentIndex(3)                 # "A"
    assert log.calls == [("n1", "channel", "A")]


def test_vec2_spin_change_emits_list(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    spins = panel._sections["n1"]._param_widgets["offset"]["widget"] \
        .findChildren(QDoubleSpinBox)
    spins[0].setValue(5.5)
    assert log.calls == [("n1", "offset", [5.5, 2.0])]
    assert panel._sections["n1"].parameters["offset"] == [5.5, 2.0]


# ── 8. multi-selection routing (BUG-B2-01 / AUD-10700 — FIXED) ─────────────
#
# ``_on_param_changed`` used to route edits through ``self._current_node_id``,
# which ``set_nodes`` pins to the FIRST selected node — so an edit made in the
# second node's section was emitted (and cached) against the first node. The
# row builders now bind the owning node id at build time and
# ``_on_param_changed`` routes dedup/write/emit through it, so per-section
# routing holds; these former xfail(strict=True) locks are positive again.

class TestMultiSelectionRoutingRegression:
    """Regression tests for BUG-B2-01 / AUD-10700 (multi-select edits write
    the owning node, not the first selected node)."""

    def test_edit_in_second_section_routes_to_its_own_node(
            self, qapp, panel: NodePropertiesPanel) -> None:
        panel.set_nodes([
            _node_data("n1", parameters={"gain": 1.0},
                       param_defs={"gain": MIXED_DEFS["gain"]}, param_options={}),
            _node_data("n2", parameters={"gain": 1.0},
                       param_defs={"gain": MIXED_DEFS["gain"]}, param_options={}),
        ])
        log = _SignalLog()
        panel.parameter_changed.connect(log)
        n2_drag = panel._sections["n2"]._param_widgets["gain"]["widget"]
        n2_drag.setValue(0.25)
        assert log.calls == [("n2", "gain", 0.25)]
        assert panel._sections["n2"].parameters["gain"] == 0.25
        assert panel._sections["n1"].parameters["gain"] == 1.0   # n1 untouched

    def test_edit_in_second_section_not_dropped_by_first_nodes_value(
            self, qapp, panel: NodePropertiesPanel) -> None:
        panel.set_nodes([
            _node_data("n1", parameters={"gain": 0.5},
                       param_defs={"gain": MIXED_DEFS["gain"]}, param_options={}),
            _node_data("n2", parameters={"gain": 1.0},
                       param_defs={"gain": MIXED_DEFS["gain"]}, param_options={}),
        ])
        log = _SignalLog()
        panel.parameter_changed.connect(log)
        n2_drag = panel._sections["n2"]._param_widgets["gain"]["widget"]
        n2_drag.setValue(0.5)      # coincides with n1's value — must still land
        assert log.calls == [("n2", "gain", 0.5)]
        assert panel._sections["n2"].parameters["gain"] == 0.5


def test_legacy_batch_signal_payload(qapp, panel: NodePropertiesPanel) -> None:
    """Legacy multi-select batch path: payload carries per-node OLD values so
    CompositePage can push one undo command for the whole batch."""
    panel._current_node_ids = ["a", "b"]
    panel._param_values_per_node = {"a": {"mix": 0.2}, "b": {"mix": 0.4}}
    panel._common_parameters = {"mix": 0.2}
    log = _SignalLog()
    panel.parameters_changed_batch.connect(log)
    panel._on_multi_param_changed("mix", 0.7)
    assert log.calls == [("a", "b"), "mix", 0.7, {"a": 0.2, "b": 0.4}] or \
        list(log.calls[0]) == [["a", "b"], "mix", 0.7, {"a": 0.2, "b": 0.4}]
    assert panel._common_parameters["mix"] == 0.7
    # the anti-recursion lock suppresses batch edits too
    panel._block_signals = True
    panel._on_multi_param_changed("mix", 0.9)
    assert len(log) == 1
    panel._block_signals = False


def test_compute_common_parameters(qapp, panel: NodePropertiesPanel) -> None:
    nodes = [
        _node_data("a", parameters={"mix": 0.5, "solo_a": 1.0}, param_options={}),
        _node_data("b", parameters={"mix": 0.5, "solo_b": 2.0}, param_options={}),
        _node_data("c", parameters={"mix": 0.9}, param_options={}),
    ]
    common = panel._compute_common_parameters(nodes)
    # only params present on every node survive; mixed values take node #1's
    assert common == {"mix": 0.5}
    assert panel._compute_common_parameters([]) == {}


# ── 9. signal anti-recursion ────────────────────────────────────────────────

def test_block_signals_flag_suppresses_ui_edit(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    drag = panel._sections["n1"]._param_widgets["gain"]["widget"]
    panel._block_signals = True
    drag.setValue(0.1)
    assert len(log) == 0
    panel._block_signals = False
    drag.setValue(0.2)
    assert log.calls == [("n1", "gain", 0.2)]


def test_set_nodes_refresh_does_not_reemit(qapp, panel: NodePropertiesPanel) -> None:
    """Refreshing with new values must not echo ``parameter_changed`` back at
    the graph (widget updates go through the blockSignals path)."""
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    new_params = dict(MIXED_PARAMS, gain=0.75, samples=32, invert=False)
    panel.set_node("n1", "generic", "Generic", new_params,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    assert len(log) == 0
    widgets = panel._sections["n1"]._param_widgets
    assert widgets["gain"]["widget"].value() == 0.75
    assert widgets["samples"]["widget"].value() == 32.0
    assert widgets["invert"]["widget"].isChecked() is False
    assert panel._current_parameters["gain"] == 0.75


# ── 10. set_node / set_nodes state switching ───────────────────────────────

def test_set_node_builds_section_and_title(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    assert list(panel._sections) == ["n1"]
    assert panel._current_node_id == "n1"
    assert panel._current_node_type == "generic"
    assert panel._title_label.text() == "Properties - Generic"


def test_set_node_none_clears_unpinned(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    panel.set_node(None, None, None, {})
    assert panel._sections == {}
    assert panel._current_node_id is None
    assert panel._title_label.text() == "Properties"


def test_multi_nodes_title_and_first_node_current(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_nodes([
        _node_data("n1"), _node_data("n2", node_title="Second"),
    ])
    assert set(panel._sections) == {"n1", "n2"}
    assert panel._current_node_id == "n1"          # first selected wins
    assert panel._title_label.text() == "Properties - 2 nodes"


def test_pinned_section_survives_deselection(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Pinned", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    panel._on_section_pin_toggled("n1", True)
    panel.set_node("n2", "generic", "Other", {"gain": 1.0},
                   param_defs={"gain": MIXED_DEFS["gain"]})
    assert "n1" in panel._sections        # pinned stays
    assert "n2" in panel._sections
    panel.set_node(None, None, None, {})  # clearing keeps pinned only
    assert set(panel._sections) == {"n1"}


def test_set_nodes_refresh_keeps_widget_identity(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    old_gain = panel._sections["n1"]._param_widgets["gain"]["widget"]
    panel.set_node("n1", "generic", "Generic", dict(MIXED_PARAMS, gain=0.4),
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    new_gain = panel._sections["n1"]._param_widgets["gain"]["widget"]
    assert new_gain is old_gain               # same keys → incremental update
    assert new_gain.value() == 0.4


def test_set_nodes_rebuilds_on_structure_change(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    old_gain = panel._sections["n1"]._param_widgets["gain"]["widget"]
    panel.set_node("n1", "generic", "Generic", {"gain": 1.0, "extra": 3},
                   param_options=MIXED_OPTIONS,
                   param_defs={"gain": MIXED_DEFS["gain"],
                               "extra": _pd("extra", "int", default=3)})
    new_gain = panel._sections["n1"]._param_widgets["gain"]["widget"]
    assert new_gain is not old_gain          # new key set → full rebuild
    assert "extra" in panel._sections["n1"]._param_widgets


def test_set_nodes_reorders_selection(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_nodes([_node_data("n1"), _node_data("n2")])
    panel.set_nodes([_node_data("n2"), _node_data("n1")])   # reversed selection
    order = [panel._content_layout.itemAt(i).widget()
             for i in range(panel._content_layout.count())]
    assert order[0] is panel._sections["n2"]
    assert order[1] is panel._sections["n1"]


def test_update_parameters_after_teardown_is_noop(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    section.prepare_for_deletion()
    assert section._is_torn_down is True
    section.update_parameters({"totally": "different"})     # must be a no-op
    assert "totally" not in section.parameters
    section.set_enabled(False)
    section.set_node_color("#ffffff")
    section.set_read_only(True)
    assert section.node_color == "#666666"      # unchanged by post-teardown calls


def test_prepare_for_deletion_is_idempotent(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    section.prepare_for_deletion()
    section.prepare_for_deletion()              # overlapping removal path
    assert section._is_torn_down is True


# ── 11. section header interactions ────────────────────────────────────────

def test_collapse_toggle(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    section.toggle_collapse()
    assert section._content_widget.isVisibleTo(section) is False
    assert section._collapse_btn.text() == "▶"
    section.toggle_collapse()
    assert section._content_widget.isVisibleTo(section) is True
    assert section._collapse_btn.text() == "▼"


def test_enable_toggle_emit_vs_silent_set(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    log = _SignalLog()
    panel.node_enabled_toggled.connect(log)
    section.set_enabled(False)                  # silent mirror from the graph
    assert len(log) == 0
    assert section._enable_btn.isChecked() is False
    section._on_enable_toggled(True)            # user clicked the switch
    assert log.calls == [("n1", True)]
    assert section.enabled is True


def test_lock_disables_content(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    assert section.is_locked() is False
    section._lock_btn.click()                   # fires _on_lock_toggled
    assert section.is_locked() is True
    assert section._content_widget.isEnabled() is False
    section._lock_btn.click()
    assert section._content_widget.isEnabled() is True


def test_read_only_locks_and_unlocks_ui(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    section.set_read_only(True)
    assert section._content_widget.isEnabled() is False
    assert section._enable_btn.isEnabled() is False
    assert section._reset_btn.isEnabled() is False
    assert "locked" in section.toolTip()
    section.set_read_only(False)
    assert section._content_widget.isEnabled() is True
    assert section._reset_btn.isEnabled() is True


def test_pin_toggled_updates_panel_state(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    assert section.is_pinned() is False
    section._pin_btn.click()                    # fires _on_pin_toggled → panel
    assert section.is_pinned() is True
    assert "n1" in panel._pinned_node_ids
    section._pin_btn.click()
    assert section.is_pinned() is False
    assert "n1" not in panel._pinned_node_ids


def test_section_reset_emits_defaults_and_rebuilds(qapp, panel: NodePropertiesPanel) -> None:
    params = dict(MIXED_PARAMS, gain=0.1, invert=False)
    panel.set_node("n1", "generic", "Generic", params,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)        # relayed via section signal
    section = panel._sections["n1"]
    section._on_reset()
    assert ("n1", "gain", 1.0) in log.calls     # ParamDef defaults, not current
    assert ("n1", "invert", True) in log.calls
    assert section.parameters["gain"] == 1.0
    assert section._param_widgets["gain"]["widget"].value() == 1.0


def test_collect_current_values_reads_live_widgets(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    widgets = section._param_widgets
    widgets["gain"]["widget"].setValue(1.5)
    widgets["invert"]["widget"].setChecked(False)
    widgets["label"]["widget"].setText("edited")
    values = section._collect_current_values()
    assert values["gain"] == 1.5
    assert values["invert"] is False
    assert values["label"] == "edited"
    # unedited kinds fall back to the synced parameters snapshot
    assert values["tint"] == "#ff0000"
    assert values["offset"] == (1.0, 2.0)


class TestPresetSnapshotRegression:
    """Regression locks for preset-snapshot defects found while writing tests."""

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-B2-02: combo presets store the display label, not the raw value")
    def test_collect_current_values_combo_returns_raw_option(
            self, qapp, panel: NodePropertiesPanel) -> None:
        """BUG-B2-02: combos store no itemData, so ``_collect_current_values``
        falls back to ``currentText()`` — the *display* label ("Gaussian"),
        not the raw option value ("gaussian"). A preset saved with the display
        label can no longer match any raw option when re-applied."""
        panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                       param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
        values = panel._sections["n1"]._collect_current_values()
        assert values["filter"] == "gaussian"    # raw value, not "Gaussian"


# ── 12. pivot display driven by image_size / pivot_mode ────────────────────

PIVOT_PARAMS: Dict[str, Any] = {
    "pivot_mode": "center", "pivot_x": 100.0, "pivot_y": 50.0, "rotation": 0.0,
}
PIVOT_DEFS: Dict[str, ParamDef] = {
    "pivot_mode": _pd("pivot_mode", "option", default="center"),
    "pivot_x": _pd("pivot_x", default=100.0, min_val=-4000.0, max_val=4000.0),
    "pivot_y": _pd("pivot_y", default=50.0, min_val=-4000.0, max_val=4000.0),
    "rotation": _pd("rotation", default=0.0, min_val=-360.0, max_val=360.0),
}
PIVOT_OPTIONS: Dict[str, List[str]] = {"pivot_mode": ["center", "custom"]}


def test_image_size_centers_pivot_readonly(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Pivot", PIVOT_PARAMS,
                   param_options=PIVOT_OPTIONS, param_defs=PIVOT_DEFS,
                   image_size=(1920, 1080))
    assert panel._image_sizes["n1"] == (1920, 1080)
    widgets = panel._sections["n1"]._param_widgets
    assert widgets["pivot_x"]["widget"].value() == 960.0
    assert widgets["pivot_y"]["widget"].value() == 540.0
    assert widgets["pivot_x"]["widget"].isEnabled() is False
    assert widgets["pivot_x"]["pivot_center_readonly"] is True


def test_pivot_mode_switch_restores_editable(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Pivot", PIVOT_PARAMS,
                   param_options=PIVOT_OPTIONS, param_defs=PIVOT_DEFS,
                   image_size=(1920, 1080))
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    widgets = panel._sections["n1"]._param_widgets
    combo = widgets["pivot_mode"]["widget"]
    combo.setCurrentIndex(1)                    # → "custom"
    assert ("n1", "pivot_mode", "custom") in log.calls
    assert widgets["pivot_x"]["widget"].isEnabled() is True
    assert widgets["pivot_x"]["widget"].value() == 100.0   # real param value
    assert widgets["pivot_x"]["pivot_center_readonly"] is False


def test_set_nodes_image_size_change_refreshes_pivot(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Pivot", PIVOT_PARAMS,
                   param_options=PIVOT_OPTIONS, param_defs=PIVOT_DEFS)
    widgets = panel._sections["n1"]._param_widgets
    assert widgets["pivot_x"]["widget"].value() == 100.0   # no size → param value
    panel.set_node("n1", "generic", "Pivot", PIVOT_PARAMS,
                   param_options=PIVOT_OPTIONS, param_defs=PIVOT_DEFS,
                   image_size=(100, 200))
    assert widgets["pivot_x"]["widget"].value() == 50.0    # refreshed center
    assert widgets["pivot_x"]["widget"].isEnabled() is False


# ── 13. incremental widget value updates ───────────────────────────────────

def test_update_section_param_value_no_echo(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    section = panel._sections["n1"]
    widgets = section._param_widgets
    panel._update_section_param_value("gain", 0.3, widgets)
    panel._update_section_param_value("filter", "box", widgets)
    panel._update_section_param_value("invert", False, widgets)
    panel._update_section_param_value("label", "synced", widgets)
    assert len(log) == 0                          # silent programmatic sync
    assert widgets["gain"]["widget"].value() == 0.3
    assert widgets["gain"]["slider"].value() == 30   # slider follows (step .01)
    assert widgets["filter"]["widget"].currentIndex() == 0
    assert widgets["invert"]["widget"].isChecked() is False
    assert widgets["label"]["widget"].text() == "synced"


def test_update_param_widget_value_various_types(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    panel._param_widgets.update(section._param_widgets)   # scratch dict view
    panel._update_param_widget_value("tint", "#00ff00")
    assert panel._param_widgets["tint"]["widget"].text() == "#00ff00"
    assert "#00ff00" in panel._param_widgets["tint"]["widget"].styleSheet()
    panel._update_param_widget_value("path", "/tmp/b.exr")
    line = panel._param_widgets["path"]["widget"].findChild(QLineEdit)
    assert line is not None and line.text() == "/tmp/b.exr"
    panel._update_param_widget_value("channel", "A")
    assert panel._param_widgets["channel"]["widget"].currentIndex() == 3
    panel._update_param_widget_value("lut", "waveform")
    assert panel._param_widgets["lut"]["widget"].text() == "waveform"
    panel._update_param_widget_value("offset", (7.0, 8.0))
    spins = panel._param_widgets["offset"]["widget"].findChildren(QDoubleSpinBox)
    assert [s.value() for s in spins] == [7.0, 8.0]


# ── 14. keyframes: normalization, toggle, autokey, undo payloads ───────────

def test_normalize_keyframe_value_matrix() -> None:
    norm = NodePropertiesPanel._normalize_keyframe_value
    assert norm(_pd("r", "float"), 1.5) == 1.5
    assert norm(_pd("n", "int"), 3.4) == 3
    assert norm(_pd("n", "int"), 3.6) == 4
    assert norm(_pd("b", "bool"), True) is True
    assert norm(_pd("o", "option"), "gaussian") == "gaussian"
    assert norm(_pd("v", "vec2"), (1, 2)) == (1.0, 2.0)
    assert norm(_pd("v", "vec3"), (1, 2, 3)) == (1.0, 2.0, 3.0)
    assert norm(_pd("c", "color"), (1, 0, 0, 1)) == (1.0, 0.0, 0.0, 1.0)
    with pytest.raises(ValueError):
        norm(_pd("v", "vec2"), (1, 2, 3))          # wrong dimension
    with pytest.raises(ValueError):
        norm(_pd("v", "vec2"), 5.0)                # not a sequence
    with pytest.raises(ValueError):
        norm(_pd("b", "bool"), 1)                  # not a bool
    with pytest.raises(ValueError):
        norm(_pd("o", "option"), 3)                # not a str


def _transform_panel(panel: NodePropertiesPanel, node: Any) -> Dict[str, ParamDef]:
    """Bind a live node through a graph stub and show a transform section."""
    defs = _transform_defs()
    panel.set_node_graph(_GraphStub({"n1": node}))
    panel.set_node("n1", "transform", "Transform",
                   _transform_params(defs), param_defs=defs)
    return defs


def test_toggle_keyframe_adds_then_removes(qapp, panel: NodePropertiesPanel) -> None:
    from src.features.composite.nodes.transform_node import TransformNode
    node = TransformNode()
    defs = _transform_panel(panel, node)
    panel._current_frame = 5
    log = _SignalLog()
    panel.keyframes_changed.connect(log)
    pdef = defs["rotation"]

    panel._toggle_keyframe_at_current_frame("rotation", pdef)
    assert "rotation" in node.keyframe_tracks
    kfs = node.keyframe_tracks["rotation"]["keyframes"]
    assert [kf["frame"] for kf in kfs] == [5]
    assert kfs[0]["value"] == node.parameters["rotation"]   # live param value
    assert log.calls[-1] == ("n1", "transform", "rotation", kfs)
    assert pdef.animated is True and pdef.keyframes == kfs  # legacy mirror
    # ◆ button turned red (playhead sits on the new keyframe)
    assert panel._sections["n1"]._param_widgets["rotation"][
        "keyframe_btn"].isChecked() is True

    panel._toggle_keyframe_at_current_frame("rotation", pdef)   # toggle off
    assert "rotation" not in node.keyframe_tracks              # last kf → track gone
    assert log.calls[-1][3] == []
    assert pdef.animated is False and pdef.keyframes is None


def test_autokey_noop_for_unanimated_param(qapp, panel: NodePropertiesPanel) -> None:
    from src.features.composite.nodes.transform_node import TransformNode
    node = TransformNode()
    defs = _transform_panel(panel, node)
    log = _SignalLog()
    panel.keyframes_changed.connect(log)
    panel._autokey_if_animated("scale_x", 2.0, defs["scale_x"])
    assert "scale_x" not in node.keyframe_tracks
    assert len(log) == 0


def test_autokey_writes_keyframe_when_animated(qapp, panel: NodePropertiesPanel) -> None:
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import InterpolationType, Keyframe, make_keyframe_track
    node = TransformNode()
    defs = _transform_panel(panel, node)
    track = make_keyframe_track("rotation", value_type="float",
                                min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(frame=0, value=5.0,
                                interpolation=InterpolationType.LINEAR))
    node.set_keyframe_track("rotation", track)
    panel._current_frame = 10

    log = _SignalLog()
    panel.keyframes_changed.connect(log)
    panel._autokey_if_animated("rotation", 45.0, defs["rotation"])
    kfs = node.keyframe_tracks["rotation"]["keyframes"]
    assert sorted(kf["frame"] for kf in kfs) == [0, 10]
    assert kfs[-1]["value"] == 45.0
    assert len(log) == 1


def test_autokey_updates_existing_keyframe_in_place(qapp, panel: NodePropertiesPanel) -> None:
    """Playhead on an existing keyframe → value replaced, shape preserved
    (still exactly one keyframe; interpolation/handles survive)."""
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import InterpolationType, Keyframe, make_keyframe_track
    node = TransformNode()
    defs = _transform_panel(panel, node)
    track = make_keyframe_track("rotation", value_type="float",
                                min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(frame=10, value=20.0,
                                interpolation=InterpolationType.BEZIER))
    node.set_keyframe_track("rotation", track)
    panel._current_frame = 10

    panel._autokey_if_animated("rotation", 30.0, defs["rotation"])
    kfs = node.keyframe_tracks["rotation"]["keyframes"]
    assert len(kfs) == 1                    # replaced, not appended
    assert kfs[0]["value"] == 30.0
    assert kfs[0]["interpolation"] == "bezier"


def test_on_keyframes_changed_empty_removes_track_and_unchecks(
        qapp, panel: NodePropertiesPanel) -> None:
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import InterpolationType, Keyframe, make_keyframe_track
    node = TransformNode()
    defs = _transform_panel(panel, node)
    track = make_keyframe_track("rotation", value_type="float",
                                min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(frame=3, value=9.0,
                                interpolation=InterpolationType.LINEAR))
    node.set_keyframe_track("rotation", track)
    pdef = defs["rotation"]

    panel._on_keyframes_changed("rotation", pdef, [])
    assert "rotation" not in node.keyframe_tracks
    assert pdef.animated is False and pdef.keyframes is None
    btn = panel._sections["n1"]._param_widgets["rotation"]["keyframe_btn"]
    assert btn.isChecked() is False


def test_pending_before_snapshot_captured_for_undo(qapp, panel: NodePropertiesPanel) -> None:
    """``_pending_keyframe_before`` stashes the pre-edit track so CompositePage
    can build a real undo command from the keyframes_changed signal."""
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import InterpolationType, Keyframe, make_keyframe_track
    node = TransformNode()
    defs = _transform_panel(panel, node)
    track = make_keyframe_track("rotation", value_type="float",
                                min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(frame=1, value=11.0,
                                interpolation=InterpolationType.LINEAR))
    node.set_keyframe_track("rotation", track)
    before = copy.deepcopy(node.keyframe_tracks["rotation"])

    panel._on_keyframes_changed("rotation", defs["rotation"], [])
    assert panel._pending_keyframe_before[("n1", "rotation")] == before


def test_keyframe_button_fallback_without_graph(qapp, panel: NodePropertiesPanel) -> None:
    pdef_animated = _pd("gain", default=1.0, animated=True,
                        keyframes=[{"frame": 0, "value": 1.0}])
    btn = panel._make_keyframe_button("gain", pdef_animated)
    assert btn.isCheckable() is True and btn.isChecked() is True
    btn2 = panel._make_keyframe_button("gain", _pd("gain", default=1.0))
    assert btn2.isChecked() is False


def test_set_keyframe_buttons_visible_hides_and_persists(
        qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    btn = panel._sections["n1"]._param_widgets["gain"]["keyframe_btn"]
    panel.set_keyframe_buttons_visible(False)
    assert btn.isHidden() is True               # explicit-hide flag
    assert btn.isEnabled() is False
    # newly built buttons inherit the flag (keep n1 selected so it survives)
    panel.set_nodes([_node_data("n1"), _node_data("n2")])
    btn2 = panel._sections["n2"]._param_widgets["gain"]["keyframe_btn"]
    assert btn2.isHidden() is True and btn2.isEnabled() is False
    assert btn.isHidden() is True               # existing ones stay hidden
    panel.set_keyframe_buttons_visible(True)
    assert btn.isHidden() is False and btn.isEnabled() is True
    assert btn2.isHidden() is False and btn2.isEnabled() is True


def test_timeline_state_drives_button_red_state(qapp, panel: NodePropertiesPanel) -> None:
    from src.features.composite.nodes.transform_node import TransformNode
    from src.shared.keyframe import InterpolationType, Keyframe, make_keyframe_track
    node = TransformNode()
    defs = _transform_panel(panel, node)
    track = make_keyframe_track("rotation", value_type="float",
                                min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(frame=5, value=15.0,
                                interpolation=InterpolationType.LINEAR))
    node.set_keyframe_track("rotation", track)

    btn = panel._sections["n1"]._param_widgets["rotation"]["keyframe_btn"]
    panel.set_timeline_state(100, 5)             # playhead ON the keyframe
    assert btn.isChecked() is True
    panel.set_timeline_state(100, 7)             # playhead off it
    assert btn.isChecked() is False


def test_set_keyframe_button_checked_shared_keys_fallback(
        qapp, panel: NodePropertiesPanel) -> None:
    """A custom row registering several params under one button (e.g. a color
    swatch) is found via ``keyframe_btn_keys``."""
    btn = QToolButton()
    btn.setCheckable(True)
    panel._param_widgets = {
        "swatch": {"type": "color", "widget": None,
                   "keyframe_btn": btn, "keyframe_btn_keys": ["r_key", "g_key"]},
    }
    panel._set_keyframe_button_checked("g_key", True)
    assert btn.isChecked() is True


# ── 15. copy/paste + panel-level search ────────────────────────────────────

def test_copy_paste_parameters_restores_values(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    panel._copy_parameters()
    assert panel._clipboard_params is not None
    panel._sections["n1"]._param_widgets["gain"]["widget"].setValue(0.1)
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    panel._paste_parameters()
    assert ("n1", "gain", 1.0) in log.calls      # restored from the clipboard
    assert panel._current_parameters["gain"] == 1.0
    # unchanged params are skipped by the equality guard (no spam)
    assert ("n1", "samples", 16) not in log.calls


def test_panel_search_filters_all_sections(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_nodes([_node_data("n1"), _node_data("n2")])
    panel._on_search_changed("gain")
    assert set(panel._sections["n1"]._param_widgets) == {"gain"}
    assert set(panel._sections["n2"]._param_widgets) == {"gain"}
    assert panel._search_filter == "gain"


# ── 16. custom builder dispatch (output node) ──────────────────────────────

OUTPUT_PARAMS: Dict[str, Any] = {"format_mode": "HD", "width": 1920, "height": 1080}
OUTPUT_DEFS: Dict[str, ParamDef] = {
    "format_mode": _pd("format_mode", "option", default="HD"),
    "width": _pd("width", "int", default=1920, min_val=16, max_val=8192),
    "height": _pd("height", "int", default=1080, min_val=16, max_val=8192),
}
OUTPUT_OPTIONS: Dict[str, List[str]] = {"format_mode": ["HD", "UHD"]}


def test_output_node_custom_ui_render_button(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "output", "Output", OUTPUT_PARAMS,
                   param_options=OUTPUT_OPTIONS, param_defs=OUTPUT_DEFS)
    section = panel._sections["n1"]
    # generic rows were still built by the custom builder
    assert section._param_widgets["width"]["type"] == "int"
    assert section._param_widgets["format_mode"]["type"] == "combo"
    # exactly one full-width QPushButton: the Render Output button
    buttons = section.findChildren(QPushButton)
    assert len(buttons) == 1
    log = _SignalLog()
    panel.render_output_requested.connect(log)
    buttons[0].click()
    assert log.calls == [("n1",)]


# ── 17. legacy multi-selection row builder (direct calls) ──────────────────

def test_multi_selection_param_row_numeric_and_bool(
        qapp, panel: NodePropertiesPanel) -> None:
    panel._param_defs = {"mix": MIXED_DEFS["gain"], "samples": MIXED_DEFS["samples"],
                         "invert": MIXED_DEFS["invert"]}
    row_f = panel._create_multi_selection_param_row("mix", 0.5, False)
    assert row_f is not None
    info = panel._param_widgets["mix"]
    assert info["type"] == "float"
    assert info["spinbox"].value() == 0.5
    assert info["slider"].value() == 50          # (0.5 - 0.0) / 0.01
    row_i = panel._create_multi_selection_param_row("samples", 16, False)
    assert row_i is not None
    info_i = panel._param_widgets["samples"]
    assert info_i["type"] == "int"
    assert info_i["slider"].value() == 16
    assert info_i["spinbox"].value() == 16
    row_b = panel._create_multi_selection_param_row("invert", True, False)
    assert row_b is not None
    assert panel._param_widgets["invert"]["type"] == "bool"
    assert panel._param_widgets["invert"]["widget"].isChecked() is True


def test_multi_selection_param_row_combo_and_str(
        qapp, panel: NodePropertiesPanel) -> None:
    panel._param_defs = {"filter": MIXED_DEFS["filter"], "label": MIXED_DEFS["label"]}
    panel._param_options = {"filter": ["box", "gaussian"]}
    panel._current_node_type = "generic"
    row_c = panel._create_multi_selection_param_row("filter", "gaussian", False)
    assert row_c is not None
    info = panel._param_widgets["filter"]
    assert info["type"] == "combo"
    assert info["options"] == ["box", "gaussian"]
    assert info["widget"].currentIndex() == 1
    row_s = panel._create_multi_selection_param_row("label", "hello", False)
    assert row_s is not None
    assert panel._param_widgets["label"]["type"] == "str"
    assert panel._param_widgets["label"]["widget"].text() == "hello"


def test_create_params_group_groups_by_pdef_group(
        qapp, panel: NodePropertiesPanel) -> None:
    panel._param_defs = dict(MIXED_DEFS)
    panel._param_defs["gain"] = replace(MIXED_DEFS["gain"], group="zz_quality")
    panel._param_options = dict(MIXED_OPTIONS)
    group = panel._create_params_group(dict(MIXED_PARAMS))
    inner = group.findChildren(QGroupBox)
    # NOTE: the legacy builder titles inner boxes with the RAW group name
    # (no _group_display_name i18n), unlike the section-based builder.
    assert any(b.title() == "zz_quality" for b in inner)


# ── 18. multi-axis keyframe toggle (shared ◆ stopwatch contract) ───────────

def test_toggle_multi_axis_keyframe_add_then_remove_both(
        qapp, panel: NodePropertiesPanel) -> None:
    from src.features.composite.nodes.transform_node import TransformNode
    node = TransformNode()
    defs = _transform_panel(panel, node)
    panel._current_frame = 4
    keys = ["scale_x", "scale_y"]
    pdefs = [defs["scale_x"], defs["scale_y"]]

    panel._toggle_multi_axis_keyframe(keys, pdefs)      # add mode
    for key in keys:
        kfs = node.keyframe_tracks[key]["keyframes"]
        assert [kf["frame"] for kf in kfs] == [4]
        assert kfs[0]["value"] == float(node.parameters[key])
    panel._toggle_multi_axis_keyframe(keys, pdefs)      # remove mode
    assert keys[0] not in node.keyframe_tracks
    assert keys[1] not in node.keyframe_tracks


# ── 19. viewer-state sync helpers ──────────────────────────────────────────

def test_set_view_mode_updates_scene3d_toggles(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    btn_2d, btn_3d = QToolButton(), QToolButton()
    for b in (btn_2d, btn_3d):
        b.setCheckable(True)
    panel._scene3d_toggle_buttons = {"n1": (btn_2d, btn_3d)}
    panel.set_view_mode("3d")
    assert btn_3d.isChecked() is True and btn_2d.isChecked() is False
    panel.set_view_mode("2d")
    assert btn_2d.isChecked() is True and btn_3d.isChecked() is False
    panel.set_view_mode("garbage")                       # anything else → 2d
    assert btn_2d.isChecked() is True and btn_3d.isChecked() is False
    assert panel._current_view_mode == "2d"


def test_camera_activation_signal_relay(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    section = panel._sections["n1"]
    panel.set_active_camera_id("other_camera")           # generic section → off
    assert section._camera_active is False
    log = _SignalLog()
    panel.camera_activation_toggled.connect(log)
    section._on_camera_activation_toggled(True)
    assert log.calls == [("n1", True)]
    assert section._camera_active is True


def test_push_current_frame_to_editor(qapp, panel: NodePropertiesPanel) -> None:
    calls: List[int] = []
    panel._active_kf_editor = SimpleNamespace(
        set_current_frame=lambda f: calls.append(f))
    panel.push_current_frame_to_editor(12)
    assert calls == [12]
    panel._active_kf_editor = None                        # no editor → no-op
    panel.push_current_frame_to_editor(20)
    assert calls == [12]


def test_update_thumbnail_is_noop(qapp, panel: NodePropertiesPanel) -> None:
    panel.update_thumbnail({"anything": 1})               # removed feature stub


# ── 20. search bar toggle + clear() ────────────────────────────────────────

def test_search_bar_toggle_and_shortcut(qapp, panel: NodePropertiesPanel) -> None:
    assert panel._search_widget.isHidden() is True
    panel._search_toggle_btn.setChecked(True)
    panel._toggle_search_bar()
    assert panel._search_widget.isHidden() is False
    panel._search_toggle_btn.setChecked(False)
    panel._toggle_search_bar()
    assert panel._search_widget.isHidden() is True
    assert panel._search_input.text() == ""               # cleared on hide
    panel._on_search_shortcut()                           # hidden → shows again
    assert panel._search_widget.isHidden() is False


def test_clear_removes_unpinned_sections(qapp, panel: NodePropertiesPanel) -> None:
    panel.set_node("n1", "generic", "Generic", MIXED_PARAMS,
                   param_options=MIXED_OPTIONS, param_defs=MIXED_DEFS)
    panel.clear()
    assert panel._sections == {}
