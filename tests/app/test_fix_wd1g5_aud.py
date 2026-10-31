"""Wave D (wd1-g5) regression tests — AUD-2500..2509 / AUD-2600..2603.

Covers the Batch D Major fixes in ``src/app/controllers/deliver_controller.py``
and ``src/app/controllers/dock_layout_controller.py``:

* AUD-2500: composite export fallback never runs OpenGL on the UI thread and
  releases its GL context on the CLI/test path.
* AUD-2501: video export re-entry guard (busy ``_video_exporter``).
* AUD-2502: ``export_file_as`` surfaces a codec ``ValueError`` to the user
  instead of letting it escape into Qt signal dispatch.
* AUD-2504: RAW camera-WB extraction reuses the cached raw handle and never
  degrades silently.
* AUD-2505: batch image decode runs off the UI thread.
* AUD-2506: batch completion restores ``None`` snapshots unconditionally.
* AUD-2507: float32 WebP export is quantised; save failures are
  distinguishable from render failures.
* AUD-2508: colour-management encode failures degrade loudly (error+stack),
  import/config failures warn.
* AUD-2509: batch export re-entry guard.
* AUD-2600: docking no longer clobbers floating_* geometry keys; restored
  floating geometry is clamped on-screen.
* AUD-2602: corrupt dock geometry values are rejected with a logged warning
  instead of a bare ``except: pass``.

AUD-2503 / AUD-2601 / AUD-2603 were verified INVALID (already fixed or
non-defect) — see docs/plans/2026-09-28-multi-machine-program/fixes/reports/
wd1-g5.md for the evidence.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock, Mock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app.controllers import deliver_controller as dc
from src.app.controllers.deliver_controller import (
    DeliverControllerMixin,
    _encode_composite_linear_to_srgb,
)
from src.shared.export.batch_queue_store import BatchQueueItem, STATUS_DONE

# Reuse the real-Qt dock host from the coverage suite for the dock tests.
from tests.app.test_dock_layout_controller_coverage import _DockHost


# ═══════════════════════════════════════════════════════════════════════
# Deliver harness (mock collaborators, no GL / engine / dialogs)
# ═══════════════════════════════════════════════════════════════════════


class _Harness(DeliverControllerMixin):
    """Minimal host exposing only what the mixin methods read."""

    def __init__(self) -> None:
        self.status_bar = MagicMock()
        self.progress_bar = MagicMock()
        self.engine = MagicMock()
        self.engine.layers = []
        self.engine.img_full_original = None
        self.engine.img_high = None
        self.crop_panel = MagicMock()
        self.crop_panel.is_crop_active.return_value = False
        self.color_manager = MagicMock()
        self.color_manager.convert_for_export.side_effect = (
            lambda img, source_space=None: img
        )
        self.export_panel = MagicMock()
        self.save_router = MagicMock()
        self.composite_page = None
        self._composite_available = False
        self.canvas = MagicMock()
        self.request_render = MagicMock()
        self.current_file_path = None
        self._config: dict = {}
        self.config_manager = MagicMock()
        self.config_manager.get.side_effect = (
            lambda key, default=None: self._config.get(key, default)
        )


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


@pytest.fixture
def patches(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch module-level collaborators (dialogs, FileIO, queue store)."""
    boxes = MagicMock()
    file_io = MagicMock()
    file_io.toos_export_keeps_linear.return_value = False
    file_io.save_image.return_value = True
    file_io._raw_lock = threading.Lock()
    file_io._current_raw_obj = None
    file_io._current_raw_path = None
    save_queue = MagicMock()
    clear_queue = MagicMock()
    single_shot = MagicMock()

    monkeypatch.setattr(dc, "QMessageBox", boxes)
    monkeypatch.setattr(dc, "FileIO", file_io)
    monkeypatch.setattr(dc, "save_batch_queue", save_queue)
    monkeypatch.setattr(dc, "clear_batch_queue", clear_queue)
    from PyQt6.QtCore import QTimer
    monkeypatch.setattr(QTimer, "singleShot", single_shot)
    return types.SimpleNamespace(
        boxes=boxes, file_io=file_io, save_queue=save_queue,
        clear_queue=clear_queue, single_shot=single_shot,
    )


