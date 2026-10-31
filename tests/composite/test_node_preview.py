"""Node-preview pipeline tests (Phase 1: pure backend).

Validates the mechanisms that let the viewer show an arbitrary node's output
instead of the Output node's ``display`` port (Fusion/Nuke-style node preview).
See ``docs/composite_node_preview_pipeline_plan_2026-06-29.md``.

Phase 1 adds three things, all tested here without a GL context:

1. ``RenderRequest.viewer_node_id`` / ``viewer_node_port`` fields (default
   None ⇒ legacy "always show Output" behaviour).
2. ``BaseEvaluator._first_image_port`` — picks the output port to show.
3. ``BaseEvaluator._resolve_viewer_node_output`` — reads the previewed node's
   texture from the per-evaluation cache dict and returns it, falling back to
   None on any miss so the caller can show Output instead.
"""

from types import SimpleNamespace

import pytest

from src.features.composite.core.graph_snapshot import NodeSnapshot
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.render.render_scheduler import RenderRequest


# ── RenderRequest fields ──────────────────────────────────────────────────

class TestRenderRequestViewerNode:
    """RenderRequest carries an optional preview target node id/port."""

    def _make(self, **kw):
        defaults = dict(snapshot=None, request_id=1, timestamp=0.0)
        defaults.update(kw)
        return RenderRequest(**defaults)

    def test_defaults_are_none(self):
        """Legacy behaviour: no preview target ⇒ show Output."""
        req = self._make()
        assert req.viewer_node_id is None
        assert req.viewer_node_port is None

    def test_can_set_viewer_node_id(self):
        req = self._make(viewer_node_id="scene3d_1")
        assert req.viewer_node_id == "scene3d_1"
        assert req.viewer_node_port is None  # auto-pick

    def test_can_set_explicit_port(self):
        req = self._make(viewer_node_id="merge_1", viewer_node_port="result")
        assert req.viewer_node_id == "merge_1"
        assert req.viewer_node_port == "result"

    def test_default_none_preserves_legacy_behaviour(self):
        """viewer_node_id=None is the sentinel for Output behaviour.

        The evaluator only takes the preview branch when viewer_node_id is
        truthy, so None must round-trip unchanged.
        """
        req = self._make(viewer_node_id=None)
        assert not req.viewer_node_id  # falsy → Output branch


# ── _first_image_port ─────────────────────────────────────────────────────

def _make_evaluator():
    """Build a BaseEvaluatorMixin instance without running __init__ (no GL needed).

    The preview helpers only use ``self`` for method dispatch — they don't
    touch any instance state — so a bare, uninitialised instance is enough.
    """
    from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
    return BaseEvaluatorMixin.__new__(BaseEvaluatorMixin)


def _ns(node_type, output_ports):
    return NodeSnapshot(
        node_id="x", node_type=node_type, parameters={},
        disabled=False, input_ports=[], output_ports=output_ports,
    )


class TestFirstImagePort:
    """``_first_image_port`` picks which port's texture the viewer shows."""

    def test_output_node_returns_display(self):
        ev = _make_evaluator()
        assert ev._first_image_port(_ns("output", [("display", "image")])) == "display"

    def test_prefers_image_typed_port(self):
        ev = _make_evaluator()
        ns = _ns("multi", [("mask", "mask"), ("result", "image")])
        assert ev._first_image_port(ns) == "result"

    def test_first_port_when_no_image_type(self):
        ev = _make_evaluator()
        ns = _ns("weird", [("a", "scalar"), ("b", "scalar")])
        assert ev._first_image_port(ns) == "a"

    def test_falls_back_to_result_when_no_ports(self):
        ev = _make_evaluator()
        assert ev._first_image_port(_ns("source", [])) == "result"


# ── _resolve_viewer_node_output ───────────────────────────────────────────

def _tex(tid):
    """Make a valid TextureResult with a given GL name."""
    return TextureResult(texture_id=tid, width=10, height=10)


