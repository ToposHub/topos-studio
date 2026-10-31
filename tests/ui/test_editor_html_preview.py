"""HTML live-preview phase-2 — editor wiring.

HTML scenes capture via a pinned browser (~0.2–0.9s/frame), so the editor
renders them on a QThread. Requests are coalesced to the newest frame and
stale results are discarded before they reach the Qt canvas.
"""

from __future__ import annotations

from dataclasses import replace
import time

import pytest
from PyQt6.QtWidgets import QToolButton

from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.core.commands import SetDurationCommand
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_page_deck import PageDeckTiming
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.ui.html_browser_preview import HtmlBrowserPreview
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    _HtmlPreviewThread,
    MotionGraphicEditorWindow,
)


def _html_scene() -> TeachingSceneDocument:
    html = "<style>.t{color:#fff}</style><div class='t'>Hi</div>"
    layer = build_html_layer(layer_id="L", sanitized_html=html, authored_source=html,
                             viewport_width=64, viewport_height=48, duration_frames=6)
    return replace(TeachingSceneDocument.create_default("s", width=64, height=48, duration_frames=6),
                   html_layer=layer)


def _rgba_frame() -> RenderedFrame:
    return RenderedFrame(frame=0, width=64, height=48, pixel_format="rgba8",
                         data=b"\xff\x00\x00\xff" * (64 * 48), backend="html")


def test_browser_preview_keeps_authored_html_and_injects_only_host_bridge(qapp) -> None:  # type: ignore[no-untyped-def]
    widget = HtmlBrowserPreview()
    authored = '<section data-topos-page="1"><h1 data-topos-node-id="title">Hello</h1></section>'
    widget.set_document(
        authored,
        PageDeckTiming(1, "default", 5.0),
        viewport_width=1920,
        viewport_height=1080,
    )
    assert authored in widget._document
    assert "toposPageDeck" in widget._document
    assert "notifyElementSelected" in widget._document
    assert "setTimeout" not in widget._document
    assert (widget._design_width, widget._design_height) == (1920, 1080)


def test_browser_preview_supports_fit_and_manual_wheel_zoom(qapp) -> None:  # type: ignore[no-untyped-def]
    widget = HtmlBrowserPreview()
    widget.set_document(
        "<body>preview</body>",
        PageDeckTiming(1, "default", 5.0),
        viewport_width=1920,
        viewport_height=1080,
    )
    widget._set_zoom(1.5, manual=True)
    assert widget.zoom_factor == pytest.approx(1.5)
    assert widget.fit_zoom is False
    widget.fit_to_view()
    assert widget.fit_zoom is True


def test_browser_preview_fit_uses_available_viewport_aspect_ratio(qapp) -> None:  # type: ignore[no-untyped-def]
    class _FakeView:
        def __init__(self) -> None:
            self.zoom = 1.0
            self.h_policy = None
            self.v_policy = None

        def width(self) -> int:
            return 960

        def height(self) -> int:
            return 540

        def setZoomFactor(self, value: float) -> None:  # noqa: N802 — Qt API spelling
            self.zoom = value

        def setHorizontalScrollBarPolicy(self, value) -> None:  # noqa: N802
            self.h_policy = value

        def setVerticalScrollBarPolicy(self, value) -> None:  # noqa: N802
            self.v_policy = value

    widget = HtmlBrowserPreview()
    widget._view = _FakeView()
    widget.set_design_size(1920, 1080)
    widget.fit_to_view()
    assert widget.zoom_factor == pytest.approx(0.5)
    assert widget._view.zoom == pytest.approx(0.5)


@pytest.fixture
def html_window(qapp):
    win = MotionGraphicEditorWindow(_html_scene())
    yield win
    win.deleteLater()
    qapp.processEvents()


@pytest.fixture
def svg_window(qapp):
    win = MotionGraphicEditorWindow(
        TeachingSceneDocument.create_default("svg", width=64, height=48, duration_frames=6))
    yield win
    win.deleteLater()
    qapp.processEvents()


class TestActionEnablement:
    def test_render_preview_enabled_for_html_scene(self, html_window) -> None:
        assert html_window._render_preview_action.isEnabled()

    def test_render_preview_disabled_for_svg_scene(self, svg_window) -> None:
        assert not svg_window._render_preview_action.isEnabled()

    def test_toolbar_groups_actions_like_the_main_project(self, html_window) -> None:
        buttons = {
            button.text(): button
            for button in html_window.findChildren(QToolButton)
            if button.objectName() == "mgMenuButton"
        }
        assert list(buttons) == ["File", "Edit", "Scene", "View", "Preview", "Export"]
        assert [action.text() for action in buttons["Export"].menu().actions()] == ["Export HTML…"]
        assert "Render Preview" in [action.text() for action in buttons["Preview"].menu().actions()]
        assert "Render Video Cache" in [action.text() for action in buttons["Preview"].menu().actions()]

    def test_video_cache_action_is_disabled_without_a_host_bridge(self, html_window) -> None:
        assert not html_window._render_video_cache_action.isEnabled()

    def test_video_cache_action_saves_then_delegates_to_host(self, qapp) -> None:  # type: ignore[no-untyped-def]
        saved: list[str] = []
        cached: list[str] = []
        window = MotionGraphicEditorWindow(
            _html_scene(),
            saver=lambda scene: saved.append(scene.scene_id),
            render_cache_callback=lambda scene: (
                cached.append(scene.scene_id) or "视频缓存已开始"
            ),
        )
        try:
            window.editor_controller.execute(SetDurationCommand(8))
            window._render_video_cache()
            assert saved == [window.editor_controller.scene.scene_id]
            assert cached == [window.editor_controller.scene.scene_id]
            assert "视频缓存已开始" in window.statusBar().currentMessage()
        finally:
            window.deleteLater()
            qapp.processEvents()