def _float_img(fill: float = 0.5, shape: Tuple[int, int, int] = (2, 3, 3)) -> np.ndarray:
    return np.full(shape, fill, dtype=np.float32)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2500 — composite export fallback
# ═══════════════════════════════════════════════════════════════════════


class TestAud2500CompositeFallback:
    def _ui_harness(self) -> _Harness:
        h = _Harness()
        # A UI session always has a live canvas viewport; the composite render
        # thread is intentionally absent so the code must refuse the fallback.
        h.canvas = types.SimpleNamespace(
            gl_viewport=MagicMock(makeCurrent=MagicMock(
                side_effect=AssertionError("UI-thread GL context use")))
        )
        return h

    def test_ui_session_refuses_ui_thread_gl(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        h = self._ui_harness()
        with caplog.at_level("ERROR", logger="src.app.controllers.deliver_controller"):
            result = h._render_composite_full_res(_float_img(), keep_linear=True)

        assert result is None
        h.canvas.gl_viewport.makeCurrent.assert_not_called()
        assert any(
            "refusing to run OpenGL on the UI thread" in r.getMessage()
            for r in caplog.records
        )

    def test_cli_fallback_releases_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = _Harness()
        # No UI surface: canvas exists but has no gl_viewport (CLI/test path).
        h.canvas = types.SimpleNamespace(gl_viewport=None)

        ctx = MagicMock()
        fake_moderngl = types.ModuleType("moderngl")
        fake_moderngl.LINEAR = 1
        fake_moderngl.create_context = MagicMock(return_value=ctx)
        monkeypatch.setitem(sys.modules, "moderngl", fake_moderngl)

        # composite_page is None → the graph lookup raises inside the try,
        # exercising the cleanup path (texture + context release).
        result = h._render_composite_full_res(
            np.zeros((2, 3, 3), dtype=np.uint8), keep_linear=True)

        assert result is None
        fake_moderngl.create_context.assert_called_once()
        color_tex = ctx.texture.return_value
        color_tex.release.assert_called_once()
        ctx.release.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2501 / AUD-2502 — video export guards and error surfacing
# ═══════════════════════════════════════════════════════════════════════


def _video_ready(harness: _Harness) -> _Harness:
    """Timeline + project present so perform_video_export passes prechecks."""
    harness.edit_page = types.SimpleNamespace(_current_timeline=MagicMock(
        timeline_id="tl-1"))
    harness._current_edit_project = MagicMock()
    harness._set_video_export_controls = MagicMock()
    harness._cleanup_video_exporter = MagicMock()
    harness._open_export_folder = MagicMock()
    return harness


def _patch_video_env(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the exporter class + delivery-matrix gate for a clean launch."""
    import src.features.color.core.delivery_matrix as dm
    import src.features.color.ui.delivery_matrix_dialog as dialog_mod
    import src.shared.export.video_export_worker as worker_mod

    exporter_cls = MagicMock()
    monkeypatch.setattr(worker_mod, "VideoExporter", exporter_cls)
    monkeypatch.setattr(dm, "engine_video_denoise_enabled", lambda eng: False)
    monkeypatch.setattr(dm, "sequence_color_stacks", lambda seq, proj: {})
    monkeypatch.setattr(dm, "sequence_video_clip_ids", lambda seq: [])
    report = MagicMock()
    monkeypatch.setattr(dm, "report_from_color_stacks", lambda *a, **k: report)
    dialog = MagicMock()
    dialog.confirm = MagicMock(return_value=True)
    monkeypatch.setattr(dialog_mod, "DeliveryMatrixDialog", dialog)
    return exporter_cls


class TestAud2501VideoReentry:
    def test_busy_exporter_blocks_second_launch(
        self, harness: _Harness, patches: Any,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exporter_cls = _patch_video_env(monkeypatch)
        _video_ready(harness)
        busy = MagicMock()
        busy.is_exporting = True
        harness._video_exporter = busy
        harness._video_export_started_at = None

        harness.perform_video_export({"path": "/tmp/out.mp4", "codec": "h264"})

        patches.boxes.warning.assert_called_once()
        patches.boxes.critical.assert_not_called()
        exporter_cls.assert_not_called()  # no second worker created
        assert harness._video_exporter is busy  # reference not overwritten

    def test_idle_exporter_allows_new_launch(
        self, harness: _Harness, patches: Any,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exporter_cls = _patch_video_env(monkeypatch)
        _video_ready(harness)
        idle = MagicMock()
        idle.is_exporting = False
        harness._video_exporter = idle

        harness.perform_video_export({"path": "/tmp/out.mp4", "codec": "h264"})

        patches.boxes.warning.assert_not_called()
        exporter_cls.assert_called_once()


class TestAud2502CodecErrorSurfaced:
    def test_export_file_as_shows_dialog_for_unknown_codec(
        self, harness: _Harness, patches: Any,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import src.shared.export.quick_export_dialog as dialog_mod
        import src.shared.feature_flags as flags_mod

        _video_ready(harness)

        class _Dialog:
            EXPORT_IMAGE = 0
            EXPORT_VIDEO = 1

            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def exec(self) -> bool:
                return True

            def get_settings(self) -> dict:
                return {"export_type": "video", "codec": "bogus",
                        "path": "/tmp/out.mp4"}

        monkeypatch.setattr(dialog_mod, "QuickExportDialog", _Dialog)
        monkeypatch.setattr(flags_mod, "get_work_mode",
                            lambda cfg: "video")

        # Must not raise out of the Qt event handler.
        harness.export_file_as()

        patches.boxes.critical.assert_called_once()
        msg = str(patches.boxes.critical.call_args.args[-1])
        assert "bogus" in msg
        assert any("Video export rejected" in r.getMessage()
                   for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2504 — RAW camera-WB extraction
# ═══════════════════════════════════════════════════════════════════════


class TestAud2504CameraWb:
    def test_reuses_cached_raw_handle(self, harness: _Harness,
                                      patches: Any) -> None:
        sentinel = types.SimpleNamespace(camera_whitebalance=[4.0, 2.0, 3.0, 2.0])
        expected = [2.0, 1.0, 1.5, 1.0]
        patches.file_io._current_raw_obj = sentinel
        patches.file_io._current_raw_path = "/batch/shot.cr2"
        patches.file_io._get_normalized_camera_wb = MagicMock(
            return_value=expected)

        result = harness._batch_extract_camera_wb("/batch/shot.cr2")

        assert result == expected
        patches.file_io._get_normalized_camera_wb.assert_called_once_with(
            sentinel)

    def test_missing_rawpy_falls_back_with_warning(
        self, harness: _Harness, patches: Any,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # sys.modules entry of None makes `import rawpy` raise ImportError.
        monkeypatch.setitem(sys.modules, "rawpy", None)

        with caplog.at_level("WARNING"):
            result = harness._batch_extract_camera_wb("/batch/shot.cr2")

        assert result == [2.0, 1.0, 1.5, 1.0]
        assert any("rawpy unavailable" in r.getMessage() for r in caplog.records)

    def test_decode_failure_logs_and_uses_named_heuristic(
        self, harness: _Harness, patches: Any,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(path: str) -> None:
            raise RuntimeError("corrupt raw")

        fake_rawpy = types.ModuleType("rawpy")
        fake_rawpy.imread = _boom
        monkeypatch.setitem(sys.modules, "rawpy", fake_rawpy)

        with caplog.at_level("WARNING"):
            result = harness._batch_extract_camera_wb("/batch/shot.cr2")

        assert result == [2.0, 1.0, 1.5, 1.0]
        warnings = [r for r in caplog.records if "WB extraction failed" in
                    r.getMessage()]
        assert warnings and warnings[0].exc_info is not None


# ═══════════════════════════════════════════════════════════════════════
# AUD-2505 — off-thread batch image load
# ═══════════════════════════════════════════════════════════════════════


class TestAud2505OffThreadLoad:
    def test_load_runs_off_ui_thread(self, harness: _Harness,
                                     patches: Any) -> None:
        seen_threads: List[threading.Thread] = []
        img = _float_img()

        def _slow_load(path: str) -> Tuple[np.ndarray, None, None]:
            seen_threads.append(threading.current_thread())
            time.sleep(0.05)
            return img, None, None

        patches.file_io.load_image.side_effect = _slow_load

        full, high, low = harness._batch_load_image("/d/a.png")

        assert full is img and high is None and low is None
        assert seen_threads, "load must have executed"
        assert all(t is not threading.current_thread() for t in seen_threads), (
            "FileIO.load_image must not run on the caller (UI) thread"
        )

    def test_load_failure_returns_nones(
        self, harness: _Harness, patches: Any,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(path: str) -> None:
            raise OSError("disk full")

        patches.file_io.load_image.side_effect = _boom

        with caplog.at_level("ERROR"):
            result = harness._batch_load_image("/d/bad.png")

        assert result == (None, None, None)
        assert any("image load failed" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2506 — None snapshot restores as None
# ═══════════════════════════════════════════════════════════════════════


class TestAud2506NoneSnapshotRestore:
    def _prime(self, harness: _Harness) -> None:
        item = BatchQueueItem("/d/a.png", status=STATUS_DONE)
        harness._batch_files = [item.file_path]
        harness._batch_items = [item]
        harness._batch_success_count = 1
        harness._batch_failed = []
        harness._batch_cancelled = False
        harness._batch_paused = False
        harness._batch_file_processing = False
        harness._batch_original_layers = None
        harness._batch_original_active_index = None
        harness._batch_original_raw_params = None
        harness._batch_original_file_path = None
        harness._batch_original_is_srgb = False
        harness._batch_original_raw_metadata = None
        harness._batch_original_last_raw_params = None
        harness._batch_original_image = None
        # Residue from the last batch file that must NOT survive completion.
        harness.engine.current_file_path = "/d/last.dng"
        harness.engine.img_full_original = _float_img(fill=0.9)
        harness.engine.img_source_full = _float_img(fill=0.9)
        harness.engine.current_raw_metadata = {"camera_wb": [9.0, 9.0, 9.0, 9.0]}
        harness.engine._last_applied_raw_params = {"enabled": True}

    def test_none_snapshots_clear_engine_state(
        self, harness: _Harness, patches: Any
    ) -> None:
        self._prime(harness)

        harness._batch_export_complete()

        assert harness.engine.current_file_path is None
        assert harness.engine.layers == []
        assert harness.engine.img_full_original is None
        assert harness.engine.img_source_full is None
        assert harness.engine.current_raw_metadata == {
            "camera_wb": [2.0, 1.0, 1.5, 1.0]}
        assert harness.engine._last_applied_raw_params == {"enabled": False}
        harness.engine.set_image.assert_not_called()
        assert harness._batch_running is False  # AUD-2509 lifecycle reset


# ═══════════════════════════════════════════════════════════════════════
# AUD-2507 — WebP dtype conversion + save-vs-render failure distinction
# ═══════════════════════════════════════════════════════════════════════


class TestAud2507WebpAndSaveFailure:
    def _prime(self, harness: _Harness, tmp_path: Any, name: str = "in.png",
               fmt: str = "WebP") -> str:
        src = str(tmp_path / name)
        harness._batch_settings = {"options": {}, "format": fmt}
        harness._batch_files = [src]
        harness._batch_export_dir = str(tmp_path / "out")
        os.makedirs(harness._batch_export_dir, exist_ok=True)
        harness._batch_paused = False
        harness._batch_cancelled = False
        harness.engine.RAW_EXTENSIONS = set()
        harness.save_router.load_single_asset_data.return_value = {}
        with open(f"{os.path.splitext(src)[0]}.topos", "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        return src

    def test_float32_webp_quantised_to_uint8(
        self, harness: _Harness, patches: Any, tmp_path: Any
    ) -> None:
        src = self._prime(harness, tmp_path, name="photo.png")
        patches.file_io.load_image.return_value = (_float_img(fill=1.0), None, None)
        harness.engine.get_current_image.return_value = _float_img(fill=1.0)

        out = harness._batch_export_single_file(src, 1)

        assert out is not None and out.endswith(".webp")
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        assert saved.max() == 255

    def test_uint16_webp_downconverted(
        self, harness: _Harness, patches: Any, tmp_path: Any
    ) -> None:
        src = self._prime(harness, tmp_path, name="scan.png")
        patches.file_io.load_image.return_value = (
            np.full((2, 2, 3), 257 * 100, dtype=np.uint16), None, None)
        harness.engine.get_current_image.return_value = np.full(
            (2, 2, 3), 257 * 100, dtype=np.uint16)

        out = harness._batch_export_single_file(src, 1)

        assert out is not None
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8

    def test_save_failure_raises_specific_error(
        self, harness: _Harness, patches: Any, tmp_path: Any
    ) -> None:
        src = self._prime(harness, tmp_path, fmt="JPEG")
        patches.file_io.load_image.return_value = (_float_img(), None, None)
        harness.engine.get_current_image.return_value = _float_img()
        patches.file_io.save_image.return_value = False

        with pytest.raises(OSError, match="failed to save output image"):
            harness._batch_export_single_file(src, 1)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2508 — loud colour-management degradation
# ═══════════════════════════════════════════════════════════════════════


class TestAud2508LoudDegradation:
    def test_transform_failure_logs_error_with_stack_and_still_encodes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import src.shared.color.color_transform_provider as provider

        def _boom(*args: Any, **kwargs: Any) -> np.ndarray:
            raise RuntimeError("lut load failed")

        monkeypatch.setattr(provider, "transform_color", _boom)
        img = np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32)

        with caplog.at_level("ERROR", logger="src.app.controllers.deliver_controller"):
            out = _encode_composite_linear_to_srgb(img)

        errors = [r for r in caplog.records
                  if "color-management transform failed" in r.getMessage()]
        assert errors and errors[0].exc_info is not None
        # Still degrades to the builtin sRGB OETF (export must not crash).
        expected_mid = 1.055 * float(np.power(0.5, 1.0 / 2.4)) - 0.055
        np.testing.assert_allclose(out[0, 0], [0.0, expected_mid, 1.0],
                                   rtol=1e-5)

    def test_descriptor_failure_warns_and_skips_transform(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import src.shared.color.color_space_descriptor as descriptor_mod
        import src.shared.color.color_transform_provider as provider

        def _bad_descriptor(*args: Any, **kwargs: Any) -> None:
            raise ValueError("bad preference keys")

        transform_calls: List[Any] = []
        monkeypatch.setattr(descriptor_mod, "descriptor_from_config",
                            _bad_descriptor)
        monkeypatch.setattr(
            provider, "transform_color",
            lambda *a, **k: transform_calls.append(a) or _float_img())

        with caplog.at_level("WARNING", logger="src.app.controllers.deliver_controller"):
            out = _encode_composite_linear_to_srgb(
                np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32))

        assert not transform_calls  # transform never reached
        assert any("failed to resolve output color space" in r.getMessage()
                   for r in caplog.records)
        # sRGB fallback applied
        expected_mid = 1.055 * float(np.power(0.5, 1.0 / 2.4)) - 0.055
        np.testing.assert_allclose(out[0, 0, 1], expected_mid, rtol=1e-5)


# ═══════════════════════════════════════════════════════════════════════
# AUD-2509 — batch export re-entry guard
# ═══════════════════════════════════════════════════════════════════════


class TestAud2509BatchReentry:
    def test_running_batch_blocks_second_start(
        self, harness: _Harness, patches: Any, tmp_path: Any
    ) -> None:
        harness._batch_running = True
        harness._batch_files = None
        harness._batch_index = 3  # mid-flight progress must not be reset

        harness.perform_batch_export({
            "files": ["/d/a.png"], "export_dir": str(tmp_path)})

        patches.boxes.warning.assert_called_once()
        assert harness._batch_files is None  # no snapshot / state overwrite
        assert harness._batch_index == 3
        patches.save_queue.assert_not_called()

    def test_guarded_flag_set_on_start_and_cleared_on_complete(
        self, harness: _Harness, patches: Any, tmp_path: Any
    ) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        assert getattr(harness, "_batch_running", False) is False

        harness.perform_batch_export({
            "files": ["/d/a.png"], "export_dir": str(out_dir),
            "options": {}})

        assert harness._batch_running is True

        harness._batch_items = [BatchQueueItem("/d/a.png", status=STATUS_DONE)]
        harness._batch_failed = []
        harness._batch_cancelled = False
        harness._batch_original_layers = None
        harness._batch_original_active_index = None
        harness._batch_original_raw_params = None
        harness._batch_original_file_path = None
        harness._batch_original_is_srgb = False
        harness._batch_original_raw_metadata = None
        harness._batch_original_last_raw_params = None
        harness._batch_original_image = None

        harness._batch_export_complete()

        assert harness._batch_running is False


# ═══════════════════════════════════════════════════════════════════════
# AUD-2600 / AUD-2602 — dock floating geometry handling
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def dock_host(qapp) -> _DockHost:
    host = _DockHost()
    yield host
    host.close()


class TestAud2600FloatingGeometry:
    def test_docking_does_not_write_floating_keys(self, dock_host: _DockHost) -> None:
        dock = dock_host.dock_grading
        dock.setFloating(True)
        dock.resize(500, 400)
        dock.move(120, 90)

        dock_host._on_dock_floating_changed(False, dock)

        assert dock_host.config_manager.get(
            "dock_dockGrading_floating_size") is None
        assert dock_host.config_manager.get(
            "dock_dockGrading_floating_pos") is None

    def test_restored_offscreen_position_clamped(
        self, dock_host: _DockHost, qapp
    ) -> None:
        from PyQt6.QtGui import QGuiApplication

        dock = dock_host.dock_lut
        dock.setFloating(True)
        dock_host.config_manager.set("dock_dockLut_floating_size", "480x360")
        dock_host.config_manager.set("dock_dockLut_floating_pos", "99999,99999")

        dock_host._on_dock_floating_changed(True, dock)

        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        assert avail.left() <= dock.x() <= avail.right() - dock.width() + 1
        assert avail.top() <= dock.y() <= avail.bottom()
        assert (dock.width(), dock.height()) == (480, 360)


class TestAud2602GeometryParseLogging:
    def test_corrupt_floating_size_logged_not_swallowed(
        self, dock_host: _DockHost, caplog: pytest.LogCaptureFixture
    ) -> None:
        dock = dock_host.dock_grading
        dock.setFloating(True)
        dock.resize(320, 240)
        dock_host.config_manager.set("dock_dockGrading_floating_size",
                                     "not-a-size")

        with caplog.at_level("WARNING"):
            dock_host._on_dock_floating_changed(True, dock)  # must not raise

        assert any("not-a-size" in r.getMessage() for r in caplog.records)
        assert (dock.width(), dock.height()) == (320, 240)  # unchanged

    def test_non_positive_floating_size_rejected(
        self, dock_host: _DockHost, caplog: pytest.LogCaptureFixture
    ) -> None:
        dock = dock_host.dock_grading
        dock.setFloating(True)
        dock.resize(320, 240)
        dock_host.config_manager.set("dock_dockGrading_floating_size", "0x400")

        with caplog.at_level("WARNING"):
            dock_host._on_dock_floating_changed(True, dock)

        assert (dock.width(), dock.height()) == (320, 240)
        assert any("non-positive" in r.getMessage() for r in caplog.records)

    def test_corrupt_docked_size_skipped_in_restore(
        self, dock_host: _DockHost, caplog: pytest.LogCaptureFixture
    ) -> None:
        dock = dock_host.dock_grading
        dock_host.config_manager.set("dock_dockGrading_docked_size",
                                     "not-a-size")
        # A zero dimension parses but must be rejected without a resizeDocks
        # call carrying a 0 size.
        dock_host.config_manager.set("dock_dockLut_docked_size", "0x300")

        with caplog.at_level("WARNING"):
            dock_host._restore_docked_sizes()  # must not raise

        assert any("not-a-size" in r.getMessage() for r in caplog.records)
