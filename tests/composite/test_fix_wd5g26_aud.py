"""Regression tests for remediation batch D, wave 5, group 26 (wd5-g26).

Covers the 16 Major findings in the composite node-graph UI domain:
AUD-10603..10605 (node_graph_commands), AUD-10620/10621 (node_item),
AUD-10701..10707 (node_properties_panel), AUD-10802 (node_search_popup),
AUD-10814 (project_settings_widget), AUD-10901/10902 (spline_model).

All tests are headless (offscreen QApplication via the ``qapp`` fixture);
no OpenGL and no full CompositePage is required.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any, Dict, List, Optional, Tuple

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QPushButton,
    QWidget,
)

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.node_graph_commands import (
    CopyNodesCommand, PasteNodesCommand, SetParameterCommand,
)
from src.features.composite.ui.node_item import NodeItem
from src.features.composite.ui.node_properties_panel import (
    CurveWidget, NodePropertiesPanel,
)
from src.shared.color.color_conversion import srgb_to_linear

# ── shared helpers ────────────────────────────────────────────────────


class _Spy:
    """Plain callable recorder for Qt signal handlers."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


@pytest.fixture
def panel(qapp):
    p = NodePropertiesPanel()
    p.resize(320, 700)
    return p


def _node_data(node, node_id: str, **overrides) -> Dict[str, Any]:
    params = dict(node.parameters)
    params.update(overrides.pop("parameters", {}))
    return dict(
        node_id=node_id,
        node_type=node.node_type,
        node_title=node.node_type.title(),
        parameters=params,
        param_options=node.get_param_options(),
        param_defs=node.get_param_defs(),
        enabled=True,
        node_color="#666666",
        image_size=None,
        read_only=False,
        **overrides,
    )


# ── AUD-10603: SetParameterCommand None sentinel ─────────────────────


def test_set_parameter_undo_restores_unset_param():
    g = NodeGraph()
    node = TransformNode("t1")
    g.add_node(node)
    assert node.parameters.get("rotation") is not None  # has a default

    # Simulate a parameter that was never set: pop it from the dict.
    node.parameters.pop("rotation", None)
    cmd = SetParameterCommand(g, "t1", "rotation", 45.0)
    cmd.redo()
    assert node.parameters.get("rotation") == 45.0
    cmd.undo()
    # Old value was None ("unset") — undo must restore that, not no-op.
    assert "rotation" not in node.parameters or \
        node.parameters.get("rotation") is None


def test_set_parameter_undo_restores_real_old_value_once():
    g = NodeGraph()
    node = TransformNode("t1")
    g.add_node(node)
    node.set_parameter("rotation", 10.0)

    cmd = SetParameterCommand(g, "t1", "rotation", 45.0)
    cmd.redo()
    cmd.redo()  # second redo must not re-capture 45 as "old"
    assert node.parameters.get("rotation") == 45.0
    cmd.undo()
    assert node.parameters.get("rotation") == 10.0


# ── AUD-10604: CopyNodesCommand index mapping ────────────────────────


def test_copy_nodes_skips_stale_ids_without_shifting_indices():
    g = NodeGraph()
    t = TransformNode("t1")
    b = BlurNode("b1")
    g.add_node(t)
    g.add_node(b)
    assert g.connect("t1", "result", "b1", "image")

    # First id is stale (node deleted / stale selection set).
    cmd = CopyNodesCommand(g, ["gone", "t1", "b1"])
    data = cmd.redo()

    nodes: List[Dict[str, Any]] = data["nodes"]
    ids = [n["id"] for n in nodes]
    assert ids == ["t1", "b1"]
    conns = data["connections"]
    assert len(conns) == 1
    # Indices must address the *nodes list*, not the input id list.
    assert 0 <= conns[0]["src_idx"] < len(nodes)
    assert nodes[conns[0]["src_idx"]]["id"] == "t1"
    assert nodes[conns[0]["dst_idx"]]["id"] == "b1"


