"""Injected GL failures retain imported sources until their read fence ends."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QThread

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer, EditRenderTask
from src.shared.gpu.gl_backend import GLContextManager
from src.shared.gpu.present_frame import SlotState, TextureLease
from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFramePlane, GpuFrameRelease


class _Lane:
    def __init__(self) -> None:
        self.locked = False

    def lock(self) -> None:
        assert not self.locked
        self.locked = True

    def unlock(self) -> None:
        assert self.locked
        self.locked = False


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> tuple[EditGpuPreviewRenderer, _Lane]:
    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    QThread.__init__(renderer)
    lane = _Lane()
    monkeypatch.setattr(GLContextManager, 'instance', lambda: SimpleNamespace(mutex=lane))
    renderer._previous_gpu_frames = []
    renderer._deferred_gpu_frame_releases = []
    renderer._current_present_lease = None
    renderer._yuv_uploader = None
    renderer._gl_pass_timer = SimpleNamespace(unsupported=True)
    renderer._snapshot_armed = False
    renderer._drain_composite_cache_invalidate = lambda: None
    renderer._drain_retired_output_pools = lambda: None
    renderer._drain_deferred_gpu_frame_releases = lambda **kwargs: None
    texture = SimpleNamespace(glo=20)
    renderer._ensure_track_fbo = lambda *args: (object(), texture)
    renderer._wrap_external_gpu_planes = lambda *args: (object(), object())
    renderer._render_yuv_to_track_fbo = lambda *args: None
    renderer._apply_crop = lambda tex, *args: tex
    renderer._apply_preview_grade = lambda tex, *args: tex
    renderer._apply_clip_effects = lambda tex, *args, **kwargs: tex
    renderer._preblend_transition_tracks = lambda *args: None
    renderer._composite_tracks = lambda *args, **kwargs: texture

    def acquire(*args, **kwargs) -> bool:
        lease = TextureLease(0, 20, 64, 32)
        lease.begin_writing(1, 88)
        renderer._current_present_lease = lease
        return True

    def finalize(**kwargs) -> object:
        renderer._current_present_lease.mark_ready(0)
        renderer._current_present_lease = None
        return object()

    def fence() -> int:
        assert lane.locked
        return 71

    renderer._ensure_composite_fbo = acquire
    renderer._finalize_present_frame = finalize
    renderer._create_source_release_fence = fence
    return renderer, lane


def _frame(released: list[int], index: int, source: Mock | None = None) -> GpuDecodedFrame:
    def release() -> None:
        released.append(index)
        if source is not None:
            source.release()

    return GpuDecodedFrame(
        frame_index=index, pts=None, time_seconds=0.0, width=64, height=32,
        visible_rect=None, pixel_format='nv12', bit_depth=8,
        planes=(GpuFramePlane('Y', 'opengl', 10, 'rectangle', 64, 32),
                GpuFramePlane('UV', 'opengl', 11, 'rectangle', 32, 16)),
        release=GpuFrameRelease(release_callback=release),
    )


@pytest.mark.parametrize('stage', ['yuv', 'grade', 'effects'])
@pytest.mark.parametrize('source_kind', ['vt_pixel_buffer', 'cuda_resident'])
def test_imported_frame_survives_failed_pass_until_fence_completion(
    renderer, stage: str, source_kind: str,
) -> None:
    renderer, lane = renderer
    source = Mock()
    releases: list[int] = []
    imported = _frame(releases, 88, source)
    renderer._import_vt_pixel_buffer_frame = lambda frame: imported
    renderer._import_cuda_resident_frame = lambda frame, **kwargs: imported
    task = EditRenderTask(88, 1, [{'frame_type': source_kind, source_kind: source}])

    def fail(*args, **kwargs):
        assert lane.locked
        assert renderer._previous_gpu_frames == [imported]
        raise RuntimeError('synthetic glUseProgram failure')

    setattr(renderer, {'yuv': '_render_yuv_to_track_fbo', 'grade': '_apply_preview_grade',
                       'effects': '_apply_clip_effects'}[stage], fail)
    with pytest.raises(RuntimeError, match='synthetic glUseProgram failure'):
        renderer._process_task_with_gpu_lane(task)
    assert not lane.locked
    assert task.layers[0]['gpu_frame'] is imported
    assert renderer._current_present_lease is None
    assert renderer._previous_gpu_frames == []
    assert renderer._deferred_gpu_frame_releases == [(71, [imported])]
    assert not imported.release.is_released
    source.release.assert_not_called()
    renderer._release_gpu_frame_list(renderer._deferred_gpu_frame_releases.pop()[1])
    assert releases == [88]
    source.release.assert_called_once()


def test_failure_after_first_track_fences_both_and_releases_unstarted_source(renderer) -> None:
    renderer, _ = renderer
    releases: list[int] = []
    first, second = _frame(releases, 1), _frame(releases, 2)
    untouched = Mock()
    task = EditRenderTask(88, 1, [
        {'frame_type': 'gpu', 'gpu_frame': first},
        {'frame_type': 'gpu', 'gpu_frame': second},
        {'frame_type': 'vt_pixel_buffer', 'vt_pixel_buffer': untouched},
    ])

    def grade(tex, index, *args):
        if index == 1:
            raise RuntimeError('second track grade failed')
        return tex

    renderer._apply_preview_grade = grade
    with pytest.raises(RuntimeError, match='second track grade failed'):
        renderer._process_task_with_gpu_lane(task)
    assert releases == []
    assert renderer._deferred_gpu_frame_releases == [(71, [first, second])]
    untouched.release.assert_called_once()
    renderer._release_gpu_frame_list(renderer._deferred_gpu_frame_releases.pop()[1])
    assert releases == [1, 2]


def test_success_publishes_once_and_releases_imported_frame_once(renderer) -> None:
    renderer, _ = renderer
    releases: list[int] = []
    source = Mock()
    imported = _frame(releases, 88, source)
    renderer._import_vt_pixel_buffer_frame = lambda frame: imported
    emitted = Mock()
    renderer.frame_ready.connect(emitted)
    renderer._process_task_with_gpu_lane(EditRenderTask(
        88, 1, [{'frame_type': 'vt_pixel_buffer', 'vt_pixel_buffer': source}],
    ))
    emitted.assert_called_once()
    assert releases == []
    renderer._release_gpu_frame_list(renderer._deferred_gpu_frame_releases.pop()[1])
    assert releases == [88]
    source.release.assert_called_once()


def _metal_preview_task() -> tuple[EditRenderTask, Mock]:
    source = Mock(pixel_buffer=object(), pixel_format='nv12', bit_depth=8,
                  width=64, height=32, color_matrix='bt709', color_range='limited',
                  color_primaries='bt709', color_transfer='bt709')
    return EditRenderTask(88, 1, [{
        'frame_type': 'vt_pixel_buffer', 'vt_pixel_buffer': source,
    }], output_fbo_size=(64, 32)), source


def test_metal_preview_publishes_gl_slot_and_fences_shared_surface(renderer) -> None:
    worker, _ = renderer
    task, source = _metal_preview_task()
    slot, release = object(), GpuFrameRelease(release_callback=Mock())
    worker._metal_preview_mode = "all"
    worker._metal_preview = Mock()
    worker._metal_preview.render.return_value = (slot, release)
    worker._composite_fbo = object()
    worker._composite_tex = SimpleNamespace(glo=20)
    worker._import_vt_pixel_buffer_frame = Mock()
    worker._note_zero_copy_import = Mock()
    emitted = Mock()
    worker.frame_ready.connect(emitted)

    worker._process_task_with_gpu_lane(task)

    worker._metal_preview.render.assert_called_once_with(source, (64, 32), lut=None)
    worker._metal_preview.blit.assert_called_once_with(slot, worker._composite_fbo)
    worker._import_vt_pixel_buffer_frame.assert_not_called()
    emitted.assert_called_once_with(88, 1, 20, 64, 32)
    source.release.assert_called_once()
    assert task.layers[0]['vt_pixel_buffer'] is None
    assert worker._deferred_gpu_frame_releases == [(71, [release])]
    assert not release.is_released
    worker._release_gpu_frame_list(worker._deferred_gpu_frame_releases.pop()[1])
    assert release.is_released


def test_metal_preview_blit_failure_preserves_source_for_gl_retry(renderer) -> None:
    worker, _ = renderer
    task, source = _metal_preview_task()
    slot, release = object(), GpuFrameRelease(release_callback=Mock())
    worker._metal_preview_mode = "all"
    worker._metal_preview = Mock()
    worker._metal_preview.render.return_value = (slot, release)
    worker._metal_preview.blit.side_effect = RuntimeError('surface blit failed')
    worker._composite_fbo = object()
    releases: list[int] = []
    imported = _frame(releases, 88, source)
    worker._import_vt_pixel_buffer_frame = Mock(return_value=imported)

    worker._process_task_with_gpu_lane(task)

    worker._import_vt_pixel_buffer_frame.assert_called_once_with(source)
    assert worker._metal_preview_mode == "off"
    source.release.assert_not_called()
    assert worker._deferred_gpu_frame_releases == [(71, [release, imported])]
    worker._release_gpu_frame_list(worker._deferred_gpu_frame_releases.pop()[1])
    assert release.is_released
    source.release.assert_called_once()


@pytest.mark.parametrize('track_info', [
    {'opacity': 0.5}, {'crop': (0.1, 0.0, 0.0, 0.0, 0.0)},
    {'transform': (5.0, 0.0, 1.0, 1.0, 0.0, 'center', 0.0, 0.0)},
    {'preview_grade': {'color_graph': object()}}, {'clip_effects': (object(),)},
])
def test_metal_preview_skips_modified_layers(renderer, track_info: dict) -> None:
    worker, _ = renderer
    task, _ = _metal_preview_task()
    task.layers[0]['track_info'] = track_info
    worker._metal_preview_mode = "all"
    worker._metal_preview = Mock()

    assert not worker._try_metal_preview(task, 64, 32)

    worker._metal_preview.render.assert_not_called()


@pytest.mark.parametrize(('platform', 'graphics', 'override', 'expected'), [
    ('darwin', 'auto', '', 'lut'), ('darwin', 'auto', '1', 'all'),
    ('darwin', 'force_opengl', '1', 'off'), ('darwin', 'force_metal', '', 'all'),
    ('darwin', 'metal', '0', 'off'), ('linux', 'metal', '1', 'off'),
])
def test_metal_preview_preference(monkeypatch: pytest.MonkeyPatch, platform: str,
                                  graphics: str, override: str, expected: str) -> None:
    from src.features.edit.core import metal_timeline_preview

    monkeypatch.setattr(metal_timeline_preview.sys, 'platform', platform)
    monkeypatch.setenv('TOPOS_BACKEND_GRAPHICS', graphics)
    monkeypatch.setenv('TOPOS_EDIT_METAL_PREVIEW', override)

    assert metal_timeline_preview.metal_preview_mode() == expected


def test_metal_preview_auto_keeps_ordinary_nv12_on_existing_route(renderer) -> None:
    worker, _ = renderer
    task, _ = _metal_preview_task()
    worker._metal_preview_mode = "lut"
    worker._metal_preview = Mock()

    assert not worker._try_metal_preview(task, 64, 32)
    worker._metal_preview.render.assert_not_called()


def test_cancelled_unstarted_task_releases_source_without_import_or_fence(renderer) -> None:
    renderer, _ = renderer
    source = Mock()
    task = EditRenderTask(88, 1, [{'frame_type': 'vt_pixel_buffer', 'vt_pixel_buffer': source}])
    renderer._release_task_sources(task)
    source.release.assert_called_once()
    assert renderer._deferred_gpu_frame_releases == []


def test_write_abort_only_accepts_unpublished_slot_and_keeps_force_warning(caplog) -> None:
    lease = TextureLease(0, 20, 64, 32)
    lease.begin_writing(1, 88)
    lease.abort_writing()
    assert lease.state is SlotState.FREE
    assert not caplog.records
    lease.begin_writing(1, 89)
    lease.mark_ready(0)
    with pytest.raises(RuntimeError, match='cannot abort_writing in state READY'):
        lease.abort_writing()
    assert lease.state is SlotState.READY
    lease.release()
    lease.begin_writing(1, 90)
    lease.release()
    assert 'force release from WRITING' in caplog.text


def test_error_log_throttles_repeats_and_keeps_first_traceback(renderer, monkeypatch, caplog) -> None:
    renderer, _ = renderer
    task = EditRenderTask(88, 1, [])
    now = [100.0]
    monkeypatch.setattr('src.features.edit.core.edit_gpu_preview_renderer.time.monotonic', lambda: now[0])
    first = RuntimeError('same GL error')
    renderer._log_task_error(task, first)
    renderer._log_task_error(task, RuntimeError('same GL error'))
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info[1] is first
    now[0] += 5.0
    renderer._log_task_error(task, RuntimeError('same GL error'))
    assert 'repeated=1' in caplog.records[-1].message
    renderer._log_task_error(task, ValueError('different failure'))
    assert len(caplog.records) == 3
    renderer._process_task = lambda task: None
    renderer._process_task_with_gpu_lane(task)
    renderer._log_task_error(task, ValueError('different failure'))
    assert len(caplog.records) == 4


@pytest.mark.parametrize('message', ['No GPU frames rendered', 'transition_side_missing',
                                     'gpu_track_render_incomplete'])
def test_nonexception_render_errors_stay_diagnosed_and_every_signal_is_forwarded(
    renderer, monkeypatch, caplog, message: str,
) -> None:
    renderer, _ = renderer
    task = EditRenderTask(88, 1, [])
    now = [100.0]
    monkeypatch.setattr('src.features.edit.core.edit_gpu_preview_renderer.time.monotonic', lambda: now[0])
    signals = Mock()
    renderer.render_error.connect(signals)
    renderer._process_task = lambda task: renderer._emit_task_render_error(task, message)
    renderer._process_task_with_gpu_lane(task)
    renderer._process_task_with_gpu_lane(task)
    assert len(caplog.records) == 1
    assert message in caplog.records[0].message
    assert signals.call_count == 2
    now[0] += 5.0
    renderer._process_task_with_gpu_lane(task)
    assert 'repeated=1' in caplog.records[-1].message
    assert signals.call_count == 3
    renderer._process_task = lambda task: None
    renderer._process_task_with_gpu_lane(task)
    renderer._process_task = lambda task: renderer._emit_task_render_error(task, message)
    renderer._process_task_with_gpu_lane(task)
    assert len(caplog.records) == 3
    assert signals.call_count == 4


@pytest.mark.parametrize('message', ['composite_cache_miss', 'present_slot_unavailable'])
def test_internal_retry_or_backpressure_keeps_signal_without_error_noise(renderer, caplog, message: str) -> None:
    renderer, _ = renderer
    signals = Mock()
    renderer.render_error.connect(signals)
    task = EditRenderTask(88, 1, [])
    renderer._process_task = lambda task: renderer._emit_task_render_error(task, message)
    renderer._process_task_with_gpu_lane(task)
    renderer._process_task_with_gpu_lane(task)
    assert not caplog.records
    assert signals.call_count == 2
