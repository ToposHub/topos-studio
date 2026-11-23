"""Still images expose the existing classic/AI backends without video NR UI."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.denoise_engine import DenoiseEngine
from src.features.color.core.history import HistoryManager
from src.features.color.core.layer import Layer
from src.features.color.ui.denoise_panel import DenoisePanel
from src.shared.video.denoise.params import VideoDenoiseParams


@pytest.fixture
def image_panel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("src.features.color.core.denoise_engine.get_models_dir", lambda subdir: str(tmp_path))
    panel = DenoisePanel(image_mode=True)
    qtbot.addWidget(panel)
    yield panel
    panel.engine.cancel()
    panel._video_param_timer.stop()
    panel._image_busy_timer.stop()


def test_image_has_classic_and_ai_without_video_controls(image_panel):
    panel = image_panel
    assert [panel.mode_combo.itemData(i) for i in range(panel.mode_combo.count())] == ["Basic", "AI"]
    assert not hasattr(panel, "temporal_frames")
    assert not hasattr(panel, "spatial_toggle")
    assert not hasattr(panel, "video_debug_overlay_chk")
    assert panel.apply_btn.isEnabled()
    assert panel.tiling_group.isHidden()
    panel.advanced_toggle.setChecked(True)
    assert not panel.tiling_group.isHidden()


def test_missing_ai_model_is_diagnosed_and_never_requested(image_panel):
    emitted = []
    image_panel.denoise_requested.connect(lambda *args: emitted.append(args))
    image_panel.mode_combo.setCurrentIndex(1)
    assert not image_panel.apply_btn.isEnabled()
    assert image_panel.model_status.text()
    assert image_panel.engine.model_dir in image_panel.model_status.text()
    image_panel.on_apply_clicked()
    assert not emitted
    image_panel.mode_combo.setCurrentIndex(0)
    assert image_panel.apply_btn.isEnabled()


def test_image_classic_requests_expected_float_parameters_only(image_panel):
    requested, video = [], []
    image_panel.denoise_requested.connect(lambda *args: requested.append(args))
    image_panel.video_denoise_changed.connect(video.append)
    image_panel.luma_control.setValue(.2)
    image_panel.color_control.setValue(.3)
    image_panel.apply_btn.click()
    assert requested == [("Basic", {"luma_strength": .2, "color_strength": .3})]
    assert not video


def test_installed_ai_selection_reuses_model_tiling_and_validates_overlap(image_panel, tmp_path):
    model = tmp_path / "NAFNet-SIDD-width64.onnx"
    model.write_bytes(b"test model; inference is not run by this UI contract")
    image_panel.refresh_models()
    image_panel.mode_combo.setCurrentIndex(1)
    assert image_panel.apply_btn.isEnabled()
    assert image_panel.tile_size_control["combo"].currentText() == "1024"
    assert image_panel.overlap_control["combo"].currentText() == "256"
    image_panel.tile_size_control["combo"].setCurrentText("256")
    image_panel.overlap_control["combo"].setCurrentText("384")
    requested = []
    image_panel.denoise_requested.connect(lambda *args: requested.append(args))
    image_panel.apply_btn.click()
    mode, params = requested[-1]
    assert mode == "AI"
    assert params == {"model_path": str(model), "blend": 1., "use_tiling": True,
                      "tile_size": 256, "overlap": 64}
    model.unlink()
    image_panel.on_apply_clicked()
    assert len(requested) == 1
    assert not image_panel.apply_btn.isEnabled()


def test_image_restore_and_reset_do_not_overwrite_hidden_video_payload(image_panel):
    video = VideoDenoiseParams().to_dict()
    video["temporal"]["enabled"] = True
    video["temporal"]["luma"] = 37
    video["future_metadata"] = {"keep": [1, 2]}
    original = deepcopy(video)
    image_panel.set_video_params_dict(video)
    video["future_metadata"]["keep"].clear()
    image_emits, video_emits, child_emits = [], [], []
    image_panel.denoise_requested.connect(lambda *args: image_emits.append(args))
    image_panel.video_denoise_changed.connect(video_emits.append)
    image_panel.mode_combo.currentIndexChanged.connect(child_emits.append)
    image_panel.set_params({"mode": "AI", "blend": .6, "tile_size": 1024,
                            "overlap": 128, "use_tiling": False})
    assert image_panel.mode_combo.currentData() == "AI"
    assert image_panel.blend_control.value() == pytest.approx(.6)
    assert not child_emits and not image_emits and not video_emits
    image_panel.reset_defaults()
    image_panel._flush_pending_video_params()
    assert image_panel.mode_combo.currentData() == "Basic"
    assert image_panel.get_video_params_dict() == original
    assert not image_emits and not video_emits
    returned = image_panel.get_video_params_dict()
    returned["future_metadata"]["keep"].clear()
    assert image_panel.get_video_params_dict() == original


def test_legacy_video_panel_keeps_its_existing_controls(qtbot):
    panel = DenoisePanel()
    qtbot.addWidget(panel)
    assert not panel.image_mode
    assert hasattr(panel, "temporal_frames") and hasattr(panel, "spatial_toggle")
    assert not hasattr(panel, "mode_combo") and not hasattr(panel, "apply_btn")
    emitted = []
    panel.video_denoise_changed.connect(emitted.append)
    panel.temporal_frames["combo"].setCurrentIndex(2)
    panel._flush_pending_video_params()
    assert emitted[-1]["temporal"]["enabled"]
    assert emitted[-1]["temporal"]["frames"] == 2


def test_models_resolve_from_shared_project_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("src.shared.paths.paths.get_project_root", lambda: str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)
    engine = DenoiseEngine()
    assert Path(engine.model_dir) == tmp_path / "models" / "denoise"
    (Path(engine.model_dir) / "installed.onnx").write_bytes(b"model")
    assert engine.get_available_models() == ["installed.onnx"]


def test_classic_signal_runs_real_worker_through_existing_controller(image_panel, qtbot, monkeypatch):
    """The restored Apply button also satisfies the existing completion slot API."""
    rng = np.random.default_rng(42)
    pixels = rng.normal(1.8, .015, (12, 12, 3)).astype(np.float32)
    layer = Layer("Grade")
    engine = SimpleNamespace(img_full_original=pixels, img_source_full=pixels,
                             current_file_path="/tmp/source.exr", active_layer_index=0,
                             layers=[layer], history=HistoryManager(),
                             source_bake_file_name=None, source_bake_type=None,
                             set_reference_image=Mock())
    def set_image(full, high, low, **kwargs):
        engine.img_full_original = full
    engine.set_image = set_image
    host = SimpleNamespace(engine=engine, denoise_panel=image_panel,
                           status_bar=Mock(), progress_bar=Mock(), layer_panel=Mock(),
                           current_file_path="/tmp/source.exr", request_render=Mock(),
                           _trigger_autosave=Mock())
    host._capture_bake_task_context = lambda: ColorControllerMixin._capture_bake_task_context(host)
    host._bake_task_context_matches = lambda context: ColorControllerMixin._bake_task_context_matches(host, context)
    image_panel.denoise_requested.connect(lambda mode, params: ColorControllerMixin.on_denoise_requested(host, mode, params))
    image_panel.engine.processing_finished.connect(lambda result: ColorControllerMixin.on_denoise_finished(host, result))
    save = Mock()
    monkeypatch.setattr("src.app.controllers.color_controller.FileIO.save_baked_layer", save)
    monkeypatch.setattr("src.app.controllers.color_controller.FileIO._resize_to_fit", lambda value, size: value)
    with qtbot.waitSignal(image_panel.engine.processing_finished, timeout=5000):
        image_panel.apply_btn.click()
    assert image_panel.engine.worker.wait(1000)
    assert len(engine.history.undo_stack) == 1
    assert engine.img_full_original.dtype == np.float32
    assert engine.img_full_original.max() > 1.
    assert engine.source_bake_type == "denoise"
    save.assert_called_once()
    assert host._trigger_autosave.call_count == 1
    assert image_panel.apply_btn.text()


@pytest.mark.parametrize("fail", [False, True])
def test_running_image_job_blocks_reset_until_success_or_error(image_panel, qtbot, monkeypatch, fail):
    """Reset cannot restore an image that an in-flight bake will overwrite."""
    from src.features.color.core.denoise_engine import DenoiseWorker

    entered, release = threading.Event(), threading.Event()
    original = DenoiseWorker._run_classic_split_denoise
    def gated(worker):
        entered.set()
        if not release.wait(5.):
            raise RuntimeError("gated worker was not released")
        if fail:
            raise RuntimeError("expected denoise failure")
        return original(worker)
    monkeypatch.setattr(DenoiseWorker, "_run_classic_split_denoise", gated)
    monkeypatch.setattr("src.app.controllers.color_controller.QMessageBox.critical", Mock())
    monkeypatch.setattr("src.app.controllers.color_controller.FileIO.save_baked_layer", Mock())
    monkeypatch.setattr("src.app.controllers.color_controller.FileIO.delete_baked_layer", Mock())
    monkeypatch.setattr("src.app.controllers.color_controller.FileIO._resize_to_fit", lambda image, size: image)
    pixels = np.ones((8, 8, 3), dtype=np.float32) * 1.8
    engine = SimpleNamespace(img_full_original=pixels, img_source_full=pixels,
                             current_file_path="/tmp/source.exr", active_layer_index=0,
                             layers=[Layer("Grade")], history=HistoryManager(),
                             source_bake_file_name=None, source_bake_type=None,
                             set_reference_image=Mock())
    def set_image(full, high, low, **kwargs):
        engine.img_full_original = full
    engine.set_image = set_image
    host = SimpleNamespace(engine=engine, denoise_panel=image_panel,
                           status_bar=Mock(), progress_bar=Mock(), layer_panel=Mock(),
                           current_file_path="/tmp/source.exr", request_render=Mock(),
                           _trigger_autosave=Mock())
    host._capture_bake_task_context = lambda: ColorControllerMixin._capture_bake_task_context(host)
    host._bake_task_context_matches = lambda context: ColorControllerMixin._bake_task_context_matches(host, context)
    image_panel.denoise_requested.connect(lambda mode, params: ColorControllerMixin.on_denoise_requested(host, mode, params))
    image_panel.engine.processing_finished.connect(lambda result: ColorControllerMixin.on_denoise_finished(host, result))
    image_panel.engine.error_occurred.connect(lambda error: ColorControllerMixin.on_denoise_error(host, error))
    image_panel.reset_requested.connect(lambda: ColorControllerMixin.on_denoise_reset_requested(host))
    resets = Mock()
    image_panel.reset_requested.connect(resets)
    try:
        image_panel.apply_btn.click()
        qtbot.waitUntil(entered.is_set, timeout=1000)
        assert image_panel.engine.is_processing()
        assert not image_panel.reset_btn.isEnabled()
        assert not image_panel.apply_btn.isEnabled()
        assert not image_panel.mode_combo.isEnabled()
        assert not image_panel.model_control["combo"].isEnabled()
        image_panel.reset_btn.click()
        image_panel.reset_defaults()
        resets.assert_not_called()
        release.set()
        qtbot.waitUntil(lambda: image_panel.reset_btn.isEnabled(), timeout=2000)
        assert image_panel.mode_combo.isEnabled()
        assert image_panel.model_control["combo"].isEnabled()
        assert image_panel.apply_btn.isEnabled()
        assert engine.source_bake_type == (None if fail else "denoise")
        assert host._trigger_autosave.call_count == (0 if fail else 1)
    finally:
        release.set()
        if image_panel.engine.worker is not None:
            assert image_panel.engine.worker.wait(2000)