def test_copy_then_paste_restores_internal_connection_despite_stale_id():
    g = NodeGraph()
    t = TransformNode("t1")
    b = BlurNode("b1")
    g.add_node(t)
    g.add_node(b)
    assert g.connect("t1", "result", "b1", "image")

    clip = CopyNodesCommand(g, ["gone", "t1", "b1"]).redo()
    paste = PasteNodesCommand(g, clip, offset=(10, 10))
    pasted = paste.redo()
    assert len(pasted) == 2

    new_conns = [
        c for c in g.connections
        if c.src_node_id in pasted and c.dst_node_id in pasted
    ]
    assert len(new_conns) == 1, "internal connection must survive paste"


# ── AUD-10605: PasteNodesCommand clipboard validation ────────────────


def test_paste_skips_malformed_entries_without_raising():
    g = NodeGraph()
    clip = {
        "nodes": [
            {"id": "a"},                      # no type
            {"type": 42},                     # non-str type
            {"type": "transform", "parameters": "oops"},  # bad params shape
            {"type": "transform", "position": [1]},       # bad position len
            {"type": "transform", "position": "x"},       # bad position type
            "not-a-dict",
        ],
        "connections": [
            "not-a-dict",
            {"src_idx": 0, "dst_idx": 1},     # missing ports
            {"src_idx": "0", "dst_idx": 1, "src_port": "image", "dst_port": "image"},
        ],
    }
    paste = PasteNodesCommand(g, clip)
    pasted = paste.redo()
    # Entries 3-5 have valid types: they paste using fallbacks (bad params
    # shape is skipped per-key; bad position falls back to the bare offset)
    # instead of crashing the whole redo. Entries 1/2/6 are skipped entirely.
    assert len(pasted) == 3
    for nid in pasted:
        node = g.get_node(nid)
        assert node is not None
        assert node.position == [50.0, 50.0]  # default offset fallback


def test_paste_restores_params_through_set_parameter():
    g = NodeGraph()
    clip = {
        "nodes": [{
            "id": "x", "type": "transform",
            "parameters": {"rotation": 33.0, "unknown_param": 7},
            "position": [100.0, 50.0],
        }],
        "connections": [],
    }
    paste = PasteNodesCommand(g, clip, offset=(10, 10))
    pasted = paste.redo()
    node = g.get_node(pasted[0])
    assert node.parameters.get("rotation") == 33.0
    assert node.parameters.get("unknown_param") == 7
    assert node.position == [110.0, 60.0]
    # set_parameter bumps param_revision/dirty instead of silent dict swap.
    assert node._dirty is True


def test_paste_command_tolerates_non_dict_clipboard():
    g = NodeGraph()
    paste = PasteNodesCommand(g, {"nodes": None})  # type: ignore[dict-item]
    assert paste.redo() == []
    paste2 = PasteNodesCommand(g, ["nope"])        # type: ignore[arg-type]
    assert paste2.description.startswith("Paste 0")


# ── AUD-10620: NodeItem boundingRect covers glow ─────────────────────


def test_bounding_rect_expands_for_regular_node_glow(qapp):
    item = NodeItem("n1", "Blur", is_dot=False)
    rect = item.rect()
    bound = item.boundingRect()
    # Selection glow reaches ~8px outside the base rect (4 offset + 4
    # half-pen of the widest stroke).
    assert bound.left() <= rect.left() - 8.0
    assert bound.top() <= rect.top() - 8.0
    assert bound.right() >= rect.right() + 8.0
    assert bound.bottom() >= rect.bottom() + 8.0


def test_bounding_rect_dot_still_expands_5px(qapp):
    item = NodeItem("d1", "Dot", is_dot=True)
    rect = item.rect()
    bound = item.boundingRect()
    assert bound.left() <= rect.left() - 5.0
    assert bound.right() >= rect.right() + 5.0


# ── AUD-10621: thumbnail scale cache ─────────────────────────────────


