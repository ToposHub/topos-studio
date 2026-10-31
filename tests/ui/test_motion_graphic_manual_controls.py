"""Qt coverage for the visible Motion Graphic manual-edit surfaces."""

import json
import pytest
from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLineEdit
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.ui.html_source_editor import HtmlSourceEditor
from src.features.teaching_animation.ui.html_devtools_panel import HtmlDevToolsPanel
from src.features.teaching_animation.ui.layer_tree_model import HTML_LAYER_ROW_ID
from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow
from src.features.teaching_animation.ui.page_timing_panel import PageTimingPanel
from src.shared.i18n.i18n import I18N


def _scene() -> TeachingSceneDocument:
    from src.features.teaching_animation.core.html_layer_commands import SetHtmlLayerCommand
    base = TeachingSceneDocument.create_default("controls", duration_frames=240)
    html = (
        '<section data-topos-page="1">A</section>'
        '<section data-topos-page="2">B</section>'
    )
    layer = build_html_layer(
        layer_id="manual_controls",
        sanitized_html=html,
        authored_source=html,
        viewport_width=1920,
        viewport_height=1080,
        fps_num=24,
        fps_den=1,
        duration_frames=240,
    )
    # Production always lands html layers through the command, which also
    # syncs scene.pages from the detected sections.
    return SetHtmlLayerCommand(layer).apply(replace(base, elements=()))