class TestRenderPreview:
    def test_visible_html_editor_starts_preview_for_current_frame(self, html_window, monkeypatch) -> None:
        requests: list[tuple[str, int, int]] = []

        def _start(scene, frame, generation):
            requests.append((scene.scene_id, frame, generation))

        monkeypatch.setattr(html_window, "_start_html_preview", _start)
        html_window.show()
        # The show event owns the initial capture; no separate button click is
        # required just to make the editor preview visible.
        assert html_window._html_preview_live
        assert requests == [(html_window.editor_controller.scene.scene_id, 0, 1)]

    def test_render_preview_starts_background_request(self, html_window, monkeypatch) -> None:
        requests: list[tuple[str, int, int]] = []

        def _start(scene, frame, generation):
            requests.append((scene.scene_id, frame, generation))

        monkeypatch.setattr(html_window, "_start_html_preview", _start)
        html_window._render_preview()
        assert html_window._html_preview_live
        assert requests == [(html_window.editor_controller.scene.scene_id, 5, 1)]

    def test_render_preview_also_delegates_full_clip_cache_to_host(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        requests: list[tuple[str, int, int]] = []
        cached: list[str] = []

        window = MotionGraphicEditorWindow(
            _html_scene(),
            render_cache_callback=lambda scene: (
                cached.append(scene.scene_id) or "已提交视频缓存"
            ),
        )
        try:
            monkeypatch.setattr(
                window,
                "_start_html_preview",
                lambda scene, frame, generation: requests.append(
                    (scene.scene_id, frame, generation)
                ),
            )
            window._render_preview()
            assert requests == [(window.editor_controller.scene.scene_id, 5, 1)]
            assert cached == [window.editor_controller.scene.scene_id]
            assert "已提交视频缓存" in window.statusBar().currentMessage()
        finally:
            window.deleteLater()
            qapp.processEvents()

    def test_successful_result_displays_rgba_frame(self, html_window) -> None:
        # Stand in for the (browser-backed) worker signal: the slot is always
        # executed on the Qt main thread before SceneView touches QImage.
        html_window._html_preview_generation = 1
        html_window._on_html_preview_ready(1, _rgba_frame())
        img = html_window.scene_view._image
        assert img is not None and not img.isNull()
        assert img.size().width() == 64 and img.size().height() == 48
        assert html_window._last_render_error is None

    def test_render_failure_shows_status_and_does_not_crash(self, html_window) -> None:
        html_window._html_preview_generation = 1
        html_window._on_html_preview_failed(1, "backend unavailable")
        assert html_window._last_render_error is not None
        assert "backend unavailable" in html_window._last_render_error

    def test_stale_result_is_discarded(self, html_window) -> None:
        html_window._html_preview_generation = 2
        html_window._on_html_preview_ready(1, _rgba_frame())
        assert html_window.scene_view._image is None

    def test_new_request_interrupts_running_worker_and_keeps_latest(self, html_window) -> None:
        class _FakeThread:
            def __init__(self):
                self.interrupted = False

            def isRunning(self):
                return True

            def requestInterruption(self):
                self.interrupted = True

        worker = _FakeThread()
        html_window._html_preview_thread = worker  # type: ignore[assignment]
        html_window._html_preview_generation = 4
        html_window._request_html_preview(html_window.editor_controller.scene, 3)
        assert worker.interrupted
        assert html_window._html_preview_pending == (html_window.editor_controller.scene, 3, 5)

    def test_background_thread_renders_immutable_snapshot(self, html_window, monkeypatch, qapp) -> None:
        class _Renderer:
            def prepare(self, scene, context):
                self.scene = scene

            def render_frame(self, animation_frame):
                frame = _rgba_frame()
                return replace(frame, frame=animation_frame.frame)

            def cleanup(self):
                pass

        from src.features.teaching_animation.render import scene_renderer_router as router
        monkeypatch.setattr(router, "select_scene_renderer", lambda scene, cancellation=None: _Renderer())
        thread = _HtmlPreviewThread(html_window.editor_controller.scene, 4, 9)
        ready: list[tuple[int, RenderedFrame]] = []
        thread.frameReady.connect(lambda generation, frame: ready.append((generation, frame)))
        thread.start()
        deadline = time.monotonic() + 2.0
        while thread.isRunning() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.001)
        thread.wait()
        qapp.processEvents()
        assert ready and ready[0][0] == 9 and ready[0][1].frame == 4
        thread.deleteLater()

    def test_html_refresh_does_not_call_render(self, html_window, monkeypatch) -> None:
        # HTML scenes must NOT render on every refresh (would freeze scrubbing).
        called = {"n": 0}

        def _render() -> RenderedFrame:
            called["n"] += 1
            return _rgba_frame()
        monkeypatch.setattr(html_window.editor_controller, "render", _render)
        html_window._refresh_scene(html_window.editor_controller.scene)
        assert called["n"] == 0   # render skipped for HTML on refresh