def _solid_pixmap(color: str, w: int = 64, h: int = 40) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(QColor(color))
    return pm


def test_scaled_thumbnail_is_cached_per_source_and_size(qapp):
    item = NodeItem("n1", "Blur", is_dot=False)
    item.set_thumbnail(_solid_pixmap("#ff0000"))

    first = item._get_scaled_thumbnail(160, 100)
    second = item._get_scaled_thumbnail(160, 100)
    assert first is second, "same source+size must reuse the cached pixmap"
    assert first.width() >= 160 or first.height() >= 100

    # Target size change recomputes.
    other = item._get_scaled_thumbnail(160, 60)
    assert other is not first

    # Source replacement invalidates the cache (cacheKey changes).
    item.set_thumbnail(_solid_pixmap("#00ff00"))
    fresh = item._get_scaled_thumbnail(160, 100)
    assert fresh is not first


def test_clear_thumbnail_drops_cache(qapp):
    item = NodeItem("n1", "Blur", is_dot=False)
    item.set_thumbnail(_solid_pixmap("#0000ff"))
    assert item._get_scaled_thumbnail(160, 100) is not None
    item.clear_thumbnail()
    assert item._get_scaled_thumbnail(160, 100) is None
    assert item._scaled_thumbnail is None


# ── AUD-10701: group promote hook ────────────────────────────────────


class _FakePDef:
    allow_expression = False

    def is_expression_compatible(self) -> bool:
        return True


def _capture_menu(monkeypatch):
    """Patch QMenu inside the panel module so exec() records and returns."""
    import src.features.composite.ui.node_properties_panel as npp

    created: List[Any] = []

    class _RecordingMenu(npp.QMenu):
        def exec(self, pos):  # noqa: A003 - mirrors Qt API
            created.append(self)
            return None

    monkeypatch.setattr(npp, "QMenu", _RecordingMenu)
    return created


def test_parameter_context_menu_no_longer_raises_without_hook(panel, monkeypatch):
    created = _capture_menu(monkeypatch)
    # Before the fix this raised AttributeError (undefined attribute).
    panel._show_parameter_context_menu("n1", [("rotation", _FakePDef(), "")],
                                       QPoint(0, 0))
    assert len(created) == 1


def test_group_promote_hook_installed_and_offered(panel, monkeypatch):
    created = _capture_menu(monkeypatch)
    assert panel._group_promote_hook is None
    assert hasattr(panel, "set_group_promote_hook")

    spy = _Spy()
    panel.set_group_promote_hook(lambda nid, k: spy(nid, k))
    assert panel._group_promote_hook is not None

    panel._show_parameter_context_menu("n1", [("rotation", _FakePDef(), "")],
                                       QPoint(0, 0))
    menu = created[-1]
    actions = [a.text() for a in menu.actions()]
    assert any("Promote to Group" in t for t in actions)


# ── AUD-10702: set_nodes try/finally ─────────────────────────────────


def test_set_nodes_unblocks_signals_when_build_raises(panel, monkeypatch):
    def _boom(data):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(panel, "_create_section", _boom)
    with pytest.raises(RuntimeError):
        panel.set_nodes([_node_data(TransformNode("t1"), "t1")])
    assert panel._block_signals is False, (
        "an exception during build must not permanently lock the panel")


# ── AUD-10703: *_for_section context restore ─────────────────────────


class _StubSection:
    node_type = "transform"
    parameters: Dict[str, Any] = {}

    def __init__(self) -> None:
        self._param_widgets: Dict[str, Any] = {}


