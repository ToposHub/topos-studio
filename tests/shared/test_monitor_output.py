from __future__ import annotations

from types import SimpleNamespace
import sys

from PyQt6.QtGui import QColor, QImage
import numpy as np

from src.shared.video import monitor_output
from src.shared.video.monitor_output import (
    DeckLinkMonitorConfig,
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