class TestResolveViewerNodeOutput:
    """The preview resolver reads the requested node's texture from eval cache.

    It must use the per-evaluation ``cache`` dict (which covers ALL evaluated
    nodes, including dynamic-bypass nodes that skip the persistent CacheManager)
    so timeline/video preview targets keep working.
    """

    def test_returns_target_node_image_port(self):
        ev = _make_evaluator()
        snap = SimpleNamespace(nodes={
            "merge_1": _ns("merge", [("result", "image")]),
        })
        cache = {"merge_1": {"result": _tex(42)}}
        out = ev._resolve_viewer_node_output(snap, cache, "merge_1", None)
        assert out is not None and out.texture_id == 42

    def test_explicit_port_overrides_auto_pick(self):
        ev = _make_evaluator()
        snap = SimpleNamespace(nodes={
            "split": _ns("split", [("mask", "mask"), ("result", "image")]),
        })
        cache = {"split": {"mask": _tex(7), "result": _tex(8)}}
        out = ev._resolve_viewer_node_output(snap, cache, "split", "mask")
        assert out.texture_id == 7

    def test_none_when_node_not_evaluated(self):
        """Node absent from cache (deleted / errored) ⇒ None ⇒ fall back to Output."""
        ev = _make_evaluator()
        snap = SimpleNamespace(nodes={"x": _ns("x", [("result", "image")])})
        assert ev._resolve_viewer_node_output(snap, {}, "x", None) is None

    def test_none_when_node_not_in_snapshot(self):
        """Stale viewer_node_id referencing a removed node ⇒ None."""
        ev = _make_evaluator()
        cache = {"x": {"result": _tex(1)}}
        assert ev._resolve_viewer_node_output(SimpleNamespace(nodes={}), cache, "x", None) is None

    def test_none_when_port_missing(self):
        """Requested port not produced ⇒ None ⇒ fall back to Output."""
        ev = _make_evaluator()
        snap = SimpleNamespace(nodes={"x": _ns("x", [("result", "image")])})
        cache = {"x": {}}  # evaluated but produced no output
        assert ev._resolve_viewer_node_output(snap, cache, "x", None) is None

    def test_none_when_texture_invalid(self):
        """Invalid texture (texture_id<=0) ⇒ is_valid False ⇒ None."""
        ev = _make_evaluator()
        snap = SimpleNamespace(nodes={"x": _ns("x", [("result", "image")])})
        cache = {"x": {"result": TextureResult(texture_id=0)}}
        assert ev._resolve_viewer_node_output(snap, cache, "x", None) is None

    def test_reads_from_dynamic_bypass_node(self):
        """Dynamic-bypass nodes (timeline/video) live in the eval cache dict,
        not CacheManager — so the resolver must read from there, not the
        persistent cache. This test pins that requirement.
        """
        ev = _make_evaluator()
        snap = SimpleNamespace(nodes={"timeline": _ns("timeline", [("result", "image")])})
        cache = {"timeline": {"result": _tex(99)}}
        out = ev._resolve_viewer_node_output(snap, cache, "timeline", None)
        assert out is not None and out.texture_id == 99


# ── Phase 2: page toggle logic + editor preview connection ─────────────────

@pytest.fixture()
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeNode:
    """Minimal node stand-in for preview-toggle tests (no NodeGraph needed)."""

    def __init__(self, node_id, node_type="scene3d", title="Scene3D"):
        self.id = node_id
        self.node_type = node_type
        self.node_title = title
        self.parameters = {}  # mutable persistent params (override must not touch)


def _make_page_with_fakes(qt_app, nodes, view_mode="2d"):
    """Build a CompositePage with stubbed editor/graph/viewer.

    The preview toggle methods only read ``_node_editor.get_selected_node_id``,
    ``_node_graph.get_node``, and call ``_evaluate_graph_now`` + the editor/
    viewer ``set_preview_node`` hooks — all stubbed here so no GL/render runs.
    ``view_mode`` controls ``_viewer.get_view_mode()`` for Phase 3 grid tests.
    """
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage()
    # Stub the editor.
    state = {"selected": None, "view_mode": view_mode}

    class _FakeEditor:
        def get_selected_node_id(self):
            return state["selected"]

        def set_preview_node(self, nid):
            state["editor_preview"] = nid

    page._node_editor = _FakeEditor()

    # Stub the node graph: get_node by id + _viewer_state for global 3D settings.
    nodes_by_id = {n.id: n for n in nodes}

    class _FakeGraph:
        def __init__(self):
            # v9: _apply_view_mode_overrides reads _viewer_state["default_3d_scene"]
            self._viewer_state = {}

        def get_node(self, nid):
            return nodes_by_id.get(nid)

    page._node_graph = _FakeGraph()

    # No properties panel in the test harness — _on_view_mode_changed guards it.
    page._properties_panel = None

    # Stub the viewer: record the last preview label set.
    viewer_state = {}

    class _FakeViewer:
        def set_preview_node(self, nid, label=None):
            viewer_state["nid"] = nid
            viewer_state["label"] = label

        def get_view_mode(self):
            return state["view_mode"]

    page._viewer = _FakeViewer()

    # Suppress actual rendering on toggle.
    page._evaluate_graph_now = lambda: state.__setitem__("evaluated", True)
    return page, state, viewer_state