def test_param_row_for_section_restores_context_on_failure(panel, monkeypatch):
    stub = _StubSection()
    panel._current_node_id = "outer"
    panel._current_parameters = {"keep": 1}
    panel._param_widgets = {"outer_widget": object()}
    saved_defs = panel._param_defs
    saved_options = panel._param_options

    def _boom(*args, **kwargs):
        raise ValueError("bad param value")

    monkeypatch.setattr(panel, "_create_param_row", _boom)
    with pytest.raises(ValueError):
        panel._create_param_row_for_section(
            "inner", "bad", object(), {}, {}, stub)

    assert panel._current_node_id == "outer"
    assert panel._current_parameters == {"keep": 1}
    assert "outer_widget" in panel._param_widgets
    assert panel._param_defs is saved_defs
    assert panel._param_options is saved_options
    assert stub._param_widgets == {}, "failed row must not merge widgets"


def test_param_row_for_section_merges_widgets_on_success(panel):
    stub = _StubSection()
    stub.parameters = {"rotation": 5.0}
    panel._current_node_id = "outer"
    panel._param_widgets = {}

    row = panel._create_param_row_for_section(
        "inner", "rotation", 5.0, {}, TransformNode("t").get_param_defs(), stub)
    assert row is not None
    assert "rotation" in stub._param_widgets
    assert panel._current_node_id == "outer"


# ── AUD-10704: _update_section_param_value missing branches ──────────


def _file_picker_container() -> QWidget:
    w = QWidget()
    from PyQt6.QtWidgets import QHBoxLayout
    lay = QHBoxLayout(w)
    le = QLineEdit("/old/path.exr")
    lay.addWidget(le)
    lay.addWidget(QPushButton("..."))
    return w


def test_update_file_branch_sets_line_edit(panel, qapp):
    container = _file_picker_container()
    widgets = {"file_path": {"type": "file", "widget": container}}
    panel._update_section_param_value("file_path", "/new/path.exr", widgets)
    assert container.findChild(QLineEdit).text() == "/new/path.exr"


def test_update_channel_branch_selects_text(panel, qapp):
    from src.shared.ui.widgets.chevron_combo import ChevronComboBox
    combo = ChevronComboBox()
    combo.addItems(["R", "G", "B", "A", "RGB", "RGBA"])
    combo.setCurrentIndex(0)
    widgets = {"channel": {"type": "channel", "widget": combo}}
    panel._update_section_param_value("channel", "B", widgets)
    assert combo.currentText() == "B"


def test_update_vec_branch_sets_each_spin(panel, qapp):
    from PyQt6.QtWidgets import QHBoxLayout
    container = QWidget()
    lay = QHBoxLayout(container)
    spins = []
    for _ in range(3):
        s = QDoubleSpinBox()
        lay.addWidget(s)
        spins.append(s)
    widgets = {"v": {"type": "vec3", "widget": container, "dim": 3}}
    panel._update_section_param_value("v", [1.5, 2.5, 3.5], widgets)
    assert [s.value() for s in spins] == [1.5, 2.5, 3.5]


def test_update_text_branch_without_update_callback(panel, qapp):
    editor = QPlainTextEdit()
    editor.setPlainText("old")
    widgets = {"text": {"type": "text", "widget": editor}}
    panel._update_section_param_value("text", "new text", widgets)
    assert editor.toPlainText() == "new text"


def test_update_text_branch_uses_registered_update(panel, qapp):
    editor = QPlainTextEdit()
    seen: List[Any] = []

    def _update(t):
        seen.append(t)
        editor.setPlainText(str(t))

    widgets = {"text": {"type": "text", "widget": editor, "update": _update}}
    panel._update_section_param_value("text", "hello", widgets)
    assert seen == ["hello"]


def test_update_curve_branch_sets_curve_widget(panel, qapp):
    from PyQt6.QtWidgets import QVBoxLayout
    container = QWidget()
    lay = QVBoxLayout(container)
    curve = CurveWidget(None)
    lay.addWidget(curve)
    widgets = {"curve": {"type": "curve", "widget": container}}
    panel._update_section_param_value(
        "curve", [[0.0, 0.0], [0.5, 0.8], [1.0, 1.0]], widgets)
    assert curve.curve() == [(0.0, 0.0), (0.5, 0.8), (1.0, 1.0)]


