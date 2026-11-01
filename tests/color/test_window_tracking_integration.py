"""Real local async tracking driven by the Window/Tracker UI and controller.

This is intentionally a CPU geometry integration test. It uses the real
TrackingService, QThread worker, FrameSampler, tracker, controller slots and
ImageEngine geometry evaluator. A geometry observer substitutes for the GL
Canvas/renderer; visual GPU preview/export are outside this test's claim.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import av
import numpy as np
import pytest
from PyQt6.QtCore import QSize, QThread, QTimer
from PyQt6.QtWidgets import QApplication

from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.layer import Layer, MaskShape
from src.features.color.tracking.tracking_evaluator import TrackingEvaluator, capture_base_geometry
from src.features.color.tracking.tracking_service import TrackingService
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.ui.mask_panel import MaskPanel
from src.features.color.ui.tracker_panel import TrackerPanel
from src.features.color.ui.tracker_status import PanelState
from src.shared.tracking.frame_sampler import FrameSampler

pytestmark = pytest.mark.integration
WIDTH, HEIGHT, FRAME_COUNT = 640, 360, 48


@pytest.fixture(scope="module")
def translation_video(tmp_path_factory: Any) -> Path:
    """A textured card translates exactly one source pixel per video frame."""
    configured = os.environ.get("TOPOS_PARITY_VIDEO")
    target = Path(configured) if configured else tmp_path_factory.mktemp("window_tracking") / "translation.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261102)
    texture = rng.integers(20, 240, (96, 128, 3), dtype=np.uint8)
    # PyAV is the production decoder dependency; OpenCV macOS builds can ship
    # without an FFmpeg encoder even when all tracking operations work.
    with av.open(str(target), "w") as container:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width, stream.height = WIDTH, HEIGHT
        stream.pix_fmt = "yuv420p"
        for frame in range(FRAME_COUNT):
            pixels = np.full((HEIGHT, WIDTH, 3), 12, dtype=np.uint8)
            pixels[132:228, 220 + frame:348 + frame] = texture
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="bgr24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return target


class GeometryEngine:
    """Real evaluator entry point without renderer construction or GPU access."""

    get_tracked_geometry = ImageEngine.get_tracked_geometry
    _tracking_frame_context = ImageEngine._tracking_frame_context

    def __init__(self, layer: Layer, context: Any) -> None:
        self.layer = layer
        self.tracking_store = TrackingStore()
        self.tracking_evaluator = TrackingEvaluator(self.tracking_store)
        self.tracking_context_provider = context
        self.img_full_original = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
        self.refresh_count = 0

    def get_active_layer(self) -> Layer:
        return self.layer

    def refresh_tracking_masks(self) -> bool:
        self.refresh_count += 1
        return True


class GeometryObserver:
    """Records evaluated overlay geometry; deliberately does not create GL UI."""

    def __init__(self, owner: TrackingHarness) -> None:
        self.owner = owner
        self.logical_size = QSize(WIDTH, HEIGHT)
        self.active_shape_id: str | None = None
        self.overlays: dict[str, dict[str, Any] | None] = {}

    def update_mask_overlay(self, layer: Layer) -> None:
        self.overlays = {shape.id: self.owner.engine.get_tracked_geometry(shape) for shape in layer.mask_shapes}

    def hide_tracker_overlay(self) -> None:
        pass


class TrackingHarness(ColorControllerMixin):
    """The production controller with only host/project/render effects isolated."""

    def __init__(self, video: Path, shape_type: str) -> None:
        self.video_controller = SimpleNamespace(current_frame=0, total_frames=FRAME_COUNT)
        self.session = SimpleNamespace(clip_id="isolated-clip", source_path=str(video), source_frame=0)
        self.layer = Layer("Integration test")
        self.engine = GeometryEngine(self.layer, self._tracker_render_context)
        self.canvas = GeometryObserver(self)
        self.mask_panel = MaskPanel()
        self.mask_panel.set_image_only_tools_visible(False)
        self.mask_panel.resize(786, 418)
        self.tracker_panel = TrackerPanel()
        self.tracker_panel.resize(786, 418)
        self.tracking_service = TrackingService()
        self.pending_mask_updates: dict[str, dict[str, Any]] = {}
        self.mask_update_timer = QTimer()
        self.mask_update_timer.setSingleShot(True)
        self.mask_update_timer.setInterval(16)
        self.mask_update_timer.timeout.connect(self.process_mask_update)
        self.messages: list[str] = []
        self.status_bar = SimpleNamespace(showMessage=lambda text, _ms=0: self.messages.append(text))
        self.render_count, self.autosave_count, self.snapshot_count = 0, 0, 0
        self.completed: list[object] = []
        self.errors: list[str] = []
        self.mask_panel.shape_added.connect(self._create_shape)
        self.mask_panel.shape_updated.connect(self.on_mask_shape_updated)
        self.tracker_panel.params_changed.connect(self._on_tracker_params_changed)
        self.tracker_panel.track_forward_requested.connect(lambda: self._start_tracker_tracking("forward"))
        self.tracker_panel.track_forward_one_requested.connect(lambda: self._start_tracker_tracking("forward", single_frame=True))
        self.tracker_panel.cancel_requested.connect(self._cancel_tracker_tracking)
        self.tracking_service.task_started.connect(self._on_tracker_task_started)
        self.tracking_service.task_progress.connect(self._on_tracker_task_progress)
        self.tracking_service.task_finished.connect(self._on_tracker_task_finished)
        self.tracking_service.task_partial_result.connect(self._on_tracker_task_finished)
        self.tracking_service.task_finished.connect(lambda _key, _gen, result: self.completed.append(result))
        self.tracking_service.task_failed.connect(self._on_tracker_task_failed)
        self.tracking_service.task_failed.connect(lambda _key, _gen, message: self.errors.append(message))
        self.tracking_service.task_cancelled.connect(self._on_tracker_task_cancelled)
        self.tracker_panel.set_params({"mode": "translation", "quality": "standard", "feature_count": 64})
        self._tracker_params = self.tracker_panel.get_params()
        # Use a real template button signal rather than inserting a shape behind the UI.
        self.mask_panel.video_panel.add_buttons[shape_type].click()

    def _create_shape(self, kind: str) -> None:
        shape = MaskShape(kind)
        shape.center = [284 / WIDTH, .5]
        shape.size = [.15, .15 * WIDTH / HEIGHT] if kind == "circle" else [.3, .4]
        shape.feather = 0
        if kind == "rect":
            shape.edge_softness = [1.25] * 4
        elif kind == "gradient":
            shape.grad_points = [[.34, .5], [.55, .5]]
        elif kind == "curve":
            shape.points = [{"pos": list(p), "c1": list(p), "c2": list(p), "type": "corner"}
                            for p in ((.31, .3), (.58, .3), (.58, .7), (.31, .7))]
        self.layer.mask_shapes.append(shape)
        self.shape = shape
        self._tracker_shape_id = shape.id
        self.canvas.active_shape_id = shape.id
        self.mask_panel.update_shapes_list(self.layer.mask_shapes)
        self.mask_panel.select_shape(shape.id)
        self.tracker_panel.set_bound_shape(kind)
        self._sync_window_geometry_controls(shape)

    def _tracker_session(self) -> SimpleNamespace:
        return self.session

    def _seek_tracker_frame(self, frame: int) -> None:
        self.video_controller.current_frame = frame
        self.tracker_panel.set_current_frame(frame)
        self._sync_window_geometry_controls(self.shape)

    def _push_mask_structure_snapshot(self) -> None:
        self.snapshot_count += 1

    def request_render(self) -> None:
        self.render_count += 1

    def _trigger_autosave(self) -> None:
        self.autosave_count += 1

    def _is_video_playing(self) -> bool:
        return False

    def close(self) -> None:
        self.mask_update_timer.stop()
        self.tracking_service.shutdown()
        self.mask_panel.close()
        self.tracker_panel.close()

    def geometry_at(self, frame: int) -> dict[str, Any]:
        value = self.engine.tracking_evaluator.evaluate_geometry("isolated-clip", self.shape.id, frame, (WIDTH, HEIGHT))
        assert value is not None
        return value


def x_anchor(geometry: dict[str, Any]) -> float:
    if geometry["type"] == "curve":
        return float(geometry["points"][0]["pos"][0])
    if geometry["type"] == "gradient":
        return float(geometry["grad_points"][0][0])
    return float(geometry["center"][0])


def click_and_wait(harness: TrackingHarness, button: Any, qtbot: Any) -> None:
    count = len(harness.completed)
    assert button.isEnabled()
    button.click()
    qtbot.waitUntil(lambda: len(harness.completed) > count or bool(harness.errors), timeout=30000)
    assert not harness.errors, harness.errors
    qtbot.waitUntil(lambda: not harness.tracking_service.is_running("isolated-clip", harness.shape.id), timeout=5000)
    assert harness.tracker_panel.state == PanelState.SUCCESS


@pytest.mark.parametrize("kind", ["circle", "rect", "gradient", "curve"])
def test_real_async_forward_frame_clip_edit_and_store_roundtrip(kind: str, translation_video: Path,
                                                               qtbot: Any, monkeypatch: Any) -> None:
    reads: list[tuple[int, bool]] = []
    original_read = FrameSampler.read_gray

    def observed_read(sampler: FrameSampler, frame: int, *args: Any, **kwargs: Any) -> Any:
        reads.append((frame, QThread.currentThread() != QApplication.instance().thread()))
        return original_read(sampler, frame, *args, **kwargs)

    monkeypatch.setattr(FrameSampler, "read_gray", observed_read)
    harness = TrackingHarness(translation_video, kind)
    try:
        initial = capture_base_geometry(harness.shape)
        click_and_wait(harness, harness.tracker_panel.btn_forward, qtbot)
        binding = harness.engine.tracking_store.get_binding("isolated-clip", harness.shape.id)
        assert binding is not None
        assert len(binding.transforms) == FRAME_COUNT
        assert not binding.failed_frames()
        assert reads and all(in_worker for _frame, in_worker in reads)
        measured = (x_anchor(harness.geometry_at(47)) - x_anchor(initial)) * WIDTH
        assert measured == pytest.approx(47.0, abs=2.0)
        assert harness.engine.refresh_count > 0 and harness.render_count > 0
        assert harness.canvas.overlays[harness.shape.id] is not None

        harness._seek_tracker_frame(24)
        before = {frame: x_anchor(harness.geometry_at(frame)) for frame in (10, 24, 30)}
        harness.tracker_panel.segment_buttons["frame"].click()
        control = harness.mask_panel.video_panel.controls["pan"]
        control.setValue(control.value() + 10 / WIDTH * 100)
        qtbot.waitUntil(lambda: not harness.pending_mask_updates, timeout=3000)
        assert 24 in binding.manual_corrections
        assert (x_anchor(harness.geometry_at(24)) - before[24]) * WIDTH == pytest.approx(10, abs=.1)
        assert x_anchor(harness.geometry_at(10)) == pytest.approx(before[10])
        assert x_anchor(harness.geometry_at(30)) == pytest.approx(before[30])

        prior = {frame: x_anchor(harness.geometry_at(frame)) for frame in (10, 24, 30)}
        harness.tracker_panel.segment_buttons["clip"].click()
        control.setValue(control.value() + 5 / WIDTH * 100)
        qtbot.waitUntil(lambda: not harness.pending_mask_updates, timeout=3000)
        for frame in prior:
            assert (x_anchor(harness.geometry_at(frame)) - prior[frame]) * WIDTH == pytest.approx(5, abs=.1)

        saved = json.dumps(harness.engine.tracking_store.to_dict())
        restored = TrackingStore.from_dict(json.loads(saved))
        evaluator = TrackingEvaluator(restored)
        for frame in (0, 10, 24, 30, 47):
            assert evaluator.evaluate_geometry("isolated-clip", harness.shape.id, frame, (WIDTH, HEIGHT)) == harness.geometry_at(frame)
        print(f"PARITY {kind}: 48 decoded frames, drift={measured - 47:.4f}px, Frame+10px/Clip+5px, JSON roundtrip OK")
    finally:
        harness.close()


def test_real_single_frame_button_advances_repeatedly(translation_video: Path, qtbot: Any) -> None:
    harness = TrackingHarness(translation_video, "circle")
    try:
        for expected_frame in (1, 2, 3):
            click_and_wait(harness, harness.tracker_panel.btn_forward_one, qtbot)
            assert harness.video_controller.current_frame == expected_frame
        binding = harness.engine.tracking_store.get_binding("isolated-clip", harness.shape.id)
        assert binding is not None
        assert set(binding.transforms) == {0, 1, 2, 3}
        assert binding.transforms[3].translation[0] == pytest.approx(3.0, abs=.3)
        print("PARITY one-frame forward: playhead 0→1→2→3 and transforms compose to +3px")
    finally:
        harness.close()


def test_real_stop_button_retains_completed_frames(translation_video: Path, qtbot: Any, monkeypatch: Any) -> None:
    original = FrameSampler.read_gray

    def slow_read(sampler: FrameSampler, frame: int, *args: Any, **kwargs: Any) -> Any:
        time.sleep(.005)
        return original(sampler, frame, *args, **kwargs)

    monkeypatch.setattr(FrameSampler, "read_gray", slow_read)
    harness = TrackingHarness(translation_video, "circle")
    cancelled = []
    harness.tracking_service.task_cancelled.connect(lambda *_args: cancelled.append(True))

    def stop_after_five(_key: str, _generation: int, done: int, _total: int) -> None:
        if done >= 5 and not cancelled:
            harness.tracker_panel.btn_stop.click()

    harness.tracking_service.task_progress.connect(stop_after_five)
    try:
        harness.tracker_panel.btn_forward.click()
        qtbot.waitUntil(lambda: bool(cancelled), timeout=5000)
        binding = harness.engine.tracking_store.get_binding("isolated-clip", harness.shape.id)
        assert 2 <= len(binding.transforms) < FRAME_COUNT
        assert not harness.completed, "A stopped pass must not be reported as a full completed pass"
        assert harness.tracker_panel.state == PanelState.SUCCESS
        assert harness.geometry_at(max(binding.transforms))["center"][0] > harness.shape.center[0]
    finally:
        harness.close()
