"""Coverage tests for ``src/app/controllers/deliver_controller.py``.

The controller is a mixin on MainWindow; following the existing convention in
``tests/app/test_deliver_batch_cancellation.py`` we instantiate it on a minimal
harness object with ``MagicMock`` collaborators and exercise the headless
units directly:

* export parameter normalisation / dtype conversion (panel + quick export)
* export settings state management (options defaults, config flags)
* video export state machine (pause / resume / cancel / elapsed / cleanup)
* batch export state machine (validation, start, per-file success / failure /
  cancel / exception, pause/resume guards, completion + queue persistence)
* path & filename generation helpers
* signal / UI plumbing (full-res progress, item status, timeline info)

Suspected src bugs found while writing these tests are recorded in
``docs/testcoverage/2026-09-28/BUGS-agent-A3.md`` (BUG-A3-xx); nothing in
src is modified here.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QPushButton

from src.app.controllers import deliver_controller as dc
from src.app.controllers.deliver_controller import (
    DeliverControllerMixin,
    _config_bool,
    _encode_composite_linear_to_srgb,
)
from src.shared.export.batch_queue_store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RENDERING,
    BatchQueueItem,
)


# ═══════════════════════════════════════════════════════════════════════
# Harness
# ═══════════════════════════════════════════════════════════════════════


class Harness(DeliverControllerMixin):
    """Minimal host exposing only what the mixin methods read.

    Collaborators are mocks so no GL / engine / dialog is required.
    """

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
        self._config: Dict[str, Any] = {}
        self.config_manager = MagicMock()
        self.config_manager.get.side_effect = (
            lambda key, default=None: self._config.get(key, default)
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def patches(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch every module-level collaborator the controller imports.

    This keeps batch-queue persistence away from the real user config dir and
    removes modal dialogs / FileIO disk access.
    """
    boxes = MagicMock()
    file_io = MagicMock()
    file_io.toos_export_keeps_linear.return_value = False
    file_io.save_image.return_value = True
    save_queue = MagicMock()
    load_queue = MagicMock(return_value=([], {}))
    clear_queue = MagicMock()
    restorable = MagicMock(return_value=False)
    single_shot = MagicMock()
    file_dialog = MagicMock()
    file_dialog.getSaveFileName.return_value = ("", "")

    monkeypatch.setattr(dc, "QMessageBox", boxes)
    monkeypatch.setattr(dc, "FileIO", file_io)
    monkeypatch.setattr(dc, "QFileDialog", file_dialog)
    monkeypatch.setattr(dc, "save_batch_queue", save_queue)
    monkeypatch.setattr(dc, "load_batch_queue", load_queue)
    monkeypatch.setattr(dc, "clear_batch_queue", clear_queue)
    monkeypatch.setattr(dc, "has_restorable_items", restorable)
    monkeypatch.setattr(QTimer, "singleShot", single_shot)
    return SimpleNamespace(
        boxes=boxes,
        file_io=file_io,
        save_queue=save_queue,
        load_queue=load_queue,
        clear_queue=clear_queue,
        restorable=restorable,
        single_shot=single_shot,
        file_dialog=file_dialog,
    )


def _float_img(fill: float = 0.5, shape: Tuple[int, int, int] = (2, 3, 3)) -> np.ndarray:
    return np.full(shape, fill, dtype=np.float32)


# ═══════════════════════════════════════════════════════════════════════
# _config_bool
# ═══════════════════════════════════════════════════════════════════════


class TestConfigBool:
    def test_none_returns_default(self) -> None:
        assert _config_bool(None, default=True) is True
        assert _config_bool(None, default=False) is False

    def test_explicit_false_beats_default(self) -> None:
        assert _config_bool(False, default=True) is False

    def test_explicit_true_with_false_default(self) -> None:
        assert _config_bool(True, default=False) is True

    def test_default_is_false_when_omitted(self) -> None:
        assert _config_bool(None) is False


# ═══════════════════════════════════════════════════════════════════════
# _encode_composite_linear_to_srgb
# ═══════════════════════════════════════════════════════════════════════


def _force_srgb_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the preference-driven transform raise so the sRGB fallback runs."""
    import src.shared.color.color_transform_provider as provider

    def _boom(*args: Any, **kwargs: Any) -> np.ndarray:
        raise RuntimeError("transform unavailable")

    monkeypatch.setattr(provider, "transform_color", _boom)


class TestEncodeCompositeLinearToSrgb:
    def test_fallback_encodes_srgb_curve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_srgb_fallback(monkeypatch)
        img = np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img)
        expected_mid = 1.055 * float(np.power(0.5, 1.0 / 2.4)) - 0.055
        np.testing.assert_allclose(out[0, 0], [0.0, expected_mid, 1.0], rtol=1e-5)

    def test_fallback_linear_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_srgb_fallback(monkeypatch)
        img = np.array([[[0.002, 0.0031308, 0.0]]], dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img)
        np.testing.assert_allclose(out[0, 0, 0], 12.92 * 0.002, rtol=1e-6)
        np.testing.assert_allclose(out[0, 0, 1], 12.92 * 0.0031308, rtol=1e-4)

    def test_fallback_negative_pixels_clamped_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BUG-A3-03 已修复：负值先钳 0 再 OETF，不再被线性段放大。

        -0.5 must encode to 0.0 (not the historical -6.46 amplification);
        positive highlights stay unclamped (2.0 → 1.353256) and alpha
        passes through untouched. See docs/testcoverage/2026-09-28/
        BUGS-agent-A3.md.
        """
        _force_srgb_fallback(monkeypatch)
        img = np.array([[[-0.5, 1.0, 2.0, 0.7]]], dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img)
        assert out.shape == (1, 1, 4)
        np.testing.assert_allclose(out[0, 0, :3], [0.0, 1.0, 1.353256], rtol=1e-5)
        assert out[0, 0, 3] == pytest.approx(0.7)  # alpha passes through
        assert out is not img  # must not mutate the caller's buffer

    def test_preference_transform_is_used_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.shared.color.color_space_descriptor as descriptor_mod
        import src.shared.color.color_transform_provider as provider

        marker = np.ones((1, 1, 3), dtype=np.float32)
        fake_dst = SimpleNamespace(name="fake-dst")
        calls: List[Any] = []

        def _fake_transform(img: np.ndarray, src: Any, dst: Any, cfg: Any) -> np.ndarray:
            calls.append((src, dst))
            return marker

        monkeypatch.setattr(
            descriptor_mod, "descriptor_from_config",
            lambda *a, **k: fake_dst,
        )
        monkeypatch.setattr(provider, "transform_color", _fake_transform)

        img = np.zeros((1, 1, 3), dtype=np.float32)
        out = _encode_composite_linear_to_srgb(img, config_manager=None)

        assert out is marker
        assert calls and calls[0][1] is fake_dst


# ═══════════════════════════════════════════════════════════════════════
# perform_panel_export
# ═══════════════════════════════════════════════════════════════════════


class TestPerformPanelExport:
    def _run_success(self, harness: Harness, patches: Any,
                     img: np.ndarray, path: str) -> Dict[str, Any]:
        harness.engine.get_current_image.return_value = img
        harness._open_export_folder = MagicMock()
        settings: Dict[str, Any] = {"path": path}
        harness.perform_panel_export(settings)
        return settings

    def test_options_created_and_preserve_metadata_normalized(
        self, harness: Harness, patches: Any
    ) -> None:
        harness._config["preserve_metadata"] = False
        settings = self._run_success(harness, patches, _float_img(), "/tmp/out.png")

        assert settings["options"] == {"preserve_metadata": False}
        saved_options = patches.file_io.save_image.call_args.kwargs["options"]
        assert saved_options is settings["options"]

    def test_render_failure_shows_error_and_skips_save(
        self, harness: Harness, patches: Any
    ) -> None:
        harness.engine.get_current_image.return_value = None
        harness.perform_panel_export({"path": "/tmp/out.png"})

        patches.file_io.save_image.assert_not_called()
        patches.boxes.critical.assert_called_once()
        msg = harness.status_bar.showMessage.call_args.args[0]
        assert "Export failed" in msg and "render" in msg.lower()

    def test_float32_jpg_converted_to_uint8(
        self, harness: Harness, patches: Any
    ) -> None:
        img = _float_img(fill=1.0)
        self._run_success(harness, patches, img, "/tmp/out.jpg")

        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        assert saved.max() == 255
        patches.file_io.save_image.assert_called_once()
        assert patches.file_io.save_image.call_args.args[0] == "/tmp/out.jpg"

    def test_float32_tiff_converted_to_uint16(
        self, harness: Harness, patches: Any
    ) -> None:
        self._run_success(harness, patches, _float_img(fill=1.0), "/tmp/out.tif")
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint16
        assert saved.max() == 65535

    def test_float32_exr_stays_float32(
        self, harness: Harness, patches: Any
    ) -> None:
        self._run_success(harness, patches, _float_img(fill=0.25), "/tmp/out.exr")
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.float32

    def test_uint16_png_downsampled_to_uint8(
        self, harness: Harness, patches: Any
    ) -> None:
        img = np.full((2, 3, 3), 65535, dtype=np.uint16)
        self._run_success(harness, patches, img, "/tmp/out.png")
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        assert saved.max() == 255  # 65535 // 256

    def test_save_failure_reports_error(
        self, harness: Harness, patches: Any
    ) -> None:
        patches.file_io.save_image.return_value = False
        harness.engine.get_current_image.return_value = _float_img()
        harness.perform_panel_export({"path": "/tmp/out.png"})

        assert patches.boxes.critical.call_count == 1
        msg = harness.status_bar.showMessage.call_args.args[0]
        assert "Export failed" in msg

    def test_success_updates_config_and_opens_folder(
        self, harness: Harness, patches: Any
    ) -> None:
        harness._config["embed_icc_profile"] = True
        harness._open_export_folder = MagicMock()
        harness.engine.get_current_image.return_value = _float_img()
        harness.perform_panel_export({"path": "/exports/dir/out.png"})

        harness.config_manager.set.assert_called_once_with("last_path", "/exports/dir")
        harness.config_manager.save_config.assert_called_once()
        harness.color_manager.embed_icc.assert_called_once_with("/exports/dir/out.png")
        harness._open_export_folder.assert_called_once_with("/exports/dir/out.png")
        msg = harness.status_bar.showMessage.call_args.args[0]
        assert "Export successful" in msg

    def test_embed_icc_skipped_when_disabled(self, harness: Harness, patches: Any) -> None:
        harness._config["embed_icc_profile"] = False
        harness._open_export_folder = MagicMock()
        harness.engine.get_current_image.return_value = _float_img()
        harness.perform_panel_export({"path": "/tmp/out.png"})
        harness.color_manager.embed_icc.assert_not_called()

    def test_crop_auto_applied_before_export(self, harness: Harness, patches: Any) -> None:
        harness.crop_panel.is_crop_active.return_value = True
        harness.engine.get_current_image.return_value = _float_img()
        harness.perform_panel_export({"path": "/tmp/out.png"})
        harness.crop_panel.set_crop_active.assert_called_once_with(False)

    def test_composite_render_used_when_flag_set(
        self, harness: Harness, patches: Any
    ) -> None:
        harness._composite_available = True
        harness.composite_page = SimpleNamespace(node_graph=object())
        composite_img = np.full((2, 3, 3), 0.9, dtype=np.float32)
        harness._render_composite_full_res = MagicMock(return_value=composite_img)
        harness.engine.get_current_image.return_value = _float_img(fill=0.1)

        harness.perform_panel_export({"path": "/tmp/out.jpg", "apply_composite": True})

        render = harness._render_composite_full_res  # type: MagicMock
        render.assert_called_once()
        assert render.call_args.kwargs["keep_linear"] is False
        saved = patches.file_io.save_image.call_args.args[1]
        # 0.9 * 255 = 229.5 → 229: proves the composite buffer was exported,
        # not the 0.1 Color buffer (which would give 25).
        assert saved[0, 0, 0] == 229

    def test_composite_none_falls_back_to_color(
        self, harness: Harness, patches: Any
    ) -> None:
        harness._composite_available = True
        harness.composite_page = SimpleNamespace(node_graph=object())
        harness._render_composite_full_res = MagicMock(return_value=None)
        harness.engine.get_current_image.return_value = _float_img(fill=1.0)

        harness.perform_panel_export({"path": "/tmp/out.jpg", "apply_composite": True})

        saved = patches.file_io.save_image.call_args.args[1]
        assert saved[0, 0, 0] == 255  # Color buffer exported

    def test_composite_exception_falls_back_to_color(
        self, harness: Harness, patches: Any
    ) -> None:
        harness._composite_available = True
        harness.composite_page = SimpleNamespace(node_graph=object())
        harness._render_composite_full_res = MagicMock(
            side_effect=RuntimeError("composite boom")
        )
        harness.engine.get_current_image.return_value = _float_img(fill=1.0)

        harness.perform_panel_export({"path": "/tmp/out.png", "apply_composite": True})

        patches.file_io.save_image.assert_called_once()
        patches.boxes.critical.assert_not_called()  # graceful fallback, not an error box