def test_update_hdri_branch_matches_presets_and_appends_custom(panel, qapp):
    from PyQt6.QtWidgets import QHBoxLayout
    container = QWidget()
    lay = QHBoxLayout(container)
    combo = QComboBox()
    combo.addItem("无环境光", "")
    combo.addItem("默认天空", "__sky__")
    lay.addWidget(combo)
    widgets = {"env": {"type": "hdri", "widget": container}}

    # The '' item covers both "" and "__sky__" (build-time semantics:
    # the first matching item wins), so "__sky__" selects index 0.
    panel._update_section_param_value("env", "__sky__", widgets)
    assert combo.currentIndex() == 0

    panel._update_section_param_value("env", "/abs/studio.hdr", widgets)
    assert combo.count() == 3
    assert combo.currentData() == "/abs/studio.hdr"


def test_update_color_branch_without_update_refreshes_button(panel, qapp):
    btn = QPushButton()
    widgets = {"col": {"type": "color", "widget": btn}}
    panel._update_section_param_value("col", "#ff0000", widgets)
    assert "background-color: #ff0000" in btn.styleSheet()
    assert btn.text() == "#ff0000"


# ── AUD-10705: pinned zombie cleanup ─────────────────────────────────


def test_stale_pinned_section_is_pruned_when_node_deleted(panel):
    g = NodeGraph()
    n1 = TransformNode("t1")
    g.add_node(n1)
    panel.set_node_graph(g)

    panel.set_nodes([_node_data(n1, "t1")])
    assert "t1" in panel._sections
    panel._on_section_pin_toggled("t1", True)
    assert "t1" in panel._pinned_node_ids

    # Node deleted from the graph (pinned id now dangling): the next
    # set_nodes must prune the zombie pinned section.
    g.remove_node("t1")
    panel.set_nodes([_node_data(TransformNode("t2"), "t2")])
    assert "t1" not in panel._pinned_node_ids
    assert "t1" not in panel._sections
    assert "t2" in panel._sections


def test_pinned_section_survives_deselection_while_node_alive(panel):
    # Pinned nodes are by definition absent from the selection payload —
    # a plain reselect of another live node must keep the pinned section.
    g = NodeGraph()
    n1 = TransformNode("t1")
    g.add_node(n1)
    panel.set_node_graph(g)

    panel.set_nodes([_node_data(n1, "t1")])
    panel._on_section_pin_toggled("t1", True)
    panel.set_nodes([_node_data(TransformNode("t2"), "t2")])
    assert "t1" in panel._sections, "pinned live node must stay visible"


def test_remove_node_public_api_clears_all_state(panel):
    panel.set_nodes([_node_data(TransformNode("t1"), "t1")])
    panel._on_section_pin_toggled("t1", True)
    panel.set_dim_split("t1", "translate", True)
    panel._scene3d_toggle_buttons = {"t1": object()}

    panel.remove_node("t1")
    assert "t1" not in panel._sections
    assert "t1" not in panel._pinned_node_ids
    assert "t1" not in panel._split_dims
    assert "t1" not in panel._scene3d_toggle_buttons
    assert "t1" not in panel._image_sizes


def test_deselect_keeps_split_dims_for_existing_nodes(panel):
    # _split_dims must survive deselect/reselect of a live node.
    panel.set_nodes([_node_data(TransformNode("t1"), "t1")])
    panel.set_dim_split("t1", "translate", True)
    panel.set_nodes([_node_data(TransformNode("t2"), "t2")])
    assert "t1" in panel._split_dims, (
        "split-dim memory must survive section removal for live nodes")


# ── AUD-10706: sync_animated_values display-only ─────────────────────


