from __future__ import annotations


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
