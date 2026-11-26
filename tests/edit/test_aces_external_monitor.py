"""Independent projector monitoring preserves the desktop and delivery views."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.color.aces_monitor_readback import AcesMonitorReadback
from src.shared.color.color_space_descriptor import ACESCG_SCENE
from src.shared.color.project_video_signal import (
    ACES_DCI_P3_MONITOR,
    ACES_DESKTOP_LUT_KEY,
    ACES_EXTERNAL_LUT_KEY,
    ACES_EXTERNAL_MONITOR_KEY,
    ACES_VERSION_KEY,
    aces_desktop_monitor_settings,
    aces_external_monitor_settings,
    project_aces_settings,
    render_aces_bgr,
)


def test_independent_aces_monitor_view_preserves_render_settings() -> None:
    settings = {"color.timeline_space": "acescg", ACES_EXTERNAL_MONITOR_KEY: ACES_DCI_P3_MONITOR}
    aces = project_aces_settings(settings)
    assert aces is not None
    assert aces.target == "rec709-g24"
    assert aces_external_monitor_settings(aces).target == "dci-p3-g26-d60-sim"
    assert aces_desktop_monitor_settings(aces).target == "rec709-g24"
    assert aces_external_monitor_settings(project_aces_settings({**settings, ACES_VERSION_KEY: "2.0"})) is None

    settings[ACES_DESKTOP_LUT_KEY] = "topos-res://lut_desktop"
    settings[ACES_EXTERNAL_LUT_KEY] = "topos-res://lut_external"
    aces = project_aces_settings({**settings, ACES_VERSION_KEY: "2.0"})
    assert aces.monitor_lut == ""
    assert aces_desktop_monitor_settings(aces).monitor_lut == "topos-res://lut_desktop"
    external = aces_external_monitor_settings(aces)
    assert external.target == "dci-p3-g26-d60-sim"
    assert external.monitor_lut == "topos-res://lut_external"


def test_projector_gl_readback_matches_cpu_output_from_graded_aces() -> None:
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PyOpenColorIO")
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except Exception as exc:
        pytest.skip(f"Standalone OpenGL unavailable: {exc}")

    project = project_aces_settings({
        "color.timeline_space": "acescg", ACES_EXTERNAL_MONITOR_KEY: ACES_DCI_P3_MONITOR,
    })
    projector = aces_external_monitor_settings(project)
    # Values immediately after grading in ACES, including a bright saturated patch.
    rgba = np.array([[[0.18, 0.18, 0.18, 1.0], [0.5, 0.1, 0.1, 1.0]],
                     [[0.1, 1.4, 0.18, 1.0], [0.06, 0.08, 0.8, 1.0]]], np.float32)
    texture = ctx.texture((2, 2), 4, rgba.tobytes(), dtype="f4")
    monitor = AcesMonitorReadback(ctx)
    try:
        monitor.submit(texture, ACESCG_SCENE, projector, 4, 10)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if result := monitor.take(10):
                break
            time.sleep(0.002)
        else:
            pytest.fail("No GPU projector readback arrived")
        frame, generation, payload = result
        assert (frame, generation, payload.width, payload.height) == (4, 10, 2, 2)
        actual = np.frombuffer(payload.data, dtype=np.uint8).reshape(2, 2, 4)
        # Feed the monitor transport's image path without an ICC or matrix stage.
        from src.shared.video.monitor_output import monitor_payload_to_bgra_frame

        bgra = np.frombuffer(monitor_payload_to_bgra_frame(payload, width=2, height=2),
                             dtype=np.uint8).reshape(2, 2, 4)
        np.testing.assert_array_equal(bgra, actual[::-1, :, [2, 1, 0, 3]])
        bgr = rgba[..., 2::-1].copy()
        expected = render_aces_bgr(bgr, ACESCG_SCENE, projector)
        expected = np.rint(np.clip(expected[..., ::-1], 0.0, 1.0) * 255)
        np.testing.assert_allclose(actual[..., :3], expected, atol=1)
        desktop = render_aces_bgr(bgr, ACESCG_SCENE, project)
        assert not np.allclose(desktop, expected[..., ::-1] / 255, atol=0.025)

        monitor.submit(texture, ACESCG_SCENE, projector, 5, 10)
        ctx.finish()
        assert monitor.take(11) is None
        assert not monitor.transfer_pending
    finally:
        monitor.release()
        texture.release()
        ctx.release()


def test_decklink_projector_route_uses_native_rgb(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.controllers.file_controller import FileOpsMixin
    from src.shared.video import monitor_output

    conf = {"decklink_monitor_enabled": True, "decklink_monitor_native_pixel_format": "yuv"}
    project = SimpleNamespace(color_management={
        "color.timeline_space": "acescct", ACES_EXTERNAL_MONITOR_KEY: ACES_DCI_P3_MONITOR,
    })
    host = SimpleNamespace(
        config_manager=conf, _current_edit_project=project, _get_work_mode=lambda: "video",
        _discard_color_monitor_pending_readback=lambda: None,
        _set_program_monitor_output_modes=lambda **_: None,
    )

    class Output:
        def __init__(self, config: monitor_output.DeckLinkMonitorConfig) -> None:
            self.config = config
            self.backend = SimpleNamespace(is_running=True, supports_gpu_texture=False)

        def start(self) -> bool:
            return True

    monkeypatch.setattr(monitor_output, "ProgramMonitorOutputController", Output)
    controller = FileOpsMixin._ensure_program_monitor_output(host)
    assert controller.config.backend == "native_sdk"
    assert controller.config.native_pixel_format == "bgra"
    assert host._program_monitor_aces_external_view.target == "dci-p3-g26-d60-sim"


def test_cpu_fallback_emits_projector_codes_from_the_graded_buffer() -> None:
    pytest.importorskip("PyOpenColorIO")
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

    settings = {"color.timeline_space": "acescg", ACES_EXTERNAL_MONITOR_KEY: ACES_DCI_P3_MONITOR}
    delivered = []
    worker = SimpleNamespace(
        _aces_external_monitor_enabled=True,
        _preview_color_settings=lambda: settings,
        aces_monitor_frame_rendered=SimpleNamespace(emit=lambda *values: delivered.append(values)),
    )
    worker._external_preview_target = lambda: PreviewRenderWorker._external_preview_target(worker)
    graded = np.array([[[0.1, 0.2, 1.5], [0.18, 0.18, 0.18]]], dtype=np.float32)
    PreviewRenderWorker._render_external_aces_frame(worker, 8, 9, graded)
    frame, generation, pixels = delivered[0]
    assert (frame, generation) == (8, 9)
    project = project_aces_settings(settings)
    projector = render_aces_bgr(graded, ACESCG_SCENE, aces_external_monitor_settings(project))
    expected = np.rint(np.clip(projector, 0.0, 1.0) * 255.0).astype(np.uint8)
    np.testing.assert_array_equal(pixels, expected)
    desktop = render_aces_bgr(graded, ACESCG_SCENE, project)
    assert not np.allclose(pixels / 255.0, desktop, atol=0.02)

    worker._aces_external_monitor_enabled = False
    PreviewRenderWorker._render_external_aces_frame(worker, 9, 9, graded)
    assert len(delivered) == 1


def test_composite_monitor_uses_scene_linear_program_texture(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from src.features.composite.render import composite_renderer as module
    from src.shared.color.color_space_descriptor import LINEAR_SCENE

    project = project_aces_settings({
        "color.timeline_space": "acescct", ACES_EXTERNAL_MONITOR_KEY: ACES_DCI_P3_MONITOR,
    })
    view = aces_external_monitor_settings(project)
    texture = SimpleNamespace(glo=17)
    payload = object()
    monitor = Mock()
    monitor.take.return_value = (99, 1, payload)
    monkeypatch.setattr(module, "AcesMonitorReadback", Mock(return_value=monitor))
    renderer = SimpleNamespace(
        ctx=Mock(), _active_viewer_roi=None, _resource_pool=None,
        _ensure_output_buffers=Mock(), _bind_external_texture=Mock(), _render_quad=Mock(),
        fbo_ring=[Mock()], tex_ring=[texture], write_idx=0,
        utility_shaders={"passthrough": {"u_texture": SimpleNamespace(value=None)}},
        frame_ready=Mock(), aces_monitor_frame_ready=Mock(), metadata_ready=Mock(), _build_aov_metadata=Mock(),
    )
    result = SimpleNamespace(width=2, height=1, format=None, bbox=None, texture_id=18)
    module.CompositeRenderer.set_aces_external_monitor(renderer, view)
    module.CompositeRenderer._render_to_output(renderer, result, request_id=99)
    monitor.submit.assert_called_once_with(texture, LINEAR_SCENE, view, 99, 1)
    renderer.frame_ready.emit.assert_called_once_with(99, 17, 2, 1)
    renderer.aces_monitor_frame_ready.emit.assert_called_once_with(99, view, payload)