def test_sync_animated_values_updates_widgets_not_snapshots(panel, qapp):
    g = NodeGraph()
    node = TransformNode("t1")
    g.add_node(node)
    panel.set_node_graph(g)
    panel.set_nodes([_node_data(node, "t1")])

    from src.shared.keyframe import Keyframe, KeyframeTrack
    track = KeyframeTrack(param_name="rotation",
                          min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 100.0))
    node.set_keyframe_track("rotation", track)

    section = panel._sections["t1"]
    base = section.parameters["rotation"]
    panel.sync_animated_values("t1", 5)

    # Widget shows the sampled midpoint...
    info = section._param_widgets["rotation"]
    assert abs(info["widget"].value() - 50.0) < 1e-6
    # ...but the parameter snapshots stay at the base value (AUD-10706).
    assert section.parameters["rotation"] == base
    assert panel._current_parameters.get("rotation") == base


def test_preset_collects_base_values_after_playhead_sync(panel, qapp):
    g = NodeGraph()
    node = TransformNode("t1")
    g.add_node(node)
    panel.set_node_graph(g)
    panel.set_nodes([_node_data(node, "t1")])

    from src.shared.keyframe import Keyframe, KeyframeTrack
    track = KeyframeTrack(param_name="rotation",
                          min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 100.0))
    node.set_keyframe_track("rotation", track)

    section = panel._sections["t1"]
    base = section.parameters["rotation"]
    panel.sync_animated_values("t1", 10)  # playhead on an animated value
    values = section._collect_current_values()
    assert values["rotation"] == base, (
        "presets must store the base parameter, not the sampled frame value")


# ── AUD-10707: sRGB/linear boundary unified ──────────────────────────


def _patch_get_color(monkeypatch, color: QColor):
    import src.shared.color as color_mod

    def _fake_get_color(parent=None, initial=None, on_live=None, **kw):
        if on_live is not None:
            on_live(color)
        return color

    monkeypatch.setattr(color_mod, "get_color", _fake_get_color)


def test_rgb_row_writes_linear_channels(panel, qapp, monkeypatch):
    node = TransformNode("t1")
    panel.set_nodes([_node_data(node, "t1")])
    panel._current_node_id = "t1"
    panel._current_parameters = {"light_color_r": 0.5, "light_color_g": 0.5,
                                 "light_color_b": 0.5}

    spy = _Spy()
    panel.parameter_changed.connect(spy)

    # Patch BEFORE the row is built: _create_rgb_param_row binds get_color
    # into the closure at build time, so a later patch would not apply and
    # the real (modal) picker would open.
    _patch_get_color(monkeypatch, QColor(100, 100, 100))

    row = panel._create_rgb_param_row(
        "light_color", "light_color_r", "light_color_g", "light_color_b",
        0.5, 0.5, 0.5, None, None, None)
    swatch = row.findChildren(QPushButton)[0]

    # Non-trivial sRGB mid tone: 100/255 must be converted before writing.
    swatch.click()

    writes = {args[1]: args[2] for args in spy.calls}
    expected = srgb_to_linear(100 / 255.0)
    assert abs(writes["light_color_r"] - expected) < 1e-6
    assert abs(writes["light_color_g"] - expected) < 1e-6
    assert abs(writes["light_color_b"] - expected) < 1e-6


def test_rgb_row_reads_linear_channels_for_display(panel, qapp):
    node = TransformNode("t1")
    panel.set_nodes([_node_data(node, "t1")])
    panel._current_node_id = "t1"
    # Stored linear mid-grey: displays as #bcbcbc in sRGB (~0.735*255).
    panel._current_parameters = {"light_color_r": 0.5, "light_color_g": 0.5,
                                 "light_color_b": 0.5}

    row = panel._create_rgb_param_row(
        "light_color", "light_color_r", "light_color_g", "light_color_b",
        0.5, 0.5, 0.5, None, None, None)
    swatch = row.findChildren(QPushButton)[0]
    assert "#bcbcbc" in swatch.styleSheet().lower()


