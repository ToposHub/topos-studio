from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
import sys

from PyQt6.QtGui import QColor, QImage
import numpy as np
import pytest

from src.shared.video import monitor_output
from src.shared.video.monitor_output import (
    DeckLinkMonitorConfig,
    FfmpegDeckLinkMonitorOutput,
    MonitorRGBAFrame,
    NativeDeckLinkSDKMonitorOutput,
    NativeGpuMonitorFrame,
    ProgramMonitorOutputController,
    build_ffmpeg_decklink_command,
    ffmpeg_supports_decklink,
    monitor_payload_to_bgra_frame,
    ndarray_to_bgra_frame,
    qimage_to_bgra_frame,
    resolve_ffmpeg_path,
)
from src.features.edit.ui.program_viewer import ProgramViewer


def test_build_ffmpeg_decklink_command_uses_monitor_defaults() -> None:
    config = DeckLinkMonitorConfig(
        enabled=True,
        device="DeckLink Mini Monitor 4K",
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
    )

    command = build_ffmpeg_decklink_command(config)

    assert command[:2] == ["/opt/homebrew/bin/ffmpeg", "-hide_banner"]
    assert command[command.index("-s") + 1] == "1920x1080"
    assert command[command.index("-r") + 1] == "25"
    assert command[command.index("-format_code") + 1] == "Hi50"
    assert command[command.index("-pix_fmt") + 1] == "bgra"
    assert command[-2:] == ["uyvy422", "DeckLink Mini Monitor 4K"]


def test_decklink_config_supports_project_config_manager_get_signature() -> None:
    class _Config:
        config = {
            "decklink_monitor_enabled": True,
            "decklink_monitor_device": "DeckLink Mini Monitor 4K",
            "color.monitor.external_calibration_lut": "/path/display.cube",
        }

        def get(self, key):
            return self.config.get(key)

    config = DeckLinkMonitorConfig.from_config(_Config())

    assert config.enabled is True
    assert config.device == "DeckLink Mini Monitor 4K"
    assert config.width == 1920
    assert config.backend == "ffmpeg"
    assert config.mode == "1080i50"
    assert config.connection == "hdmi"
    assert config.native_pixel_format == "yuv"
    assert config.calibration_lut == "/path/display.cube"