# ═══════════════════════════════════════════════════════════════════════
# perform_quick_export + _export_color + _export_composite
# ═══════════════════════════════════════════════════════════════════════


class TestPerformQuickExport:
    def test_default_source_uses_color_exporter(self, harness: Harness, patches: Any) -> None:
        harness._export_color = MagicMock(return_value=_float_img())
        harness._export_composite = MagicMock(return_value=_float_img())
        harness.perform_quick_export({"path": "/tmp/out.png"})

        harness._export_color.assert_called_once()
        harness._export_composite.assert_not_called()

    def test_composite_source_dispatch(self, harness: Harness, patches: Any) -> None:
        harness._composite_available = True
        harness.composite_page = SimpleNamespace(node_graph=object())
        harness._export_color = MagicMock(return_value=_float_img())
        harness._export_composite = MagicMock(return_value=_float_img(fill=0.9))

        harness.perform_quick_export({"path": "/tmp/out.png", "export_source": "composite"})

        harness._export_composite.assert_called_once()
        harness._export_color.assert_not_called()

    def test_render_failure_reports_and_returns(
        self, harness: Harness, patches: Any
    ) -> None:
        harness._export_color = MagicMock(return_value=None)
        harness.perform_quick_export({"path": "/tmp/out.png"})

        patches.file_io.save_image.assert_not_called()
        patches.boxes.critical.assert_called_once()
        assert "Export failed" in harness.status_bar.showMessage.call_args.args[0]

    def test_uint16_downconverted_to_uint8(
        self, harness: Harness, patches: Any
    ) -> None:
        """BUG-A3-02 已修复：quick export 对 8 位格式做 uint16→uint8 降位。

        ``perform_panel_export`` and ``_batch_export_single_file`` both
        downsample uint16 sources via ``/ 256`` for 8-bit formats; quick
        export now does the same (see docs/testcoverage/2026-09-28/
        BUGS-agent-A3.md), so all three export paths agree.
        """
        harness._export_color = MagicMock(
            return_value=np.full((2, 3, 3), 512, dtype=np.uint16)
        )
        harness._open_export_folder = MagicMock()
        harness.perform_quick_export({"path": "/tmp/out.png"})

        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        np.testing.assert_array_equal(saved, np.full((2, 3, 3), 2, dtype=np.uint8))
        harness.config_manager.set.assert_called_once_with("last_path", "/tmp")