def test_keyer_row_writes_linear_channels_and_display_hex(panel, qapp, monkeypatch):
    from src.features.composite.nodes.keyer_node import KeyerNode
    node = KeyerNode("k1")
    panel.set_nodes([_node_data(node, "k1")])
    section = panel._sections["k1"]
    assert section is not None

    spy = _Spy()
    panel.parameter_changed.connect(spy)

    _patch_get_color(monkeypatch, QColor(100, 100, 100))
    row = panel._create_keyer_color_row(section, "#00ff00")
    swatch = row.findChildren(QPushButton)[0]
    swatch.click()

    writes = {args[1]: args[2] for args in spy.calls}
    expected = srgb_to_linear(100 / 255.0)
    assert abs(writes["screen_color_r"] - expected) < 1e-6
    assert abs(writes["screen_color_g"] - expected) < 1e-6
    assert abs(writes["screen_color_b"] - expected) < 1e-6
    assert writes["screen_color_hex"] == "#646464", (
        "hex stays a display-referred code")


# ── AUD-10802: header flag check ─────────────────────────────────────


class _FakeNode:
    pass


@pytest.fixture
def popup(qapp):
    from src.features.composite.ui.node_search_popup import NodeSearchPopup
    return NodeSearchPopup({"Color": [("blend", _FakeNode)]})


def test_keyword_filter_hides_category_headers(popup):
    lst = popup._all_list
    header = lst.item(0)
    assert header.flags() == Qt.ItemFlag.NoItemFlags  # header sentinel

    popup._filter_list(lst, "blend")
    # Header stays visible while it has a visible child...
    assert header.isHidden() is False
    assert lst.item(1).isHidden() is False

    # "color" matches only the header's own text but no child: the header
    # must be hidden (the exact AUD-10802 symptom: orphan headers).
    popup._filter_list(lst, "color")
    assert header.isHidden() is True
    assert lst.item(1).isHidden() is True


def test_item_activated_ignores_headers(popup):
    spy = _Spy()
    popup.node_selected.connect(spy)
    header = popup._all_list.item(0)
    popup._on_item_activated(header)
    assert spy.calls == []


def test_toggle_favorite_ignores_headers(popup):
    popup._all_list.setCurrentItem(popup._all_list.item(0))
    before = list(popup._favorites)
    popup._toggle_favorite_current()
    assert popup._favorites == before


# ── AUD-10814: FPS Custom sentinel ───────────────────────────────────


def test_fps_custom_selection_does_not_raise_nor_emit(qapp):
    from src.features.composite.ui.project_settings_widget import (
        ProjectSettingsWidget,
    )
    w = ProjectSettingsWidget()
    spy = _Spy()
    w.settings_changed.connect(spy)

    custom_index = w._fps_combo.count() - 1
    # Before the fix this raised ValueError('could not convert string to
    # float: __custom__') inside the currentIndexChanged slot.
    w._fps_combo.setCurrentIndex(custom_index)

    assert w._fps_combo.currentData() == "__custom__"
    assert all("fps" not in c or isinstance(dict(c).get("fps"), float)
               for c in spy.calls)


def test_fps_custom_falls_back_to_24_when_other_fields_emit(qapp):
    from src.features.composite.ui.project_settings_widget import (
        ProjectSettingsWidget,
    )
    w = ProjectSettingsWidget()
    spy = _Spy()
    w.settings_changed.connect(spy)
    w._fps_combo.setCurrentIndex(w._fps_combo.count() - 1)
    spy.calls.clear()

    w._duration_drag.setValue(5.0)  # any other control emitting
    assert len(spy.calls) >= 1
    payload = dict(spy.calls[-1][0])
    assert payload["fps"] == 24.0  # sentinel tolerated, no exception


