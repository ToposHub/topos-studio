"""Single-window registry keyed by stable scene_id."""

from __future__ import annotations

from typing import Any, Callable

from ..core.model import TeachingSceneDocument
from ..scene_store import TeachingSceneStore, get_shared_scene_store


class EditorWindowRegistry:
    def __init__(self) -> None:
        self._windows: dict[str, Any] = {}
        self._geometry: dict[str, bytes] = {}
        self.scene_store: TeachingSceneStore = get_shared_scene_store()

    def open(self, scene: TeachingSceneDocument, *, parent: Any = None,
             saver: Callable[[TeachingSceneDocument], None] | None = None,
             insert_callback: Callable[[TeachingSceneDocument], None] | None = None,
             render_cache_callback: Callable[[TeachingSceneDocument], str] | None = None) -> Any:
        self.scene_store.register(scene)
        window = self._windows.get(scene.scene_id)
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
            return window
        from .motion_graphic_editor_window import MotionGraphicEditorWindow
        window = MotionGraphicEditorWindow(
            scene, parent=parent, registry=self, saver=saver,
            scene_store=self.scene_store, insert_callback=insert_callback,
            render_cache_callback=render_cache_callback,
        )
        if scene.scene_id in self._geometry:
            window.restoreGeometry(self._geometry[scene.scene_id])
        self._windows[scene.scene_id] = window
        window.show()
        # Match the re-open path: a Tool window that is only shown can keep
        # keyboard focus with the owner project window on macOS, leaving the
        # editor's Space playback gated behind the host's context heuristics.
        window.raise_()
        window.activateWindow()
        return window

    def remember_geometry(self, scene_id: str, geometry: bytes) -> None:
        self._geometry[scene_id] = geometry

    def unregister(self, scene_id: str, window: Any) -> None:
        if self._windows.get(scene_id) is window:
            self._windows.pop(scene_id, None)

    def get(self, scene_id: str) -> Any | None:
        return self._windows.get(scene_id)

    def close_all(self) -> None:
        for window in tuple(self._windows.values()):
            window.close()
