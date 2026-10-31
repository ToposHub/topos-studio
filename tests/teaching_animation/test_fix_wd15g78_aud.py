"""Fix verification for remediation batch wd15-g78 (batch D, teaching UI tail).

Covers REMEDIATION_PLAN entries:
- AUD-30703  layer-tree multi-row drag divergence (rollback rebuild)
- AUD-30800  hardcoded Space play key bypassing user shortcut config
- AUD-30901  save() I/O failures crashing Qt slots
- AUD-30902  DevTools close flow: apply failures must keep the editor open
- AUD-30903  per-mouse-tick full scene refresh during canvas drag
- AUD-30904  DevTools Apply execute() outside exception protection
- AUD-31006  multi-select Content edit destroying dict content
- AUD-31007  transform rows emitting one undo per drag tick
- AUD-31009  duration DragValue emitting one SetDurationCommand per tick
- AUD-31011  QSvgRenderer accumulation in SceneView
- AUD-31201  startup fatal path using sys.exit instead of _orderly_shutdown
- GAP-014   module.py low-coverage helpers (intent/parsers/memory)
- GAP-017   motion_graphic_editor_window.py paths exercised above
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QEvent, QModelIndex, Qt
from PyQt6.QtGui import QKeyEvent, QKeySequence

from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)
from src.features.teaching_animation.ui.scene_inspector import SceneInspector
from src.features.teaching_animation.ui.scene_timeline import SceneTimeline
from src.features.teaching_animation.ui.scene_view import SceneView


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text(eid: str, x: float = 10.0, y: float = 10.0, z: int = 0) -> SceneElement:
    return SceneElement(
        element_id=eid, type="text", z_index=z, visible=True,
        transform=SceneTransform(x=x, y=y, width=200.0, height=60.0),
        style={}, content=f"content-{eid}",
    )


def _shape(eid: str) -> SceneElement:
    return SceneElement(
        element_id=eid, type="shape", z_index=1, visible=True,
        transform=SceneTransform(x=0.0, y=0.0, width=100.0, height=100.0),
        style={}, content={"kind": "rect", "corner_radius": 4},
    )


def _svg_scene(*element_specs: Any) -> TeachingSceneDocument:
    scene = replace(TeachingSceneDocument.create_default("wd15", duration_frames=48),
                    elements=())
    for spec in element_specs:
        scene = scene.with_element_added(spec)
    return scene


def _svg_frame(payload: str = "<svg xmlns='http://www.w3.org/2000/svg'/>") -> RenderedFrame:
    return RenderedFrame(frame=0, width=64, height=48, pixel_format="svg",
                         data=payload.encode("utf-8"))


def _list_ids(panel: LayerTreePanel) -> list[str]:
    from src.features.teaching_animation.ui.layer_tree_panel import _ROLE_ROW_ID
    return [str(panel.list_widget.item(i).data(_ROLE_ROW_ID))
            for i in range(panel.list_widget.count())]


def _emit_rows_moved(panel: LayerTreePanel, start: int, end: int, dest: int) -> None:
    panel.list_widget.model().rowsMoved.emit(QModelIndex(), start, end,
                                             QModelIndex(), dest)


# ---------------------------------------------------------------------------
# AUD-30703 — layer tree multi-row drag
# ---------------------------------------------------------------------------

class TestLayerTreeMultiRowDrag:
    def _panel(self) -> tuple[LayerTreePanel, list[str]]:
        panel = LayerTreePanel()
        # Document paint-ascending tuple [c, b, a]; the list shows a, b, c
        # top-most first (z 2, 1, 0) under one page header.
        scene = _svg_scene(
            _text("c", z=0), _text("b", z=1), _text("a", z=2),
        )
        panel.set_scene(scene)
        return panel, _list_ids(panel)

    def test_single_row_move_still_commits_reorder(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel, original = self._panel()
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(lambda rid, idx: emitted.append((rid, idx)))
        # Move c (row 3) above a (row 1) with only c selected.
        panel._updating = True
        try:
            item = panel.list_widget.takeItem(3)
            panel.list_widget.insertItem(1, item)
            panel.list_widget.setCurrentItem(item)
            item.setSelected(True)
        finally:
            panel._updating = False
        _emit_rows_moved(panel, 3, 3, 1)
        # Visual element order is now [c, a, b]; c is top-most -> tuple index
        # N-1-position = 3-1-0 = 2.
        assert emitted == [("c", 2)]
        assert _list_ids(panel) != original  # the visual move stands

    def test_multi_row_block_move_rolls_back_instead_of_diverging(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel, original = self._panel()
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(lambda rid, idx: emitted.append((rid, idx)))
        panel._updating = True
        try:
            # Qt applied a contiguous two-row block move (b, a down).
            first = panel.list_widget.takeItem(1)
            second = panel.list_widget.takeItem(1)
            panel.list_widget.insertItem(3, second)
            panel.list_widget.insertItem(3, first)
            for item in (first, second):
                item.setSelected(True)
        finally:
            panel._updating = False
        _emit_rows_moved(panel, 1, 2, 3)
        assert emitted == []  # no partial commit
        assert _list_ids(panel) == original  # visual state snapped back

    def test_multi_select_single_row_move_also_rolls_back(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel, original = self._panel()
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(lambda rid, idx: emitted.append((rid, idx)))
        panel._updating = True
        try:
            c_item = panel.list_widget.item(3)
            b_item = panel.list_widget.item(2)
            c_item.setSelected(True)
            b_item.setSelected(True)
        finally:
            panel._updating = False
        _emit_rows_moved(panel, 3, 3, 1)
        assert emitted == []
        assert _list_ids(panel) == original

    def test_programmatic_update_guard_still_returns_early(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel, original = self._panel()
        panel._updating = True
        _emit_rows_moved(panel, 1, 2, 3)
        panel._updating = False
        assert _list_ids(panel) == original


# ---------------------------------------------------------------------------
# AUD-30800 — configurable play/pause key
# ---------------------------------------------------------------------------

class TestConfigurablePlayKey:
    def _press(self, widget, key) -> None:  # type: ignore[no-untyped-def]
        event = QKeyEvent(QEvent.Type.KeyPress, key,
                          Qt.KeyboardModifier.NoModifier)
        widget.keyPressEvent(event)

    def test_default_space_toggles_on_both_sides(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui.mg_keyframes_panel import MgKeyframesPanel
        panel = MgKeyframesPanel()
        toggled: list[bool] = []
        panel.playbackToggleRequested.connect(lambda: toggled.append(True))
        self._press(panel._tree, Qt.Key.Key_Space)
        self._press(panel._tracks, Qt.Key.Key_Space)
        assert len(toggled) == 2

    def test_rebound_key_unbinds_space(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui import mg_keyframes_panel
        from src.features.teaching_animation.ui import mg_shortcuts
        from src.features.teaching_animation.ui.mg_keyframes_panel import MgKeyframesPanel
        monkeypatch.setattr(
            mg_shortcuts, "configured_shortcut",
            lambda action_id, default: QKeySequence("P"),
        )
        panel = MgKeyframesPanel()
        toggled: list[bool] = []
        panel.playbackToggleRequested.connect(lambda: toggled.append(True))
        self._press(panel._tracks, Qt.Key.Key_Space)
        assert toggled == []  # Space no longer forces playback
        self._press(panel._tree, Qt.Key.Key_Space)
        assert toggled == []
        self._press(panel._tracks, Qt.Key.Key_P)
        self._press(panel._tree, Qt.Key.Key_P)
        assert len(toggled) == 2  # the configured key works everywhere

    def test_event_matches_reflects_rebind(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui import mg_shortcuts
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                          Qt.KeyboardModifier.NoModifier)
        assert mg_shortcuts.event_matches(
            event, mg_shortcuts.PLAY_PAUSE_ACTION_ID, mg_shortcuts.PLAY_PAUSE_DEFAULT)
        monkeypatch.setattr(
            mg_shortcuts, "configured_shortcut",
            lambda action_id, default: QKeySequence("P"),
        )
        assert not mg_shortcuts.event_matches(
            event, mg_shortcuts.PLAY_PAUSE_ACTION_ID, mg_shortcuts.PLAY_PAUSE_DEFAULT)


# ---------------------------------------------------------------------------
# AUD-30901 — save() protection
# ---------------------------------------------------------------------------

class _FakeCoordinator:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scheduled: list[TeachingSceneDocument] = []

    def schedule(self, scene: TeachingSceneDocument) -> None:
        if self.error is not None:
            raise self.error
        self.scheduled.append(scene)


class TestSaveProtection:
    def _dirty(self, window: MotionGraphicEditorWindow) -> None:
        from src.features.teaching_animation.core.commands import SetDurationCommand
        window.editor_controller.execute(SetDurationCommand(72))
        assert window.editor_controller.is_dirty

    def test_saver_oserror_is_contained_and_scene_stays_dirty(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        coordinator = _FakeCoordinator()

        def failing_saver(scene: TeachingSceneDocument) -> None:
            raise OSError("disk full")

        window = MotionGraphicEditorWindow(_svg_scene(), saver=failing_saver)
        try:
            self._dirty(window)
            monkeypatch.setattr(
                "src.features.teaching_animation.scene_cache.get_shared_scene_cache_coordinator",
                lambda: coordinator,
            )
            window.save()  # must not raise inside the slot
            assert window.editor_controller.is_dirty  # retry remains possible
            assert coordinator.scheduled == []  # nothing new to cache
            assert window.statusBar().currentMessage() != ""
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_saver_runtimeerror_is_contained(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def failing_saver(scene: TeachingSceneDocument) -> None:
            raise RuntimeError("host saver rejected")

        window = MotionGraphicEditorWindow(_svg_scene(), saver=failing_saver)
        try:
            self._dirty(window)
            monkeypatch.setattr(
                "src.features.teaching_animation.scene_cache.get_shared_scene_cache_coordinator",
                lambda: _FakeCoordinator(),
            )
            window.save()
            assert window.editor_controller.is_dirty
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_coordinator_failure_does_not_break_successful_save(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        saved: list[TeachingSceneDocument] = []

        def saver(scene: TeachingSceneDocument) -> None:
            saved.append(scene)

        window = MotionGraphicEditorWindow(_svg_scene(), saver=saver)
        try:
            self._dirty(window)
            monkeypatch.setattr(
                "src.features.teaching_animation.scene_cache.get_shared_scene_cache_coordinator",
                lambda: _FakeCoordinator(RuntimeError("worker shut down")),
            )
            window.save()
            assert saved  # the save itself landed
            assert not window.editor_controller.is_dirty
        finally:
            window.editor_controller.mark_clean()
            window.close()


# ---------------------------------------------------------------------------
# AUD-30902 / AUD-30904 — DevTools apply + close flow
# ---------------------------------------------------------------------------

class TestDevToolsApplyCloseFlow:
    def test_unready_browser_clears_close_flag_and_keeps_open(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        window = MotionGraphicEditorWindow(_svg_scene())
        closed: list[bool] = []
        monkeypatch.setattr(window, "close", lambda: closed.append(True))
        try:
            window._close_after_devtools_apply = True
            window._apply_live_dom_source("")  # early exit: browser not ready
            assert window._close_after_devtools_apply is False
            assert closed == []
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_execute_valueerror_is_caught_and_close_is_aborted(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        window = MotionGraphicEditorWindow(_svg_scene())
        closed: list[bool] = []
        monkeypatch.setattr(window, "close", lambda: closed.append(True))
        monkeypatch.setattr(
            window, "_build_native_property_command",
            lambda overlay: (object(), ()),
        )
        executed: list[Any] = []

        def flaky_execute(command: Any) -> Any:
            # First call: the composite DevTools Apply (apply-time ValueError,
            # AUD-30904). Second call: the native-only fallback.
            executed.append(command)
            if len(executed) == 1:
                raise ValueError("sanitizer rejected the live DOM")
            return None

        monkeypatch.setattr(window.editor_controller, "execute", flaky_execute)
        try:
            window._close_after_devtools_apply = True
            window._apply_live_dom_source("<p>live</p>",
                                          live_native_overlay="<div/>")
            assert len(executed) == 2  # composite attempted, then native-only
            assert window._close_after_devtools_apply is False
            assert closed == []  # authored changes are never silently dropped
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_failed_apply_without_native_edits_keeps_editor_open(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        window = MotionGraphicEditorWindow(_svg_scene())  # no html_layer
        closed: list[bool] = []
        monkeypatch.setattr(window, "close", lambda: closed.append(True))
        try:
            window._close_after_devtools_apply = True
            # SVG scene: ReplaceHtmlSourceCommand.apply raises ValueError
            # ("scene has no html_layer") inside execute — previously that
            # escaped _apply_html_source's caller contract.
            window._apply_live_dom_source("<p>live</p>")
            assert window._close_after_devtools_apply is False
            assert closed == []
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_successful_apply_saves_and_closes(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.core.html_layer import build_html_layer
        html = "<section data-topos-page='1'><h1>Hello</h1></section>"
        layer = build_html_layer(layer_id="L", sanitized_html=html,
                                 authored_source=html,
                                 viewport_width=64, viewport_height=48,
                                 duration_frames=6)
        scene = replace(
            TeachingSceneDocument.create_default("s", width=64, height=48,
                                                 duration_frames=6),
            html_layer=layer,
        )
        window = MotionGraphicEditorWindow(scene)
        closed: list[bool] = []
        saved: list[bool] = []
        monkeypatch.setattr(window, "close", lambda: closed.append(True))
        monkeypatch.setattr(window, "save", lambda: saved.append(True))
        try:
            window._close_after_devtools_apply = True
            window._apply_live_dom_source(
                "<section data-topos-page='1'><h1>Updated</h1></section>")
            assert saved == [True]
            assert window._close_after_devtools_apply is False
            qapp.processEvents()
            assert closed == [True]
        finally:
            window.editor_controller.mark_clean()
            window.close()


# ---------------------------------------------------------------------------
# AUD-30903 — drag refresh routing
# ---------------------------------------------------------------------------

class TestDragRefreshRouting:
    def test_transform_preview_skips_panel_rebuilds(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        scene = _svg_scene(_text("a", x=10.0, y=10.0, z=1))
        window = MotionGraphicEditorWindow(scene)
        tree_calls: list[int] = []
        frame_calls: list[int] = []

        def spy_layer_tree(scene_arg, **kwargs) -> None:
            tree_calls.append(1)
            window._layer_tree_set_scene_orig(scene_arg, **kwargs)

        def spy_scene_view(scene_arg) -> None:
            frame_calls.append(1)
            window._scene_view_set_frame_orig(scene_arg)

        window._layer_tree_set_scene_orig = window.layer_tree.set_scene
        window._scene_view_set_frame_orig = window.scene_view.set_frame
        monkeypatch.setattr(window.layer_tree, "set_scene", spy_layer_tree)
        monkeypatch.setattr(window.scene_view, "set_frame", spy_scene_view)
        try:
            window.editor_controller.begin_preview("a")
            window._drag_anchors = {"a": (10.0, 10.0)}
            baseline_tree = len(tree_calls)
            for x in (14.0, 18.0, 22.0):
                window._element_dragged("a", x, 10.0)
            # Light path: the canvas re-rendered every tick...
            assert len(frame_calls) == 3
            # ...but no panel rebuild happened during the drag.
            assert len(tree_calls) == baseline_tree

            window._on_drag_finished("a", 0.0, 0.0)
            # Commit takes the full-refresh route.
            assert len(tree_calls) > baseline_tree
            assert window.editor_controller.scene.element_by_id("a").transform.x == 22.0
            # One drag gesture == one undo step.
            window.editor_controller.undo()
            assert window.editor_controller.scene.element_by_id("a").transform.x == 10.0
        finally:
            window._drag_anchors = {}
            window.editor_controller.mark_clean()
            window.close()

    def test_empty_diff_still_takes_full_refresh(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # SceneDiff does not track markers/metadata, so an "empty" diff may
        # still carry document changes the panels must reflect (e.g. marker
        # adds); it must never be dropped.
        from src.features.teaching_animation.ui.editor_controller import SceneDiff
        window = MotionGraphicEditorWindow(_svg_scene())
        full: list[bool] = []
        light: list[bool] = []
        monkeypatch.setattr(window, "_refresh_scene", lambda scene: full.append(True))
        monkeypatch.setattr(window, "_refresh_transform_preview",
                            lambda scene: light.append(True))
        try:
            window._on_scene_notified(window.editor_controller.scene, SceneDiff())
            assert full == [True]
            assert light == []
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_element_add_takes_full_refresh_even_during_drag(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui.editor_controller import SceneDiff
        window = MotionGraphicEditorWindow(_svg_scene())
        window._drag_anchors = {"a": (1.0, 1.0)}
        full: list[bool] = []
        light: list[bool] = []
        monkeypatch.setattr(window, "_refresh_scene", lambda scene: full.append(True))
        monkeypatch.setattr(window, "_refresh_transform_preview",
                            lambda scene: light.append(True))
        try:
            # An added element has no changed_fields entry; the light path
            # must not swallow it (the layer tree has to show it).
            window._on_scene_notified(
                window.editor_controller.scene,
                SceneDiff(changed_element_ids=frozenset({"new"}),
                          changed_fields={}),
            )
            assert full == [True]
            assert light == []
        finally:
            window._drag_anchors = {}
            window.editor_controller.mark_clean()
            window.close()

    def test_structural_change_takes_full_refresh(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ui.editor_controller import SceneDiff
        window = MotionGraphicEditorWindow(_svg_scene())
        window._drag_anchors = {"a": (1.0, 1.0)}
        refreshed: list[bool] = []
        monkeypatch.setattr(window, "_refresh_scene",
                            lambda scene: refreshed.append(True))
        try:
            # A duration change must never be answered with a light preview.
            window._on_scene_notified(
                window.editor_controller.scene,
                SceneDiff(changed_element_ids=frozenset({"a"}),
                          changed_fields={"a": frozenset({"transform"})},
                          duration_changed=True),
            )
            assert refreshed == [True]  # full refresh only
        finally:
            window._drag_anchors = {}
            window.editor_controller.mark_clean()
            window.close()


# ---------------------------------------------------------------------------
# AUD-31006 — multi-select content edit
# ---------------------------------------------------------------------------

class TestMultiSelectContentEdit:
    def test_dict_content_gets_text_merge_not_replacement(self, qapp) -> None:  # type: ignore[no-untyped-def]
        scene = _svg_scene(_text("t1", z=1), _shape("s1"))
        window = MotionGraphicEditorWindow(scene)
        try:
            window._selection_updated(("t1", "s1"), {"content": "new copy"})
            elements = window.editor_controller.scene
            assert elements.element_by_id("t1").content == "new copy"
            shape_content = elements.element_by_id("s1").content
            assert isinstance(shape_content, dict)
            assert shape_content["kind"] == "rect"
            assert shape_content["corner_radius"] == 4
            assert shape_content["text"] == "new copy"
            # One composite command: a single undo restores both elements.
            window.editor_controller.undo()
            restored = window.editor_controller.scene
            assert restored.element_by_id("t1").content == "content-t1"
            assert dict(restored.element_by_id("s1").content) == {
                "kind": "rect", "corner_radius": 4}
        finally:
            window.editor_controller.mark_clean()
            window.close()


# ---------------------------------------------------------------------------
# AUD-31007 — transform row deferral
# ---------------------------------------------------------------------------

class TestTransformRowDeferral:
    def test_ticks_defer_until_interaction_ends(self, qapp) -> None:  # type: ignore[no-untyped-def]
        inspector = SceneInspector()
        emitted: list[tuple[str, dict]] = []
        inspector.elementUpdateRequested.connect(
            lambda eid, updates: emitted.append((eid, updates)))
        inspector.set_selection((_text("t1", x=1.0),))
        inspector.x_spin.setValue(4.0)
        inspector.x_spin.setValue(7.0)
        assert emitted == []  # per-tick emissions are gone
        inspector.x_spin.interaction_ended.emit()
        assert len(emitted) == 1
        assert emitted[0][0] == "t1"
        assert emitted[0][1]["transform"].x == 7.0

    def test_multi_select_uses_single_patch_commit(self, qapp) -> None:  # type: ignore[no-untyped-def]
        inspector = SceneInspector()
        emitted: list[tuple[tuple, dict]] = []
        inspector.selectionUpdateRequested.connect(
            lambda ids, updates: emitted.append((tuple(ids), updates)))
        inspector.set_selection((_text("t1", x=1.0), _text("t2", x=2.0)))
        inspector.y_spin.setValue(30.0)
        inspector.y_spin.interaction_ended.emit()
        assert len(emitted) == 1
        assert set(emitted[0][0]) == {"t1", "t2"}
        assert emitted[0][1]["transform_patch"]["y"] == 30.0

    def test_commit_without_pending_ticks_is_silent(self, qapp) -> None:  # type: ignore[no-untyped-def]
        inspector = SceneInspector()
        emitted: list[bool] = []
        inspector.elementUpdateRequested.connect(lambda *a: emitted.append(True))
        inspector.set_selection((_text("t1"),))
        inspector._commit_deferred_transform_change()  # stray release
        assert emitted == []


# ---------------------------------------------------------------------------
# AUD-31009 — duration DragValue deferral
# ---------------------------------------------------------------------------

class TestDurationDeferral:
    def test_ticks_emit_once_per_interaction(self, qapp) -> None:  # type: ignore[no-untyped-def]
        timeline = SceneTimeline()
        timeline.set_scene(_svg_scene())  # fps 24
        requested: list[int] = []
        timeline.durationChangeRequested.connect(lambda frames: requested.append(frames))
        timeline.duration_spin.setValue(2.0)
        timeline.duration_spin.setValue(2.5)
        assert requested == []
        timeline.duration_spin.interaction_ended.emit()
        assert requested == [60]  # round(2.5 * 24)

    def test_second_gesture_emits_again(self, qapp) -> None:  # type: ignore[no-untyped-def]
        timeline = SceneTimeline()
        timeline.set_scene(_svg_scene())
        requested: list[int] = []
        timeline.durationChangeRequested.connect(lambda frames: requested.append(frames))
        timeline.duration_spin.setValue(1.0)
        timeline.duration_spin.interaction_ended.emit()
        timeline.duration_spin.setValue(1.5)
        timeline.duration_spin.interaction_ended.emit()
        assert requested == [24, 36]

    def test_programmatic_scene_sync_does_not_emit(self, qapp) -> None:  # type: ignore[no-untyped-def]
        timeline = SceneTimeline()
        requested: list[int] = []
        timeline.durationChangeRequested.connect(lambda frames: requested.append(frames))
        timeline.set_scene(_svg_scene())
        assert requested == []
        timeline.duration_spin.interaction_ended.emit()
        assert requested == []  # nothing staged by set_scene


# ---------------------------------------------------------------------------
# AUD-31011 — SVG renderer retirement
# ---------------------------------------------------------------------------

class TestSvgRendererRetirement:
    def test_set_frame_retires_previous_renderer(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        view = SceneView()
        view.set_frame(_svg_frame("<svg/>"))
        first = view._renderer
        retired: list[object] = []
        monkeypatch.setattr(first, "deleteLater",
                            lambda: retired.append(first), raising=False)
        view.set_frame(_svg_frame("<svg><rect/></svg>"))
        assert view._renderer is not first
        assert retired == [first]

    def test_rgba_frame_retires_renderer(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        view = SceneView()
        view.set_frame(_svg_frame("<svg/>"))
        first = view._renderer
        retired: list[object] = []
        monkeypatch.setattr(first, "deleteLater",
                            lambda: retired.append(first), raising=False)
        view.set_frame(RenderedFrame(
            frame=0, width=4, height=4, pixel_format="rgba8",
            data=b"\xff\x00\x00\xff" * 16))
        assert view._renderer is None
        assert retired == [first]

    def test_scene_change_retires_renderer(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        view = SceneView()
        view.set_scene(TeachingSceneDocument.create_default("one"))
        view.set_frame(_svg_frame("<svg/>"))
        first = view._renderer
        retired: list[object] = []
        monkeypatch.setattr(first, "deleteLater",
                            lambda: retired.append(first), raising=False)
        view.set_scene(TeachingSceneDocument.create_default("two"))
        assert view._renderer is None
        assert retired == [first]


# ---------------------------------------------------------------------------
# AUD-31201 — startup fatal exit path
# ---------------------------------------------------------------------------

class TestStartupFatalPath:
    def test_fatal_startup_uses_orderly_shutdown(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import src.main as main_module
        calls: list[tuple[Any, int]] = []

        def fake_shutdown(app: Any, exit_code: int) -> None:
            calls.append((app, exit_code))

        monkeypatch.setattr(main_module, "_orderly_shutdown", fake_shutdown)
        # Fail early — before QApplication/MainWindow construction.
        def fail_format(fmt: Any) -> None:
            raise RuntimeError("surface format rejected")

        monkeypatch.setattr(
            main_module.QSurfaceFormat, "setDefaultFormat", fail_format)
        # Must not raise SystemExit (the old sys.exit(1) path) out of main().
        main_module.main()
        assert calls and calls[0][1] == 1

    def test_orderly_shutdown_accepts_missing_app(self) -> None:  # type: ignore[no-untyped-def]
        import inspect
        import src.main as main_module
        # The signature must tolerate a None app (startup failure predating
        # QApplication construction) without a runtime crash.
        signature = inspect.signature(main_module._orderly_shutdown)
        assert "None" in str(signature.parameters["app"].annotation)


# ---------------------------------------------------------------------------
# GAP-014 — module.py helper coverage
# ---------------------------------------------------------------------------

class TestModuleIntentClassification:
    def test_animation_request_detection(self) -> None:
        assert TeachingAnimationModule._is_ai_agent_animation_request(
            "帮我生成一个教学动画片段")
        assert not TeachingAnimationModule._is_ai_agent_animation_request(
            "如何生成motion graphic视频？")
        assert not TeachingAnimationModule._is_ai_agent_animation_request(
            "今天天气如何")

    def test_generation_authorization(self) -> None:
        assert TeachingAnimationModule._is_generation_authorization("可以")
        assert TeachingAnimationModule._is_generation_authorization("开始生成吧")
        assert not TeachingAnimationModule._is_generation_authorization("不要生成")
        assert not TeachingAnimationModule._is_generation_authorization(
            "我想要更酷的配色")

    def test_planning_request(self) -> None:
        assert TeachingAnimationModule._is_motion_graphic_planning_request(
            "先讨论一下方案")
        assert not TeachingAnimationModule._is_motion_graphic_planning_request(
            "直接生成一个教学动画")

    def test_edit_request_vs_fresh_creation(self) -> None:
        assert TeachingAnimationModule._is_motion_graphic_edit_request(
            "把标题改成蓝色")
        assert not TeachingAnimationModule._is_motion_graphic_edit_request(
            "重新生成一个新的")
        assert not TeachingAnimationModule._is_motion_graphic_edit_request(
            "怎么修改标题")

    def test_html_generate_request(self) -> None:
        assert TeachingAnimationModule._is_html_generate_request(
            "做一个freeform html动画")
        assert not TeachingAnimationModule._is_html_generate_request(
            "如何写css动画")

    def test_confirmation_and_cancellation(self) -> None:
        assert TeachingAnimationModule._is_confirmation_text("OK")
        assert TeachingAnimationModule._is_cancellation_text("取消")
        assert not TeachingAnimationModule._is_confirmation_text("cancel")

    def test_resume_text(self) -> None:
        assert TeachingAnimationModule._is_motion_graphic_resume_text("继续！")
        assert not TeachingAnimationModule._is_motion_graphic_resume_text("新话题")


class TestModuleTextParsers:
    def test_extract_quoted_value_takes_last(self) -> None:
        assert TeachingAnimationModule._extract_quoted_value(
            '把"你好"改成"再见"') == "再见"
        assert TeachingAnimationModule._extract_quoted_value("no quotes") is None

    def test_color_from_text(self) -> None:
        assert TeachingAnimationModule._color_from_text("用 #AABBCC") == "#aabbcc"
        assert TeachingAnimationModule._color_from_text("改成红色") == "#ef4444"
        assert TeachingAnimationModule._color_from_text("透明背景") is None

    def test_requested_duration_seconds_bounds(self) -> None:
        assert TeachingAnimationModule._requested_duration_seconds("30秒") == 30.0
        assert TeachingAnimationModule._requested_duration_seconds("0.2秒") == 1.0
        assert TeachingAnimationModule._requested_duration_seconds("999 s") == 300.0
        assert TeachingAnimationModule._requested_duration_seconds("no duration") == 15.0

    def test_storyboard_labels(self) -> None:
        assert TeachingAnimationModule._storyboard_label(0, 5) == "first_frame"
        assert TeachingAnimationModule._storyboard_label(4, 5) == "last_frame"
        assert TeachingAnimationModule._storyboard_label(2, 5) == "mid_frame"
        assert TeachingAnimationModule._storyboard_label(1, 9) == "frame_1"

    def test_proposal_selection(self) -> None:
        assert TeachingAnimationModule._extract_proposal_selection("B方案") == "B"
        assert TeachingAnimationModule._extract_proposal_selection("选择 a") == "A"
        assert TeachingAnimationModule._extract_proposal_selection(
            "我觉得这个不错") is None

    def test_parse_motion_graphic_json_response(self) -> None:
        parse = TeachingAnimationModule._parse_motion_graphic_json_response
        assert parse('```json\n{"a": 1}\n```') == {"a": 1}
        assert parse('前言 {"b": 2}') == {"b": 2}
        assert parse('{"items": [1, 2,]}') == {"items": [1, 2]}
        with pytest.raises(ValueError):
            parse("definitely not json")


class TestModulePlanFormatting:
    def test_plan_rows_without_creative_brief(self) -> None:
        rows = TeachingAnimationModule._motion_graphic_plan_rows({})
        assert rows == [{"label": "画布", "value": "当前时间线画布"}]

    def test_plan_rows_with_creative_brief(self) -> None:
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief
        brief = MotionGraphicBrief(brief_id="b1", width=800, height=450)
        rows = TeachingAnimationModule._motion_graphic_plan_rows({
            "brief": brief,
            "creative_brief": {
                "concept": "轨道运动",
                "subject_motifs": ["星星", "轨道"],
                "palette": ["#111111"],
                "concept_extra": "",
            },
        })
        assert rows[0] == {"label": "画布", "value": "800×450"}
        labels = [row["label"] for row in rows]
        assert "核心创意" in labels and "主题母题" in labels and "色彩方向" in labels

    def test_plan_review_and_approved_context(self) -> None:
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief
        plan = {
            "brief": MotionGraphicBrief(brief_id="b1", width=800, height=450),
            "seconds": 8.0,
            "creative_brief": {"concept": "概念", "palette": ["#123456"]},
        }
        review = TeachingAnimationModule._format_motion_graphic_plan_review(plan)
        assert "| 画布 | 800×450 |" in review
        assert "8 秒" in review
        context = TeachingAnimationModule._approved_motion_graphic_plan_context(plan)
        assert "核心创意：概念" in context
        assert "画布" not in context  # canvas row is excluded from hand-off

    def test_canvas_kwargs_fallback(self) -> None:
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief
        assert TeachingAnimationModule._brief_canvas_kwargs({}) == {
            "width": 1920, "height": 1080}
        brief = MotionGraphicBrief(brief_id="b1", width=800, height=450)
        assert TeachingAnimationModule._brief_canvas_kwargs({"brief": brief}) == {
            "width": 800, "height": 450}


class TestModuleFeedbackHelpers:
    def test_categorize_feedback(self) -> None:
        assert TeachingAnimationModule._categorize_feedback("节奏太快") == ("pacing",)
        assert "color" in TeachingAnimationModule._categorize_feedback("换个配色")
        assert TeachingAnimationModule._categorize_feedback("随便说说") == ("goal",)

    def test_normalize_feedback_notes_bounds_and_dedupes(self) -> None:
        normalize = TeachingAnimationModule._normalize_feedback_notes
        assert normalize(["keep", "Keep", "b", "c", "d"]) == ("b", "c", "d")
        assert normalize({"not": "a list"}) == ()
        assert normalize("single note") == ("single note",)
        assert len(normalize(["x" * 500])[0]) <= \
            TeachingAnimationModule._MAX_PERSISTED_HTML_EDIT_FEEDBACK_CHARS

    def test_format_feedback_memory(self) -> None:
        assert TeachingAnimationModule._format_feedback_memory(
            ["a", "b"]) == "- a\n- b"


class TestModuleTaskMemory:
    def _brief(self):  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.ai.brief import MotionGraphicBrief
        return MotionGraphicBrief(brief_id="b1", topic="主题",
                                  duration_seconds=8.0)

    def test_export_skips_plans_without_brief(self) -> None:
        module = TeachingAnimationModule()
        module._pending_motion_graphic_requests["r1"] = {"user_text": "x"}
        assert module._export_motion_graphic_task_memory() is None

    def test_export_restore_roundtrip(self) -> None:
        module = TeachingAnimationModule()
        module._pending_motion_graphic_requests["r1"] = {
            "brief": self._brief(),
            "user_text": "做一个产品介绍动画",
            "phase": "plan_review",
            "seconds": 8.0,
            "fps": 24.0,
        }
        payload = module._export_motion_graphic_task_memory()
        assert payload is not None
        assert payload["version"] == TeachingAnimationModule._MOTION_GRAPHIC_TASK_MEMORY_VERSION
        fresh = TeachingAnimationModule()
        assert fresh._restore_motion_graphic_task_memory(payload) == 1
        plan = fresh._pending_motion_graphic_requests["r1"]
        assert plan["phase"] == "resume_required"
        assert plan["render_mode"] == "html"
        assert plan["plan_approved"] is False

    def test_restore_rejects_bad_payloads(self) -> None:
        module = TeachingAnimationModule()
        module._pending_motion_graphic_requests["stale"] = {"brief": object()}
        assert module._restore_motion_graphic_task_memory(None) == 0
        assert module._restore_motion_graphic_task_memory(
            {"version": 99, "tasks": []}) == 0
        assert module._pending_motion_graphic_requests == {}

    def test_task_text_bounds_length(self) -> None:
        module = TeachingAnimationModule()
        long_text = "x" * 5000
        assert len(module._safe_motion_graphic_task_text(long_text)) == \
            TeachingAnimationModule._MAX_RECOVERABLE_MOTION_GRAPHIC_TEXT_CHARS

    def test_supported_edit_commands_include_base(self) -> None:
        commands = TeachingAnimationModule.get_supported_edit_commands()
        for name in ("add_element", "update_element", "reorder_element",
                     "set_animation"):
            assert name in commands


class TestModuleElementLookup:
    def test_find_scene_element_by_title_and_subtitle(self) -> None:
        module = TeachingAnimationModule()
        elements = (
            SceneElement(element_id="title_main", type="text",
                         transform=SceneTransform(), style={}, content="主标题"),
            SceneElement(element_id="subtitle_main", type="text",
                         transform=SceneTransform(), style={}, content="副标题"),
        )

        class FakeScene:
            def __init__(self, items: tuple[SceneElement, ...]) -> None:
                self.elements = items

            def element_by_id(self, eid: str) -> Any:
                return next(
                    (e for e in self.elements if e.element_id == eid), None)

        scene = FakeScene(elements)
        assert module._find_scene_element(scene, "把标题改大") is elements[0]
        assert module._find_scene_element(scene, "调整subtitle") is elements[1]
        assert module._find_scene_element(scene, "元素: subtitle_main") is elements[1]

    def test_context_fingerprint_is_stable(self) -> None:
        class FakeTimeline:
            timeline_id = "t1"
            revision = 3

        class FakeTrack:
            track_id = "g1"
            name = "Graphics"

        first = TeachingAnimationModule._collect_context_fingerprint(
            FakeTimeline(), FakeTrack(), 5)
        second = TeachingAnimationModule._collect_context_fingerprint(
            FakeTimeline(), FakeTrack(), 5)
        assert first == second


class TestModuleSurfaces:
    def test_static_html_client_passthrough(self) -> None:
        from src.features.teaching_animation.module import _StaticHtmlClient
        client = _StaticHtmlClient("<p>hi</p>")
        assert client.complete([{"role": "user"}]) == "<p>hi</p>"

    def test_module_registration_surfaces(self) -> None:
        module = TeachingAnimationModule()
        assert module.is_available() is True
        assert module.register_pages(object()) == []
        assert isinstance(module.get_node_types(), tuple)