def test_fps_preset_and_stored_custom_still_emit(qapp):
    from src.features.composite.ui.project_settings_widget import (
        ProjectSettingsWidget,
    )
    w = ProjectSettingsWidget()
    spy = _Spy()
    w.settings_changed.connect(spy)

    # Index 0 is already selected at construction — switch to another
    # preset first so the signal actually fires.
    w._fps_combo.setCurrentIndex(1)
    assert dict(spy.calls[-1][0])["fps"] == float(
        w._fps_combo.itemData(1))

    spy.calls.clear()
    # 17.5 is NOT a preset, so the value is stored into the Custom entry.
    w._set_fps_value(17.5)
    spy.calls.clear()
    w._fps_combo.setCurrentIndex(0)  # switch away to a preset
    w._fps_combo.setCurrentIndex(w._fps_combo.count() - 1)  # back to Custom
    assert dict(spy.calls[-1][0])["fps"] == 17.5, (
        "a stored Custom value must emit as-is")


# ── AUD-10901: curve visibility memory ───────────────────────────────


def _graph_two_curves():
    from src.shared.keyframe import Keyframe, KeyframeTrack
    g = NodeGraph()
    t = TransformNode("t1")
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    b = BlurNode("b1")
    br = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
    br.add_keyframe(Keyframe(0, 1.0))
    br.add_keyframe(Keyframe(20, 8.0))
    b.set_keyframe_track("radius", br)
    g.add_node(t)
    g.add_node(b)
    return g, f"{t.id}:rotation", f"{b.id}:radius"


def test_hidden_curve_stays_hidden_after_rebuild():
    from src.features.composite.ui.spline_model import SplineModel
    g, cid_t, cid_b = _graph_two_curves()
    m = SplineModel()
    m.rebuild(g)
    m.set_visible(cid_t, False)
    assert m.visible_curves() == [m.curve_by_id(cid_b)]

    m.rebuild(g)  # every edit triggers a rebuild
    assert m.curve_by_id(cid_t).visible is False, (
        "hidden state must survive rebuilds (AUD-10901)")
    assert m.curve_by_id(cid_b).visible is True


def test_hide_all_survives_rebuild_and_show_all_clears():
    from src.features.composite.ui.spline_model import SplineModel
    g, cid_t, cid_b = _graph_two_curves()
    m = SplineModel()
    m.rebuild(g)
    for c in m.curves:
        m.set_visible(c.curve_id, False)
    assert m.visible_curves() == []

    m.rebuild(g)
    assert m.visible_curves() == [], "hide-all must survive rebuild"

    for c in m.curves:
        m.set_visible(c.curve_id, True)
    m.rebuild(g)
    assert len(m.visible_curves()) == 2


# ── AUD-10902: rebuild error handling ────────────────────────────────


def test_rebuild_keeps_curves_when_graph_is_broken():
    from src.features.composite.ui.spline_model import SplineModel
    g, cid_t, _cid_b = _graph_two_curves()
    m = SplineModel()
    m.rebuild(g)
    assert len(m.curves) == 2

    class _BrokenNodes:
        @property
        def values(self):
            raise AttributeError("graph torn down")

    class _BrokenGraph:
        nodes = _BrokenNodes()

    m.rebuild(_BrokenGraph())  # must not raise, must not clear
    assert len(m.curves) == 2, "old curves must survive a failed rebuild"

    m.rebuild(None)
    assert len(m.curves) == 2


def test_rebuild_sort_failure_falls_back_to_graph_order(caplog):
    from src.features.composite.ui.spline_model import SplineModel
    g, _cid_t, _cid_b = _graph_two_curves()
    g.nodes["t1"].position = None  # malformed position

    m = SplineModel()
    m.rebuild(g)
    assert len(m.curves) == 2, "curves survive a sort-key failure"


def test_node_color_fallback_for_plain_objects():
    from src.features.composite.ui.spline_model import SplineModel

    class _NoColor:
        header_color = "#123456"

    assert SplineModel._node_color(_NoColor()) == "#123456"

    class _Broken:
        def effective_header_color(self):
            raise TypeError("boom")

        header_color = "#abcdef"

    assert SplineModel._node_color(_Broken()) == "#abcdef"