class TestPagePreviewToggle:
    """``toggle_preview_selected_node`` (the "1" shortcut) toggle semantics."""

    def test_enter_preview_for_previewable_node(self, qt_app):
        nodes = [_FakeNode("s3d", "scene3d", "Scene3D")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes)
        state["selected"] = "s3d"

        page.toggle_preview_selected_node()

        assert page._preview_node_id == "s3d"
        assert state.get("editor_preview") == "s3d"   # dashed connection shown
        assert viewer.get("nid") == "s3d"             # viewer label shown
        assert viewer.get("label") == "Scene3D"
        assert state.get("evaluated") is True         # re-render triggered

    def test_toggle_off_when_same_node_pressed_again(self, qt_app):
        nodes = [_FakeNode("s3d", "scene3d", "Scene3D")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes)
        page._preview_node_id = "s3d"  # already previewing
        state["selected"] = "s3d"

        page.toggle_preview_selected_node()

        assert page._preview_node_id is None           # back to Output
        assert state.get("editor_preview") is None     # connection cleared
        assert viewer.get("nid") is None               # label cleared

    def test_switch_to_different_node(self, qt_app):
        nodes = [_FakeNode("a", "merge", "Merge"), _FakeNode("b", "blur", "Blur")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes)
        page._preview_node_id = "a"
        state["selected"] = "b"

        page.toggle_preview_selected_node()

        assert page._preview_node_id == "b"
        assert state.get("editor_preview") == "b"

    def test_no_selection_exits_preview(self, qt_app):
        nodes = [_FakeNode("s3d", "scene3d", "Scene3D")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes)
        page._preview_node_id = "s3d"
        state["selected"] = None

        page.toggle_preview_selected_node()

        assert page._preview_node_id is None

    def test_output_node_not_previewable(self, qt_app):
        """Output is excluded — previewing it is a no-op (it's the default)."""
        nodes = [_FakeNode("out", "output", "Output")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes)
        state["selected"] = "out"

        page.toggle_preview_selected_node()

        assert page._preview_node_id is None


class TestResolvePreviewNodeId:
    """``_resolve_preview_node_id`` validates the preview target each render."""

    def test_none_when_no_preview(self, qt_app):
        page, _, _ = _make_page_with_fakes(qt_app, [_FakeNode("x")])
        page._preview_node_id = None
        assert page._resolve_preview_node_id() is None

    def test_returns_id_when_node_exists(self, qt_app):
        nodes = [_FakeNode("x", "merge", "Merge")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes)
        page._preview_node_id = "x"
        assert page._resolve_preview_node_id() == "x"

    def test_clears_stale_id_for_deleted_node(self, qt_app):
        """A preview id whose node was deleted resolves to None AND self-clears."""
        page, state, viewer = _make_page_with_fakes(qt_app, [])
        page._preview_node_id = "gone"
        assert page._resolve_preview_node_id() is None
        assert page._preview_node_id is None  # lazily cleared
        assert state.get("editor_preview") is None  # connection torn down


