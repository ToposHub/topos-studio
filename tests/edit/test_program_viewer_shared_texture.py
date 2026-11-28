from __future__ import annotations

from unittest.mock import Mock

from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot


def test_panned_viewer_uses_current_framebuffer_size(qtbot: QtBot, monkeypatch: MonkeyPatch) -> None:
    from src.features.edit.ui import program_viewer

    viewer = program_viewer.ProgramViewer()
    qtbot.addWidget(viewer)
    viewer.resize(322, 242)
    viewer._gl_initialized = True
    viewer._current_tex_id = 7
    viewer._prog = 1
    viewer.set_zoom(1.67)
    viewer.set_pan(0.65, 0.4)
    logical_rect = viewer._calculate_display_rect()

    gl = Mock()
    draw = Mock(return_value=False)
    monkeypatch.setattr(program_viewer, "_gl", gl)
    monkeypatch.setattr(viewer, "_render_texture", draw)
    monkeypatch.setattr(viewer, "_paint_overlays", Mock())

    for dpr, dimensions in ((1.0, (322, 242)), (2.0, (644, 484)), (1.25, (403, 303))):
        monkeypatch.setattr(viewer, "devicePixelRatioF", lambda dpr=dpr: dpr)
        viewer._paintGL_impl()
        gl.glViewport.assert_called_with(0, 0, *dimensions)
        draw.assert_called_with(logical_rect)


def test_lease_identifies_only_a_successfully_drawn_texture(qapp, monkeypatch) -> None:
    from src.features.edit.ui import program_viewer

    viewer = program_viewer.ProgramViewer()
    key = object()
    viewer._gl_initialized = True
    viewer._current_tex_id = 7
    viewer._prog = 1
    viewer._copied_present_key = key
    monkeypatch.setattr(viewer, "isVisible", lambda: True)
    monkeypatch.setattr(viewer, "_native_layer_covers", lambda: False)
    monkeypatch.setattr(viewer, "_reset_copy_gl_state", Mock())
    monkeypatch.setattr(viewer, "_paint_overlays", Mock())
    monkeypatch.setattr(program_viewer, "_gl", Mock())
    draw = Mock(return_value=True)
    monkeypatch.setattr(viewer, "_render_texture", draw)
    frames: list[int] = []
    viewer.frame_presented.connect(frames.append)
    viewer._texture_frame_index = 7
    viewer.set_clip_info("incoming", 20)

    assert viewer.painted_present_key() is None
    viewer._paintGL_impl()
    assert viewer.painted_present_key() is key
    assert frames == [7]

    draw.return_value = False
    viewer._paintGL_impl()
    assert viewer.painted_present_key() is None

    viewer.set_texture(9, 1920, 1080)
    draw.return_value = True
    viewer._paintGL_impl()
    assert viewer.painted_present_key() is None


def test_reused_shared_texture_id_still_repaints(qapp) -> None:
    """A reused GPU texture slot can contain a newer playback frame."""
    from src.features.edit.ui.program_viewer import ProgramViewer

    class TrackingProgramViewer(ProgramViewer):
        def __init__(self) -> None:
            super().__init__()
            self.update_calls = 0

        def update(self) -> None:  # type: ignore[override]
            self.update_calls += 1

    viewer = TrackingProgramViewer()

    viewer.set_shared_texture(7, 1920, 1080)
    first_update_count = viewer.update_calls
    viewer.set_shared_texture(7, 1920, 1080)

    assert viewer.update_calls > first_update_count


