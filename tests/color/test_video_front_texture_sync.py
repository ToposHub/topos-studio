"""Regression tests for Color's cross-context video presentation fence."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from OpenGL.GL import GL_TIMEOUT_IGNORED

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.image_engine import ImageEngine


def test_video_front_fence_flushes_and_retires_the_previous_fence() -> None:
    class _Renderer:
        _retire_front_present_fence = AsyncRenderer._retire_front_present_fence

    renderer = _Renderer()
    renderer.current_front_fence = 11
    renderer.ctx = Mock()
    renderer._finish_call_count = 0

    with (
        patch('OpenGL.GL.glFenceSync', return_value=22) as fence_sync,
        patch('OpenGL.GL.glFlush') as flush,
        patch('OpenGL.GL.glDeleteSync') as delete_sync,
    ):
        result = AsyncRenderer._publish_front_present_fence(renderer)

    assert result == 22
    assert renderer.current_front_fence == 22
    fence_sync.assert_called_once()
    flush.assert_called_once()
    delete_sync.assert_called_once_with(11)
    renderer.ctx.finish.assert_not_called()


def test_video_front_fence_preserves_opaque_glsync_object() -> None:
    class _OpaqueSync:
        def __bool__(self) -> bool:
            return True

        def __int__(self) -> int:
            raise TypeError('opaque GLsync cannot be converted to int')

    class _Renderer:
        _retire_front_present_fence = AsyncRenderer._retire_front_present_fence

    opaque_sync = _OpaqueSync()
    renderer = _Renderer()
    renderer.current_front_fence = None
    renderer.ctx = Mock()
    renderer._finish_call_count = 0

    with (
        patch('OpenGL.GL.glFenceSync', return_value=opaque_sync),
        patch('OpenGL.GL.glFlush'),
    ):
        result = AsyncRenderer._publish_front_present_fence(renderer)

    assert result is opaque_sync
    assert renderer.current_front_fence is opaque_sync
    renderer.ctx.finish.assert_not_called()

    renderer.current_front_tex_id = 42
    with patch('OpenGL.GL.glWaitSync') as wait_sync:
        should_draw = ImageEngine._wait_for_front_texture_fence(renderer, 42)
    assert should_draw is True
    wait_sync.assert_called_once_with(opaque_sync, 0, GL_TIMEOUT_IGNORED)

    with patch('OpenGL.GL.glDeleteSync') as delete_sync:
        AsyncRenderer._retire_front_present_fence(renderer)
    delete_sync.assert_called_once_with(opaque_sync)
    assert renderer.current_front_fence is None


def test_video_front_fence_falls_back_to_finish_when_sync_is_unavailable() -> None:
    class _Renderer:
        _retire_front_present_fence = AsyncRenderer._retire_front_present_fence

    renderer = _Renderer()
    renderer.current_front_fence = None
    renderer.ctx = Mock()
    renderer._finish_call_count = 0

    with (
        patch('OpenGL.GL.glFenceSync', return_value=0),
        patch('OpenGL.GL.glFlush') as flush,
    ):
        result = AsyncRenderer._publish_front_present_fence(renderer)

    assert result is None
    assert renderer.current_front_fence is None
    flush.assert_called_once()
    renderer.ctx.finish.assert_called_once()
    assert renderer._finish_call_count == 1


def test_video_front_fence_flush_failure_finishes_and_deletes_sync() -> None:
    class _Renderer:
        _retire_front_present_fence = AsyncRenderer._retire_front_present_fence

    opaque_sync = object()
    renderer = _Renderer()
    renderer.current_front_fence = None
    renderer.ctx = Mock()
    renderer._finish_call_count = 0

    with (
        patch('OpenGL.GL.glFenceSync', return_value=opaque_sync),
        patch('OpenGL.GL.glFlush', side_effect=RuntimeError('flush failed')),
        patch('OpenGL.GL.glDeleteSync') as delete_sync,
    ):
        result = AsyncRenderer._publish_front_present_fence(renderer)

    assert result is None
    assert renderer.current_front_fence is None
    renderer.ctx.finish.assert_called_once()
    delete_sync.assert_called_once_with(opaque_sync)


def test_ui_waits_for_the_matching_published_front_texture() -> None:
    renderer = SimpleNamespace(current_front_tex_id=42, current_front_fence=99)

    with patch('OpenGL.GL.glWaitSync') as wait_sync:
        should_draw = ImageEngine._wait_for_front_texture_fence(renderer, 42)

    assert should_draw is True
    wait_sync.assert_called_once_with(99, 0, GL_TIMEOUT_IGNORED)


def test_ui_does_not_wait_for_a_fence_from_a_different_front_texture() -> None:
    renderer = SimpleNamespace(current_front_tex_id=42, current_front_fence=99)

    with patch('OpenGL.GL.glWaitSync') as wait_sync:
        should_draw = ImageEngine._wait_for_front_texture_fence(renderer, 41)

    assert should_draw is False
    wait_sync.assert_not_called()


def test_ui_draws_current_texture_when_fence_wait_is_unavailable() -> None:
    renderer = SimpleNamespace(current_front_tex_id=42, current_front_fence=99)

    with patch('OpenGL.GL.glWaitSync', side_effect=RuntimeError('unsupported')):
        should_draw = ImageEngine._wait_for_front_texture_fence(renderer, 42)

    assert should_draw is True


def test_ui_presents_current_texture_when_playback_replaces_captured_texture() -> None:
    class _Mutex:
        def __init__(self) -> None:
            self.unlocked = False

        def tryLock(self) -> bool:
            return True

        def unlock(self) -> None:
            self.unlocked = True

    mutex = _Mutex()
    engine = SimpleNamespace(
        gl_manager=SimpleNamespace(mutex=mutex),
        renderer=SimpleNamespace(current_front_tex_id=42, current_front_fence=99),
        _wait_for_front_texture_fence=ImageEngine._wait_for_front_texture_fence,
        _do_draw_background_tex_id=Mock(),
    )

    with patch('OpenGL.GL.glWaitSync'):
        did_draw = ImageEngine._draw_background_tex_id(
            engine,
            41,
            (1920, 1080),
        )

    assert did_draw is True
    engine._do_draw_background_tex_id.assert_called_once_with(
        42,
        (1920, 1080),
        None,
        0.0,
        False,
        None,
    )
    assert mutex.unlocked is True


def test_sync_gpu_none_reports_incomplete_without_blocking() -> None:
    renderer = SimpleNamespace(
        video_render_sync_mode='none',
        ctx=Mock(),
        _finish_call_count=0,
    )

    render_complete = AsyncRenderer._sync_gpu(renderer)

    assert render_complete is False
    renderer.ctx.finish.assert_not_called()
    assert renderer._finish_call_count == 0


def test_sync_gpu_finish_reports_complete() -> None:
    renderer = SimpleNamespace(
        video_render_sync_mode='finish',
        ctx=Mock(),
        _finish_call_count=0,
    )

    render_complete = AsyncRenderer._sync_gpu(renderer)

    assert render_complete is True
    renderer.ctx.finish.assert_called_once()
    assert renderer._finish_call_count == 1


def test_incomplete_paused_grading_front_publishes_fence() -> None:
    class _Renderer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def _publish_front_present_fence(self) -> int:
            self.calls.append('publish')
            return 123

        def _retire_front_present_fence(self) -> None:
            self.calls.append('retire')

    renderer = _Renderer()

    AsyncRenderer._update_front_present_sync(
        renderer,
        render_complete=False,
    )

    assert renderer.calls == ['publish']


def test_completed_front_retires_obsolete_fence() -> None:
    class _Renderer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def _publish_front_present_fence(self) -> int:
            self.calls.append('publish')
            return 123

        def _retire_front_present_fence(self) -> None:
            self.calls.append('retire')

    renderer = _Renderer()

    AsyncRenderer._update_front_present_sync(
        renderer,
        render_complete=True,
    )

    assert renderer.calls == ['retire']