class TestEditorPreviewConnection:
    """``NodeEditorWidget.set_preview_node`` toggles the dashed connection."""

    def test_set_and_clear(self, qt_app):
        # Stub ShortcutManager so shortcuts register (not under test here, but
        # the editor's __init__ calls _setup_shortcuts).
        import src.features.composite.ui.node_editor_widget as ne_mod

        class _FakeSM:
            _inst = None

            @classmethod
            def instance(cls):
                return cls._inst

            def __init__(self):
                self.m = {}
                _FakeSM._inst = self

            def register(self, aid, label, group, default):
                self.m[aid] = default

            def get_shortcut(self, aid):
                return self.m.get(aid, "")

        _FakeSM._inst = _FakeSM()
        ne_mod.ShortcutManager = _FakeSM
        try:
            from src.features.composite.ui.node_editor_widget import NodeEditorWidget
            from src.features.composite.ui.node_item import NodeItem

            ed = NodeEditorWidget()
            # Register two node items manually (avoid a full NodeGraph build).
            src_item = NodeItem("src", "Source", "#555555")
            out_item = NodeItem("out", "Output", "#555555")
            ed._scene.addItem(src_item)
            ed._scene.addItem(out_item)
            ed._node_items["src"] = src_item
            ed._node_items["out"] = out_item

            ed.set_preview_node("src")
            assert ed._preview_connection is not None
            assert ed._preview_connection in src_item._preview_connections
            assert ed._preview_connection in out_item._preview_connections

            ed.set_preview_node(None)
            assert ed._preview_connection is None
            assert src_item._preview_connections == []
            assert out_item._preview_connections == []
        finally:
            # Restore real ShortcutManager reference.
            from src.shared.ui.shortcut_manager import ShortcutManager
            ne_mod.ShortcutManager = ShortcutManager

    def test_no_op_when_target_missing(self, qt_app):
        import src.features.composite.ui.node_editor_widget as ne_mod

        class _FakeSM:
            _inst = None

            @classmethod
            def instance(cls):
                return cls._inst

            def __init__(self):
                self.m = {}
                _FakeSM._inst = self

            def register(self, aid, label, group, default):
                self.m[aid] = default

            def get_shortcut(self, aid):
                return self.m.get(aid, "")

        _FakeSM._inst = _FakeSM()
        ne_mod.ShortcutManager = _FakeSM
        try:
            from src.features.composite.ui.node_editor_widget import NodeEditorWidget
            ed = NodeEditorWidget()
            ed.set_preview_node("does-not-exist")
            assert ed._preview_connection is None
        finally:
            from src.shared.ui.shortcut_manager import ShortcutManager
            ne_mod.ShortcutManager = ShortcutManager


# ── Phase 3: preview-driven grid policy + 3D auto-preview ──────────────────

def _snap(nodes_spec):
    """Build a SimpleNamespace snapshot mimicking GraphSnapshot.nodes.

    ``nodes_spec``: list of (node_id, node_type, params_dict).
    """
    from src.features.composite.core.graph_snapshot import NodeSnapshot
    nodes = {}
    for nid, ntype, params in nodes_spec:
        nodes[nid] = NodeSnapshot(
            node_id=nid, node_type=ntype, parameters=dict(params),
            disabled=False, input_ports=[], output_ports=[],
        )
    return SimpleNamespace(nodes=nodes)