def test_direction_turn_releases_only_unpainted_old_scrub_slot(qapp, monkeypatch) -> None:
    from src.features.edit.core.edit_gpu_preview_renderer import ScrubSourceFrame
    from src.features.edit.ui import program_viewer
    from src.shared.gpu.present_frame import GpuPresentFrame

    viewer = program_viewer.ProgramViewer()
    frames = (ScrubSourceFrame('top', 'clip', 207, 203, 1),)
    already_drawn = GpuPresentFrame(8, 640, 360, 178, 4, 2, extra={
        'scrub_request_token': 10,
        'scrub_source_frames': frames,
    })
    old_pending = GpuPresentFrame(9, 640, 360, 192, 4, 3, extra={'scrub_request_token': 11})
    viewer._gl_initialized = True
    viewer._current_tex_id = 7
    viewer._prog = 1
    monkeypatch.setattr(viewer, '_native_layer_covers', lambda: False)
    monkeypatch.setattr(viewer, '_reset_copy_gl_state', Mock())
    monkeypatch.setattr(viewer, '_paint_overlays', Mock())
    monkeypatch.setattr(viewer, 'defaultFramebufferObject', lambda: 0)
    monkeypatch.setattr(program_viewer, '_gl', Mock())
    monkeypatch.setattr(viewer, '_render_texture', Mock(return_value=True))
    drawn_ack = Mock()
    viewer.set_present_frame(already_drawn, ack_cb=drawn_ack)
    viewer._paintGL_impl()
    drawn_ack.assert_called_once_with(2)
    ack = Mock()
    viewer.set_present_frame(old_pending, ack_cb=ack)
    viewer._current_frame_index = 192

    assert viewer.retire_scrub_direction(4, 12) == (178, frames)
    ack.assert_called_once_with(3)
    assert viewer._pending_present_frame is None and viewer._shared_tex_id is None
    assert viewer._current_tex_id == 7 and viewer._current_frame_index == 178

    next_frame = GpuPresentFrame(10, 640, 360, 177, 4, 4, extra={'scrub_request_token': 12})
    ack.reset_mock()
    viewer.set_present_frame(next_frame, ack_cb=ack)
    assert viewer.retire_scrub_direction(4, 12) == (178, frames)
    assert viewer._pending_present_frame is next_frame
    ack.assert_not_called()
    viewer._ack_pending_present_without_display()


def test_no_clip_state_preserves_existing_frame(qapp) -> None:
    """Transient no-clip results must not expose an empty GL surface."""
    from src.features.edit.ui.program_viewer import ProgramViewer

    class TrackingProgramViewer(ProgramViewer):
        def __init__(self) -> None:
            super().__init__()
            self.clear_calls = 0
            self.update_calls = 0

        def clear_texture(self) -> None:  # type: ignore[override]
            self.clear_calls += 1

        def update(self) -> None:  # type: ignore[override]
            self.update_calls += 1

    viewer = TrackingProgramViewer()
    viewer._shared_tex_id = 7
    viewer._shared_tex_width = 1920
    viewer._shared_tex_height = 1080

    viewer.set_error_state("no_clip")

    assert viewer.clear_calls == 0
    assert viewer._shared_tex_id == 7
    assert viewer._error_state == "no_clip"
    assert viewer.update_calls == 1


def test_program_viewer_is_passive_display(qapp) -> None:
    """ProgramViewer must not own a second frame-request pipeline."""
    from src.features.edit.ui.program_viewer import ProgramViewer, ProgramViewerWidget

    viewer = ProgramViewer()
    widget = ProgramViewerWidget()

    assert not hasattr(viewer, "frame_requested")
    assert not hasattr(widget, "frame_requested")
    assert not hasattr(viewer, "_frame_request_timer")

    viewer.set_clip_info("clip-001", 42)

    assert viewer._current_clip_id == "clip-001"
    assert viewer._current_frame_index == 42
    assert not hasattr(viewer, "_pending_clip_id")


def test_program_viewer_gl_wrapper_does_not_bind_pyopengl_functions(qapp) -> None:
    """PyOpenGL functions must not receive _GLWrapper as an implicit self."""
    from src.features.edit.ui import program_viewer

    assert not hasattr(program_viewer._gl.glReadPixels, "__self__")
    assert not hasattr(program_viewer._gl.glGetBufferSubData, "__self__")