def _calibration_lut(path: Path) -> Path:
    rows = [f"{blue} {green * 0.5} {red}"
            for blue in (0, 1) for green in (0, 1) for red in (0, 1)]
    path.write_text("LUT_3D_SIZE 2\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_ffmpeg_monitor_calibrates_display_rgb_before_transport(tmp_path: Path) -> None:
    lut = _calibration_lut(tmp_path / "display.cube")
    config = DeckLinkMonitorConfig(width=1, height=1, input_pix_fmt="uyvy422", calibration_lut=str(lut))
    output = FfmpegDeckLinkMonitorOutput(config)
    output._queue.put_nowait((4, np.array([[[26, 100, 204]]], dtype=np.uint8)))

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(output._drain_latest_frame).result()

    assert output._last_frame == bytes((204, 50, 26, 255))
    assert output._last_frame_index == 4
    command = build_ffmpeg_decklink_command(config)
    assert command[command.index("-pix_fmt") + 1] == "bgra"


def test_monitor_reloads_calibration_when_reselecting_an_overwritten_file(tmp_path: Path) -> None:
    lut = _calibration_lut(tmp_path / "display.cube")
    output = FfmpegDeckLinkMonitorOutput(DeckLinkMonitorConfig(width=1, height=1, calibration_lut=str(lut)))
    payload = np.array([[[26, 100, 204]]], dtype=np.uint8)
    with ThreadPoolExecutor(max_workers=1) as executor:
        output._queue.put_nowait((4, payload))
        executor.submit(output._drain_latest_frame).result()
        assert output._last_frame == bytes((204, 50, 26, 255))
        lut.write_text("LUT_3D_SIZE 2\n" + "0.50 0.25 0.00\n" * 8, encoding="utf-8")
        output.reload_calibration()
        output._queue.put_nowait((5, payload))
        executor.submit(output._drain_latest_frame).result()

    assert output._last_frame == bytes((0, 64, 128, 255))
    assert output._last_frame_index == 5


def test_monitor_with_unavailable_calibration_retains_last_calibrated_frame(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    config = DeckLinkMonitorConfig(width=1, height=1, calibration_lut=str(tmp_path / "missing.cube"))
    output = FfmpegDeckLinkMonitorOutput(config)
    output._last_frame = bytes((1, 2, 3, 255))
    output._last_frame_index = 1
    output._queue.put_nowait((4, np.array([[[26, 100, 204]]], dtype=np.uint8)))

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(output._drain_latest_frame).result()

    assert output._last_frame == bytes((1, 2, 3, 255))
    assert output._last_frame_index == 1
    assert "DeckLink monitor calibration failed" in caplog.text


def test_native_monitor_calibration_disables_gpu_bypass_and_runs_on_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lut = _calibration_lut(tmp_path / "display.cube")
    submitted = Event()
    pixels: list[tuple[bytes, int]] = []

    def submit_bgra(frame: bytes, width: int, height: int, frame_index: int) -> None:
        assert (width, height, frame_index) == (1, 1, 7)
        pixels.append((frame, get_ident()))
        submitted.set()

    def submit_gpu_texture(*args: object) -> None:
        pytest.fail("Active display calibration must use frame readback")

    native = SimpleNamespace(supports_gpu_texture=True, submit_bgra=submit_bgra, submit_gpu_texture=submit_gpu_texture)
    monkeypatch.setitem(sys.modules, "topos_decklink", SimpleNamespace(create_output=lambda **_: native))
    output = NativeDeckLinkSDKMonitorOutput(DeckLinkMonitorConfig(
        device="DeckLink", enabled=True, backend="native_sdk", width=1, height=1,
        native_pixel_format="bgra", calibration_lut=str(lut),
    ))
    assert output.start()
    try:
        assert not output.supports_gpu_texture
        assert not output.submit_gpu_frame(NativeGpuMonitorFrame(7, 44, 1, 1, 0, True))
        assert output.submit_frame_payload(7, np.array([[[26, 100, 204]]], dtype=np.uint8))
        assert submitted.wait(2.0)
        assert pixels[0][0] == bytes((204, 50, 26, 255))
        assert pixels[0][1] != get_ident()
    finally:
        output.stop()


def test_resolve_ffmpeg_path_checks_common_gui_paths(monkeypatch) -> None:
    monkeypatch.setattr(monitor_output.shutil, "which", lambda _path: None)
    monkeypatch.setattr(
        monitor_output.os.path,
        "exists",
        lambda path: path == "/usr/local/bin/ffmpeg",
    )
    monkeypatch.setattr(monitor_output.os, "access", lambda _path, _mode: True)

    assert resolve_ffmpeg_path("ffmpeg") == "/usr/local/bin/ffmpeg"


def test_ffmpeg_supports_decklink_requires_format(monkeypatch) -> None:
    monkeypatch.setattr(monitor_output, "resolve_ffmpeg_path", lambda _path: "/usr/bin/ffmpeg")

    class _Completed:
        stdout = "File formats:\n D  mov\n"
        stderr = ""

    monkeypatch.setattr(monitor_output.subprocess, "run", lambda *args, **kwargs: _Completed())

    assert ffmpeg_supports_decklink("ffmpeg") is False


def test_qimage_to_bgra_frame_letterboxes_and_converts_channels(qapp) -> None:
    image = QImage(4, 2, QImage.Format.Format_RGB888)
    image.fill(QColor(10, 20, 30))

    frame = qimage_to_bgra_frame(image, width=4, height=4)

    assert len(frame) == 4 * 4 * 4
    top_left = frame[0:4]
    center_left = frame[(1 * 4 + 0) * 4 : (1 * 4 + 1) * 4]
    assert top_left == bytes([0, 0, 0, 255])
    assert center_left == bytes([30, 20, 10, 255])


def test_ndarray_to_bgra_frame_converts_bgr_payload() -> None:
    frame_array = np.zeros((2, 4, 3), dtype=np.uint8)
    frame_array[:, :] = [30, 20, 10]

    frame = ndarray_to_bgra_frame(frame_array, width=4, height=4)

    assert len(frame) == 4 * 4 * 4
    top_left = frame[0:4]
    center_left = frame[(1 * 4 + 0) * 4 : (1 * 4 + 1) * 4]
    assert top_left == bytes([0, 0, 0, 255])
    assert center_left == bytes([30, 20, 10, 255])


def test_monitor_rgba_frame_converts_and_flips_gl_orientation() -> None:
    rgba = np.array(
        [
            [[255, 0, 0, 255], [0, 255, 0, 255]],
            [[0, 0, 255, 255], [255, 255, 255, 255]],
        ],
        dtype=np.uint8,
    )

    frame = monitor_payload_to_bgra_frame(
        MonitorRGBAFrame(rgba.tobytes(), 2, 2, is_gl_oriented=True),
        width=2,
        height=2,
    )

    assert frame[0:4] == bytes([255, 0, 0, 255])
    assert frame[4:8] == bytes([255, 255, 255, 255])
    assert frame[8:12] == bytes([0, 0, 255, 255])
    assert frame[12:16] == bytes([0, 255, 0, 255])


def test_monitor_rgba_frame_cover_mode_center_crops_without_letterbox() -> None:
    rgba = np.array(
        [
            [[255, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255], [255, 255, 255, 255]],
            [[255, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255], [255, 255, 255, 255]],
        ],
        dtype=np.uint8,
    )

    frame = monitor_payload_to_bgra_frame(
        MonitorRGBAFrame(rgba.tobytes(), 4, 2, is_gl_oriented=False, fit_mode="cover"),
        width=2,
        height=2,
    )

    assert len(frame) == 2 * 2 * 4
    assert frame[0:4] == bytes([0, 255, 0, 255])
    assert frame[4:8] == bytes([255, 0, 0, 255])
    assert frame[8:12] == bytes([0, 255, 0, 255])
    assert frame[12:16] == bytes([255, 0, 0, 255])


def test_program_monitor_controller_submits_only_qimage(qapp) -> None:
    image = QImage(2, 2, QImage.Format.Format_RGB888)
    image.fill(QColor(255, 0, 0))

    backend = SimpleNamespace(
        is_running=True,
        start=lambda: True,
        stop=lambda: None,
        submit_qimage=lambda frame, qimage: (frame, qimage.width(), qimage.height()),
    )
    controller = ProgramMonitorOutputController(
        DeckLinkMonitorConfig(enabled=True, device="DeckLink"),
        backend=backend,
    )

    assert controller.submit_frame(12, image) == (12, 2, 2)
    assert controller.submit_frame(13, (1, 1920, 1080)) is False


def test_program_monitor_controller_submits_video_payload() -> None:
    payload = np.zeros((2, 2, 3), dtype=np.uint8)
    calls = []

    backend = SimpleNamespace(
        is_running=True,
        start=lambda: True,
        stop=lambda: None,
        submit_frame_payload=lambda frame, item: calls.append((frame, item.shape[1], item.shape[0])) or True,
    )
    controller = ProgramMonitorOutputController(
        DeckLinkMonitorConfig(enabled=True, device="DeckLink"),
        backend=backend,
    )

    assert controller.submit_video_frame(14, payload) is True
    assert calls == [(14, 2, 2)]


def test_pbo_rgba_to_qimage_flips_gl_oriented_frame(qapp) -> None:
    rgba = np.array(
        [
            [[255, 0, 0, 255], [0, 255, 0, 255]],
            [[0, 0, 255, 255], [255, 255, 255, 255]],
        ],
        dtype=np.uint8,
    )

    image = ProgramViewer._pbo_rgba_to_qimage(rgba.tobytes(), 2, 2, True)

    assert image is not None
    assert image.pixelColor(0, 0) == QColor(0, 0, 255)
    assert image.pixelColor(1, 1) == QColor(0, 255, 0)


def test_native_decklink_backend_submits_gpu_texture(monkeypatch) -> None:
    calls = []

    class _NativeOutput:
        supports_gpu_texture = True

        def start(self) -> None:
            calls.append(("start",))

        def submit_gpu_texture(self, *args) -> None:
            calls.append(("gpu", args))

        def stop(self) -> None:
            calls.append(("stop",))

    module = SimpleNamespace(
        create_output=lambda **kwargs: calls.append(("create", kwargs)) or _NativeOutput()
    )
    monkeypatch.setitem(sys.modules, "topos_decklink", module)

    backend = NativeDeckLinkSDKMonitorOutput(
        DeckLinkMonitorConfig(enabled=True, device="DeckLink", backend="native_sdk")
    )

    assert backend.start() is True
    assert backend.submit_gpu_frame(
        NativeGpuMonitorFrame(
            frame_index=7,
            texture_id=44,
            width=1920,
            height=1080,
            fence_ns=0,
            is_gl_oriented=True,
        )
    ) is True
    backend.stop()

    assert calls[0][0] == "create"
    assert calls[1] == ("start",)
    assert calls[2] == ("gpu", (44, 1920, 1080, 7, 0, True))
    assert calls[3] == ("stop",)


def test_native_decklink_backend_submits_qimage_off_ui_thread(monkeypatch, qapp) -> None:
    calls = []

    class _NativeOutput:
        supports_gpu_texture = False

        def start(self) -> None:
            calls.append(("start",))

        def submit_bgra(self, frame, width, height, frame_index) -> None:
            calls.append(("bgra", len(frame), width, height, frame_index))

        def stop(self) -> None:
            calls.append(("stop",))

    module = SimpleNamespace(
        create_output=lambda **kwargs: calls.append(("create", kwargs)) or _NativeOutput()
    )
    monkeypatch.setitem(sys.modules, "topos_decklink", module)

    backend = NativeDeckLinkSDKMonitorOutput(
        DeckLinkMonitorConfig(
            enabled=True,
            device="DeckLink",
            backend="native_sdk",
            width=4,
            height=4,
        )
    )
    image = QImage(4, 4, QImage.Format.Format_RGB888)
    image.fill(QColor(20, 30, 40))

    assert backend.start() is True
    assert backend.submit_qimage(3, image) is True
    assert not any(call[0] == "bgra" for call in calls)

    import time

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(call[0] == "bgra" for call in calls):
        time.sleep(0.01)

    backend.stop()

    assert ("bgra", 4 * 4 * 4, 4, 4, 3) in calls


def test_native_decklink_backend_submits_ndarray_off_ui_thread(monkeypatch) -> None:
    calls = []

    class _NativeOutput:
        supports_gpu_texture = False

        def start(self) -> None:
            calls.append(("start",))

        def submit_bgra(self, frame, width, height, frame_index) -> None:
            calls.append(("bgra", len(frame), width, height, frame_index))

        def stop(self) -> None:
            calls.append(("stop",))

    module = SimpleNamespace(
        create_output=lambda **kwargs: calls.append(("create", kwargs)) or _NativeOutput()
    )
    monkeypatch.setitem(sys.modules, "topos_decklink", module)

    backend = NativeDeckLinkSDKMonitorOutput(
        DeckLinkMonitorConfig(
            enabled=True,
            device="DeckLink",
            backend="native_sdk",
            width=4,
            height=4,
        )
    )
    payload = np.zeros((2, 4, 3), dtype=np.uint8)
    payload[:, :] = [30, 20, 10]

    assert backend.start() is True
    assert backend.submit_frame_payload(5, payload) is True

    import time

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(call[0] == "bgra" for call in calls):
        time.sleep(0.01)

    backend.stop()

    assert ("bgra", 4 * 4 * 4, 4, 4, 5) in calls


def test_program_monitor_controller_uses_native_backend() -> None:
    controller = ProgramMonitorOutputController(
        DeckLinkMonitorConfig(enabled=True, device="DeckLink", backend="native_sdk")
    )

    assert isinstance(controller.backend, NativeDeckLinkSDKMonitorOutput)


def test_program_monitor_controller_falls_back_to_native_backend(monkeypatch) -> None:
    class _FfmpegBackend:
        is_running = False

        def __init__(self, config):
            self.config = config

        def start(self):
            return False

        def stop(self):
            pass

    class _NativeBackend:
        is_running = True
        supports_gpu_texture = False

        def __init__(self, config):
            self.config = config

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(monitor_output, "FfmpegDeckLinkMonitorOutput", _FfmpegBackend)
    monkeypatch.setattr(monitor_output, "NativeDeckLinkSDKMonitorOutput", _NativeBackend)

    controller = ProgramMonitorOutputController(
        DeckLinkMonitorConfig(enabled=True, device="DeckLink", backend="ffmpeg")
    )

    assert controller.start() is True
    assert isinstance(controller.backend, _NativeBackend)
    assert controller.config.backend == "ffmpeg"