def test_page_timing_panel_edits_each_page_and_emits_one_request(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.shared.ui.widgets import DragValue
    from src.shared.ui.widgets.topos_controls import ToposClickableSlider

    panel = PageTimingPanel()
    panel.set_scene(_scene())
    emitted: list[tuple[str, float, str]] = []
    panel.timingRequested.connect(lambda mode, default, durations: emitted.append((mode, default, durations)))

    panel._custom.setChecked(True)
    panel._duration_inputs[0].setValue(2.5)
    panel._duration_inputs[1].setValue(7.5)
    panel._apply_timing.click()

    assert emitted == [("custom", 5.0, "2.5, 7.5")]
    assert isinstance(panel._default_seconds, DragValue)
    assert isinstance(panel._transition_seconds, DragValue)
    assert isinstance(panel._default_slider, ToposClickableSlider)
    assert isinstance(panel._transition_slider, ToposClickableSlider)
    assert all(isinstance(value, DragValue) for value in panel._duration_inputs)
    assert panel._default_seconds._max == 300.0
    assert panel._apply_timing is panel._apply_transition
    assert panel._apply_timing.text() == "Apply"


def test_page_timing_panel_emits_global_and_page_backgrounds(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = PageTimingPanel()
    panel.set_scene(_scene())
    emitted: list[tuple[str, tuple[str, ...]]] = []
    panel.backgroundSettingsRequested.connect(
        lambda global_value, page_values: emitted.append(
            (str(global_value), tuple(str(value) for value in page_values)),
        ),
    )

    panel._global_background_edit.setText("#102030")
    panel._page_background_inherit[0].setChecked(False)
    panel._page_background_edits[0].setText("#ff0000")
    panel._apply_button.click()

    assert emitted == [("#102030", ("#ff0000", ""))]


def test_html_source_editor_emits_manual_source(qapp) -> None:  # type: ignore[no-untyped-def]
    editor = HtmlSourceEditor()
    scene = _scene()
    editor.set_layer(scene.html_layer)
    emitted: list[str] = []
    editor.sourceEditRequested.connect(emitted.append)
    editor.editor.setPlainText("<section class='slide'>Edited</section>")
    editor.apply_button.click()

    assert emitted == ["<section class='slide'>Edited</section>"]


def test_html_source_editor_keeps_unapplied_draft_across_layer_refresh(qapp) -> None:  # type: ignore[no-untyped-def]
    editor = HtmlSourceEditor()
    scene = _scene()
    layer = scene.html_layer
    assert layer is not None
    editor.set_layer(layer)
    editor.editor.setPlainText("<section>Draft that must survive refresh</section>")
    assert editor.has_pending_draft is True

    # A parameter edit produces a new immutable layer instance with the same
    # layer id.  The source pane must preserve the user's unapplied draft.
    refreshed = replace(layer, revision=layer.revision + 1)
    editor.set_layer(refreshed)
    assert editor.editor.toPlainText() == "<section>Draft that must survive refresh</section>"
    assert editor.has_pending_draft is True


def test_html_editor_opens_on_unified_parameters_instead_of_empty_structured_form(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        assert window._panel_tabs.currentIndex() == 0
        assert window._selected_elements == frozenset({HTML_LAYER_ROW_ID})
        # The window itself is not shown in this fixture, so ``isVisible`` is
        # false through Qt's ancestor chain; the panel must nevertheless not
        # be hidden by its own HTML-scene gate.
        assert window.page_timing.isHidden() is False
        assert window.page_timing.parentWidget() is window.html_inspector._form_host
        assert window.html_inspector._form_layout.indexOf(window.page_timing) >= 0
        assert window._panel_tabs.isTabEnabled(2) is True
        assert window._panel_tabs.isTabEnabled(3) is True
        assert window.html_devtools.isEnabled() is True
        assert [window._panel_tabs.tabText(index) for index in range(window._panel_tabs.count())] == [
            "Parameters", "Scenes", "Source", "DevTools", "Library",
        ]
        assert window.layer_tree.parentWidget() is window._attributes_splitter
        assert window._panel_tabs.elideMode() == Qt.TextElideMode.ElideNone
        assert window.timeline._transport_bar.parentWidget() is window.keyframes_panel._transport_layout.parentWidget()
        assert window.timeline._layout.indexOf(window.timeline._transport_bar) == -1
        assert window.timeline.play_button.width() == 32
        assert window.timeline.previous_page_button.width() == 28
        assert window.timeline.next_page_button.width() == 28
        assert window.timeline.marker_button.width() >= 76
        assert window.timeline.loop_button.icon().isNull() is False
        assert window.timeline.duration_spin.value() == 10.0
        assert window.timeline.duration_spin._suffix == " s"
    finally:
        window.close()


def test_motion_graphic_editor_uses_current_english_locale(qapp) -> None:  # type: ignore[no-untyped-def]
    I18N.initialize()
    assert I18N.load_locale("en_US")
    TeachingAnimationModule().register_i18n()
    window = MotionGraphicEditorWindow(_scene())
    try:
        assert [window._panel_tabs.tabText(index) for index in range(window._panel_tabs.count())] == [
            "Parameters", "Scenes", "Source", "DevTools", "Library",
        ]
        assert window.timeline.marker_button.text() == "Marker"
        assert window._timeline_panel.layout().indexOf(window.timeline) == 0
    finally:
        window.close()


def test_single_page_timing_hides_redundant_page_list(qapp) -> None:  # type: ignore[no-untyped-def]
    base = _scene()
    html = '<section data-topos-page="1">Only page</section>'
    layer = build_html_layer(
        layer_id="single-page", sanitized_html=html, authored_source=html,
        viewport_width=1920, viewport_height=1080, fps_num=24, fps_den=1, duration_frames=240,
    )
    window = MotionGraphicEditorWindow(replace(base, html_layer=layer))
    try:
        assert window.page_timing._page_scroll.isHidden() is True
        assert window.page_timing._custom.isHidden() is True
        window.page_timing._collapse.click()
        assert window.page_timing._content.isHidden() is True
        assert window.page_timing._collapse.text() == "▶"
    finally:
        window.close()


def test_view_menu_panel_entry_restores_a_splitter_hidden_inspector(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        # Exercise the same laid-out desktop state in which a user can drag
        # the splitter, without starting an unrelated browser capture.
        window._html_preview_live = True
        window.show()
        qapp.processEvents()
        window._main_splitter.setSizes([1440, 0])
        window._show_right_panel_tab(0)
        qapp.processEvents()

        assert window._panel_tabs.currentIndex() == 0
        assert window._main_splitter.sizes()[1] >= 320
    finally:
        window.close()


def test_keyframes_toolbar_hosts_compact_transport(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.resize(1600, 900)
        window.show()
        qapp.processEvents()

        transport = window.timeline._transport_bar
        assert transport.parentWidget() is window.keyframes_panel._transport_layout.parentWidget()
        assert window.timeline.isHidden() is True
        # The timecode is painted in the same header band as Edit's ruler.
        assert window.keyframes_panel.time_display == "00:00:00:00"
        assert transport.width() < 450
        assert window.timeline.height() == 18
    finally:
        window.close()


def test_space_toggles_transport_without_stealing_text_input(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        qapp.processEvents()
        space = lambda: QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier,
        )
        window.keyPressEvent(space())
        assert window.timeline._timer.isActive() is True
        window.keyPressEvent(space())
        assert window.timeline._timer.isActive() is False

        input_field = QLineEdit(window)
        input_field.show()
        input_field.setFocus()
        qapp.processEvents()
        assert qapp.focusWidget() is input_field
        window.keyPressEvent(space())
        assert window.timeline._timer.isActive() is False
        assert window.windowFlags() & Qt.WindowType.Tool
        assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    finally:
        window.close()


def test_space_from_keyframes_timeline_toggles_playback(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        qapp.processEvents()
        track = window.keyframes_panel._tracks
        track.setFocus()
        window.timeline.stop()
        first = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier,
        )
        assert window.eventFilter(track, first) is True
        assert window.timeline._timer.isActive() is True
        second = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier,
        )
        assert window.eventFilter(track, second) is True
        assert window.timeline._timer.isActive() is False
    finally:
        window.close()


def test_real_space_key_event_from_keyframes_timeline_toggles_playback(qapp) -> None:  # type: ignore[no-untyped-def]
    """Exercise the same delivered key event a user sends from the timeline."""
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        qapp.processEvents()
        focus_targets = (
            window.keyframes_panel._tree,
            window.keyframes_panel._tracks,
            window.keyframes_panel._horizontal_scrollbar,
            window.keyframes_panel._zoom_slider,
            window.timeline.slider,
        )
        for target in focus_targets:
            window.timeline.stop()
            target.setFocus()
            window.activateWindow()
            qapp.processEvents()
            QTest.keyClick(target, Qt.Key.Key_Space)
            qapp.processEvents()
            assert window.timeline._timer.isActive() is True, target.objectName()

            QTest.keyClick(target, Qt.Key.Key_Space)
            qapp.processEvents()
            assert window.timeline._timer.isActive() is False, target.objectName()
    finally:
        window.close()


def test_host_global_filter_routes_space_to_motion_graphic_window(qapp) -> None:  # type: ignore[no-untyped-def]
    """Cover the host filter that also exists in the running application."""
    from src.app.main_window import MainWindow

    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        qapp.processEvents()
        target = window.keyframes_panel._tracks
        target.setFocus()
        window.activateWindow()
        qapp.processEvents()
        window.timeline.stop()

        host = MainWindow.__new__(MainWindow)
        assert MainWindow._handle_motion_graphic_space_playback(host) is True
        assert window.timeline._timer.isActive() is True
    finally:
        window.close()


def test_space_survives_panel_scoped_shortcut_on_host(qapp) -> None:  # type: ignore[no-untyped-def]
    """A WidgetWithChildrenShortcut Space on the host must not steal MG playback.

    The Edit timeline panel registers Space with WidgetWithChildrenShortcut
    context.  ApplicationShortcut/WindowShortcut there used to consume the key
    before any event filter could see it — even while the Motion Graphic
    editor (a modeless Tool window) was the active window — so Space went
    dead in the editor.
    """
    from PyQt6.QtGui import QShortcut
    from PyQt6.QtWidgets import QWidget

    host = QWidget()
    host_child = QWidget(host)
    host_child.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    host.show()
    steal = QShortcut(Qt.Key.Key_Space, host)
    steal.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    stolen: list[bool] = []
    steal.activated.connect(lambda: stolen.append(True))

    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        qapp.processEvents()
        window.timeline.stop()
        target = window.keyframes_panel._tracks
        target.setFocus()
        window.activateWindow()
        qapp.processEvents()

        QTest.keyClick(target, Qt.Key.Key_Space)
        qapp.processEvents()
        assert window.timeline._timer.isActive() is True
        assert stolen == []

        QTest.keyClick(target, Qt.Key.Key_Space)
        qapp.processEvents()
        assert window.timeline._timer.isActive() is False
        assert stolen == []

        # Focus inside the shortcut's own scope still triggers it: Edit
        # transport keeps working when focus is in the timeline panel.
        host_child.setFocus()
        host.activateWindow()
        qapp.processEvents()
        QTest.keyClick(host_child, Qt.Key.Key_Space)
        qapp.processEvents()
        assert stolen == [True]
    finally:
        window.close()
        host.close()


def test_page_transport_uses_viewer_icons_and_timecode(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.timeline.set_frame(25)
        assert window.timeline.timecode_label.text() == "1.04 s"
        window.timeline.next_page_button.click()
        assert window.editor_controller.current_frame == 120
        window.timeline.previous_page_button.click()
        assert window.editor_controller.current_frame == 0
        window.timeline.toggle_play()
        assert window.timeline.play_button.icon().isNull() is False
    finally:
        window.close()


def test_page_apply_is_one_undo_operation_and_markers_are_visible(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtWidgets import QMessageBox
    window = MotionGraphicEditorWindow(_scene())
    # Clearing all markers now asks for confirmation (WP-5); auto-accept it.
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
    try:
        before = len(window.editor_controller._stack._undo_stack)
        window.page_timing._default_seconds.setValue(8.0)
        window.page_timing._apply_button.click()
        assert len(window.editor_controller._stack._undo_stack) == before + 1

        window.timeline.markerRequested.emit(12)
        assert window.editor_controller.scene.markers[0].frame == 12
        assert window.timeline.slider._markers == ((12, "Marker 12"),)
        window.timeline.markerJumpRequested.emit(12)
        assert window.timeline.slider.value() == 12
        before_clear = len(window.editor_controller._stack._undo_stack)
        window.timeline.markersClearRequested.emit()
        assert window.editor_controller.scene.markers == ()
        assert len(window.editor_controller._stack._undo_stack) == before_clear + 1
        window.editor_controller.undo()
        assert window.editor_controller.scene.markers[0].frame == 12
    finally:
        # This test exercises commands directly; do not leave an offscreen
        # QMessageBox waiting for a user decision during teardown.
        window.editor_controller.mark_clean()
        window.close()


def test_page_backgrounds_apply_and_undo_with_global_fallback(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.page_timing._global_background_edit.setText("#102030")
        window.page_timing._page_background_inherit[0].setChecked(False)
        window.page_timing._page_background_edits[0].setText("#ff0000")
        before = window.editor_controller.scene
        window.page_timing._apply_button.click()
        after = window.editor_controller.scene

        assert after.background["color"] == "#102030"
        assert after.pages[0].background == "#ff0000"
        assert after.pages[1].background == ""
        window.editor_controller.undo()
        assert window.editor_controller.scene == before
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_transport_can_stop_at_end_when_loop_is_disabled(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.timeline.loop_button.setChecked(False)
        window.timeline.set_frame(window.timeline.slider.maximum())
        window.timeline.toggle_play()
        window.timeline._advance()
        assert window.timeline.slider.value() == window.timeline.slider.maximum()
        assert window.timeline._timer.isActive() is False
    finally:
        window.close()


def test_vector_draw_tool_creates_an_undoable_native_shape_layer(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        before = len(window.editor_controller.scene.elements)
        window._set_shape_draw_mode("star")
        assert window.scene_view.draw_shape_kind == "star"
        window.scene_view.shapeDrawn.emit("star", 120.0, 80.0, 160.0, 120.0)
        created = window.editor_controller.scene.elements[-1]
        assert len(window.editor_controller.scene.elements) == before + 1
        assert created.type == "shape"
        assert created.content["kind"] == "star"
        assert created.transform.width == 160.0
        window.editor_controller.undo()
        assert len(window.editor_controller.scene.elements) == before
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_preview_header_exposes_native_vector_tools(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(TeachingSceneDocument.create_default("vector-tools"))
    try:
        assert set(window._preview_tool_buttons) == {
            None, "rect", "rounded_rect", "ellipse", "line", "arrow",
            "triangle", "polygon", "star", "path",
        }
        assert window._preview_tool_buttons[None].isChecked() is True
        assert window._preview_tool_buttons["star"].isEnabled() is True
        window._preview_tool_buttons["star"].click()
        assert window.scene_view.draw_shape_kind == "star"
        assert window._preview_tool_buttons["star"].isChecked() is True
        assert window._preview_tool_buttons[None].isChecked() is False
    finally:
        window.close()

    html_window = MotionGraphicEditorWindow(_scene())
    try:
        # HTML templates have a browser background, but can now receive
        # composited native vector overlays rather than disabling the tools.
        assert html_window._preview_tool_buttons["star"].isEnabled() is True
    finally:
        html_window.close()


def test_pen_path_and_direct_anchor_edit_are_one_command_each(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        before = len(window.editor_controller._stack._undo_stack)
        window._draw_path_layer(((20.0, 20.0), (160.0, 20.0), (80.0, 120.0), (20.0, 20.0)))
        path = window.editor_controller.scene.elements[-1]
        assert path.content["kind"] == "path"
        assert path.content["closed"] is True
        assert len(window.editor_controller._stack._undo_stack) == before + 1

        window._update_path_anchors(path.element_id, ((30.0, 30.0), (160.0, 20.0), (80.0, 120.0)))
        updated = window.editor_controller.scene.element_by_id(path.element_id)
        assert updated is not None
        assert updated.content["anchors"][0] == [0.0, 10.0]
        assert len(window.editor_controller._stack._undo_stack) == before + 2
        window.editor_controller.undo()
        assert window.editor_controller.scene.element_by_id(path.element_id) == path
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_native_shape_properties_defer_a_numeric_gesture_to_one_update(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform
    from src.features.teaching_animation.ui.scene_inspector import SceneInspector

    element = SceneElement(
        "star", "shape", transform=SceneTransform(width=120.0, height=100.0),
        style={"fill": "#ff8800", "stroke": "#000000", "stroke_width": 2.0},
        content={"kind": "star", "points": 5, "inner_ratio": 0.45},
    )
    inspector = SceneInspector()
    emitted: list[tuple[str, object]] = []
    inspector.elementUpdateRequested.connect(lambda element_id, update: emitted.append((element_id, update)))
    inspector.set_element(element)

    assert inspector._shape_section.isHidden() is False
    assert inspector.star_points.isHidden() is False
    assert inspector.corner_radius.isHidden() is True
    inspector.star_points.slider.setValue(7)
    assert emitted == []
    inspector.star_points.interactionEnded.emit()
    assert emitted == [("star", {"content": {"kind": "star", "points": 7, "inner_ratio": 0.45}})]

    inspector.set_element(SceneElement("text", "text", content="Hello"))
    assert inspector._shape_section.isHidden() is True


def test_devtools_panel_has_safe_offscreen_fallback(qapp) -> None:  # type: ignore[no-untyped-def]
    panel = HtmlDevToolsPanel()
    assert panel._open_button.text() == "Open"
    assert panel._apply_button.text() == "Apply"
    assert panel._discard_button.text() == "Discard"
    panel.set_enabled_for_document(True)
    assert panel._open_button.isEnabled() is True
    panel.set_enabled_for_document(False)
    assert panel._open_button.isEnabled() is False
    assert panel._apply_button.isEnabled() is False


def test_editor_uses_chrome_like_preview_left_inspector_right_layout(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        assert window._main_splitter.widget(0) is window._preview_frame
        assert window._main_splitter.widget(1) is window._panel_tabs
        assert window._panel_tabs.indexOf(window.html_source_editor) == 2
        assert window._panel_tabs.indexOf(window.html_devtools) == 3
        assert window._panel_tabs.minimumWidth() >= 420
        assert window._panel_tabs.maximumWidth() > 1000
    finally:
        window.close()


def test_browser_click_on_native_element_selects_the_same_layer(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.commands import AddElementCommand
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform

    window = MotionGraphicEditorWindow(_scene())
    try:
        element = SceneElement(
            "shape_picker", "shape", transform=SceneTransform(x=10, y=20, width=40, height=30),
            style={"fill": "#ff8800"}, content={"kind": "rect"},
        )
        window.editor_controller.execute(AddElementCommand(element))
        # A browser click on the native overlay carries the SceneElement id and
        # must select the same layer as clicking its row in the Layers panel.
        window._browser_element_selected("shape_picker")
        assert window._selected_elements == frozenset({"shape_picker"})
        assert window.inspector_stack.currentIndex() == 0
        # Authored editable slots keep the HTML-layer row selected.
        window._browser_element_selected("title")
        assert window._selected_elements == frozenset({HTML_LAYER_ROW_ID})
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_escape_returns_to_the_select_tool(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtCore import QEvent

    window = MotionGraphicEditorWindow(_scene())
    try:
        window._set_shape_draw_mode("star")
        assert window.scene_view.draw_shape_kind == "star"
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        monkeypatch.setattr(window, "isVisible", lambda: True)
        monkeypatch.setattr(window, "isActiveWindow", lambda: True)
        assert window.eventFilter(window, event) is True
        assert window.scene_view.draw_shape_kind is None
        assert window._preview_tool_buttons[None].isChecked() is True
    finally:
        window.close()


def test_native_overlay_script_wires_clicks_to_selection(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.ui.html_browser_preview import HtmlBrowserPreview

    preview = HtmlBrowserPreview()
    script = preview._native_overlay_script()
    assert "data-topos-native-element" in script
    assert "notifyElementSelected" in script
    assert "pointerEvents" in script
    # A draw tool hands pointer input to the surface above instead.
    preview.set_native_draw_tool("rect")
    assert "'rect'" in preview._native_overlay_script()


def test_inspector_exposes_text_appearance_and_transform_sections(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform
    from src.features.teaching_animation.ui.scene_inspector import SceneInspector

    text = SceneElement(
        "t1", "text",
        transform=SceneTransform(x=10.0, y=20.0, width=300.0, height=80.0),
        style={"color": "#ffffff", "font_family": "Inter", "font_size": 48.0,
               "font_weight": "bold", "text_align": "center", "line_height": 1.4,
               "letter_spacing": 2.0, "opacity": 0.5},
        content={"text": "Hello"},
    )
    inspector = SceneInspector()
    emitted: list[tuple[str, object]] = []
    inspector.elementUpdateRequested.connect(lambda element_id, update: emitted.append((element_id, update)))
    inspector.set_selection((text,))

    assert inspector._text_section.isVisibleTo(inspector._text_section.parentWidget()) is True
    assert inspector._appearance_section.isVisibleTo(inspector._appearance_section.parentWidget()) is True
    assert inspector.font_size.value() == 48.0
    assert inspector.font_weight.currentData() == "bold"
    assert inspector.text_align.currentData() == "center"
    assert inspector.opacity.value() == 50.0
    assert inspector.width_spin.value() == 300.0
    assert inspector.height_spin.value() == 80.0

    # One slider gesture: only the touched field is committed, once.
    inspector.font_size.slider.setValue(64)
    assert emitted == []
    inspector.font_size.interactionEnded.emit()
    assert len(emitted) == 1
    element_id, update = emitted[0]
    assert element_id == "t1"
    assert update["style"]["font_size"] == 64.0

    # Opacity is stored as the 0-1 style value the renderer expects.
    emitted.clear()
    inspector.opacity.slider.setValue(80)
    inspector.opacity.interactionEnded.emit()
    assert emitted[0][1]["style"]["opacity"] == 0.8

    # Weight/alignment combos commit immediately through the same patch path.
    emitted.clear()
    inspector.font_weight.setCurrentIndex(inspector.font_weight.findData("700"))
    assert emitted[0][1]["style"]["font_weight"] == "700"
    assert emitted[0][1]["style"]["text_align"] == "center"

    # Shapes show the Shape section but not the Text section.
    shape = SceneElement(
        "s1", "shape", transform=SceneTransform(width=100.0, height=60.0),
        style={"fill": "#ff8800"}, content={"kind": "rect"},
    )
    inspector.set_selection((shape,))
    assert inspector._text_section.isHidden() is True
    assert inspector._shape_section.isHidden() is False
    assert inspector._appearance_section.isHidden() is False


def test_native_overlay_materializes_text_typography_and_appearance() -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform
    from src.features.teaching_animation.render.html_native_overlay import native_overlay_markup

    text = SceneElement(
        "t2", "text", transform=SceneTransform(x=0.0, y=0.0, width=200.0, height=60.0),
        style={"color": "#fff", "font_size": 48.0, "font_weight": "bold",
               "text_align": "center", "line_height": 1.4, "letter_spacing": 2.0,
               "opacity": 0.9, "blend_mode": "screen", "blur": 3.0,
               "shadow_color": "#000000", "shadow_x": 2.0, "shadow_y": 3.0, "shadow_blur": 8.0},
        content={"text": "Title"},
    )
    markup = native_overlay_markup([text])
    for expected in (
        'font-size:48.0000px', 'font-weight:bold', 'text-align:center',
        'line-height:1.4000', 'letter-spacing:2.0000px', 'opacity:0.9000',
        'mix-blend-mode:screen', 'filter:blur(3.0000px)',
        'box-shadow:2.0000px 3.0000px 8.0000px #000000',
    ):
        assert expected in markup

    # Hostile style values never reach the DOM: unknown enum values fall back.
    hostile = replace(text, element_id="t3", style={
        "font_weight": "bold' onmouseover=alert(1)", "text_align": "center;}</style>",
        "blend_mode": "<script>", "font_family": "Foo; bar", "shadow_color": "javascript:alert(1)"})
    markup = native_overlay_markup([hostile])
    assert "onmouseover" not in markup
    assert "</style>" not in markup
    assert "<script>" not in markup
    assert "javascript:" not in markup
    assert "font-weight:normal" in markup
    assert "text-align:left" in markup


def test_double_click_edits_text_in_place(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.commands import AddElementCommand
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform

    window = MotionGraphicEditorWindow(_scene())
    try:
        element = SceneElement(
            "text_edit", "text", transform=SceneTransform(x=10, y=10, width=200, height=60),
            style={"color": "#ffffff"}, content={"text": "Old"},
        )
        window.editor_controller.execute(AddElementCommand(element))
        undo_depth = len(window.editor_controller._stack._undo_stack)
        window._browser_text_edited("text_edit", "New title")
        updated = window.editor_controller.scene.element_by_id("text_edit")
        assert updated is not None and updated.content["text"] == "New title"
        assert len(window.editor_controller._stack._undo_stack) == undo_depth + 1
        window.editor_controller.undo()
        restored = window.editor_controller.scene.element_by_id("text_edit")
        assert restored is not None and restored.content["text"] == "Old"

        # Canvas double-click routes to the Parameters content field.
        window.scene_view.elementEditRequested.emit("text_edit")
        assert window._selected_elements == frozenset({"text_edit"})
        assert window.inspector.content_edit.text() == "Old"
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_browser_overlay_script_wires_in_place_text_editing(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.ui.html_browser_preview import HtmlBrowserPreview

    preview = HtmlBrowserPreview()
    script = preview._native_overlay_script()
    assert "ondblclick" in script
    assert "contentEditable" in script
    assert "notifyNativeTextEdited" in script
    assert "__toposApplyingNativeOverlay" in script


def test_frame_layer_creation_and_layout_section(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform
    from src.features.teaching_animation.ui.scene_inspector import SceneInspector

    window = MotionGraphicEditorWindow(_scene())
    try:
        before = len(window.editor_controller.scene.elements)
        window._create_layer("frame")
        frame = window.editor_controller.scene.elements[-1]
        assert len(window.editor_controller.scene.elements) == before + 1
        assert frame.type == "frame"
        assert frame.layout["mode"] == "flex-column"
        assert window._selected_elements == frozenset({frame.element_id})
        window.editor_controller.undo()
        assert len(window.editor_controller.scene.elements) == before
    finally:
        window.editor_controller.mark_clean()
        window.close()

    inspector = SceneInspector()
    emitted: list[tuple[str, object]] = []
    inspector.elementUpdateRequested.connect(lambda element_id, update: emitted.append((element_id, update)))
    frame = SceneElement(
        "frame_x", "frame", transform=SceneTransform(width=500.0, height=300.0),
        style={"fill": "#1e293b"},
        layout={"mode": "flex-row", "gap": 12.0, "padding": 20.0, "columns": 3},
    )
    inspector.set_selection((frame,))
    assert inspector._layout_section.isHidden() is False
    assert inspector.layout_mode.currentData() == "flex-row"
    assert inspector.layout_gap.value() == 12.0
    # Grid-only controls stay hidden until the mode is grid.
    assert inspector.layout_columns.isHidden() is True

    # Mode combo commits immediately, merging into the existing layout.
    inspector.layout_mode.setCurrentIndex(inspector.layout_mode.findData("grid"))
    assert emitted[-1][1]["layout"]["mode"] == "grid"
    assert emitted[-1][1]["layout"]["gap"] == 12.0
    assert inspector.layout_columns.isHidden() is False

    # Gap slider gesture defers to one commit containing only the touched field.
    emitted.clear()
    inspector.layout_gap.slider.setValue(40)
    assert emitted == []
    inspector.layout_gap.interactionEnded.emit()
    layout = emitted[0][1]["layout"]
    assert layout["gap"] == 40.0
    assert layout["mode"] == "flex-row"  # untouched fields keep the element value

    # Non-frame elements hide the Layout section.
    text = SceneElement("t9", "text", content={"text": "hi"})
    inspector.set_selection((text,))
    assert inspector._layout_section.isHidden() is True


def _token_scene() -> TeachingSceneDocument:  # type: ignore[no-untyped-def]
    base = _scene()
    metadata = dict(base.metadata)
    metadata["design_tokens"] = {"palette": {"accent": "#00ff88"}}
    metadata["brand_themes"] = {
        "midnight": {"palette": {"accent": "#111827", "surface": "#000000"}},
    }
    return replace(base, metadata=metadata)


def test_native_layers_resolve_token_refs_and_expose_css_variables() -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.design_tokens import read_scene_tokens
    from src.features.teaching_animation.core.model import SceneElement
    from src.features.teaching_animation.render.html_native_overlay import (
        native_overlay_css_variables, native_overlay_markup)

    scene = _token_scene()
    tokens = read_scene_tokens(scene.metadata)
    bound = SceneElement("t1", "text", style={"color": "$palette.accent"}, content={"text": "Hi"})
    markup = native_overlay_markup([bound], tokens=tokens)
    assert "color:#00ff88" in markup  # resolved, not the literal $ref
    # Active theme overrides the scene token.
    themed = read_scene_tokens({**scene.metadata, "active_brand_theme": "midnight"})
    assert "#111827" in native_overlay_markup([bound], tokens=themed)
    css = native_overlay_css_variables(tokens)
    assert "--topos-color-accent:#00ff88" in css
    assert css.startswith("#topos-native-vector-overlay{")


def test_color_token_combo_binds_and_overrides(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.design_tokens import read_scene_tokens
    from src.features.teaching_animation.core.model import SceneElement
    from src.features.teaching_animation.ui.scene_inspector import SceneInspector

    inspector = SceneInspector()
    emitted: list[tuple[str, object]] = []
    inspector.elementUpdateRequested.connect(lambda element_id, update: emitted.append((element_id, update)))
    tokens = read_scene_tokens(_token_scene().metadata)
    element = SceneElement("t1", "text", style={"color": "#ffffff"}, content={"text": "Hi"})
    inspector.set_selection((element,), tokens=tokens)

    # Binding to the accent token writes a symbolic $palette reference.
    inspector.color_token.setCurrentIndex(inspector.color_token.findData("accent"))
    assert emitted[-1][1]["style"]["color"] == "$palette.accent"
    assert emitted[-1][1]["style"]["fill"] == "$palette.accent"

    # A bound element shows the token selected and previews the resolved hex.
    bound = SceneElement("t2", "text", style={"color": "$palette.accent"}, content={"text": "Hi"})
    inspector.set_selection((bound,), tokens=tokens)
    assert inspector.color_token.currentData() == "accent"
    # Switching back to Custom commits a local hex override.
    inspector.color_token.setCurrentIndex(0)
    assert emitted[-1][1]["style"]["color"].startswith("#")


def test_copy_paste_style_and_theme_switch_are_undoable(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.commands import AddElementCommand
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform

    window = MotionGraphicEditorWindow(_token_scene())
    try:
        source = SceneElement(
            "src", "shape", transform=SceneTransform(width=100.0, height=100.0),
            style={"fill": "#123456", "stroke_width": 4.0}, content={"kind": "rect"})
        target = SceneElement(
            "dst", "shape", transform=SceneTransform(width=100.0, height=100.0),
            style={"fill": "#ffffff"}, content={"kind": "rect"})
        window.editor_controller.execute(AddElementCommand(source))
        window.editor_controller.execute(AddElementCommand(target))

        window._set_selection({"src"})
        window._copy_style()
        window._set_selection({"dst"})
        window._paste_style()
        pasted = window.editor_controller.scene.element_by_id("dst")
        assert pasted is not None and pasted.style["fill"] == "#123456"
        window.editor_controller.undo()
        restored = window.editor_controller.scene.element_by_id("dst")
        assert restored is not None and restored.style["fill"] == "#ffffff"

        # Theme switch is one undoable metadata edit that flips token resolution.
        window._apply_brand_theme("midnight")
        from src.features.teaching_animation.core.design_tokens import read_active_brand_theme
        assert read_active_brand_theme(window.editor_controller.scene.metadata) is not None
        assert read_active_brand_theme(window.editor_controller.scene.metadata).theme_id == "midnight"
        window.editor_controller.undo()
        assert read_active_brand_theme(window.editor_controller.scene.metadata) is None
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_timeline_scrubs_native_css_animations_and_marker_clear_confirms(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PyQt6.QtWidgets import QMessageBox

    window = MotionGraphicEditorWindow(_scene())
    try:
        calls: list[float] = []
        monkeypatch.setattr(
            window.html_browser_preview, "set_native_time",
            lambda seconds: calls.append(seconds))
        window._html_browser_active = True
        window._frame_changed(12)
        assert calls and calls[-1] == pytest.approx(12 / 24.0)

        # Clearing every marker asks first and only clears after Yes.
        from src.features.teaching_animation.core.commands import AddMarkerCommand
        from src.features.teaching_animation.core.model import SceneMarker
        window.editor_controller.execute(AddMarkerCommand(SceneMarker("m1", 5, "Intro")))
        assert len(window.editor_controller.scene.markers) == 1
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No))
        window._confirm_clear_markers()
        assert len(window.editor_controller.scene.markers) == 1
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
        window._confirm_clear_markers()
        assert len(window.editor_controller.scene.markers) == 0
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_template_packages_enforce_size_limits(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.motion_graphic_template_library import (
        load_motion_graphic_template_package)

    package = tmp_path / "huge"
    package.mkdir()
    (package / "template.html").write_text("<html>" + "x" * (5 * 1024 * 1024) + "</html>", encoding="utf-8")
    (package / "preview.gif").write_bytes(b"GIF89a" + b"\x00" * 64)
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "template_id": "huge", "display_name": "Huge",
        "template_file": "template.html", "preview_file": "preview.gif",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="KiB limit"):
        load_motion_graphic_template_package(package)


def test_detach_component_instance_bakes_overrides(qapp) -> None:  # type: ignore[no-untyped-def]
    import json as _json
    from src.features.teaching_animation.core.commands import AddElementCommand
    from src.features.teaching_animation.core.components import (
        ComponentInstance, read_component_instances)
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform

    window = MotionGraphicEditorWindow(_scene())
    try:
        element = SceneElement(
            "card_title", "text", transform=SceneTransform(x=10, y=10, width=200, height=60),
            style={"color": "#ffffff", "font_size": 40.0}, content={"text": "Title"})
        window.editor_controller.execute(AddElementCommand(element))
        metadata = dict(window.editor_controller.scene.metadata)
        metadata["components"] = {
            "version": 1,
            "masters": [],
            "instances": [ComponentInstance("inst1", "master1", ("card_title",),
                                            {"card_title": {"style": {"color": "#00ff88"}}}).to_dict()],
            "active_variants": {},
        }
        from src.features.teaching_animation.core.commands import SetSceneMetadataCommand
        window.editor_controller.execute(SetSceneMetadataCommand(metadata))
        assert len(read_component_instances(window.editor_controller.scene.metadata)) == 1

        window._set_selection({"card_title"})
        assert window._detach_component_action.isEnabled() is True
        window._detach_component_instance()
        scene = window.editor_controller.scene
        assert read_component_instances(scene.metadata) == ()
        detached = scene.element_by_id("card_title")
        assert detached is not None
        assert detached.style["color"] == "#00ff88"  # override baked in
        window.editor_controller.undo()
        assert len(read_component_instances(window.editor_controller.scene.metadata)) == 1
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_source_tab_shows_read_only_generated_scene_output(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.commands import AddElementCommand
    from src.features.teaching_animation.core.model import SceneElement

    window = MotionGraphicEditorWindow(_scene())
    try:
        window.editor_controller.execute(AddElementCommand(SceneElement(
            "gen_title", "text", style={"color": "#ffffff"}, content={"text": "Generated"})))
        generated = window.html_source_editor.generated_view.toPlainText()
        assert 'data-topos-native-element="gen_title"' in generated
        assert window.html_source_editor.generated_view.isReadOnly() is True
        # Collapsed by default; the toggle reveals it (isVisible is unreliable
        # while the parent window itself is not shown in offscreen tests).
        assert window.html_source_editor.generated_view.isHidden() is True
        window.html_source_editor.generated_toggle.setChecked(True)
        assert window.html_source_editor.generated_view.isHidden() is False
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_cache_quality_tiers_drive_bitrate_and_status(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.edit.core.render_cache import RenderCacheSettings
    from src.shared.media.motion_graphic_delivery import (
        describe_motion_graphic_quality,
        motion_graphic_bitrate_kbps, motion_graphic_min_bitrate_kbps)
    from src.features.teaching_animation.cache_quality import (
        get_cache_quality, set_cache_quality)

    # The three tiers scale the 1080p reference rate by pixel area.
    assert motion_graphic_bitrate_kbps(1920, 1080, preset="proxy") == 8_000
    assert motion_graphic_bitrate_kbps(1920, 1080, preset="balanced") == 20_000
    assert motion_graphic_bitrate_kbps(3840, 2160, preset="high_quality") == 160_000
    assert motion_graphic_min_bitrate_kbps(1920, 1080, preset="proxy") == 4_000
    # Explicit requests can never undercut the preset target.
    assert motion_graphic_bitrate_kbps(
        1920, 1080, requested_kbps=1000, preset="balanced") == 20_000
    assert describe_motion_graphic_quality("high_quality") == "High Quality"
    assert RenderCacheSettings().motion_graphic_quality == "high_quality"
    assert RenderCacheSettings.from_dict(
        {"motion_graphic_quality": "proxy"}).motion_graphic_quality == "proxy"

    try:
        window = MotionGraphicEditorWindow(_scene())
        try:
            assert window._cache_quality_actions["high_quality"].isChecked() is True
            window._set_cache_quality("proxy")
            assert get_cache_quality() == "proxy"
            assert window._cache_quality_actions["proxy"].isChecked() is True
            assert window._cache_quality_actions["high_quality"].isChecked() is False
            assert "Proxy" in window._cache_status
            # The submitted status names the tier and its estimated bitrate.
            window._render_cache_callback = None
        finally:
            window.editor_controller.mark_clean()
            window.close()
    finally:
        set_cache_quality("high_quality")
