"""Batch A (w2-g1) regression tests for four Critical audit findings.

Covers:
- AUD-10801: ``Qt.ItemFlag.UserRole`` does not exist in PyQt6 — the Add Node
  search popup used it in 7 places and crashed with AttributeError during
  ``__init__``. Fixed to ``Qt.ItemDataRole.UserRole``.
- AUD-10910: Spline view context menu connected module-level functions
  (``_delete_selected_keys`` / ``_select_all_keys``) directly to
  ``QAction.triggered``, so ``checked=False`` was passed as the ``self``
  argument → AttributeError inside a Qt slot (process abort under PyQt6).
  Fixed with bound lambdas.
- AUD-9500: ``CompositePage._retry_renderer_init`` referenced an undefined
  ``viewer_node_id`` local → NameError once the deferred init succeeded.
  Fixed by resolving the viewer node id inside the retry path.
- AUD-10700: parameter edits made in a non-first section (multi-selection /
  pinned) were routed to the first selected node. Row builders now bind the
  owning node id at build time and ``_on_param_changed`` routes through it.

Everything here is headless (QT_QPA_PLATFORM=offscreen); no GL, no rendering.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QMenu

from src.features.composite.nodes.base_node import ParamDef
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel


# ── shared panel builders (mirrors test_node_properties_panel_coverage) ─────

def _pd(name: str, ptype: str = "float", default: Any = None, **kw: Any) -> ParamDef:
    return ParamDef(name=name, ptype=ptype, default=default, **kw)


DEFS: Dict[str, ParamDef] = {
    "gain": _pd("gain", default=1.0, min_val=0.0, max_val=2.0, step=0.01),
    "filter": _pd("filter", "option", default="gaussian"),
    "offset": _pd("offset", "vec2", default=(1.0, 2.0),
                  min_val=-100.0, max_val=100.0),
}
OPTIONS: Dict[str, List[str]] = {"filter": ["box", "gaussian", "lanczos"]}


def _node_data(node_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": "generic",
        "node_title": node_id.upper(),
        "parameters": dict(parameters),
        "param_options": dict(OPTIONS),
        "param_defs": dict(DEFS),
        "state": "dirty",
        "progress": 0.0,
        "error_message": "",
        "enabled": True,
        "node_color": "#666666",
        "image_size": None,
        "read_only": False,
    }


class _SignalLog:
    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(tuple(args))


@pytest.fixture
def panel(qapp) -> NodePropertiesPanel:
    return NodePropertiesPanel()


# ── AUD-10801: NodeSearchPopup construct + UserRole round-trip ──────────────

class _FakeNode:
    node_title = "Fake Node"


def test_node_search_popup_constructs_and_stores_user_role(qapp, monkeypatch):
    """Pre-fix: ``__init__`` raised AttributeError (Qt.ItemFlag.UserRole)."""
    from src.features.composite.ui.node_search_popup import NodeSearchPopup

    # Keep the test hermetic — do not read/write the user's real QSettings.
    monkeypatch.setattr(NodeSearchPopup, "_load_favorites", lambda self: [])
    monkeypatch.setattr(NodeSearchPopup, "_load_recent_nodes", lambda self: [])

    categories: Dict[str, List[Tuple[str, type]]] = {
        "Color": [("constant", _FakeNode), ("grade", _FakeNode)],
        "Filter": [("blur", _FakeNode)],
    }
    popup = NodeSearchPopup(categories)  # must not raise

    # Node rows carry their node_type under the UserRole data role.
    node_types = []
    for i in range(popup._all_list.count()):
        data = popup._all_list.item(i).data(Qt.ItemDataRole.UserRole)
        if data is not None:
            node_types.append(data)
    assert "constant" in node_types
    assert "blur" in node_types
    # NOTE: ``_filter_list`` is NOT exercised here — its header handling
    # (``item.flags() & Qt.ItemFlag.NoItemFlags`` is always falsy, so headers
    # with ``node_type=None`` reach ``filter_text in node_type``) is the
    # separately tracked AUD-10802 finding and stays out of this batch.
    popup.close()


# ── AUD-10910: spline context menu actions bind the view ────────────────────

def _make_spline_view(qapp):
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode
    from src.features.composite.ui.spline_model import SplineModel
    from src.features.composite.ui.spline_view import SplineView
    from src.shared.keyframe import Keyframe, KeyframeTrack

    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    for f, v in [(0, 0.0), (10, 30.0)]:
        tr.add_keyframe(Keyframe(f, v))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m


def test_spline_context_menu_delete_and_select_all_receive_view(
        qapp, monkeypatch):
    """``triggered(bool)`` must not inject ``False`` as the module function's
    ``self`` argument — pre-fix, either menu entry aborted the process."""
    from src.features.composite.ui import spline_view as sv
    from src.shared.i18n.i18n import I18N

    view, model = _make_spline_view(qapp)
    model.curves[0].keys[0].selected = True  # enables the Delete entry

    recorded_delete: List[Any] = []
    recorded_select_all: List[Any] = []

    def _fake_delete(self_arg):
        recorded_delete.append(self_arg)

    def _fake_select_all(self_arg):
        recorded_select_all.append(self_arg)

    monkeypatch.setattr(sv, "_delete_selected_keys", _fake_delete)
    monkeypatch.setattr(sv, "_select_all_keys", _fake_select_all)

    captured_menus: List[QMenu] = []
    monkeypatch.setattr(
        QMenu, "exec",
        lambda menu, *a, **k: (captured_menus.append(menu), None)[1])

    event = SimpleNamespace(globalPos=lambda: QPoint(10, 10))
    sv._sv_contextMenuEvent(view, event)
    assert captured_menus, "context menu was not built"

    menu = captured_menus[0]
    delete_label = I18N.tr("keyframes.delete_kf", "Delete keyframes")
    select_all_label = I18N.tr("keyframes.select_all", "Select all")
    by_text = {action.text(): action for action in menu.actions()}

    assert delete_label in by_text
    assert select_all_label in by_text
    by_text[delete_label].trigger()
    by_text[select_all_label].trigger()

    # The bound module functions must receive the SplineView, never the
    # ``checked=False`` bool that triggered(bool) passes to bare functions.
    assert recorded_delete == [view]
    assert recorded_select_all == [view]
    view.close()


# ── AUD-9500: _retry_renderer_init resolves the viewer node id ─────────────

def test_retry_renderer_init_resolves_viewer_node_id(monkeypatch):
    """Pre-fix: NameError on ``viewer_node_id`` once initialize_renderer
    succeeds; the retry path submitted nothing and aborted."""
    from src.features.composite.render.composite_renderer import CompositeCommand
    from src.features.composite.ui.composite_page import CompositePage
    from src.features.composite.render.render_scheduler import RenderRequest

    submitted: List[Tuple[Any, Dict[str, Any]]] = []

    class _FakeRenderer:
        def submit(self, command: Any, payload: Dict[str, Any]) -> None:
            submitted.append((command, payload))

    class _Graph:
        def snapshot(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(generation=1)

    fake = SimpleNamespace(
        _renderer=None,
        _is_active=True,
        _request_id_gen=SimpleNamespace(next_id=lambda: 7),
        _node_graph=_Graph(),
        _last_color_texture_id=None,
        _last_color_width=0,
        _last_color_height=0,
        _last_color_revision=0,
        _current_keyframe_frame=lambda: 0,
        comp_clip_fps=lambda: 24.0,
        _inject_viewer3d_scene_snapshot=lambda _snap: _snap,
        _apply_view_mode_overrides=lambda _snap: None,
        _runtime_context=None,
        _timeline_clip_gpu_provider=None,
        _project=None,
        _resolve_viewer_node_id=lambda: "viewer-node-1",
        _view_signature=lambda: "",
        _is_quad_view=lambda: False,
        _single_ortho_view_code=lambda: None,
        _quad_active_view_code=lambda: None,
        _selected_3d_node_id_for_request=lambda: None,
        _isolate_3d_node_id_for_request=lambda: None,
        _global_3d_scene_settings=lambda: None,
        _preview_quality=1.0,
        _gl_retry_count=0,
    )

    def _initialize_renderer() -> None:
        fake._renderer = _FakeRenderer()

    fake.initialize_renderer = _initialize_renderer

    # Must not raise (pre-fix NameError: viewer_node_id undefined).
    CompositePage._retry_renderer_init(fake)

    assert len(submitted) == 1
    command, payload = submitted[0]
    assert command == CompositeCommand.EVALUATE_GRAPH
    request = payload["request"]
    assert isinstance(request, RenderRequest)
    assert request.viewer_node_id == "viewer-node-1"


# ── AUD-10700: per-section routing (beyond the flipped coverage locks) ─────

def test_combo_edit_in_second_section_routes_to_own_node(
        qapp, panel: NodePropertiesPanel) -> None:
    panel.set_nodes([
        _node_data("n1", {"gain": 1.0, "filter": "box"}),
        _node_data("n2", {"gain": 1.0, "filter": "box"}),
    ])
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    combo = panel._sections["n2"]._param_widgets["filter"]["widget"]
    combo.setCurrentIndex(1)  # "gaussian"
    assert log.calls == [("n2", "filter", "gaussian")]
    assert panel._sections["n2"].parameters["filter"] == "gaussian"
    assert panel._sections["n1"].parameters["filter"] == "box"


def test_vec2_edit_in_second_section_routes_to_own_node(
        qapp, panel: NodePropertiesPanel) -> None:
    from PyQt6.QtWidgets import QDoubleSpinBox
    panel.set_nodes([
        _node_data("n1", {"gain": 1.0, "offset": (1.0, 2.0)}),
        _node_data("n2", {"gain": 1.0, "offset": (1.0, 2.0)}),
    ])
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    spins = panel._sections["n2"]._param_widgets["offset"]["widget"] \
        .findChildren(QDoubleSpinBox)
    spins[0].setValue(5.5)
    assert log.calls == [("n2", "offset", [5.5, 2.0])]
    assert panel._sections["n2"].parameters["offset"] == [5.5, 2.0]
    assert panel._sections["n1"].parameters["offset"] == (1.0, 2.0)


def test_pinned_section_edit_routes_to_pinned_node(
        qapp, panel: NodePropertiesPanel) -> None:
    """Pin n2, then select n1: n2's section stays visible and its edits must
    still write n2 (pre-fix they silently wrote n1)."""
    panel.set_nodes([_node_data("n2", {"gain": 1.0})])
    panel._sections["n2"]._pin_btn.click()
    assert "n2" in panel._pinned_node_ids

    panel.set_nodes([_node_data("n1", {"gain": 1.0})])
    assert "n2" in panel._sections  # pinned section survives
    assert panel._current_node_id == "n1"

    log = _SignalLog()
    panel.parameter_changed.connect(log)
    drag = panel._sections["n2"]._param_widgets["gain"]["widget"]
    drag.setValue(0.25)
    assert log.calls == [("n2", "gain", 0.25)]
    assert panel._sections["n2"].parameters["gain"] == 0.25
    assert panel._sections["n1"].parameters["gain"] == 1.0


def test_keyframes_changed_routes_to_requested_node(
        qapp, panel: NodePropertiesPanel) -> None:
    """The keyframe write path honours the routed node id (◆ buttons bind it
    at build time)."""
    panel.set_nodes([
        _node_data("n1", {"gain": 1.0}),
        _node_data("n2", {"gain": 1.0}),
    ])
    log = _SignalLog()
    panel.keyframes_changed.connect(log)
    kfs = [{"frame": 0, "value": 0.5}]
    panel._on_keyframes_changed("gain", DEFS["gain"], kfs, "n2")
    assert log.calls == [("n2", "generic", "gain", kfs)]


def test_first_section_routing_unchanged(
        qapp, panel: NodePropertiesPanel) -> None:
    """Panel-level routing for the first (current) node keeps legacy
    behaviour, including the panel parameter cache."""
    panel.set_nodes([
        _node_data("n1", {"gain": 1.0}),
        _node_data("n2", {"gain": 1.0}),
    ])
    log = _SignalLog()
    panel.parameter_changed.connect(log)
    drag = panel._sections["n1"]._param_widgets["gain"]["widget"]
    drag.setValue(0.75)
    assert log.calls == [("n1", "gain", 0.75)]
    assert panel._current_parameters["gain"] == 0.75
    assert panel._sections["n1"].parameters["gain"] == 0.75
    assert panel._sections["n2"].parameters["gain"] == 1.0