class TestPreviewDrivenGrid:
    """``_apply_view_mode_overrides`` gates bg + grid by preview context.

    v9 重构后：场景参数移至全局「3D Scene Settings」，_apply_view_mode_overrides
    不再修改快照节点参数，而是返回 {node_id: {show_grid/bg_opaque/show_axes}}
    覆盖字典。门控策略不变：

    - 3D mode + previewing a scene3d node → that node's overrides use
      show_grid/bg_opaque (global preview switches, default True)
    - otherwise → overrides use output_show_grid/output_bg_opaque (default False,
      clean composite)
    """

    def test_grid_on_only_for_virtual_viewer_scene_in_3d(self, qt_app):
        """3D Viewer grid/bg belong to the virtual work scene, not real Scene3D."""
        nodes = [_FakeNode("a", "scene3d", "A"), _FakeNode("b", "scene3d", "B")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._preview_node_id = None
        snap = _snap([("a", "scene3d", {}),
                      ("b", "scene3d", {}),
                      (page._VIEWER3D_VIRTUAL_NODE_ID, "scene3d", {})])

        overrides = page._apply_view_mode_overrides(snap)

        # 虚拟 viewer 场景是 3D 预览目标 → 用预览开关（默认 True）
        vid = page._VIEWER3D_VIRTUAL_NODE_ID
        assert overrides[vid]["show_grid"] is True
        assert overrides[vid]["bg_opaque"] is True
        # 真实 Scene3D 节点不是预览目标 → 用 output_* 默认（False）
        assert overrides["a"]["show_grid"] is False
        assert overrides["a"]["bg_opaque"] is False
        assert overrides["b"]["show_grid"] is False
        assert overrides["b"]["bg_opaque"] is False

    def test_grid_off_for_all_in_2d(self, qt_app):
        """2D mode → all scene3d overrides use output_* defaults (False)."""
        nodes = [_FakeNode("a", "scene3d", "A")]
        page, state, _ = _make_page_with_fakes(qt_app, nodes, view_mode="2d")
        page._preview_node_id = "a"  # even if previewing it, 2D → no ref elements
        snap = _snap([("a", "scene3d", {})])

        overrides = page._apply_view_mode_overrides(snap)

        assert overrides["a"]["show_grid"] is False
        assert overrides["a"]["bg_opaque"] is False

    def test_grid_off_in_3d_when_previewing_output(self, qt_app):
        """3D mode but previewing Output (preview_node_id=None) → all off."""
        nodes = [_FakeNode("a", "scene3d", "A")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._preview_node_id = None  # previewing Output
        snap = _snap([("a", "scene3d", {})])

        overrides = page._apply_view_mode_overrides(snap)

        assert overrides["a"]["show_grid"] is False
        assert overrides["a"]["bg_opaque"] is False

    def test_grid_off_when_previewing_non_scene3d_in_3d(self, qt_app):
        """3D + previewing a merge node → scene3d ref elements off."""
        nodes = [_FakeNode("m", "merge", "M"), _FakeNode("a", "scene3d", "A")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._preview_node_id = "m"
        snap = _snap([("a", "scene3d", {})])

        overrides = page._apply_view_mode_overrides(snap)

        assert overrides["a"]["show_grid"] is False
        assert overrides["a"]["bg_opaque"] is False

    def test_does_not_mutate_persistent_params(self, qt_app):
        """The override returns a dict; it must not touch node/snapshot params."""
        nodes = [_FakeNode("a", "scene3d", "A")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="2d")
        node = nodes[0]
        node.parameters = {"show_grid": True, "bg_opaque": True}  # persistent ON
        snap = _snap([("a", "scene3d", {"show_grid": True, "bg_opaque": True})])

        overrides = page._apply_view_mode_overrides(snap)

        # overrides 字典反映 output_* 默认（False），但快照和节点参数不变
        assert overrides["a"]["show_grid"] is False
        assert overrides["a"]["bg_opaque"] is False
        assert snap.nodes["a"].parameters["show_grid"] is True  # snapshot untouched
        assert node.parameters["show_grid"] is True              # node untouched


class TestAutoPreviewFor3D:
    """3D mode enters the virtual work scene; 2D exits preview."""

    def test_3d_enters_virtual_scene_without_previewing_scene3d(self, qt_app):
        """_on_view_mode_changed('3d') clears node-preview; virtual scene drives viewer."""
        nodes = [_FakeNode("s3d", "scene3d", "Scene3D")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._is_active = True  # _on_view_mode_changed early-returns unless active
        page._viewer3d_node_id = "s3d"

        page._on_view_mode_changed("3d")

        assert page._preview_node_id is None
        assert state.get("editor_preview") is None
        assert viewer.get("nid") is None

    def test_3d_skip_camera3d_focus(self, qt_app):
        """camera3d focus has no image output → not auto-previewed."""
        nodes = [_FakeNode("cam", "camera3d", "Camera3D")]
        page, state, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._is_active = True
        page._viewer3d_node_id = "cam"

        page._on_view_mode_changed("3d")

        assert page._preview_node_id is None  # camera3d not previewable

    def test_2d_exits_preview(self, qt_app):
        """_on_view_mode_changed('2d') exits node-preview back to Output."""
        nodes = [_FakeNode("s3d", "scene3d", "Scene3D")]
        page, state, viewer = _make_page_with_fakes(qt_app, nodes, view_mode="2d")
        page._is_active = True
        page._preview_node_id = "s3d"  # was previewing
        state["editor_preview"] = "s3d"

        page._on_view_mode_changed("2d")

        assert page._preview_node_id is None
        assert state.get("editor_preview") is None

    def test_3d_clears_non_scene3d_preview(self, qt_app):
        """Entering 3D switches to the virtual work scene instead of node-preview."""
        nodes = [_FakeNode("m", "merge", "M"), _FakeNode("s3d", "scene3d", "S")]
        page, state, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._is_active = True
        page._preview_node_id = "m"  # user's explicit choice
        page._viewer3d_node_id = "s3d"

        page._on_view_mode_changed("3d")

        assert page._preview_node_id is None
        assert state.get("editor_preview") is None


# ── Bug fix: view_signature must force re-eval on Tab/preview toggle ───────

class TestViewSignatureDedup:
    """The renderer's skip-check must include the view signature.

    Regression for the bug where the grid/background only hid on the FIRST
    connect of Scene3D→Output, then Tab toggles stopped working. Root cause:
    the renderer's request-dedup signature only tracked graph *topology*
    (generation), so a pure view-mode change (which bakes different
    show_grid/bg_opaque into Scene3D's output) was masked as a no-op and the
    stale grid-on result was shown. The fix adds view_signature (view_mode +
    preview target) to both RenderRequest and the skip signature.
    """

    def test_request_carries_view_signature(self):
        """RenderRequest has a view_signature field (default empty)."""
        req = RenderRequest(snapshot=None, request_id=1, timestamp=0.0)
        assert hasattr(req, 'view_signature')
        assert req.view_signature == ""  # default

    def test_signature_changes_with_view_mode(self, qt_app):
        """Tab toggle (2d→3d) changes the page's view_signature."""
        nodes = [_FakeNode("s3d", "scene3d", "S")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="2d")

        sig_2d = page._view_signature()
        # Simulate Tab → 3d
        page._viewer.get_view_mode = lambda: "3d"
        sig_3d = page._view_signature()

        assert sig_2d != sig_3d, "Tab toggle must change view_signature"

    def test_signature_changes_with_preview_target(self, qt_app):
        """Pressing '1' to preview a node changes the signature."""
        nodes = [_FakeNode("s3d", "scene3d", "S")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="2d")

        sig_off = page._view_signature()
        page._preview_node_id = "s3d"
        sig_on = page._view_signature()

        assert sig_off != sig_on, "preview toggle must change view_signature"

    def test_signature_changes_with_viewer_orbit_camera_nav(self, qt_app):
        """3D Viewer orbit camera movement must be visible to renderer dedup."""
        nodes = [_FakeNode("s3d", "scene3d", "S")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        nav_params = {
            "cam_orbit_yaw": 30.0,
            "cam_orbit_pitch": 20.0,
            "cam_dist": 5.0,
            "cam_target_x": 0.0,
            "cam_target_y": 0.0,
            "cam_target_z": 0.0,
        }
        page._viewer._nav_params_dict = lambda: dict(nav_params)

        sig_before = page._view_signature()
        nav_params["cam_orbit_yaw"] = 45.0
        sig_after = page._view_signature()

        assert sig_before != sig_after
        assert "cam_orbit_yaw=45.0000" in sig_after

    def test_signature_changes_with_free_camera_eye_nav(self, qt_app):
        """Active free-camera navigation uses cam_eye_* rather than cam_orbit_*."""
        nodes = [_FakeNode("s3d", "scene3d", "S")]
        page, _, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        nav_params = {
            "cam_eye_x": 4.0,
            "cam_eye_y": 3.0,
            "cam_eye_z": 5.0,
            "cam_target_x": 0.0,
            "cam_target_y": 0.0,
            "cam_target_z": 0.0,
        }
        page._viewer._nav_params_dict = lambda: dict(nav_params)

        sig_before = page._view_signature()
        nav_params["cam_eye_x"] = 6.0
        sig_after = page._view_signature()

        assert sig_before != sig_after
        assert "cam_eye_x=6.0000" in sig_after

    def test_virtual_viewer_nav_preview_evaluates_immediately(self, qt_app):
        """Dragging the default 3D Viewer camera should redraw immediately."""
        nodes = [_FakeNode("s3d", "scene3d", "S")]
        page, state, _ = _make_page_with_fakes(qt_app, nodes, view_mode="3d")
        page._viewer3d_active_camera_id = None
        calls = {"now": 0, "debounced": 0}
        page._evaluate_graph_now = lambda: calls.__setitem__("now", calls["now"] + 1)
        page._evaluate_graph = lambda: calls.__setitem__(
            "debounced", calls["debounced"] + 1)

        page._on_viewer3d_nav_preview(
            page._VIEWER3D_VIRTUAL_NODE_ID,
            {"cam_dist": 8.0},
        )

        assert calls == {"now": 1, "debounced": 0}

    def test_renderer_skip_check_includes_view_signature(self):
        """The renderer's skip condition reads view_signature from the request."""
        import inspect
        from src.features.composite.render.composite_renderer import CompositeRenderer
        src = inspect.getsource(CompositeRenderer._handle_evaluate_graph)
        assert 'view_signature' in src, \
            "renderer skip-check must compare view_signature (else Tab = no-op)"
        assert 'view_same' in src, "view_signature must be part of the skip guard"
        assert 'current_view_signature' in src

    def test_renderer_tracks_last_view_signature(self):
        """The renderer records _last_view_signature after each valid eval."""
        import inspect
        from src.features.composite.render.composite_renderer import CompositeRenderer
        init_src = inspect.getsource(CompositeRenderer.__init__)
        eval_src = inspect.getsource(CompositeRenderer._handle_evaluate_graph)
        assert '_last_view_signature' in init_src, \
            "renderer __init__ must declare _last_view_signature"
        assert '_last_view_signature = current_view_signature' in eval_src, \
            "renderer must update _last_view_signature on valid result"
