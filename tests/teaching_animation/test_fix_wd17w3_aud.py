"""WD17-W3 regression tests.

AUD-28005: the confirmation paths (html_generate confirm, Motion Graphic
confirm fallback and the no-controller staging fallback) run the full-size
real-browser validation on a QThread instead of the UI thread, reporting
through queued signals; the UI event loop keeps spinning while the capture
runs and the write happens on the UI thread after the gate passes.

AUD-30640: numeric inspector rows never ``float()``/``int()``-cast
unvalidated ``parameter_ui`` metadata — malformed template data degrades to
the declared defaults instead of raising through the scene refresh, and a
non-sequence ``enum`` degrades to a plain text row.

AUD-30702: layer-tree reorder indices are computed against the full
page-grouped display sequence (all pages expanded), never against the
currently visible rows, so a collapsed page can no longer shift the emitted
tuple index into a wrong document position.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
from src.features.teaching_animation.core.model import (
    SceneElement,
    ScenePage,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.scene_store import get_shared_scene_store
from src.features.teaching_animation.ui.html_inspector import HtmlInspector


def _frame() -> RenderedFrame:
    return RenderedFrame(
        frame=0, width=4, height=4, pixel_format="rgba",
        data=bytes(range(64)),
    )


def _spin_until(predicate: Callable[[], bool], timeout_ms: int = 5000) -> None:
    """Process UI events until ``predicate`` holds; fail on timeout.

    The polling timer only ever fires while the Qt event loop spins, so a
    passing spin also proves the loop was never blocked (AUD-28005).
    """
    from PyQt6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll = QTimer()
    poll.setSingleShot(False)

    def _check() -> None:
        if predicate():
            loop.quit()

    poll.timeout.connect(_check)
    poll.start(15)
    deadline.start(timeout_ms)
    loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate(), "condition not reached before the spin timeout"


# --------------------------------------------------------------------------- #
# AUD-30640 — html_inspector numeric metadata degradation
# --------------------------------------------------------------------------- #


def _token_layer(tokens: tuple[str, ...], metadata: dict[str, Any],
                 html: str = "") -> FreeformHtmlMotionLayer:
    body = html or (
        "<html><head><style>:root{"
        + "".join(f"--{name}: 0.5; " for name in tokens)
        + "}</style></head><body><main data-topos-page=\"1\">x</main></body></html>"
    )
    return FreeformHtmlMotionLayer(
        layer_id=f"layer-{uuid.uuid4().hex[:8]}",
        sanitized_html=body,
        viewport_width=640,
        viewport_height=360,
        fps_num=24,
        fps_den=1,
        duration_frames=24,
        editable_tokens=tokens,
        metadata=metadata,
    )


class TestNumericMetadataDegradation:
    def test_malformed_bounds_fall_back_to_defaults(self, qapp) -> None:  # type: ignore[no-untyped-def]
        inspector = HtmlInspector()
        layer = _token_layer(
            ("speed",),
            {"parameter_ui": {"speed": {
                "min": "abc", "max": None, "step": [1], "decimals": "two",
            }}},
        )
        # AUD-30640: this used to raise ValueError/TypeError inside set_layer
        # and abort the whole window scene refresh.
        inspector.set_layer(layer)
        from src.shared.ui.widgets import ParamSliderRow

        sliders = [
            widget for widget in inspector._form_host.findChildren(ParamSliderRow)
        ]
        assert len(sliders) == 1
        inspector.deleteLater()

    def test_ui_number_rejects_bool_none_strings_and_nan(self) -> None:
        for bad in ("abc", None, True, [1], float("nan"), float("inf")):
            assert HtmlInspector._ui_number({"min": bad}, "min", 0.25) == 0.25
        assert HtmlInspector._ui_number({"min": 2}, "min", 0.25) == 2.0
        assert HtmlInspector._ui_number({"min": 1.5}, "min", 0.25) == 1.5
        assert HtmlInspector._ui_number({}, "min", -1.0) == -1.0

    def test_ui_int_degrades_and_clamps(self) -> None:
        for bad in ("two", None, False, 2.5, float("nan")):
            assert HtmlInspector._ui_int({"decimals": bad}, "decimals", 3) == 3
        assert HtmlInspector._ui_int({"decimals": 1}, "decimals", 3) == 1
        assert HtmlInspector._ui_int({"decimals": -4}, "decimals", 3) == 0

    def test_zero_step_degrades_instead_of_dividing_by_zero(self, qapp) -> None:  # type: ignore[no-untyped-def]
        inspector = HtmlInspector()
        layer = _token_layer(
            ("speed",), {"parameter_ui": {"speed": {"step": 0}}},
        )
        inspector.set_layer(layer)  # must not raise ZeroDivisionError
        from src.shared.ui.widgets import ParamSliderRow

        assert inspector._form_host.findChildren(ParamSliderRow)
        inspector.deleteLater()

    def test_string_enum_degrades_to_text_row(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from PyQt6.QtWidgets import QLineEdit

        from src.shared.ui.widgets import ChevronComboBox

        inspector = HtmlInspector()
        layer = _token_layer(
            ("style",),
            {
                "motion_graphic_parameter_schema": (
                    {"name": "style", "type": "string", "enum": "bold"},
                ),
            },
            html=(
                "<html><head><style>:root{--style: bold;}</style></head>"
                "<body><main data-topos-page=\"1\">x</main></body></html>"
            ),
        )
        inspector.set_layer(layer)  # a string enum used to iterate per character
        assert inspector._form_host.findChildren(QLineEdit)
        assert not inspector._form_host.findChildren(ChevronComboBox)
        inspector.deleteLater()


# --------------------------------------------------------------------------- #
# AUD-30702 — layer tree reorder against the full document
# --------------------------------------------------------------------------- #


def _element(eid: str, z: int = 0, page_id: str = "") -> SceneElement:
    return SceneElement(
        element_id=eid, type="text", z_index=z, visible=True,
        transform=SceneTransform(x=10.0, y=10.0, width=200.0, height=60.0),
        style={}, content=eid, page_id=page_id,
    )


def _paged_scene(*, p1: int = 2, p2: int = 1, p3: int = 0) -> TeachingSceneDocument:
    """Two pages; ``p1``/``p2`` elements per page, z unique and descending."""
    scene = replace(
        TeachingSceneDocument.create_default("wd17w3", duration_frames=48),
        elements=(),
        pages=(
            ScenePage(page_id="pg1", duration_frames=24),
            ScenePage(page_id="pg2", duration_frames=24),
        ),
    )
    top = p1 + p2
    for index in range(p1):
        scene = scene.with_element_added(
            _element(f"a{index}", z=top - index, page_id="pg1"))
    for index in range(p2):
        scene = scene.with_element_added(
            _element(f"b{index}", z=p2 - index, page_id="pg2"))
    assert p3 == 0
    return scene


def _row_ids(panel) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        str(panel.list_widget.item(index).data(256))
        for index in range(panel.list_widget.count())
    ]


class TestFullDocumentReorderIndices:
    def _panel(self, scene: TeachingSceneDocument, expanded: tuple[str, ...]):
        from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel

        panel = LayerTreePanel()
        panel.set_scene(scene, expanded_page_ids=expanded)
        return panel

    def test_full_display_sequence_matches_all_expanded_rows(self, qapp) -> None:  # type: ignore[no-untyped-def]
        scene = _paged_scene(p1=2, p2=1)  # a0(z3) a1(z2) | b0(z1)
        panel = self._panel(scene, expanded=("pg1", "pg2"))
        assert panel._full_display_element_ids() == ["a0", "a1", "b0"]
        visible = [rid for rid in _row_ids(panel) if not rid.startswith("__")]
        assert visible == ["a0", "a1", "b0"]
        panel.deleteLater()

    def test_footer_move_with_later_page_collapsed_uses_full_index(self, qapp) -> None:  # type: ignore[no-untyped-def]
        # Visible element rows count 2 (a0, a1); the collapsed pg2 hides b0.
        # Moving a1 up must emit the FULL-document index 2 (N=3, display
        # position 0), not the visible-row index 1 the old formula produced.
        scene = _paged_scene(p1=2, p2=1)
        panel = self._panel(scene, expanded=("pg1",))
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(
            lambda rid, index: emitted.append((rid, index)))
        panel.list_widget.setCurrentRow(2)  # current on a1
        panel._move_current(-1)
        assert emitted == [("a1", 2)]
        panel.deleteLater()

    def test_footer_move_on_later_page_counts_preceding_pages(self, qapp) -> None:  # type: ignore[no-untyped-def]
        # All pages expanded: pg2's block starts after pg1's two elements, so
        # moving b1 (pg2 local position 1) up must map through the page-block
        # offset, not treat the page-local target as a global position.
        scene = _paged_scene(p1=2, p2=2)  # full display: a0 a1 | b0 b1
        panel = self._panel(scene, expanded=("pg1", "pg2"))
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(
            lambda rid, index: emitted.append((rid, index)))
        rows = _row_ids(panel)
        panel.list_widget.setCurrentRow(rows.index("b1"))
        panel._move_current(-1)
        # Full position = 2 (block offset) + 0 (target) → tuple 4-1-2 = 1.
        assert emitted == [("b1", 1)]
        panel.deleteLater()

    def test_drag_with_collapsed_later_page_emits_full_index(self, qapp) -> None:  # type: ignore[no-untyped-def]
        scene = _paged_scene(p1=2, p2=1)
        panel = self._panel(scene, expanded=("pg1",))
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(
            lambda rid, index: emitted.append((rid, index)))
        # Drag a0 below a1 inside pg1 (rows: [pg1, a0, a1, bg, pg2, global]).
        item = panel.list_widget.takeItem(1)
        panel.list_widget.insertItem(2, item)
        panel.list_widget.setCurrentItem(item)
        panel._rows_moved(None, 1, 1, None, 2)
        # Visible order [a1, a0]; a0 at visible position 1 == full position 1
        # (nothing collapsed above) → tuple index 3-1-1 = 1 (old code: 0).
        assert emitted == [("a0", 1)]
        panel.deleteLater()

    def test_drag_with_collapsed_earlier_page_counts_hidden_block(self, qapp) -> None:  # type: ignore[no-untyped-def]
        scene = _paged_scene(p1=2, p2=2)
        panel = self._panel(scene, expanded=("pg2",))
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(
            lambda rid, index: emitted.append((rid, index)))
        # Rows: [pg1(coll), pg2, b1, b0, global].  Drag b1 below b0.
        rows = _row_ids(panel)
        item = panel.list_widget.takeItem(rows.index("b1"))
        panel.list_widget.insertItem(rows.index("b0") + 1, item)
        panel.list_widget.setCurrentItem(item)
        panel._rows_moved(None, rows.index("b1"), rows.index("b1"), None,
                          rows.index("b0") + 1)
        # Visible [b0, b1]: b1 at position 1; the collapsed pg1 block (2
        # elements) sits above in the full display → full position 3 →
        # tuple 4-1-3 = 0.
        assert emitted == [("b1", 0)]
        panel.deleteLater()

    def test_emitted_index_moves_element_to_intended_tuple_slot(self, qapp) -> None:  # type: ignore[no-untyped-def]
        # Round-trip through the real consumer: the emitted full-document
        # index must place the element exactly where the visible→full mapping
        # says, not merely stay inside the valid range.
        scene = _paged_scene(p1=2, p2=1)
        panel = self._panel(scene, expanded=("pg1",))
        emitted: list[tuple[str, int]] = []
        panel.reorderRequested.connect(
            lambda rid, index: emitted.append((rid, index)))
        rows = _row_ids(panel)
        panel.list_widget.setCurrentRow(rows.index("a1"))
        panel._move_current(-1)  # a1 to the top of its page / the display
        (rid, index), = emitted
        assert (rid, index) == ("a1", 2)
        reordered = scene.with_elements_reordered(rid, index)
        # Document tuple was [a0, a1, b0] (paint-ascending); moving a1 to the
        # mirrored top position puts it at the tuple end.
        assert [element.element_id for element in reordered.elements] == [
            "a0", "b0", "a1",
        ]
        panel.deleteLater()


# --------------------------------------------------------------------------- #
# AUD-28005 — browser validation off the UI thread
# --------------------------------------------------------------------------- #


class _RecorderPanel:
    def __init__(self) -> None:
        self.proposals: list[tuple] = []
        self.cleared: list[str] = []
        self.statuses: list[tuple] = []
        self.messages: list[tuple] = []

    def set_motion_graphic_proposal(self, *args) -> None:
        self.proposals.append(args)

    def clear_motion_graphic_proposal(self, request_id) -> None:
        self.cleared.append(str(request_id))

    def set_activity_status(self, text, *, detail="", active=True, status="running"):
        self.statuses.append((str(text), str(detail), bool(active), status))

    def append_chat_message(self, role, text, **_kwargs) -> None:
        self.messages.append((str(role), str(text)))


class _RecordingService:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def append_chat_message(self, role, text, **_kwargs) -> None:
        self.messages.append((str(role), str(text)))


class _FakeTrack:
    def __init__(self) -> None:
        self.track_id = "gfx"
        self.track_role = "graphics"
        self.clips: list[Any] = []


class _FakeTimeline:
    def __init__(self) -> None:
        self.video_tracks = [_FakeTrack()]
        self._clips: dict[str, Any] = {}

    def get_clip_by_id(self, clip_id: str) -> Any:
        return self._clips.get(clip_id)


class _FakeTimelineView:
    def __init__(self) -> None:
        self._timeline = _FakeTimeline()
        self.commands: list[Any] = []

    def execute_command(self, command: Any) -> None:
        self.commands.append(command)
        clip = SimpleNamespace(
            asset_id=getattr(command, "_asset_id", None)
            or getattr(command, "asset_id", "asset"),
            timeline_duration=getattr(command, "_duration", 48),
        )
        self._timeline._clips[getattr(command, "_clip_id", "clip")] = clip


class _FakeTimelinePanel:
    def __init__(self) -> None:
        self._view = _FakeTimelineView()

    def get_timeline_view(self) -> _FakeTimelineView:
        return self._view


def _html_scene(scene_id: str, html: str) -> TeachingSceneDocument:
    return replace(
        TeachingSceneDocument.create_default(
            "WD17W3 Clip", scene_id=scene_id, width=640, height=360,
            fps_num=25, duration_frames=50,
        ),
        html_layer=FreeformHtmlMotionLayer(
            layer_id=f"layer-{scene_id}",
            sanitized_html=html,
            viewport_width=640,
            viewport_height=360,
            fps_num=25,
            fps_den=1,
            duration_frames=50,
        ),
    )


def _deck(pages: tuple[str, ...]) -> str:
    body = "".join(
        f'<main data-topos-page="{i}">{text}</main>'
        for i, text in enumerate(pages, start=1)
    )
    return f"<!doctype html><html><body>{body}</body></html>"


def _window(**overrides: Any) -> SimpleNamespace:
    window = SimpleNamespace(
        timeline_panel=_FakeTimelinePanel(),
        ai_edit_assistant_panel=_RecorderPanel(),
        _ai_assistant_service=_RecordingService(),
        _ai_edit_controller=None,
        _current_edit_project=None,
    )
    for key, value in overrides.items():
        setattr(window, key, value)
    return window


class TestBrowserValidationJob:
    def test_job_runs_off_main_thread_and_keeps_loop_spinning(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        module = TeachingAnimationModule()
        main_thread = threading.get_ident()
        validation_threads: list[int] = []
        completed: list[str] = []

        def _slow_validate(_scene: Any) -> tuple[RenderedFrame, int]:
            validation_threads.append(threading.get_ident())
            time.sleep(0.15)  # long enough to prove the loop keeps serving
            return _frame(), 0

        monkeypatch.setattr(module, "_validate_motion_graphic_scene", _slow_validate)
        timer_fired: list[bool] = []

        from PyQt6.QtCore import QTimer

        QTimer.singleShot(30, lambda: timer_fired.append(True))
        started = module._start_browser_validation_job(
            "unit:1", object(),
            lambda rendered: completed.append("ok"),
            lambda message: completed.append(message),
        )
        assert started is True
        assert completed == []  # nothing synchronous
        _spin_until(lambda: bool(completed))
        assert completed == ["ok"]
        assert validation_threads and validation_threads[0] != main_thread
        # A timer scheduled before the spin fired while the capture ran, so
        # the UI event loop was never blocked (AUD-28005).
        assert timer_fired == [True]
        assert module._browser_validation_jobs == {}

    def test_job_failure_is_reported_on_the_ui_thread(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        module = TeachingAnimationModule()

        def _failing_validate(_scene: Any) -> tuple[RenderedFrame, int]:
            raise RuntimeError("HTML 验证捕获为空：候选帧=[0]")

        monkeypatch.setattr(module, "_validate_motion_graphic_scene", _failing_validate)
        failures: list[str] = []
        started = module._start_browser_validation_job(
            "unit:2", object(), lambda _rendered: None, failures.append,
        )
        assert started is True
        _spin_until(lambda: bool(failures))
        assert failures and "捕获为空" in failures[0]
        assert module._browser_validation_jobs == {}

    def test_empty_capture_frame_reports_failure(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.features.teaching_animation.module import _BrowserValidationJob

        job = _BrowserValidationJob(
            object(), validate=lambda _scene: (SimpleNamespace(
                data=b"", width=0, height=0,
            ), 3),
        )
        failures: list[str] = []
        job.failed.connect(failures.append)
        job.succeeded.connect(lambda _rendered: failures.append("BAD"))
        job.start()
        _spin_until(lambda: bool(failures))
        job.wait()
        assert failures == ["渲染验证帧 3 为空"]


class TestConfirmHtmlGenerateOffThread:
    def _plan(self, module: TeachingAnimationModule, scene_id: str,
              raw_html: str) -> str:
        store = get_shared_scene_store()
        request_id = f"html-generate-{uuid.uuid4().hex[:8]}"
        module._pending_html_generate_requests[request_id] = {
            "scene_id": scene_id,
            "raw_html": raw_html,
            "scene_revision": store.revision(scene_id),
            "constraints": module._html_generation_constraints(
                store.get(scene_id)),
            "known_asset_ids": (),
            "user_text": "把标题改成「新品发布」",
        }
        return request_id

    def test_confirm_defers_browser_gate_then_applies_on_ui_thread(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("TOPOS_TEST_MODE", raising=False)
        store = get_shared_scene_store()
        store.clear()
        store.register(_html_scene("scene-w3a", _deck(("旧标题",))))
        window = _window()
        panel = window.ai_edit_assistant_panel
        module = TeachingAnimationModule()
        request_id = self._plan(module, "scene-w3a", _deck(("新品发布",)))
        main_thread = threading.get_ident()
        validation_threads: list[int] = []

        def _validate(_scene: Any) -> tuple[RenderedFrame, int]:
            validation_threads.append(threading.get_ident())
            time.sleep(0.05)
            return _frame(), 0

        monkeypatch.setattr(module, "_validate_motion_graphic_scene", _validate)
        try:
            result = module._confirm_html_generate_request(window, request_id)
            # AUD-28005: the gate is deferred; the confirm returns "accepted,
            # in flight" and the write happens after the queued completion.
            assert result is None
            assert request_id in module._pending_html_generate_requests
            assert "旧标题" in store.get("scene-w3a").html_layer.sanitized_html
            _spin_until(
                lambda: request_id not in module._pending_html_generate_requests)
            assert "新品发布" in store.get("scene-w3a").html_layer.sanitized_html
            assert request_id in panel.cleared
            assert validation_threads and validation_threads[0] != main_thread
        finally:
            store.clear()
            module._browser_validation_jobs.clear()

    def test_confirm_failure_surfaces_diagnosis_without_write(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("TOPOS_TEST_MODE", raising=False)
        store = get_shared_scene_store()
        store.clear()
        store.register(_html_scene("scene-w3b", _deck(("旧标题",))))
        window = _window()
        module = TeachingAnimationModule()
        request_id = self._plan(module, "scene-w3b", _deck(("坏文档",)))

        def _failing(_scene: Any) -> tuple[RenderedFrame, int]:
            raise RuntimeError("HTML 验证捕获为空：候选帧=[0, 12]")

        monkeypatch.setattr(module, "_validate_motion_graphic_scene", _failing)
        try:
            assert module._confirm_html_generate_request(window, request_id) is None
            _spin_until(lambda: bool(window._ai_assistant_service.messages))
            assert "旧标题" in store.get("scene-w3b").html_layer.sanitized_html
            assert any(
                "捕获为空" in text
                for _role, text in window._ai_assistant_service.messages)
            assert window.ai_edit_assistant_panel.cleared == []
        finally:
            store.clear()
            module._browser_validation_jobs.clear()

    def test_test_mode_keeps_synchronous_gate(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("TOPOS_TEST_MODE", "1")
        store = get_shared_scene_store()
        store.clear()
        store.register(_html_scene("scene-w3c", _deck(("旧标题",))))
        window = _window()
        module = TeachingAnimationModule()
        request_id = self._plan(module, "scene-w3c", _deck(("新品发布",)))
        monkeypatch.setattr(
            module, "_validate_motion_graphic_scene", lambda _s: (_frame(), 0))
        try:
            result = module._confirm_html_generate_request(window, request_id)
            assert result is True  # deterministic bool, write already applied
            assert "新品发布" in store.get("scene-w3c").html_layer.sanitized_html
        finally:
            store.clear()


class TestConfirmMotionGraphicOffThread:
    def _motion_plan(self, module: TeachingAnimationModule,
                     scene: TeachingSceneDocument) -> str:
        request_id = f"motion-{uuid.uuid4().hex[:8]}"
        module._pending_motion_graphic_requests[request_id] = {
            "seconds": 2.0,
            "fps": 24.0,
            "frame": 0,
            "scene": scene,
            "user_text": "生成",
        }
        return request_id

    def test_json_scene_confirm_runs_gate_off_thread(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("TOPOS_TEST_MODE", raising=False)
        store = get_shared_scene_store()
        store.clear()
        scene = TeachingSceneDocument.create_default(
            "scene-w3m", width=64, height=36, fps_num=24, duration_frames=48,
        )
        window = _window()
        module = TeachingAnimationModule()
        request_id = self._motion_plan(module, scene)
        main_thread = threading.get_ident()
        validation_threads: list[int] = []

        def _validate(_scene: Any) -> tuple[RenderedFrame, int]:
            validation_threads.append(threading.get_ident())
            time.sleep(0.05)
            return _frame(), 0

        monkeypatch.setattr(module, "_validate_motion_graphic_scene", _validate)
        try:
            result = module._confirm_motion_graphic_request(window, request_id)
            assert result is None  # deferred to the background gate
            view = window.timeline_panel.get_timeline_view()
            _spin_until(lambda: bool(view.commands))
            assert validation_threads and validation_threads[0] != main_thread
            assert len(view.commands) == 1  # clip inserted once, after the gate
        finally:
            store.clear()
            module._browser_validation_jobs.clear()

    def test_staging_fallback_without_controller_uses_background_gate(
        self, qapp, monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("TOPOS_TEST_MODE", raising=False)
        store = get_shared_scene_store()
        store.clear()
        window = _window()  # no _ai_edit_controller → no postprocess channel
        panel = window.ai_edit_assistant_panel
        module = TeachingAnimationModule()
        request_id = f"motion-{uuid.uuid4().hex[:8]}"
        module._pending_motion_graphic_requests[request_id] = {
            "seconds": 4.0,
            "fps": 24.0,
            "frame": 0,
            "variant": 0,
            "user_text": "生成网页",
            "render_mode": "html",
        }
        main_thread = threading.get_ident()
        validation_threads: list[int] = []

        def _validate(_scene: Any) -> tuple[RenderedFrame, int]:
            validation_threads.append(threading.get_ident())
            time.sleep(0.05)
            return _frame(), 0

        monkeypatch.setattr(module, "_validate_motion_graphic_scene", _validate)
        html = _deck(("第一页", "第二页"))
        try:
            handled = module._handle_model_motion_graphic_html_response(
                window, request_id, html,
            )
            assert handled is True
            plan = module._pending_motion_graphic_requests[request_id]
            assert plan.get("browser_validated") is not True  # gate still running
            _spin_until(lambda: bool(panel.proposals))
            assert validation_threads and validation_threads[0] != main_thread
            assert plan.get("browser_validated") is True
            assert module._browser_validation_jobs == {}
        finally:
            store.clear()
            module._browser_validation_jobs.clear()