class TestExportColor:
    def test_quick_uses_viewport_grab(self, harness: Harness, patches: Any) -> None:
        grabbed = _float_img(fill=0.3)
        harness.engine.grab_viewport_image.return_value = grabbed
        out = harness._export_color({"path": "/tmp/out.png", "quick_export": True})

        assert out is grabbed
        harness.engine.grab_viewport_image.assert_called_once_with(None)
        harness.engine.get_current_image.assert_not_called()
        assert "Quick exporting" in harness.status_bar.showMessage.call_args.args[0]

    def test_quick_falls_back_to_full_res_when_grab_none(
        self, harness: Harness, patches: Any
    ) -> None:
        full = _float_img(fill=0.4)
        harness.engine.grab_viewport_image.return_value = None
        harness.engine.get_current_image.return_value = full

        out = harness._export_color({"path": "/tmp/out.png", "quick_export": True})

        assert out is full
        harness.engine.get_current_image.assert_called_once_with(force_full_res=True)

    def test_high_quality_full_res_with_lanczos_resize(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cv2

        full = _float_img(fill=0.5)
        harness.engine.get_current_image.return_value = full
        resize = MagicMock(return_value="resized")
        monkeypatch.setattr(cv2, "resize", resize)

        out = harness._export_color({
            "path": "/tmp/out.png",
            "quick_export": False,
            "options": {"resize": (64, 48)},
        })

        assert out == "resized"
        resize.assert_called_once()
        assert resize.call_args.args[:2] == (full, (64, 48))
        assert resize.call_args.kwargs["interpolation"] == cv2.INTER_LANCZOS4

    def test_high_quality_without_resize(self, harness: Harness, patches: Any) -> None:
        full = _float_img(fill=0.5)
        harness.engine.get_current_image.return_value = full
        out = harness._export_color({
            "path": "/tmp/out.png", "quick_export": False, "options": {},
        })
        assert out is full
        assert "high quality" in harness.status_bar.showMessage.call_args.args[0]


class TestExportComposite:
    def _quick_page(self, readback: Optional[np.ndarray]) -> SimpleNamespace:
        renderer = MagicMock()
        renderer.readback_output.return_value = readback
        return SimpleNamespace(renderer=renderer, node_graph=MagicMock())

    def test_quick_readback_encoded_for_display_formats(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        encoded = _float_img(fill=0.8)
        enc = MagicMock(return_value=encoded)
        monkeypatch.setattr(dc, "_encode_composite_linear_to_srgb", enc)
        harness.composite_page = self._quick_page(_float_img(fill=0.5))

        out = harness._export_composite({"path": "/tmp/out.png", "quick_export": True})

        assert out is encoded
        enc.assert_called_once()

    def test_quick_readback_kept_linear_for_linear_formats(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patches.file_io.toos_export_keeps_linear.return_value = True
        enc = MagicMock()
        monkeypatch.setattr(dc, "_encode_composite_linear_to_srgb", enc)
        linear = _float_img(fill=0.5)
        harness.composite_page = self._quick_page(linear)

        out = harness._export_composite({"path": "/tmp/x.toos", "quick_export": True})

        assert out is linear
        enc.assert_not_called()
        patches.file_io.toos_export_keeps_linear.assert_called_once()

    def test_quick_readback_none_falls_back_to_color(
        self, harness: Harness, patches: Any
    ) -> None:
        harness.composite_page = self._quick_page(None)
        harness._export_color = MagicMock(return_value="color-img")

        out = harness._export_composite({"path": "/tmp/out.png", "quick_export": True})

        assert out == "color-img"
        assert "falling back" in harness.status_bar.showMessage.call_args.args[0]

    def test_full_res_color_render_none_falls_back(
        self, harness: Harness, patches: Any
    ) -> None:
        harness.composite_page = SimpleNamespace(renderer=None, node_graph=MagicMock())
        harness.engine.get_current_image.return_value = None
        harness._export_color = MagicMock(return_value="color-img")

        out = harness._export_composite({"path": "/tmp/out.png", "quick_export": False})

        assert out == "color-img"

    def test_full_res_composite_none_falls_back(
        self, harness: Harness, patches: Any
    ) -> None:
        harness.composite_page = SimpleNamespace(renderer=None, node_graph=MagicMock())
        harness.engine.get_current_image.return_value = _float_img()
        harness._render_composite_full_res = MagicMock(return_value=None)
        harness._export_color = MagicMock(return_value="color-img")

        out = harness._export_composite({"path": "/tmp/out.png", "quick_export": False})

        assert out == "color-img"

    def test_full_res_composite_with_resize(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cv2

        harness.composite_page = SimpleNamespace(renderer=None, node_graph=MagicMock())
        harness.engine.get_current_image.return_value = _float_img()
        harness._render_composite_full_res = MagicMock(return_value=_float_img(fill=0.7))
        resize = MagicMock(return_value="resized")
        monkeypatch.setattr(cv2, "resize", resize)

        out = harness._export_composite({
            "path": "/tmp/out.png",
            "quick_export": False,
            "options": {"resize": (32, 16)},
        })

        assert out == "resized"
        assert resize.call_args.args[1] == (32, 16)


# ═══════════════════════════════════════════════════════════════════════
# _open_export_folder
# ═══════════════════════════════════════════════════════════════════════


class TestOpenExportFolder:
    def test_disabled_config_is_noop(self, harness: Harness,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        popen = MagicMock()
        monkeypatch.setattr(dc.subprocess, "Popen", popen)
        harness._config["open_folder_after_export"] = False
        harness._open_export_folder("/exports/a.png")
        popen.assert_not_called()

    def test_darwin_opens_folder(self, harness: Harness,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
        popen = MagicMock()
        monkeypatch.setattr(dc.subprocess, "Popen", popen)
        monkeypatch.setattr(dc.platform, "system", lambda: "Darwin")
        harness._config["open_folder_after_export"] = True
        harness._open_export_folder("/exports/sub/a.png")
        popen.assert_called_once_with(["open", "/exports/sub"])

    def test_popen_failure_swallowed(self, harness: Harness,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            dc.subprocess, "Popen",
            MagicMock(side_effect=OSError("no shell")),
        )
        monkeypatch.setattr(dc.platform, "system", lambda: "Darwin")
        harness._config["open_folder_after_export"] = True
        harness._open_export_folder("/exports/a.png")  # must not raise


# ═══════════════════════════════════════════════════════════════════════
# Video export state machine
# ═══════════════════════════════════════════════════════════════════════


class TestVideoExportControls:
    def test_controls_visibility_and_pause_label(self, harness: Harness,
                                                 qapp: Any,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
        i18n = MagicMock()
        i18n.tr.side_effect = lambda key, default=None, **kw: (
            "PAUSE_LABEL" if key == "export.pause" else "RESUME_LABEL"
        )
        monkeypatch.setattr(dc, "I18N", i18n)
        pause_btn = QPushButton()
        cancel_btn = QPushButton()
        pause_btn.setEnabled(False)
        cancel_btn.setEnabled(False)
        harness._video_export_pause_btn = pause_btn
        harness._video_export_cancel_btn = cancel_btn

        harness._set_video_export_controls(True, False)

        assert pause_btn.isEnabled() is True
        assert cancel_btn.isEnabled() is True
        assert pause_btn.text() == "PAUSE_LABEL"
        assert pause_btn.isVisibleTo(pause_btn) is True  # setVisible(True) called

        harness._set_video_export_controls(True, True)
        assert pause_btn.text() == "RESUME_LABEL"

        harness._set_video_export_controls(False, False)
        assert pause_btn.isEnabled() is False
        assert pause_btn.isVisibleTo(pause_btn) is False

    def test_missing_buttons_is_noop(self, harness: Harness) -> None:
        harness._set_video_export_controls(True, False)  # must not raise


class TestToggleVideoExportPause:
    def _exporter(self) -> MagicMock:
        exporter = MagicMock()
        exporter.is_exporting = True
        return exporter

    def test_no_exporter_is_noop(self, harness: Harness) -> None:
        harness._set_video_export_controls = MagicMock()
        harness._toggle_video_export_pause()
        harness._set_video_export_controls.assert_not_called()

    def test_not_exporting_is_noop(self, harness: Harness) -> None:
        exporter = self._exporter()
        exporter.is_exporting = False
        harness._video_exporter = exporter
        harness._toggle_video_export_pause()
        exporter.pause.assert_not_called()

    def test_pause_flips_state(self, harness: Harness) -> None:
        exporter = self._exporter()
        harness._video_exporter = exporter
        harness._video_export_paused = False
        harness._set_video_export_controls = MagicMock()

        harness._toggle_video_export_pause()

        exporter.pause.assert_called_once_with()
        assert harness._video_export_paused is True
        assert harness._video_export_pause_started_at is not None
        harness._set_video_export_controls.assert_called_once_with(True, True)

    def test_resume_accrues_paused_total_and_clears_state(
        self, harness: Harness
    ) -> None:
        exporter = self._exporter()
        harness._video_exporter = exporter
        harness._video_export_paused = True
        harness._video_export_pause_started_at = time.perf_counter() - 2.0
        harness._video_export_paused_total = 1.0
        harness._set_video_export_controls = MagicMock()

        harness._toggle_video_export_pause()

        exporter.resume.assert_called_once_with()
        assert harness._video_export_paused is False
        assert harness._video_export_pause_started_at is None
        assert harness._video_export_paused_total == pytest.approx(3.0, abs=0.5)
        harness._set_video_export_controls.assert_called_once_with(True, False)


class TestCancelVideoExport:
    def test_no_exporter_is_noop(self, harness: Harness) -> None:
        harness._cancel_video_export()  # must not raise
        harness.status_bar.showMessage.assert_not_called()

    def test_cancel_calls_exporter_and_disables_button(
        self, harness: Harness
    ) -> None:
        exporter = MagicMock()
        exporter.is_exporting = True
        harness._video_exporter = exporter
        cancel_btn = MagicMock()
        harness._video_export_cancel_btn = cancel_btn

        harness._cancel_video_export()

        exporter.cancel.assert_called_once_with()
        cancel_btn.setEnabled.assert_called_once_with(False)
        assert harness.status_bar.showMessage.call_count == 1


class TestVideoExportElapsed:
    def test_not_started_returns_zero(self, harness: Harness) -> None:
        assert harness._video_export_elapsed() == 0.0

    def test_elapsed_subtracts_paused_total(self, harness: Harness) -> None:
        harness._video_export_started_at = time.perf_counter() - 10.0
        harness._video_export_paused_total = 4.0
        assert harness._video_export_elapsed() == pytest.approx(6.0, abs=0.5)

    def test_active_pause_subtracted_live(self, harness: Harness) -> None:
        harness._video_export_started_at = time.perf_counter() - 10.0
        harness._video_export_paused_total = 1.0
        harness._video_export_pause_started_at = time.perf_counter() - 3.0
        assert harness._video_export_elapsed() == pytest.approx(6.0, abs=0.5)


class TestCleanupVideoExporter:
    def test_cleanup_resets_all_state(self, harness: Harness) -> None:
        exporter = MagicMock()
        harness._video_exporter = exporter
        harness._video_export_started_at = 1.0
        harness._video_export_pause_started_at = None
        harness._video_export_paused = False
        harness._set_video_export_controls = MagicMock()

        harness._cleanup_video_exporter()

        exporter.cleanup.assert_called_once()
        assert harness._video_exporter is None
        assert harness._video_export_started_at is None
        assert harness._video_export_paused is False
        harness._set_video_export_controls.assert_called_once_with(False, False)

    def test_paused_cleanup_accrues_total(self, harness: Harness) -> None:
        exporter = MagicMock()
        harness._video_exporter = exporter
        harness._video_export_paused = True
        harness._video_export_pause_started_at = time.perf_counter() - 2.5
        harness._video_export_paused_total = 0.5
        harness._set_video_export_controls = MagicMock()

        harness._cleanup_video_exporter()

        assert harness._video_export_paused_total == pytest.approx(3.0, abs=0.5)

    def test_cleanup_exception_swallowed(self, harness: Harness) -> None:
        exporter = MagicMock()
        exporter.cleanup.side_effect = RuntimeError("thread stuck")
        harness._video_exporter = exporter
        harness._set_video_export_controls = MagicMock()
        harness._cleanup_video_exporter()  # must not raise
        assert harness._video_exporter is None


# ═══════════════════════════════════════════════════════════════════════
# perform_video_export
# ═══════════════════════════════════════════════════════════════════════


def _video_sequence() -> MagicMock:
    sequence = MagicMock()
    sequence.timeline_id = "tl-1"
    return sequence


def _video_harness(harness: Harness) -> Harness:
    harness.edit_page = SimpleNamespace(_current_timeline=_video_sequence())
    harness._current_edit_project = MagicMock()
    harness._set_video_export_controls = MagicMock()
    harness._cleanup_video_exporter = MagicMock()
    harness._open_export_folder = MagicMock()
    return harness


class TestPerformVideoExport:
    def test_no_timeline_shows_critical(self, harness: Harness, patches: Any) -> None:
        harness.edit_page = None
        harness.perform_video_export({"path": "/tmp/out.mp4"})
        patches.boxes.critical.assert_called_once()
        assert harness.progress_bar.show.called is False

    def test_unknown_codec_raises_instead_of_silent_h264(
        self, harness: Harness, patches: Any
    ) -> None:
        _video_harness(harness)
        with pytest.raises(ValueError, match="bogus"):
            harness.perform_video_export({"path": "/tmp/out.mp4", "codec": "bogus"})

    def test_invalid_range_downgraded_to_entire_before_codec_check(
        self, harness: Harness, patches: Any
    ) -> None:
        _video_harness(harness)
        settings = {
            "path": "/tmp/out.mp4",
            "codec": "bogus",  # force the ValueError endpoint after range check
            "has_range": True,
            "range_start": 10,
            "range_end": 2,
        }
        with pytest.raises(ValueError):
            harness.perform_video_export(settings)
        patches.boxes.warning.assert_called_once()  # invalid range flagged
        patches.boxes.critical.assert_not_called()  # no timeline error

    def test_validation_error_shows_critical_and_never_starts(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.shared.export.video_export_worker as worker_mod

        exporter_cls = MagicMock()
        monkeypatch.setattr(worker_mod, "VideoExporter", exporter_cls)
        _video_harness(harness)

        harness.perform_video_export({"path": "", "codec": "h264"})

        patches.boxes.critical.assert_called_once()
        exporter_cls.assert_not_called()

    def test_successful_launch_wires_callbacks(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        _video_harness(harness)

        harness.perform_video_export({
            "path": "/tmp/out.mp4", "codec": "h264",
            "has_range": True, "range_start": 0, "range_end": 9, "fps": 24,
        })

        exporter_cls.assert_called_once()
        assert exporter_cls.call_args.kwargs["parent"] is harness
        instance = exporter_cls.return_value
        instance.export.assert_called_once()
        kwargs = instance.export.call_args.kwargs
        assert kwargs["config"].output_path == "/tmp/out.mp4"
        assert kwargs["config"].range_type.value != ""  # real enum resolved
        for cb in ("on_finished", "on_error", "on_progress"):
            assert callable(kwargs[cb])
        assert harness.progress_bar.setRange.call_args == ((0, 100),)
        assert harness._video_export_started_at is not None
        assert harness._video_export_paused is False
        harness._set_video_export_controls.assert_called_once_with(True, False)

    def test_progress_callback_updates_bar_and_status(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_successful_launch_wires_callbacks(harness, patches, monkeypatch)
        exporter = harness._video_exporter
        on_progress = exporter.export.call_args.kwargs["on_progress"]

        harness.progress_bar.reset_mock()
        harness.status_bar.reset_mock()
        harness._video_export_started_at = time.perf_counter() - 5.0
        harness._video_export_paused_total = 0.0

        on_progress(10, 100, 10.0)

        harness.progress_bar.setValue.assert_called_once_with(10)
        msg = harness.status_bar.showMessage.call_args.args[0]
        assert "10/100" in msg

    def test_finished_callback_cleans_up(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_successful_launch_wires_callbacks(harness, patches, monkeypatch)
        exporter = harness._video_exporter
        on_finished = exporter.export.call_args.kwargs["on_finished"]

        on_finished("/exports/movie.mp4")

        harness.progress_bar.hide.assert_called_once()
        harness.config_manager.set.assert_called_once_with("last_path", "/exports")
        harness._open_export_folder.assert_called_once_with("/exports/movie.mp4")
        harness._cleanup_video_exporter.assert_called_once()

    def test_error_callback_cancelled_is_not_an_error_box(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_successful_launch_wires_callbacks(harness, patches, monkeypatch)
        on_error = harness._video_exporter.export.call_args.kwargs["on_error"]

        on_error("EXPORT_CANCELLED")

        patches.boxes.critical.assert_not_called()
        harness._cleanup_video_exporter.assert_called_once()

    def test_error_callback_real_error_shows_box(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_successful_launch_wires_callbacks(harness, patches, monkeypatch)
        on_error = harness._video_exporter.export.call_args.kwargs["on_error"]

        on_error("encoder exploded")

        patches.boxes.critical.assert_called_once()
        assert "encoder exploded" in patches.boxes.critical.call_args.args[-1]
        harness._cleanup_video_exporter.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# perform_lut_export (guard paths only — full render needs GPU stack)
# ═══════════════════════════════════════════════════════════════════════


class TestPerformLutExport:
    def test_no_visible_layers_rejected(self, harness: Harness, patches: Any) -> None:
        harness.engine.layers = []
        harness.perform_lut_export({"path": "/tmp/lut.cube"})
        patches.boxes.critical.assert_called_once()
        assert "No visible layers" in harness.status_bar.showMessage.call_args.args[0]

    def test_hidden_layers_filtered_out(self, harness: Harness, patches: Any) -> None:
        hidden = MagicMock()
        hidden.visible = False
        harness.engine.layers = [hidden]
        harness.perform_lut_export({"path": "/tmp/lut.cube"})
        patches.boxes.critical.assert_called_once()  # hidden → still none visible

    def test_decline_confirmation_aborts(self, harness: Harness, patches: Any) -> None:
        layer = MagicMock()
        layer.visible = True
        layer.has_grading_effects.return_value = False  # triggers the question
        harness.engine.layers = [layer]
        patches.boxes.question.return_value = patches.boxes.StandardButton.No

        harness.perform_lut_export({"path": "/tmp/lut.cube"})

        patches.boxes.question.assert_called_once()
        harness.progress_bar.show.assert_not_called()  # aborted before progress


# ═══════════════════════════════════════════════════════════════════════
# export_file_as (dialog dispatch)
# ═══════════════════════════════════════════════════════════════════════


class TestExportFileAs:
    def _patch_dialog(self, monkeypatch: pytest.MonkeyPatch,
                      settings: Dict[str, Any]) -> MagicMock:
        import src.shared.export.quick_export_dialog as qed

        dialog_cls = MagicMock()
        dialog_cls.return_value.exec.return_value = True
        dialog_cls.return_value.get_settings.return_value = settings
        monkeypatch.setattr(qed, "QuickExportDialog", dialog_cls)
        return dialog_cls

    def test_image_settings_dispatch_to_quick_export(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialog_cls = self._patch_dialog(monkeypatch, {"export_type": "image"})
        harness._build_timeline_info = MagicMock()
        harness.perform_quick_export = MagicMock()

        harness.export_file_as()

        dialog_cls.assert_called_once()
        harness.perform_quick_export.assert_called_once_with({"export_type": "image"})

    def test_lut_and_video_dispatch(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness._build_timeline_info = MagicMock()
        harness.perform_lut_export = MagicMock()
        harness.perform_video_export = MagicMock()

        self._patch_dialog(monkeypatch, {"export_type": "lut"})
        harness.export_file_as()
        harness.perform_lut_export.assert_called_once()

        self._patch_dialog(monkeypatch, {"export_type": "video"})
        harness.export_file_as()
        harness.perform_video_export.assert_called_once()

    def test_dialog_cancelled_dispatches_nothing(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.shared.export.quick_export_dialog as qed

        dialog_cls = MagicMock()
        dialog_cls.return_value.exec.return_value = False
        monkeypatch.setattr(qed, "QuickExportDialog", dialog_cls)
        harness._build_timeline_info = MagicMock()
        harness.perform_quick_export = MagicMock()

        harness.export_file_as()

        harness.perform_quick_export.assert_not_called()

    def test_default_name_from_current_file(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialog_cls = self._patch_dialog(monkeypatch, {"export_type": "image"})
        harness._build_timeline_info = MagicMock()
        harness.perform_quick_export = MagicMock()
        harness.current_file_path = "/photos/sunset raw final.png"

        harness.export_file_as()

        assert dialog_cls.call_args.kwargs["default_name"] == "sunset raw final_exported"


# ═══════════════════════════════════════════════════════════════════════
# export_srt
# ═══════════════════════════════════════════════════════════════════════


class TestExportSrt:
    def test_no_timeline_warns(self, harness: Harness, patches: Any) -> None:
        harness.edit_page = None
        harness.export_srt()
        patches.boxes.warning.assert_called_once()

    def test_no_subtitle_tracks_informs(self, harness: Harness, patches: Any) -> None:
        harness.edit_page = SimpleNamespace(_current_timeline=SimpleNamespace(
            subtitle_tracks=[]))
        harness.export_srt()
        patches.boxes.information.assert_called_once()
        patches.file_dialog.getSaveFileName.assert_not_called()

    def test_dialog_cancel_aborts(self, harness: Harness, patches: Any,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
        timeline = SimpleNamespace(
            subtitle_tracks=[], name="T", range_start=None)
        harness.edit_page = SimpleNamespace(_current_timeline=timeline)
        # bypass the no-tracks guard by giving the timeline a track
        timeline.subtitle_tracks = [SimpleNamespace(
            is_enabled=True, is_visible=True, subtitle_clips=[])]
        patches.file_dialog.getSaveFileName.return_value = ("", "")

        harness.export_srt()

        patches.boxes.information.assert_not_called()

    def test_success_appends_srt_and_counts_clips(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.features.deliver.srt_exporter as srt_mod

        timeline = SimpleNamespace(
            name="Episode 01",
            subtitle_tracks=[
                SimpleNamespace(is_enabled=True, is_visible=True, subtitle_clips=[1, 2]),
                SimpleNamespace(is_enabled=False, is_visible=True, subtitle_clips=[9]),
            ],
        )
        harness.edit_page = SimpleNamespace(_current_timeline=timeline)
        patches.file_dialog.getSaveFileName.return_value = ("/subs/episode", "")
        export_fn = MagicMock(return_value=True)
        monkeypatch.setattr(srt_mod, "export_srt", export_fn)

        harness.export_srt()

        export_fn.assert_called_once_with(timeline, "/subs/episode.srt")
        info_text = patches.boxes.information.call_args.args[-1]
        assert "2 clips" in info_text  # disabled track's clip not counted

    def test_export_failure_shows_critical(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.features.deliver.srt_exporter as srt_mod

        timeline = SimpleNamespace(
            subtitle_tracks=[SimpleNamespace(
                is_enabled=True, is_visible=True, subtitle_clips=[1])])
        harness.edit_page = SimpleNamespace(_current_timeline=timeline)
        patches.file_dialog.getSaveFileName.return_value = ("/subs/ep.srt", "")
        monkeypatch.setattr(srt_mod, "export_srt", MagicMock(return_value=False))

        harness.export_srt()

        patches.boxes.critical.assert_called_once()

    def test_export_exception_shows_critical(
        self, harness: Harness, patches: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.features.deliver.srt_exporter as srt_mod

        timeline = SimpleNamespace(
            subtitle_tracks=[SimpleNamespace(
                is_enabled=True, is_visible=True, subtitle_clips=[1])])
        harness.edit_page = SimpleNamespace(_current_timeline=timeline)
        patches.file_dialog.getSaveFileName.return_value = ("/subs/ep.srt", "")
        monkeypatch.setattr(
            srt_mod, "export_srt", MagicMock(side_effect=OSError("disk full")))

        harness.export_srt()

        patches.boxes.critical.assert_called_once()
        assert "disk full" in patches.boxes.critical.call_args.args[-1]


# ═══════════════════════════════════════════════════════════════════════
# _build_timeline_info
# ═══════════════════════════════════════════════════════════════════════


def _fake_sequence(**overrides: Any) -> SimpleNamespace:
    base: Dict[str, Any] = dict(
        name="Seq A",
        duration_frames=100,
        range_start=10,
        range_end=50,
        width=0,
        height=0,
        fps=SimpleNamespace(to_fps=lambda: 24000 / 1001.0),
        video_tracks=[SimpleNamespace(
            duration_frames=100,
            clips=[SimpleNamespace(timeline_end=100, asset_id="asset-1")],
        )],
        audio_tracks=(),
        subtitle_tracks=(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildTimelineInfo:
    def test_no_edit_page_returns_empty(self, harness: Harness) -> None:
        info = harness._build_timeline_info()
        assert info["has_timeline"] is False
        assert info["name"] == ""
        assert info["fps"] == 0.0

    def test_no_timeline_returns_empty(self, harness: Harness) -> None:
        harness.edit_page = SimpleNamespace(_current_timeline=None)
        assert harness._build_timeline_info()["has_timeline"] is False

    def test_full_info_with_asset_resolution(self, harness: Harness) -> None:
        harness.edit_page = SimpleNamespace(_current_timeline=_fake_sequence())
        harness._current_edit_project = SimpleNamespace(
            get_asset_by_id=lambda aid: SimpleNamespace(
                metadata={"width": 3840, "height": 2160}))

        info = harness._build_timeline_info()

        assert info["has_timeline"] is True
        assert info["name"] == "Seq A"
        assert info["duration_frames"] == 100
        assert info["has_range"] is True
        assert (info["range_start"], info["range_end"]) == (10, 50)
        assert (info["width"], info["height"]) == (3840, 2160)
        assert info["fps"] == pytest.approx(23.976, abs=0.01)

    def test_no_inout_points_defaults_full_range(self, harness: Harness) -> None:
        harness.edit_page = SimpleNamespace(
            _current_timeline=_fake_sequence(range_start=None, range_end=None))
        harness._current_edit_project = None

        info = harness._build_timeline_info()

        assert info["has_range"] is False
        assert info["range_end"] == 99  # duration - 1

    def test_plain_float_fps(self, harness: Harness) -> None:
        harness.edit_page = SimpleNamespace(
            _current_timeline=_fake_sequence(fps=25.0, width=1920, height=1080))
        assert harness._build_timeline_info()["fps"] == 25.0

    def test_exception_returns_empty(self, harness: Harness) -> None:
        class _Boom:
            @property
            def _current_timeline(self) -> Any:
                raise RuntimeError("page broken")

        harness.edit_page = _Boom()
        info = harness._build_timeline_info()
        assert info["has_timeline"] is False

    def test_empty_timeline_still_has_duration(self, harness: Harness) -> None:
        """An empty sequence keeps a default canvas (black deliverable)."""
        harness.edit_page = SimpleNamespace(_current_timeline=_fake_sequence(
            duration_frames=0, video_tracks=(), range_start=None, range_end=None))
        info = harness._build_timeline_info()
        assert info["duration_frames"] > 0
        assert info["has_timeline"] is True


# ═══════════════════════════════════════════════════════════════════════
# perform_batch_export (validation + startup state)
# ═══════════════════════════════════════════════════════════════════════


class TestPerformBatchExport:
    def test_empty_files_rejected(self, harness: Harness, patches: Any,
                                  tmp_path: Any) -> None:
        harness.perform_batch_export({"files": [], "export_dir": str(tmp_path)})
        patches.boxes.warning.assert_called_once()
        patches.single_shot.assert_not_called()

    def test_invalid_export_dir_rejected(self, harness: Harness, patches: Any) -> None:
        harness.perform_batch_export({"files": ["/x/a.png"], "export_dir": "/no/such"})
        patches.boxes.warning.assert_called_once()
        patches.single_shot.assert_not_called()

    def test_valid_start_initialises_state_and_schedules(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        files = [str(tmp_path / "a.png"), str(tmp_path / "b.png")]
        harness._auto_save_current_file_before_batch = MagicMock()
        harness._connect_full_resolution_progress_signal = MagicMock()

        harness.perform_batch_export({
            "files": files, "export_dir": str(tmp_path), "format": "PNG"})

        assert harness._batch_files == files
        assert harness._batch_index == 0
        assert harness._batch_success_count == 0
        assert harness._batch_failed == []
        assert harness._batch_cancelled is False
        assert harness._batch_paused is False
        assert harness._batch_skip_done is False
        assert [i.file_path for i in harness._batch_items] == files
        assert all(i.status == STATUS_PENDING for i in harness._batch_items)
        patches.save_queue.assert_called_once()
        harness.export_panel.start_batch_ui.assert_called_once_with(2)
        assert patches.single_shot.call_count == 1
        assert patches.single_shot.call_args.args[1] == harness._process_next_batch_file

    def test_fresh_export_autosaves_current_file(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        harness._auto_save_current_file_before_batch = MagicMock()
        harness._connect_full_resolution_progress_signal = MagicMock()
        harness.perform_batch_export({
            "files": [str(tmp_path / "a.png")], "export_dir": str(tmp_path)})
        harness._auto_save_current_file_before_batch.assert_called_once()

    def test_matching_restored_queue_is_resume(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        files = [str(tmp_path / "a.png")]
        restored = [BatchQueueItem(file_path=files[0], status=STATUS_DONE)]
        harness._restored_queue_items = restored
        harness._restored_queue_settings = {"format": "PNG"}
        harness._auto_save_current_file_before_batch = MagicMock()
        harness._connect_full_resolution_progress_signal = MagicMock()

        harness.perform_batch_export({
            "files": files, "export_dir": str(tmp_path), "format": "PNG"})

        harness._auto_save_current_file_before_batch.assert_not_called()
        assert harness._batch_items == restored  # state markers preserved
        assert harness._batch_skip_done is True
        assert harness._restored_queue_items is None

    def test_mismatched_restored_queue_is_fresh(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        harness._restored_queue_items = [BatchQueueItem(file_path="/other.png")]
        harness._auto_save_current_file_before_batch = MagicMock()
        harness._connect_full_resolution_progress_signal = MagicMock()

        harness.perform_batch_export({
            "files": [str(tmp_path / "a.png")], "export_dir": str(tmp_path)})

        harness._auto_save_current_file_before_batch.assert_called_once()
        assert harness._batch_skip_done is False
        assert harness._batch_items[0].status == STATUS_PENDING


class TestAutoSaveCurrentFileBeforeBatch:
    def test_no_current_file_skipped(self, harness: Harness) -> None:
        harness.current_file_path = None
        harness._auto_save_current_file_before_batch()
        harness.save_router.save_with_context.assert_not_called()

    def test_current_file_not_in_queue_skipped(self, harness: Harness) -> None:
        harness.current_file_path = "/work/a.png"
        harness.export_panel.batch_list.get_all_files.return_value = ["/other/b.png"]
        flush = MagicMock()
        harness._flush_composite_to_edit_before_leave = flush

        harness._auto_save_current_file_before_batch()

        flush.assert_not_called()
        harness.save_router.save_with_context.assert_not_called()

    def test_current_file_in_queue_saved(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.project.save_router import SaveContext

        harness.current_file_path = "/work/a.png"
        harness.export_panel.batch_list.get_all_files.return_value = ["/work/a.png"]
        flush = MagicMock()
        harness._flush_composite_to_edit_before_leave = flush

        harness._auto_save_current_file_before_batch()

        flush.assert_called_once()
        harness.save_router.save_with_context.assert_called_once_with(
            SaveContext.LIBRARY_SINGLE)

    def test_save_failure_does_not_raise(self, harness: Harness) -> None:
        harness.current_file_path = "/work/a.png"
        harness.export_panel.batch_list.get_all_files.return_value = ["/work/a.png"]
        harness.save_router.save_with_context.side_effect = RuntimeError("io")
        harness._auto_save_current_file_before_batch()  # must not raise


# ═══════════════════════════════════════════════════════════════════════
# Batch pause / resume / checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestBatchPauseResume:
    def test_pause_without_batch_is_noop(self, harness: Harness) -> None:
        harness._batch_files = None
        harness._pause_batch_export()
        harness.export_panel.set_batch_paused.assert_not_called()

    def test_pause_sets_flag_and_ui(self, harness: Harness) -> None:
        harness._batch_files = ["/x/a.png"]
        harness._batch_index = 0
        harness._pause_batch_export()
        assert harness._batch_paused is True
        harness.export_panel.set_batch_paused.assert_called_once_with(True)
        assert harness.status_bar.showMessage.called is True

    def test_resume_without_batch_is_noop(self, harness: Harness,
                                          patches: Any) -> None:
        harness._batch_files = None
        harness._resume_batch_export()
        patches.single_shot.assert_not_called()

    def test_resume_reschedules_when_idle(self, harness: Harness, patches: Any) -> None:
        harness._batch_files = ["/x/a.png"]
        harness._batch_paused = True
        harness._batch_file_processing = False
        harness._process_next_batch_file = MagicMock()

        harness._resume_batch_export()

        assert harness._batch_paused is False
        harness.export_panel.set_batch_paused.assert_called_once_with(False)
        assert patches.single_shot.call_count == 1

    def test_resume_while_processing_does_not_reschedule(
        self, harness: Harness, patches: Any
    ) -> None:
        """Nested event loop finishes the current item — no double dispatch."""
        harness._batch_files = ["/x/a.png"]
        harness._batch_paused = True
        harness._batch_file_processing = True

        harness._resume_batch_export()

        patches.single_shot.assert_not_called()

    def test_checkpoint_true_when_running(self, harness: Harness) -> None:
        harness._batch_paused = False
        harness._batch_cancelled = False
        assert harness._batch_checkpoint() is True

    def test_checkpoint_false_when_cancelled(self, harness: Harness) -> None:
        harness._batch_paused = False
        harness._batch_cancelled = True
        assert harness._batch_checkpoint() is False

    def test_checkpoint_exits_when_cancelled_during_pause(
        self, harness: Harness
    ) -> None:
        harness._batch_paused = True
        harness._batch_cancelled = True
        assert harness._batch_checkpoint() is False


# ═══════════════════════════════════════════════════════════════════════
# _process_next_batch_file (state machine)
# ═══════════════════════════════════════════════════════════════════════


class TestProcessNextBatchFile:
    def _prime(self, harness: Harness, files: List[str],
               statuses: Optional[List[str]] = None) -> List[BatchQueueItem]:
        items = [
            BatchQueueItem(file_path=f, status=s)
            for f, s in zip(files, statuses or [STATUS_PENDING] * len(files))
        ]
        harness._batch_files = files
        harness._batch_items = items
        harness._batch_index = 0
        harness._batch_settings = {"options": {}}
        harness._batch_cancelled = False
        harness._batch_paused = False
        harness._batch_file_processing = False
        harness._batch_success_count = 0
        harness._batch_failed = []
        harness._batch_skip_done = False
        harness._update_global_batch_progress = MagicMock()
        harness._update_item_ui_status = MagicMock()
        harness._batch_export_single_file = MagicMock()
        harness._batch_export_complete = MagicMock()
        return items

    def test_success_marks_done_and_advances(self, harness: Harness, patches: Any) -> None:
        items = self._prime(harness, ["/d/a.png", "/d/b.png"])
        harness._batch_export_single_file.return_value = "/out/a.jpg"

        harness._process_next_batch_file()

        assert harness._batch_success_count == 1
        assert items[0].status == STATUS_DONE
        assert items[0].output_path == "/out/a.jpg"
        assert items[0].finished_at is not None
        assert items[0].error is None
        assert harness._batch_index == 1
        assert harness._batch_file_processing is False
        patches.single_shot.assert_called_once()
        patches.save_queue.assert_called()

    def test_failure_marks_failed_and_records_name(
        self, harness: Harness, patches: Any
    ) -> None:
        items = self._prime(harness, ["/d/a.png"])
        harness._batch_export_single_file.return_value = None

        harness._process_next_batch_file()

        assert harness._batch_failed == ["a.png"]
        assert items[0].status == STATUS_FAILED
        assert items[0].error == "render returned no output"
        assert harness._batch_success_count == 0

    def test_cancelled_file_stays_pending_for_retry(
        self, harness: Harness, patches: Any
    ) -> None:
        items = self._prime(harness, ["/d/a.png"])

        def _cancel_during_render(path: str, index: int) -> None:
            harness._batch_cancelled = True  # user cancels mid-render
            return None

        harness._batch_export_single_file.side_effect = _cancel_during_render

        harness._process_next_batch_file()

        assert harness._batch_failed == []  # cancellation ≠ failure
        assert items[0].status == STATUS_PENDING
        assert items[0].error == "cancelled by user"

    def test_exception_marks_failed_with_error_text(
        self, harness: Harness, patches: Any
    ) -> None:
        items = self._prime(harness, ["/d/a.png"])
        harness._batch_export_single_file.side_effect = RuntimeError("boom")

        harness._process_next_batch_file()

        assert harness._batch_failed == ["a.png"]
        assert items[0].status == STATUS_FAILED
        assert "boom" in items[0].error
        assert harness._batch_index == 1  # still advances past the poison file

    def test_done_items_skipped_on_resume(self, harness: Harness, patches: Any) -> None:
        items = self._prime(harness, ["/d/a.png"], statuses=[STATUS_DONE])
        harness._batch_skip_done = True

        harness._process_next_batch_file()

        harness._batch_export_single_file.assert_not_called()
        assert harness._batch_success_count == 0
        assert items[0].status == STATUS_DONE

    def test_index_past_end_completes(self, harness: Harness, patches: Any) -> None:
        self._prime(harness, ["/d/a.png"])
        harness._batch_index = 1
        harness._process_next_batch_file()
        harness._batch_export_complete.assert_called_once()
        patches.single_shot.assert_not_called()

    def test_cancelled_before_file_completes(self, harness: Harness, patches: Any) -> None:
        self._prime(harness, ["/d/a.png"])
        harness._batch_cancelled = True
        harness._process_next_batch_file()
        harness._batch_export_complete.assert_called_once()

    def test_paused_does_nothing(self, harness: Harness, patches: Any) -> None:
        self._prime(harness, ["/d/a.png"])
        harness._batch_paused = True
        harness._process_next_batch_file()
        harness._batch_export_single_file.assert_not_called()
        patches.single_shot.assert_not_called()
        assert harness._batch_index == 0


# ═══════════════════════════════════════════════════════════════════════
# _batch_export_single_file
# ═══════════════════════════════════════════════════════════════════════


class TestBatchExportSingleFile:
    def _prime(self, harness: Harness, tmp_path: Any, name: str = "in.png",
               fmt: str = "JPEG", with_sidecar: bool = True) -> str:
        src = str(tmp_path / name)
        harness._batch_settings = {"options": {}, "format": fmt}
        harness._batch_files = [src]
        harness._batch_export_dir = str(tmp_path / "out")
        os.makedirs(harness._batch_export_dir, exist_ok=True)
        harness._batch_paused = False
        harness._batch_cancelled = False
        harness.engine.RAW_EXTENSIONS = {".dng", ".cr2"}
        harness.save_router.load_single_asset_data.return_value = {}
        if with_sidecar:
            with open(f"{os.path.splitext(src)[0]}.topos", "w",
                      encoding="utf-8") as fh:
                fh.write("{}")
        return src

    def test_missing_topos_sidecar_exports_successfully(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        """BUG-A3-01 (fixed): no .topos sidecar → fresh layer, export succeeds.

        Regression guard for the former UnboundLocalError: the ``else``
        branch of the sidecar check used to leave ``project_data``
        unassigned, and the flip-state check a few lines later read it and
        raised UnboundLocalError. In production the per-file caller caught
        it and marked every sidecar-less queue item FAILED. Fixed in
        docs/testcoverage/2026-09-28/BUGS-agent-A3.md (BUG-A3-01); this
        now asserts a successful export instead.
        """
        src = self._prime(harness, tmp_path, name="nosidecar.png",
                          with_sidecar=False)
        patches.file_io.load_image.return_value = (_float_img(), None, None)
        harness.engine.get_current_image.return_value = _float_img()

        out = harness._batch_export_single_file(src, 1)

        assert out == os.path.join(harness._batch_export_dir, "nosidecar.jpg")
        from src.features.color.core.layer import Layer
        layers = harness.engine.layers
        assert len(layers) == 1
        assert isinstance(layers[0], Layer)
        assert layers[0].name == "Layer 1"
        harness.engine.set_flip_state.assert_not_called()

    def test_cancel_before_load_returns_none(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        src = self._prime(harness, tmp_path)
        harness._batch_cancelled = True
        assert harness._batch_export_single_file(src, 1) is None
        patches.file_io.load_image.assert_not_called()

    def test_load_failure_returns_none(self, harness: Harness, patches: Any,
                                       tmp_path: Any) -> None:
        src = self._prime(harness, tmp_path)
        patches.file_io.load_image.return_value = (None, None, None)
        assert harness._batch_export_single_file(src, 1) is None
        harness.engine.get_current_image.assert_not_called()

    def test_render_failure_returns_none(self, harness: Harness, patches: Any,
                                         tmp_path: Any) -> None:
        src = self._prime(harness, tmp_path)
        patches.file_io.load_image.return_value = (_float_img(), None, None)
        harness.engine.get_current_image.return_value = None
        assert harness._batch_export_single_file(src, 1) is None
        patches.file_io.save_image.assert_not_called()

    def test_success_converts_and_saves(self, harness: Harness, patches: Any,
                                        tmp_path: Any) -> None:
        src = self._prime(harness, tmp_path, name="photo.png")
        patches.file_io.load_image.return_value = (_float_img(fill=1.0), None, None)
        harness.engine.get_current_image.return_value = _float_img(fill=1.0)

        out = harness._batch_export_single_file(src, 1)

        assert out is not None
        assert out == os.path.join(harness._batch_export_dir, "photo.jpg")
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        assert saved.max() == 255
        # engine fields driven directly (no preview render triggered)
        assert harness.engine.img_full_original is not None
        assert harness.engine.current_file_path == src
        assert harness.engine.is_current_image_srgb is True

    def test_uint16_to_uint8_conversion(self, harness: Harness, patches: Any,
                                        tmp_path: Any) -> None:
        src = self._prime(harness, tmp_path, name="scan.png", fmt="PNG")
        patches.file_io.load_image.return_value = (
            np.full((2, 2, 3), 257 * 100, dtype=np.uint16), None, None)
        harness.engine.get_current_image.return_value = np.full(
            (2, 2, 3), 257 * 100, dtype=np.uint16)

        out = harness._batch_export_single_file(src, 1)

        assert out is not None and out.endswith("scan.png")
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.uint8
        assert saved[0, 0, 0] == 100

    def test_raw_source_sets_raw_metadata_defaults(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        src = self._prime(harness, tmp_path, name="frame.dng")
        patches.file_io.load_image.return_value = (_float_img(), None, None)
        harness.engine.get_current_image.return_value = _float_img()

        harness._batch_export_single_file(src, 1)

        assert harness.engine.is_current_image_srgb is False
        metadata = harness.engine.current_raw_metadata
        assert len(metadata["camera_wb"]) == 4  # rawpy-missing fallback shape

    def test_topos_sidecar_layers_applied_and_composite_graph_used(
        self, harness: Harness, patches: Any, tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.features.composite.core.node_engine as engine_mod

        src = self._prime(harness, tmp_path, name="graded.png")
        sidecar = f"{os.path.splitext(src)[0]}.topos"
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("{}")
        sentinel_layers: List[Any] = [object()]
        harness.save_router.load_single_asset_data.return_value = {
            "layers": sentinel_layers,
            "raw_params": {"enabled": True},
            "composite_graph": {"nodes": []},
        }
        graph_calls: List[Dict[str, Any]] = []

        class _FakeGraph:
            @classmethod
            def from_dict(cls, data: Dict[str, Any]) -> "_FakeGraph":
                graph_calls.append(data)
                return cls()

        monkeypatch.setattr(engine_mod, "NodeGraph", _FakeGraph)
        harness._normalize_standalone_graph_for_batch = MagicMock()
        composite_img = np.full((2, 2, 3), 0.9, dtype=np.float32)
        harness._render_composite_full_res = MagicMock(return_value=composite_img)
        patches.file_io.load_image.return_value = (_float_img(fill=0.1), None, None)
        harness.engine.get_current_image.return_value = _float_img(fill=0.1)
        harness._batch_settings["apply_composite"] = True

        out = harness._batch_export_single_file(src, 1)

        assert out is not None
        assert harness.engine.layers == sentinel_layers
        assert harness.engine.current_raw_params == {"enabled": True}
        assert graph_calls and graph_calls[0] == {"nodes": []}
        harness._normalize_standalone_graph_for_batch.assert_called_once()
        render = harness._render_composite_full_res  # type: MagicMock
        assert render.call_args.kwargs["keep_linear"] is False
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved[0, 0, 0] == 229  # 0.9 composite, not 0.1 color

    def test_topos_without_composite_graph_falls_back_to_color(
        self, harness: Harness, patches: Any, tmp_path: Any, caplog: Any
    ) -> None:
        src = self._prime(harness, tmp_path, name="plain.png")
        sidecar = f"{os.path.splitext(src)[0]}.topos"
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("{}")
        harness.save_router.load_single_asset_data.return_value = {"layers": []}
        harness._batch_settings["apply_composite"] = True
        patches.file_io.load_image.return_value = (_float_img(fill=1.0), None, None)
        harness.engine.get_current_image.return_value = _float_img(fill=1.0)

        with caplog.at_level("WARNING"):
            out = harness._batch_export_single_file(src, 1)

        assert out is not None
        assert any("No composite graph" in r.getMessage() for r in caplog.records)

    def test_exr_source_keeps_linear_composite(
        self, harness: Harness, patches: Any, tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.features.composite.core.node_engine as engine_mod

        # keep_linear follows the INPUT file extension (.exr), not the output
        src = self._prime(harness, tmp_path, name="hdr.exr", fmt="OpenEXR")
        sidecar = f"{os.path.splitext(src)[0]}.topos"
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("{}")
        harness.save_router.load_single_asset_data.return_value = {
            "composite_graph": {"nodes": []}}
        monkeypatch.setattr(
            engine_mod, "NodeGraph",
            type("_G", (), {"from_dict": classmethod(lambda cls, d: object())}))
        harness._normalize_standalone_graph_for_batch = MagicMock()
        composite_img = np.full((2, 2, 3), 0.5, dtype=np.float32)
        harness._render_composite_full_res = MagicMock(return_value=composite_img)
        patches.file_io.load_image.return_value = (_float_img(), None, None)
        harness.engine.get_current_image.return_value = _float_img()
        harness._batch_settings["apply_composite"] = True

        out = harness._batch_export_single_file(src, 1)

        assert out is not None and out.endswith("hdr.exr")
        render = harness._render_composite_full_res  # type: MagicMock
        assert render.call_args.kwargs["keep_linear"] is True
        saved = patches.file_io.save_image.call_args.args[1]
        assert saved.dtype == np.float32  # EXR not downconverted


# ═══════════════════════════════════════════════════════════════════════
# _batch_generate_filename
# ═══════════════════════════════════════════════════════════════════════


class TestBatchGenerateFilename:
    def test_original_filename_mode(self, harness: Harness) -> None:
        harness._batch_settings = {"format": "JPEG"}
        assert harness._batch_generate_filename("/d/my photo.png", 3) == "my photo.jpg"

    def test_unknown_format_defaults_to_jpg(self, harness: Harness) -> None:
        harness._batch_settings = {"format": "Nope"}
        assert harness._batch_generate_filename("/d/a.png", 1) == "a.jpg"

    def test_format_map_variants(self, harness: Harness) -> None:
        for fmt, ext in [("PNG", ".png"), ("OpenEXR", ".exr"),
                         ("TIFF", ".tif"), ("WebP", ".webp")]:
            harness._batch_settings = {"format": fmt}
            assert harness._batch_generate_filename("/d/a.png", 1) == f"a{ext}"

    def test_prefix_sequence_mode(self, harness: Harness) -> None:
        harness._batch_settings = {
            "format": "PNG", "naming_mode": "Prefix + Sequence",
            "prefix": "frame_", "seq_digits": 4,
        }
        assert harness._batch_generate_filename("/d/a.png", 7) == "frame_0007.png"

    def test_custom_pattern_mode(self, harness: Harness) -> None:
        harness._batch_settings = {
            "format": "PNG", "naming_mode": "Custom Pattern",
            "custom_pattern": "{name}_{n:02d}{ext}",
        }
        assert harness._batch_generate_filename("/d/shot.png", 5) == "shot_05.png"

    def test_custom_pattern_bad_placeholder_falls_back(self, harness: Harness) -> None:
        harness._batch_settings = {
            "format": "PNG", "naming_mode": "Custom Pattern",
            "custom_pattern": "{unknown_key}",
        }
        assert harness._batch_generate_filename("/d/shot.png", 5) == "shot_005.png"


# ═══════════════════════════════════════════════════════════════════════
# _batch_export_complete
# ═══════════════════════════════════════════════════════════════════════


class TestBatchExportComplete:
    def _prime(self, harness: Harness, items: List[BatchQueueItem],
               failed: List[str], cancelled: bool = False) -> None:
        harness._batch_files = [i.file_path for i in items]
        harness._batch_items = items
        harness._batch_success_count = sum(
            1 for i in items if i.status == STATUS_DONE)
        harness._batch_failed = failed
        harness._batch_cancelled = cancelled
        harness._batch_paused = False
        harness._batch_file_processing = True
        harness._batch_original_layers = None
        harness._batch_original_active_index = None
        harness._batch_original_raw_params = None
        harness._batch_original_file_path = None
        harness._batch_original_is_srgb = True
        harness._batch_original_raw_metadata = None
        harness._batch_original_last_raw_params = None
        harness._batch_original_image = None

    def test_success_clears_queue_and_informs(
        self, harness: Harness, patches: Any
    ) -> None:
        self._prime(harness, [BatchQueueItem("/d/a.png", status=STATUS_DONE)], [])
        harness._batch_export_complete()

        patches.clear_queue.assert_called_once()
        patches.boxes.information.assert_called_once()
        patches.boxes.warning.assert_not_called()
        harness.export_panel.finish_batch_ui.assert_called_once()
        harness.export_panel.set_batch_paused.assert_called_once_with(False)
        harness.request_render.assert_called_once()

    def test_failures_keep_queue_and_warn(self, harness: Harness, patches: Any) -> None:
        self._prime(
            harness,
            [BatchQueueItem("/d/bad.png", status=STATUS_FAILED)],
            failed=["bad.png"],
        )
        harness._batch_export_complete()

        patches.clear_queue.assert_not_called()
        patches.boxes.warning.assert_called_once()
        assert "bad.png" in patches.boxes.warning.call_args.args[-1]

    def test_cancelled_informs_with_progress_count(
        self, harness: Harness, patches: Any
    ) -> None:
        items = [
            BatchQueueItem("/d/a.png", status=STATUS_DONE),
            BatchQueueItem("/d/b.png", status=STATUS_PENDING),
        ]
        self._prime(harness, items, failed=[], cancelled=True)
        harness._batch_export_complete()

        patches.clear_queue.assert_not_called()  # pending item stays restorable
        patches.boxes.information.assert_called_once()
        assert "1/2" in patches.boxes.information.call_args.args[-1]

    def test_engine_state_restored(self, harness: Harness, patches: Any) -> None:
        self._prime(harness, [BatchQueueItem("/d/a.png", status=STATUS_DONE)], [])
        original_layers: List[Any] = [object()]
        harness._batch_original_layers = original_layers
        harness._batch_original_active_index = 3
        harness._batch_original_raw_params = {"exposure": 0.5}
        harness._batch_original_file_path = "/orig/current.png"
        harness._batch_original_is_srgb = False
        harness._batch_original_raw_metadata = {"camera_wb": [1, 1, 1, 1]}
        harness._batch_original_last_raw_params = {"enabled": False}

        harness._batch_export_complete()

        assert harness.engine.layers == original_layers
        assert harness.engine.active_layer_index == 3
        assert harness.engine.current_raw_params == {"exposure": 0.5}
        assert harness.engine.current_file_path == "/orig/current.png"
        assert harness.engine.is_current_image_srgb is False
        assert harness.engine.current_raw_metadata == {"camera_wb": [1, 1, 1, 1]}

    def test_original_image_restored_via_set_image(
        self, harness: Harness, patches: Any
    ) -> None:
        self._prime(harness, [BatchQueueItem("/d/a.png", status=STATUS_DONE)], [])
        original = _float_img(fill=0.2)
        harness._batch_original_image = original

        harness._batch_export_complete()

        harness.engine.set_image.assert_called_once()
        call = harness.engine.set_image.call_args
        assert call.args[0] is original
        assert call.kwargs.get("is_new_file") is False


# ═══════════════════════════════════════════════════════════════════════
# Progress plumbing
# ═══════════════════════════════════════════════════════════════════════


class TestFullResolutionProgress:
    def test_ignores_events_outside_batch(self, harness: Harness) -> None:
        harness._batch_file_processing = False
        harness._on_full_resolution_progress("req", 10, "render")
        harness.progress_bar.setValue.assert_not_called()
        harness.status_bar.showMessage.assert_not_called()

    def test_ignores_events_with_no_files(self, harness: Harness) -> None:
        harness._batch_file_processing = True
        harness._batch_files = []
        harness._on_full_resolution_progress("req", 10, "render")
        harness.progress_bar.setValue.assert_not_called()

    def test_percent_clamped_to_100(self, harness: Harness) -> None:
        harness._batch_file_processing = True
        harness._batch_files = ["/d/a.png"]
        harness._batch_index = 0
        harness._on_full_resolution_progress("req", 150, "render")
        harness.progress_bar.setValue.assert_called_once_with(100)

    def test_phase_dedup_avoids_status_spam(self, harness: Harness) -> None:
        harness._batch_file_processing = True
        harness._batch_files = ["/d/a.png"]
        harness._batch_index = 0
        harness._last_full_res_progress_phase = "render"

        harness._on_full_resolution_progress("req", 10, "render")
        assert harness.status_bar.showMessage.call_count == 0

        harness._on_full_resolution_progress("req", 20, "save")
        assert harness.status_bar.showMessage.call_count == 1
        assert harness._last_full_res_progress_phase == "save"


class TestUpdateGlobalBatchProgress:
    def test_updates_bar(self, harness: Harness) -> None:
        harness._update_global_batch_progress(2, 5)
        harness.progress_bar.setRange.assert_called_once_with(0, 500)
        harness.progress_bar.setValue.assert_called_once_with(200)
        harness.progress_bar.show.assert_called_once()

    def test_no_progress_bar_is_safe(self, harness: Harness) -> None:
        harness.progress_bar = None  # type: ignore[assignment]
        harness._update_global_batch_progress(0, 3)  # must not raise

    def test_bar_failure_swallowed(self, harness: Harness) -> None:
        harness.progress_bar.setRange.side_effect = RuntimeError("dead widget")
        harness._update_global_batch_progress(0, 3)  # must not raise


class TestUpdateItemUiStatus:
    def test_status_forwarded(self, harness: Harness) -> None:
        harness.export_panel.batch_list.count.return_value = 3
        harness._update_item_ui_status(1, STATUS_RENDERING)
        harness.export_panel.batch_list.set_item_status.assert_called_once_with(
            1, STATUS_RENDERING)

    def test_index_out_of_range_ignored(self, harness: Harness) -> None:
        harness.export_panel.batch_list.count.return_value = 1
        harness._update_item_ui_status(5, STATUS_DONE)
        harness.export_panel.batch_list.set_item_status.assert_not_called()

    def test_missing_batch_list_ignored(self, harness: Harness) -> None:
        harness.export_panel.batch_list = None
        harness._update_item_ui_status(0, STATUS_DONE)  # must not raise


class TestConnectFullResolutionProgressSignal:
    def test_engine_without_signal_is_noop(self, harness: Harness) -> None:
        del harness.engine.full_resolution_progress  # type: ignore[attr-defined]
        harness._connect_full_resolution_progress_signal()
        assert getattr(harness, "_full_res_progress_connected", False) is False

    def test_connects_once_only(self, harness: Harness) -> None:
        harness._connect_full_resolution_progress_signal()
        assert harness._full_res_progress_connected is True
        harness.engine.full_resolution_progress.connect.assert_called_once_with(
            harness._on_full_resolution_progress)

        harness._connect_full_resolution_progress_signal()
        assert harness.engine.full_resolution_progress.connect.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# _normalize_standalone_graph_for_batch
# ═══════════════════════════════════════════════════════════════════════


class _FakeNode:
    def __init__(self, node_id: str, node_type: str,
                 params: Optional[Dict[str, Any]] = None) -> None:
        self.node_id = node_id
        self.node_type = node_type
        self.parameters = params or {}


class _FakeConn:
    def __init__(self, src: str, dst: str, src_port: str = "output",
                 dst_port: str = "image_in") -> None:
        self.src_node_id = src
        self.src_port_name = src_port
        self.dst_node_id = dst
        self.dst_port_name = dst_port


class _FakeGraph:
    def __init__(self, nodes: Dict[str, _FakeNode],
                 connections: List[_FakeConn]) -> None:
        self.nodes = nodes
        self.connections = list(connections)
        self.removed: List[str] = []
        self.added: List[_FakeNode] = []

    def remove_node(self, node_id: str) -> None:
        self.removed.append(node_id)
        self.nodes.pop(node_id, None)
        self.connections = [
            c for c in self.connections if c.src_node_id != node_id]

    def add_node(self, node: _FakeNode) -> None:
        self.added.append(node)
        self.nodes[node.node_id] = node


class TestNormalizeStandaloneGraphForBatch:
    def _patch_node_classes(self, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
        import src.features.composite.nodes.base_node as base_mod
        import src.features.composite.nodes.color_node as color_mod

        created: List[Any] = []
        connections: List[Any] = []

        class _FakeColorNode:
            def __init__(self, node_id: str) -> None:
                self.node_id = node_id
                created.append(self)

        class _FakeConnection:
            def __init__(self, src_node_id: str, src_port_name: str,
                         dst_node_id: str, dst_port_name: str) -> None:
                self.src_node_id = src_node_id
                self.src_port_name = src_port_name
                self.dst_node_id = dst_node_id
                self.dst_port_name = dst_port_name
                connections.append(self)

        monkeypatch.setattr(color_mod, "COLORNode", _FakeColorNode)
        monkeypatch.setattr(base_mod, "Connection", _FakeConnection)
        return connections

    def test_empty_timeline_source_replaced_by_color_input(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connections = self._patch_node_classes(monkeypatch)
        graph = _FakeGraph(
            nodes={
                "src": _FakeNode("src", "timeline_clip_input"),
                "grade": _FakeNode("grade", "topos_color"),
            },
            connections=[_FakeConn("src", "grade", "output", "image_in")],
        )

        harness._normalize_standalone_graph_for_batch(graph)

        assert graph.removed == ["src"]
        assert "src" in graph.nodes
        assert graph.nodes["src"].node_id == "src"  # id kept stable
        rebuilt = [c for c in connections if c.src_node_id == "src"]
        assert len(rebuilt) == 1
        assert rebuilt[0].src_port_name == "result"  # color_input port name
        assert rebuilt[0].dst_node_id == "grade"
        assert rebuilt[0].dst_port_name == "image_in"

    def test_referenced_source_left_alone(self, harness: Harness,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_node_classes(monkeypatch)
        graph = _FakeGraph(
            nodes={
                "src": _FakeNode("src", "timeline_clip_input",
                                 {"timeline_id": "tl", "clip_id": "c1"}),
            },
            connections=[],
        )
        harness._normalize_standalone_graph_for_batch(graph)
        assert graph.removed == []
        assert graph.added == []
        assert graph.nodes["src"].node_type == "timeline_clip_input"

    def test_graph_without_sources_untouched(self, harness: Harness,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_node_classes(monkeypatch)
        graph = _FakeGraph(
            nodes={"a": _FakeNode("a", "color_input")},
            connections=[_FakeConn("a", "b")],
        )
        harness._normalize_standalone_graph_for_batch(graph)
        assert graph.removed == [] and graph.added == []
        assert len(graph.connections) == 1

    def test_whitespace_only_ids_count_as_empty(self, harness: Harness,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_node_classes(monkeypatch)
        graph = _FakeGraph(
            nodes={"src": _FakeNode(
                "src", "timeline_clip_input",
                {"timeline_id": "  ", "clip_id": "", "asset_id": "  "})},
            connections=[],
        )
        harness._normalize_standalone_graph_for_batch(graph)
        assert graph.removed == ["src"]


# ═══════════════════════════════════════════════════════════════════════
# Queue persistence
# ═══════════════════════════════════════════════════════════════════════


class TestQueuePersistence:
    def test_has_restorable_true(self, harness: Harness, patches: Any) -> None:
        patches.restorable.return_value = True
        assert harness._has_restorable_queue() is True

    def test_has_restorable_error_returns_false(self, harness: Harness,
                                                patches: Any) -> None:
        patches.restorable.side_effect = OSError("no queue file")
        assert harness._has_restorable_queue() is False

    def test_prompt_restore_yes_refills_panel(
        self, harness: Harness, patches: Any, tmp_path: Any
    ) -> None:
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        item = BatchQueueItem(file_path=str(src), status=STATUS_RENDERING)
        patches.load_queue.return_value = ([item], {"format": "PNG"})
        patches.boxes.question.return_value = patches.boxes.StandardButton.Yes

        harness._prompt_restore_queue()

        harness.export_panel.restore_items.assert_called_once_with([item.to_dict()])
        assert harness._restored_queue_items == [item]
        assert harness._restored_queue_settings == {"format": "PNG"}
        patches.clear_queue.assert_not_called()

    def test_prompt_restore_no_clears_queue(self, harness: Harness,
                                            patches: Any, tmp_path: Any) -> None:
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        patches.load_queue.return_value = (
            [BatchQueueItem(file_path=str(src), status=STATUS_PENDING)], {})
        patches.boxes.question.return_value = object()  # not Yes

        harness._prompt_restore_queue()

        patches.clear_queue.assert_called_once()
        assert harness._restored_queue_items is None
        harness.export_panel.restore_items.assert_not_called()

    def test_prompt_restore_skips_missing_files(self, harness: Harness,
                                                patches: Any) -> None:
        patches.load_queue.return_value = (
            [BatchQueueItem(file_path="/gone/a.png", status=STATUS_PENDING)], {})

        harness._prompt_restore_queue()

        patches.boxes.question.assert_not_called()
        patches.clear_queue.assert_called_once()

    def test_prompt_restore_no_restorable_clears(self, harness: Harness,
                                                 patches: Any) -> None:
        patches.load_queue.return_value = (
            [BatchQueueItem(file_path="/d/a.png", status=STATUS_DONE)], {})
        harness._prompt_restore_queue()
        patches.clear_queue.assert_called_once()
        patches.boxes.question.assert_not_called()

    def test_prompt_restore_load_error_is_silent(self, harness: Harness,
                                                 patches: Any) -> None:
        patches.load_queue.side_effect = OSError("corrupt json")
        harness._prompt_restore_queue()  # must not raise
        patches.clear_queue.assert_not_called()

    def test_persist_on_close_saves_active_items(self, harness: Harness,
                                                 patches: Any) -> None:
        items = [BatchQueueItem(file_path="/d/a.png")]
        settings = {"format": "PNG"}
        harness._batch_items = items
        harness._batch_settings = settings

        harness._persist_batch_queue_on_close()

        patches.save_queue.assert_called_once_with(items, settings)

    def test_persist_on_close_nothing_to_do(self, harness: Harness,
                                            patches: Any) -> None:
        harness._batch_items = None
        harness._batch_settings = None
        harness._persist_batch_queue_on_close()
        patches.save_queue.assert_not_called()

    def test_persist_on_close_error_swallowed(self, harness: Harness,
                                              patches: Any) -> None:
        harness._batch_items = [BatchQueueItem(file_path="/d/a.png")]
        patches.save_queue.side_effect = OSError("disk full")
        harness._persist_batch_queue_on_close()  # must not raise
