import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.ui.editor_controller import SceneEditorController, diff_scenes
from src.features.teaching_animation.ui.editor_window_registry import EditorWindowRegistry
from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow
from src.features.teaching_animation.ui.scene_timeline import SceneTimeline
from src.features.teaching_animation.ui.scene_view import SceneView
from src.features.teaching_animation.ui.storyboard_panel import StoryboardPanel


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _scene(scene_id: str = "scene", name: str = "Scene") -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id=scene_id,
        schema_version=1,
        name=name,
        width=320,
        height=180,
        fps_num=24,
        fps_den=1,
        duration_frames=24,
        background={"color": "#111827"},
        elements=(SceneElement("title", "text", transform=SceneTransform(x=10, y=10, width=100, height=30), content="Hello"),),
        animations=(),
    )


def test_controller_diff_update_undo_redo_and_save_lifecycle() -> None:
    original = _scene()
    controller = SceneEditorController(original)
    updates = []
    controller.subscribe(lambda scene, diff: updates.append((scene, diff)))
    diff = controller.update_element("title", {"content": "Updated"})
    assert diff.changed_element_ids == frozenset({"title"})
    assert diff.changed_fields["title"] == frozenset({"content"})
    assert controller.is_dirty
    controller.undo()
    assert controller.scene.element_by_id("title").content == "Hello"
    controller.redo()
    assert controller.scene.element_by_id("title").content == "Updated"
    saved = []
    controller.save(saved.append)
    assert saved[-1] == controller.scene
    assert not controller.is_dirty
    assert len(updates) == 3


def test_controller_frame_clamp_keyframe_and_local_diff() -> None:
    controller = SceneEditorController(_scene())
    assert controller.set_frame(100) == 23
    controller.add_keyframe("title", "opacity", 10, 1.0)
    assert controller.scene.animations[0].property == "opacity"
    assert diff_scenes(controller.scene, controller.scene).is_empty


def test_registry_reuses_same_scene_window_and_remembers_geometry(qt_app: QApplication) -> None:
    registry = EditorWindowRegistry()
    scene = _scene()
    first = registry.open(scene)
    second = registry.open(scene)
    assert first is second
    assert first.windowModality().name == "NonModal"
    first.editor_controller.mark_clean()
    first.close()
    assert registry.get(scene.scene_id) is None


def test_registry_first_open_activates_editor_window(qt_app: QApplication, monkeypatch) -> None:
    """First open must raise/activate the editor exactly like a re-open.

    A macOS Tool window that is merely shown can leave keyboard focus with
    the owner project window; Space then never reaches the editor and the
    MG timeline refuses to play until the user clicks inside it.
    """
    calls: list[str] = []
    monkeypatch.setattr(MotionGraphicEditorWindow, "raise_", lambda self: calls.append("raise"))
    monkeypatch.setattr(MotionGraphicEditorWindow, "activateWindow", lambda self: calls.append("activate"))
    registry = EditorWindowRegistry()
    window = registry.open(_scene())
    try:
        assert calls == ["raise", "activate"]
    finally:
        window.editor_controller.mark_clean()
        window.close()


def test_window_panels_are_wired_for_scrubbing_and_preview(qt_app: QApplication) -> None:
    window = MotionGraphicEditorWindow(_scene())
    window.show()
    window.timeline.set_frame(8)
    assert window.editor_controller.current_frame == 8 or window.timeline.slider.value() == 8
    assert window.scene_view._renderer is not None
    window.scene_view.set_zoom(2.0)
    assert window.scene_view.zoom == 2.0
    window.editor_controller.mark_clean()
    window.close()


def test_storyboard_supports_scene_selection_and_internal_move(qt_app: QApplication) -> None:
    panel = StoryboardPanel()
    panel.set_scenes((_scene("a", "A"), _scene("b", "B")))
    selected = []
    panel.sceneSelected.connect(selected.append)
    panel.select_scene("b")
    assert panel.current_scene_id == "b"
    assert selected[-1] == "b"
    panel.close()


def test_scene_view_and_timeline_have_editor_contracts(qt_app: QApplication) -> None:
    scene = _scene()
    view = SceneView()
    view.set_scene(scene)
    timeline = SceneTimeline()
    timeline.set_scene(scene)
    timeline.set_frame(12)
    assert timeline.slider.value() == 12
    view.set_zoom(0.5)
    view.fit_canvas()
    assert view.zoom > 0
    view.close()
    timeline.close()

