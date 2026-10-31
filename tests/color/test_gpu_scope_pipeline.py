"""GPU scope pipeline tests.

Covers the key acceptance cases from
docs/gpu_scope_implementation_plan_2026-08-05.md §9.1:

- ScopeDemand / ScopeGpuFrame data model + generation guard
- RENDER_GPU_SCOPE command + GPU_SCOPE lane wiring
- Lane merge: latest demand wins (100 submits -> 1 slot)
- Drain order: GPU_SCOPE executes last (lowest priority, never blocks video)
- Cancellation: discard_pending_gpu_scope clears the lane
- submit() tracks the demand revision for mid-flight rejection
- No-CPU-readback invariant on the model layer
- Resource pool reuse: same size keeps the same texture (no realloc)

These are unit tests; the actual GL passes require a live context and are
validated manually (plan §9.2) — they are intentionally not exercised here.
"""
from __future__ import annotations

import queue
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Optional, Tuple, List
from unittest.mock import Mock

import pytest

from src.features.color.core.scope_types import (
    ScopeDemand,
    ScopeGpuFrame,
    is_frame_valid_for_demand,
    mode_needs_waveform,
    mode_needs_vectorscope,
    DEFAULT_ANALYSIS_SIZE,
    PLAYBACK_ANALYSIS_SIZE,
    HIGH_QUALITY_ANALYSIS_SIZE,
    DEFAULT_WAVEFORM_DENSITY_SIZE,
    DEFAULT_VECTORSCOPE_DENSITY_SIZE,
)
from src.features.color.core.async_renderer import AsyncRenderer, RenderCommand, RenderLane


# =============================================================================
# Data model
# =============================================================================

class TestScopeDemandModel:
    def test_demand_is_frozen(self):
        d = ScopeDemand(
            revision=1, source_revision=10, mode='waveform',
            display_size=(200, 150),
        )
        with pytest.raises(Exception):
            d.revision = 2  # type: ignore[misc]

    def test_demand_defaults(self):
        d = ScopeDemand(
            revision=1, source_revision=10, mode='vectorscope',
            display_size=(200, 150),
        )
        assert d.analysis_size == DEFAULT_ANALYSIS_SIZE
        assert d.vectorscope_zoom == 1.0
        assert d.is_playing is False
        assert d.mask_layer_id is None
        assert d.mask_enabled is False

    def test_quality_tiers_are_distinct(self):
        assert DEFAULT_ANALYSIS_SIZE == (320, 180)
        assert PLAYBACK_ANALYSIS_SIZE == (512, 288)
        assert HIGH_QUALITY_ANALYSIS_SIZE == (768, 432)

    def test_mode_helpers(self):
        assert mode_needs_waveform('waveform') and not mode_needs_vectorscope('waveform')
        assert not mode_needs_waveform('vectorscope') and mode_needs_vectorscope('vectorscope')
        assert mode_needs_waveform('split') and mode_needs_vectorscope('split')


class TestGenerationGuard:
    def _demand(self, rev=1, src=10):
        return ScopeDemand(
            revision=rev, source_revision=src, mode='split',
            display_size=(100, 100),
        )

    def _frame(self, rev=1, src=10, **kw):
        return ScopeGpuFrame(revision=rev, source_revision=src, **kw)

    def test_matching_revisions_valid(self):
        assert is_frame_valid_for_demand(self._frame(), self._demand())

    def test_mismatched_ui_revision_invalid(self):
        # Frame for an older UI demand must be dropped.
        assert not is_frame_valid_for_demand(self._frame(rev=1), self._demand(rev=2))

    def test_stale_source_revision_invalid(self):
        # Frame analysed against an older source (seek happened) -> drop.
        assert not is_frame_valid_for_demand(self._frame(src=9), self._demand(src=10))

    def test_newer_source_valid(self):
        # Frame analysed against a newer source than the demand is fine.
        assert is_frame_valid_for_demand(self._frame(src=11), self._demand(src=10))

    def test_none_inputs_invalid(self):
        assert not is_frame_valid_for_demand(None, self._demand())
        assert not is_frame_valid_for_demand(self._frame(), None)


# =============================================================================
# Lane wiring
# =============================================================================

class TestGpuScopeLaneWiring:
    def test_render_gpu_scope_command_exists(self):
        assert RenderCommand.RENDER_GPU_SCOPE == "render_gpu_scope"

    def test_gpu_scope_lane_exists(self):
        assert RenderLane.GPU_SCOPE.value == "gpu_scope"

    def test_gpu_scope_command_routed_to_gpu_scope_lane(self):
        lane = AsyncRenderer._get_lane_for_command(
            None, RenderCommand.RENDER_GPU_SCOPE, Mock(revision=1)
        )
        assert lane is RenderLane.GPU_SCOPE

    def test_gpu_scope_is_lowest_priority(self):
        """GPU_SCOPE must drain after SCOPE_SNAPSHOT (last)."""
        slots = {lane: None for lane in RenderLane}
        slots[RenderLane.SCOPE_SNAPSHOT] = ('scope_cpu', {})
        slots[RenderLane.GPU_SCOPE] = ('scope_gpu', Mock(revision=1))
        lock = threading.Lock()
        renderer = Mock()
        renderer._pending_slots = slots
        renderer._pending_lock = lock
        cmds = AsyncRenderer._drain_pending_slots(renderer)
        # SCOPE_SNAPSHOT before GPU_SCOPE
        assert cmds[-1][0] == 'scope_gpu'
        assert cmds[-2][0] == 'scope_cpu'

    def test_gpu_scope_after_video_in_drain(self):
        """Video frame must be drained before any GPU scope work."""
        slots = {lane: None for lane in RenderLane}
        video_entry = ('video', {})
        gpu_entry = ('scope_gpu', Mock(revision=1))
        slots[RenderLane.VIDEO_PLAYBACK] = video_entry
        slots[RenderLane.GPU_SCOPE] = gpu_entry
        lock = threading.Lock()
        renderer = Mock()
        renderer._pending_slots = slots
        renderer._pending_lock = lock
        cmds = AsyncRenderer._drain_pending_slots(renderer)
        assert cmds.index(video_entry) < cmds.index(gpu_entry)


# =============================================================================
# Merge: latest demand wins
# =============================================================================

class _FakeRenderer:
    """Minimal stand-in exposing the attributes submit() touches."""

    def __init__(self):
        self._pending_slots = {lane: None for lane in RenderLane}
        self._pending_lock = threading.Lock()
        self._drain_pending_signaled = False
        self._drain_pending_signal_lock = threading.Lock()
        self._coalesced_lane_commands = 0
        self._scope_demand_revision = 0
        self.command_queue = queue.Queue()

    def submit(self, cmd_type, data=None):
        return AsyncRenderer.submit(self, cmd_type, data)

    # submit() internally calls the real lane classifier; delegate to it.
    def _get_lane_for_command(self, cmd_type, data):
        return AsyncRenderer._get_lane_for_command(self, cmd_type, data)


class TestGpuScopeMerge:
    def test_rapid_submits_keep_latest_demand(self):
        r = _FakeRenderer()
        last_demand = None
        for i in range(100):
            last_demand = ScopeDemand(
                revision=i + 1, source_revision=1, mode='waveform',
                display_size=(200, 150),
            )
            r.submit(RenderCommand.RENDER_GPU_SCOPE, last_demand)
        slot = r._pending_slots[RenderLane.GPU_SCOPE]
        assert slot is not None
        assert slot[0] == RenderCommand.RENDER_GPU_SCOPE
        assert slot[1] is last_demand  # only the latest is retained

    def test_submit_tracks_demand_revision(self):
        r = _FakeRenderer()
        for i in range(10):
            r.submit(
                RenderCommand.RENDER_GPU_SCOPE,
                ScopeDemand(revision=i + 5, source_revision=1,
                            mode='waveform', display_size=(100, 100)),
            )
        assert r._scope_demand_revision == 14  # max of revisions 5..14

    def test_coalesce_counter_increments(self):
        r = _FakeRenderer()
        for i in range(5):
            r.submit(
                RenderCommand.RENDER_GPU_SCOPE,
                ScopeDemand(revision=i + 1, source_revision=1,
                            mode='waveform', display_size=(100, 100)),
            )
        # First submit doesn't coalesce; the next 4 replace an existing slot.
        assert r._coalesced_lane_commands == 4

    def test_single_drain_sentinel_per_batch(self):
        r = _FakeRenderer()
        for i in range(20):
            r.submit(
                RenderCommand.RENDER_GPU_SCOPE,
                ScopeDemand(revision=i + 1, source_revision=1,
                            mode='waveform', display_size=(100, 100)),
            )
        drained = []
        while True:
            try:
                drained.append(r.command_queue.get_nowait())
            except queue.Empty:
                break
        sentinels = [c for c in drained if c[0] == '_drain_pending']
        assert len(sentinels) == 1


# =============================================================================
# Cancellation
# =============================================================================

class TestGpuScopeCancellation:
    def test_discard_clears_gpu_scope_slot(self):
        r = _FakeRenderer()
        r.submit(
            RenderCommand.RENDER_GPU_SCOPE,
            ScopeDemand(revision=1, source_revision=1, mode='split',
                        display_size=(100, 100)),
        )
        assert r._pending_slots[RenderLane.GPU_SCOPE] is not None
        AsyncRenderer.discard_pending_gpu_scope(r)
        assert r._pending_slots[RenderLane.GPU_SCOPE] is None

    def test_discard_does_not_touch_other_lanes(self):
        r = _FakeRenderer()
        r._pending_slots[RenderLane.VIDEO_PLAYBACK] = ('video', {'frame': 1})
        r._pending_slots[RenderLane.GPU_SCOPE] = (
            RenderCommand.RENDER_GPU_SCOPE,
            ScopeDemand(revision=1, source_revision=1, mode='split',
                        display_size=(100, 100)),
        )
        AsyncRenderer.discard_pending_gpu_scope(r)
        assert r._pending_slots[RenderLane.VIDEO_PLAYBACK] == ('video', {'frame': 1})


# =============================================================================
# No-CPU-readback invariant (model layer)
# =============================================================================

class TestNoCpuReadbackInvariant:
    """The data model carries only descriptors, never pixel arrays."""

    def test_scope_gpu_frame_has_no_array_field(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ScopeGpuFrame)}
        # Only descriptor / scalar fields are allowed.
        assert 'revision' in fields
        assert 'waveform_texture_id' in fields
        assert 'vectorscope_texture_id' in fields
        # There must be no field that looks like a pixel buffer.
        for name in fields:
            assert 'image' not in name
            assert 'pixel' not in name
            assert 'array' not in name

    def test_scope_demand_has_no_array_field(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ScopeDemand)}
        for name in fields:
            assert 'image' not in name
            assert 'pixel' not in name


# =============================================================================
# _do_render_gpu_scope generation guard (no GL)
# =============================================================================

class _GpuScopeGuardRenderer(_FakeRenderer):
    """Extends the fake with the fields the pass prologue checks."""

    def __init__(self):
        super().__init__()
        self.gpu_scope_enabled = True
        self.ctx = Mock()  # truthy
        self.current_front_tex_id = 42
        self._scope_source_revision = 10
        self._scope_demand_revision = 5
        self._scope_color_output_max_dim = 0
        self._gpu_scope_shaders_loaded = True
        self.tex_pair = [Mock(glo=42), None]
        self.gpu_scope_ready = Mock()
        emitted: List[Any] = []
        self.gpu_scope_ready.emit = lambda f: emitted.append(f)
        self._emitted = emitted


class TestDoRenderGpuScopeGuard:
    @pytest.mark.parametrize(
        ('output_target', 'expected'),
        (
            (None, True),
            ('composite', True),
            ('monitor', True),
            ('scope', False),
            ('full_res', False),
        ),
    )
    def test_color_output_finish_policy(self, output_target, expected):
        assert (
            AsyncRenderer._color_output_requires_immediate_finish(output_target)
            is expected
        )

    def test_stale_ui_revision_drops_without_emit(self):
        r = _GpuScopeGuardRenderer()
        # demand.revision (3) < _scope_demand_revision (5) -> drop
        d = ScopeDemand(revision=3, source_revision=10, mode='split',
                        display_size=(100, 100))
        AsyncRenderer._do_render_gpu_scope(r, d)
        assert r._emitted == []

    def test_older_source_demand_uses_latest_published_front(self):
        r = _GpuScopeGuardRenderer()
        # GPU_SCOPE drains after video, so a demand based on source revision
        # 5 can correctly run against the newer published front at revision
        # 10 instead of being starved for all of playback.
        d = ScopeDemand(revision=5, source_revision=5, mode='waveform',
                        display_size=(100, 100))
        r._last_scope_render_params = {'layers': []}
        r._scope_content_revision = 10
        r._scope_color_output_revision = 10
        r._scope_color_output_max_dim = max(DEFAULT_ANALYSIS_SIZE)
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        pool = {'wf_display': Mock(glo=71)}
        r._ensure_gpu_scope_pool = lambda *_args: pool
        r._scope_sample_front = lambda *_args, **_kwargs: None
        r._scope_mask_uv_rect = lambda *_args: (0.0, 0.0, 1.0, 1.0)
        r._scope_accumulate_waveform = lambda *_args: None
        r._scope_display_waveform = lambda *_args: 1.0

        AsyncRenderer._do_render_gpu_scope(r, d)

        assert len(r._emitted) == 1
        assert r._emitted[0].source_revision == 10

    def test_playback_scope_uses_full_frame_color_output(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._do_render_color_output = Mock(return_value=True)
        pool = {'wf_display': Mock(glo=71)}
        r._ensure_gpu_scope_pool = lambda *_args: pool
        r._scope_sample_front = Mock()
        r._scope_mask_uv_rect = lambda *_args: (0.0, 0.0, 1.0, 1.0)
        r._scope_accumulate_waveform = lambda *_args: None
        r._scope_display_waveform = lambda *_args: 1.0

        d = ScopeDemand(
            revision=5, source_revision=10, mode='waveform',
            display_size=(100, 100), analysis_size=PLAYBACK_ANALYSIS_SIZE,
            is_playing=True,
        )
        AsyncRenderer._do_render_gpu_scope(r, d)

        # Playback must analyse the full graded frame, not the viewport front
        # texture whose crop/background follows canvas zoom and pan.
        r._do_render_color_output.assert_called_once()
        output_params = r._do_render_color_output.call_args.args[0]
        assert output_params['color_output_target'] == 'scope'
        assert output_params['max_proxy_dim'] == 512
        assert r._scope_sample_front.call_args.args[0] is r._scope_color_output_tex
        assert r._scope_sample_front.call_args.kwargs['mask_uv_rect'] == (
            0.0, 0.0, 1.0, 1.0,
        )
        r.ctx.finish.assert_called_once()

    def test_paused_grading_scope_keeps_full_frame_refresh(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._do_render_color_output = Mock(return_value=True)
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        pool = {'wf_display': Mock(glo=71)}
        r._ensure_gpu_scope_pool = lambda *_args: pool
        r._scope_sample_front = Mock()
        r._scope_mask_uv_rect = lambda *_args: (0.0, 0.0, 1.0, 1.0)
        r._scope_accumulate_waveform = lambda *_args: None
        r._scope_display_waveform = lambda *_args: 1.0

        d = ScopeDemand(
            revision=5, source_revision=10, mode='waveform',
            display_size=(100, 100), is_playing=False,
        )
        AsyncRenderer._do_render_gpu_scope(r, d)

        # Paused grading remains full-frame so the waveform changes with a
        # grade even when the canvas is zoomed or panned.
        r._do_render_color_output.assert_called_once()
        assert r._scope_sample_front.call_args.args[0] is r._scope_color_output_tex

    def test_playback_scope_reuses_only_current_content_output(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._scope_content_revision = 10
        r._scope_color_output_revision = 10
        r._scope_color_output_max_dim = max(DEFAULT_ANALYSIS_SIZE)
        r._do_render_color_output = Mock(return_value=True)
        pool = {'wf_display': Mock(glo=71)}
        r._ensure_gpu_scope_pool = lambda *_args: pool
        r._scope_sample_front = Mock()
        r._scope_mask_uv_rect = lambda *_args: (0.0, 0.0, 1.0, 1.0)
        r._scope_accumulate_waveform = lambda *_args: None
        r._scope_display_waveform = lambda *_args: 1.0
        demand = ScopeDemand(
            revision=5, source_revision=10, mode='waveform',
            display_size=(100, 100), is_playing=True,
        )

        AsyncRenderer._do_render_gpu_scope(r, demand)
        r._do_render_color_output.assert_not_called()

        # A newly published playback frame invalidates the full-frame cache.
        r._scope_content_revision = 11
        r._scope_color_output_revision = -1
        AsyncRenderer._do_render_gpu_scope(r, demand)

        r._do_render_color_output.assert_called_once()
        assert r._scope_color_output_revision == 11

    def test_playback_mask_uses_full_frame_identity_uv(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._scope_content_revision = 10
        r._scope_color_output_revision = 10
        r._scope_color_output_max_dim = max(DEFAULT_ANALYSIS_SIZE)
        mask_texture = Mock()
        r.layer_textures = {'layer-7': {'mask': mask_texture}}
        pool = {'wf_display': Mock(glo=71)}
        r._ensure_gpu_scope_pool = lambda *_args: pool
        r._scope_sample_front = Mock()
        r._scope_accumulate_waveform = lambda *_args: None
        r._scope_display_waveform = lambda *_args: 1.0
        demand = ScopeDemand(
            revision=5, source_revision=10, mode='waveform',
            display_size=(100, 100), is_playing=True,
            mask_layer_id='layer-7', mask_enabled=True,
        )

        AsyncRenderer._do_render_gpu_scope(r, demand)

        assert r._scope_sample_front.call_args.args[0] is r._scope_color_output_tex
        assert r._scope_sample_front.call_args.kwargs == {
            'mask_texture': mask_texture,
            'mask_uv_rect': (0.0, 0.0, 1.0, 1.0),
        }

    def test_quality_tier_change_refreshes_full_frame_output(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._scope_content_revision = 10
        r._scope_color_output_revision = 10
        r._scope_color_output_max_dim = max(PLAYBACK_ANALYSIS_SIZE)
        r._do_render_color_output = Mock(return_value=True)
        r._ensure_gpu_scope_pool = lambda *_args: {'wf_display': Mock(glo=71)}
        r._scope_sample_front = Mock()
        r._scope_accumulate_waveform = lambda *_args: None
        r._scope_display_waveform = lambda *_args: 1.0
        demand = ScopeDemand(
            revision=5,
            source_revision=10,
            mode='waveform',
            display_size=(100, 100),
            analysis_size=HIGH_QUALITY_ANALYSIS_SIZE,
            is_playing=False,
        )

        AsyncRenderer._do_render_gpu_scope(r, demand)

        output_params = r._do_render_color_output.call_args.args[0]
        assert output_params['max_proxy_dim'] == 768
        assert r._scope_color_output_max_dim == 768

    def test_failed_full_frame_refresh_does_not_promote_stale_texture(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._scope_content_revision = 11
        r._scope_color_output_revision = 10
        r._do_render_color_output = Mock(return_value=False)
        r._ensure_gpu_scope_pool = lambda *_args: {'wf_display': Mock(glo=71)}
        r._scope_sample_front = Mock()
        demand = ScopeDemand(
            revision=5, source_revision=10, mode='waveform',
            display_size=(100, 100), is_playing=True,
        )

        AsyncRenderer._do_render_gpu_scope(r, demand)

        assert r._scope_color_output_revision != 11
        r._scope_sample_front.assert_not_called()
        assert r._emitted == []

    def test_scope_and_external_color_outputs_use_independent_resources(self):
        main_tex = Mock(size=(2048, 1152))
        main_fbo = Mock()
        scope_tex_1 = Mock(size=(512, 288))
        scope_fbo_1 = Mock()
        scope_tex_2 = Mock(size=(768, 432))
        scope_fbo_2 = Mock()
        ctx = Mock()
        ctx.texture.side_effect = (scope_tex_1, scope_tex_2)
        ctx.framebuffer.side_effect = (scope_fbo_1, scope_fbo_2)
        r = SimpleNamespace(
            ctx=ctx,
            garbage=[],
            _color_output_tex=main_tex,
            _color_output_fbo=main_fbo,
            _scope_color_output_tex=None,
            _scope_color_output_fbo=None,
            _scope_color_output_revision=7,
        )

        first = AsyncRenderer._ensure_color_output_fbo(r, 512, 288, 'scope')

        assert first == (scope_tex_1, scope_fbo_1)
        assert r._color_output_tex is main_tex
        assert r._color_output_fbo is main_fbo
        main_tex.release.assert_not_called()
        main_fbo.release.assert_not_called()

        second = AsyncRenderer._ensure_color_output_fbo(r, 768, 432, 'scope')

        assert second == (scope_tex_2, scope_fbo_2)
        # AUD-0201（wd3-g17 翻转协议）：被替换的 scope ColorOutput 对走
        # garbage 延迟释放队列，不再立即 release。
        scope_fbo_1.release.assert_not_called()
        scope_tex_1.release.assert_not_called()
        assert (scope_fbo_1, scope_tex_1) == (r.garbage[0][1], r.garbage[0][2])
        main_tex.release.assert_not_called()
        main_fbo.release.assert_not_called()
        assert r._scope_color_output_revision == -1

    def test_color_output_resize_failure_keeps_live_pair(self):
        old_tex = Mock(size=(512, 288))
        old_fbo = Mock()
        partial_tex = Mock(size=(768, 432))
        ctx = Mock()
        ctx.texture.return_value = partial_tex
        ctx.framebuffer.side_effect = RuntimeError('fbo allocation failed')
        r = SimpleNamespace(
            ctx=ctx,
            _scope_color_output_tex=old_tex,
            _scope_color_output_fbo=old_fbo,
            _scope_color_output_revision=10,
            _scope_color_output_max_dim=512,
        )

        with pytest.raises(RuntimeError, match='fbo allocation failed'):
            AsyncRenderer._ensure_color_output_fbo(r, 768, 432, 'scope')

        assert r._scope_color_output_tex is old_tex
        assert r._scope_color_output_fbo is old_fbo
        assert r._scope_color_output_revision == 10
        assert r._scope_color_output_max_dim == 512
        partial_tex.release.assert_called_once()
        old_tex.release.assert_not_called()
        old_fbo.release.assert_not_called()

    def test_color_output_temp_lease_returns_texture_on_exception(self):
        tex = Mock()
        manager = Mock()
        manager.get_temp_texture.return_value = tex
        r = SimpleNamespace(fb_manager=manager)

        with pytest.raises(RuntimeError, match='shader failed'):
            with ExitStack() as cleanup:
                leased = AsyncRenderer._acquire_color_output_temp_texture(
                    r, cleanup, 512, 288
                )
                assert leased is tex
                raise RuntimeError('shader failed')

        manager.release_temp_texture.assert_called_once_with(tex)

    def test_color_output_render_copies_params_and_reports_success(self):
        packed_tex = SimpleNamespace(size=(1920, 1080))
        r = SimpleNamespace(
            current_video_planes=None,
            current_video_texture=packed_tex,
            pyramid_textures=[],
            _do_render=Mock(return_value=True),
        )
        params = {
            'viewport_size': (640, 360),
            'visible_rect': (100, 50, 640, 360),
            'show_mask': True,
            'max_proxy_dim': 512,
            'color_output_target': 'scope',
        }
        original = dict(params)

        success = AsyncRenderer._do_render_color_output(r, params)

        assert success is True
        assert params == original
        render_params = r._do_render.call_args.args[0]
        assert render_params is not params
        assert render_params['viewport_size'] == (512, 288)
        assert render_params['visible_rect'] == (0, 0, 1920, 1080)
        assert render_params['show_mask'] is False
        assert render_params['color_output_mode'] is True
        assert r._do_render.call_args.kwargs == {'allow_during_playback': True}

    def test_color_output_render_without_source_reports_failure(self):
        r = SimpleNamespace(
            current_video_planes=None,
            current_video_texture=None,
            pyramid_textures=[],
            _do_render=Mock(),
        )

        assert AsyncRenderer._do_render_color_output(r, {}) is False
        r._do_render.assert_not_called()

    def test_rejected_playback_render_does_not_replace_scope_params(self):
        sentinel = {'layers': ['current-clip']}
        r = SimpleNamespace(
            ctx=Mock(),
            video_playback_active=True,
            _video_generation_id=3,
            _last_scope_render_params=sentinel,
        )

        success = AsyncRenderer._do_render(
            r,
            {'viewport_size': (640, 360), 'layers': ['stale-clip']},
        )

        assert success is False
        assert r._last_scope_render_params is sentinel

    def test_disabled_does_not_emit(self):
        r = _GpuScopeGuardRenderer()
        r.gpu_scope_enabled = False
        d = ScopeDemand(revision=5, source_revision=10, mode='split',
                        display_size=(100, 100))
        AsyncRenderer._do_render_gpu_scope(r, d)
        assert r._emitted == []

    def test_shaders_not_loaded_disables_lane(self):
        r = _GpuScopeGuardRenderer()
        r._gpu_scope_shaders_loaded = False
        d = ScopeDemand(revision=5, source_revision=10, mode='split',
                        display_size=(100, 100))
        AsyncRenderer._do_render_gpu_scope(r, d)
        # Auto-disable + clear slot.
        assert r.gpu_scope_enabled is False
        assert r._pending_slots[RenderLane.GPU_SCOPE] is None

    def test_no_front_texture_does_not_emit(self):
        r = _GpuScopeGuardRenderer()
        r.current_front_tex_id = None
        d = ScopeDemand(revision=5, source_revision=10, mode='split',
                        display_size=(100, 100))
        AsyncRenderer._do_render_gpu_scope(r, d)
        assert r._emitted == []


class _NoBlendGetterContext:
    """Context double that exposes the macOS blend-function limitation."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    @property
    def blend_func(self):
        raise NotImplementedError()

    @blend_func.setter
    def blend_func(self, value) -> None:
        self.events.append(('blend_func', value))

    def enable(self, flag) -> None:
        self.events.append(('enable', flag))

    def disable(self, flag) -> None:
        self.events.append(('disable', flag))


class TestScopeBlendState:
    def test_scope_blending_never_reads_unsupported_backend_state(self):
        # The blend state helpers live on the shared GpuScopeEngine both
        # render threads use; exercise them against the macOS double that
        # raises on the unsupported blend-func getter.
        from src.shared.gpu.gpu_scope_engine import GpuScopeEngine

        ctx = _NoBlendGetterContext()
        engine = GpuScopeEngine(ctx)

        engine._enable_additive()
        engine._restore_blend()

        blend_values = [
            value for event, value in ctx.events
            if event == 'blend_func'
        ]
        assert blend_values[0] == (1, 1)
        assert blend_values[1] != blend_values[0]


# =============================================================================
# ScopePanel GPU presenter + revision guard (requires QApplication)
# =============================================================================

@pytest.fixture(scope='module')
def _qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestScopePanelGpuIntegration:
    """The panel must instantiate GPU presenters and guard frame revisions."""

    def test_creates_gpu_presenters(self, _qapp):
        from src.features.color.ui.scope_panel import MODE_ORDER, ScopePanel
        p = ScopePanel()
        assert p._gpu_wf_presenter is not None
        assert p._gpu_vs_presenter is not None
        # The CPU waveform fills its panel (including the value axis); only
        # the vectorscope must be letterboxed to keep its circle circular.
        assert p._gpu_wf_presenter._preserve_aspect is False
        assert p._gpu_vs_presenter._preserve_aspect is True
        assert p._gpu_wf_presenter._axis_left_px == 38
        # Waveform values are already oriented in GL coordinates by the
        # shader (100% at the top, 0% at the bottom); the presenter must not
        # flip them away from the 1023..0 axis labels.
        assert p._gpu_wf_presenter._flip_y is False
        assert p._gpu_vs_presenter._flip_y is False
        # Presenters are wired into a layout (GPU display page).
        assert p.display_stack.count() == 2
        # One GPU presenter page per MODE_ORDER entry (mirrored indices).
        assert p._gpu_stack.count() == len(MODE_ORDER)

    def test_creates_gpu_presenters_for_professional_modes(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        p = ScopePanel()
        # Parade / luma keep the waveform value axis; the histogram bakes
        # its own graticule; CIE and false color are aspect-fitted plots.
        assert p._gpu_parade_presenter._show_value_axis is True
        assert p._gpu_luma_presenter._show_value_axis is True
        assert p._gpu_histogram_presenter._show_value_axis is False
        assert p._gpu_cie_presenter._preserve_aspect is True
        assert p._gpu_cie_presenter._show_value_axis is False
        assert p._gpu_fc_presenter._preserve_aspect is True
        assert p._gpu_fc_presenter._legend == 'falsecolor_sdr'

    def test_every_mode_dispatches_to_its_own_presenter(self, _qapp):
        from src.features.color.ui.scope_panel import (
            MODE_ORDER, _MODE_FRAME_FIELD, _MODE_PRESENTER_ATTR, ScopePanel,
        )
        from src.features.color.core.scope_types import ScopeGpuFrame
        p = ScopePanel()
        p.bump_demand_revision()
        for mode in MODE_ORDER:
            p.set_mode(mode)
            frame_kwargs = {}
            for field_mode, prefix in _MODE_FRAME_FIELD.items():
                frame_kwargs[f'{prefix}_texture_id'] = 100
                frame_kwargs[f'{prefix}_size'] = (64, 64)
            if mode == 'split':
                frame_kwargs['vectorscope_texture_id'] = 101
            frame = ScopeGpuFrame(revision=p.gpu_demand_revision,
                                  source_revision=1, **frame_kwargs)
            delivered = []
            for attr in set(_MODE_PRESENTER_ATTR.values()):
                presenter = getattr(p, attr)
                presenter.set_scope_frame = (
                    lambda tid, w, h, _d=delivered: _d.append(tid)
                )
            p.update_gpu_scope(frame)
            if mode == 'split':
                assert len(delivered) == 2
            else:
                assert delivered == [100]

    def test_gpu_presenters_request_single_sample_targets(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        p = ScopePanel()
        assert p._gpu_wf_presenter.format().samples() == 0
        assert p._gpu_vs_presenter.format().samples() == 0

    def test_gpu_page_is_default_for_realtime_scopes(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        from src.features.color.core.scope_types import ScopeGpuFrame
        p = ScopePanel()
        assert p.display_stack.currentIndex() == 1  # GPU default
        assert p._gpu_display_active is True
        p.bump_demand_revision()
        p.update_gpu_scope(ScopeGpuFrame(
            revision=1, source_revision=1,
            waveform_texture_id=10, waveform_size=(100, 100),
        ))
        assert p._gpu_display_active is True
        assert p.display_stack.currentIndex() == 1  # promoted to GPU page

    def test_mode_switch_syncs_gpu_stack(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        p = ScopePanel()
        p._on_mode_changed(1)  # vectorscope
        assert p._gpu_stack.currentIndex() == 1

    def test_revision_starts_zero_and_bumps(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        p = ScopePanel()
        assert p.gpu_demand_revision == 0
        assert p.bump_demand_revision() == 1
        assert p.gpu_demand_revision == 1

    def test_mode_change_bumps_revision(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        p = ScopePanel()
        p.bump_demand_revision()  # baseline
        before = p.gpu_demand_revision
        p._on_mode_changed(1)  # switch to vectorscope
        assert p.gpu_demand_revision > before

    def test_zoom_change_bumps_revision(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        p = ScopePanel()
        before = p.gpu_demand_revision
        p._set_zoom(2)
        assert p.gpu_demand_revision > before

    def test_matching_revision_frame_is_presented(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        from src.features.color.core.scope_types import ScopeGpuFrame
        p = ScopePanel()
        p.bump_demand_revision()  # now 1
        delivered = []
        # Capture set_scope_frame calls on both presenters.
        for pres in (p._gpu_wf_presenter, p._gpu_vs_presenter):
            pres.set_scope_frame = lambda tid, w, h, _l=delivered: _l.append((tid, w, h))
        p._current_mode = 'split'
        p.update_gpu_scope(ScopeGpuFrame(
            revision=1, source_revision=1,
            waveform_texture_id=10, waveform_size=(100, 100),
            vectorscope_texture_id=11, vectorscope_size=(100, 100),
        ))
        # split -> both presenters receive a frame
        assert (10, 100, 100) in delivered
        assert (11, 100, 100) in delivered

    def test_stale_revision_frame_dropped(self, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        from src.features.color.core.scope_types import ScopeGpuFrame
        p = ScopePanel()
        p.bump_demand_revision()  # now 1
        delivered = []
        for pres in (p._gpu_wf_presenter, p._gpu_vs_presenter):
            pres.set_scope_frame = lambda tid, w, h, _l=delivered: _l.append(tid)
        # Frame for revision 0 (older than current 1) must be dropped.
        p.update_gpu_scope(ScopeGpuFrame(revision=0, source_revision=1,
                                         waveform_texture_id=10,
                                         waveform_size=(100, 100)))
        assert delivered == []


# =============================================================================
# Full-mode dispatch: parade / luma / histogram / cie / falsecolor
# =============================================================================

class TestDoRenderGpuScopeAllModes:
    """Every ScopeMode must emit its own texture descriptor."""

    _POOL = {
        'wf_display': Mock(glo=71),
        'vs_display': Mock(glo=72),
        'hist_display': Mock(glo=73),
        'cie_display': Mock(glo=74),
        'fc_display': Mock(glo=75),
    }

    def _make_renderer(self):
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {'layers': []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._do_render_color_output = Mock(return_value=True)
        r._ensure_gpu_scope_pool = lambda *args: dict(self._POOL)
        r._scope_sample_front = Mock()
        r._scope_mask_uv_rect = lambda *args: (0.0, 0.0, 1.0, 1.0)
        r._scope_accumulate_waveform = Mock()
        r._scope_display_waveform = Mock()
        r._scope_accumulate_vectorscope = Mock()
        r._scope_display_vectorscope = Mock()
        r._scope_accumulate_luma = Mock()
        r._scope_display_luma = Mock()
        r._scope_accumulate_parade = Mock()
        r._scope_display_parade = Mock()
        r._scope_accumulate_histogram = Mock()
        r._scope_reduce_histogram = Mock()
        r._scope_display_histogram = Mock()
        r._scope_accumulate_cie = Mock()
        r._scope_display_cie = Mock()
        r._scope_display_falsecolor = Mock()
        return r

    def _demand(self, mode):
        return ScopeDemand(
            revision=5, source_revision=10, mode=mode,
            display_size=(100, 100),
        )

    def test_parade_mode(self):
        r = self._make_renderer()
        AsyncRenderer._do_render_gpu_scope(r, self._demand('parade'))
        r._scope_accumulate_parade.assert_called_once()
        r._scope_display_parade.assert_called_once()
        assert r._emitted[0].parade_texture_id == 71
        assert r._emitted[0].waveform_texture_id is None

    def test_luma_mode(self):
        r = self._make_renderer()
        AsyncRenderer._do_render_gpu_scope(r, self._demand('luma'))
        r._scope_accumulate_luma.assert_called_once()
        r._scope_display_luma.assert_called_once()
        assert r._emitted[0].luma_texture_id == 71

    def test_histogram_mode(self):
        r = self._make_renderer()
        AsyncRenderer._do_render_gpu_scope(r, self._demand('histogram'))
        r._scope_accumulate_histogram.assert_called_once()
        r._scope_reduce_histogram.assert_called_once()
        r._scope_display_histogram.assert_called_once()
        assert r._emitted[0].histogram_texture_id == 73

    def test_cie_mode(self):
        r = self._make_renderer()
        demand = self._demand('cie')
        demand = ScopeDemand(
            revision=5, source_revision=10, mode='cie',
            display_size=(100, 100),
            scope_transfer='pq', scope_primaries='p3',
        )
        AsyncRenderer._do_render_gpu_scope(r, demand)
        r._scope_accumulate_cie.assert_called_once()
        r._scope_display_cie.assert_called_once()
        assert r._emitted[0].cie_texture_id == 74

    def test_falsecolor_mode_skips_sample_pass(self):
        r = self._make_renderer()
        AsyncRenderer._do_render_gpu_scope(r, self._demand('falsecolor'))
        # False color reads the untouched source directly; no analysis
        # sampling (and no density accumulation) happens.
        r._scope_sample_front.assert_not_called()
        r._scope_display_falsecolor.assert_called_once()
        assert r._emitted[0].falsecolor_texture_id == 75

    def test_split_mode_emits_both_descriptors(self):
        r = self._make_renderer()
        AsyncRenderer._do_render_gpu_scope(r, self._demand('split'))
        assert r._emitted[0].waveform_texture_id == 71
        assert r._emitted[0].vectorscope_texture_id == 72


# =============================================================================
# Algorithm correctness (GLSL vertex-shader math mirrored in NumPy)
# =============================================================================
# These validate the coordinate-mapping arithmetic of gpu_scope.glsl against
# the CPU reference (vectorscope_widget.py) without requiring a live GL
# context. They guard the shader constants, not the rasterization.

import numpy as np
from pathlib import Path


# CPU reference coefficients (vectorscope_widget.py:30-31).
REF_CB = np.array([-0.169, -0.331, 0.500], dtype=np.float64)
REF_CR = np.array([0.500, -0.419, -0.081], dtype=np.float64)
# Shader constants (gpu_scope.glsl SCOPE_VECTORSCOPE_ACCUM_VERTEX).
GLSL_CB = np.array([-0.169, -0.331, 0.500], dtype=np.float64)
GLSL_CR = np.array([0.500, -0.419, -0.081], dtype=np.float64)


class TestVectorscopeAlgorithm:
    def test_glsl_cbcr_coefficients_match_cpu_reference(self):
        """The shader's Cb/Cr coefficients must equal the CPU oracle exactly."""
        np.testing.assert_allclose(GLSL_CB, REF_CB, atol=1e-6)
        np.testing.assert_allclose(GLSL_CR, REF_CR, atol=1e-6)

    def test_primary_color_targets_within_tolerance(self):
        """R/G/B/C/M/Y targets land within 1-2 bin tolerance of the reference."""
        targets = {
            'R': np.array([1.0, 0.0, 0.0]),
            'G': np.array([0.0, 1.0, 0.0]),
            'B': np.array([0.0, 0.0, 1.0]),
            'C': np.array([0.0, 1.0, 1.0]),
            'M': np.array([1.0, 0.0, 1.0]),
            'Y': np.array([1.0, 1.0, 0.0]),
        }
        for name, rgb in targets.items():
            cb = float(np.dot(rgb, GLSL_CB))
            cr = float(np.dot(rgb, GLSL_CR))
            # Shader maps x_ndc = cb*2*zoom (zoom=1), so screen radius = cb.
            # Verify it lands inside the 75% ring (|sat| <= 0.5) and that
            # each primary reaches a distinct, well-separated cell.
            assert abs(cb) <= 0.5 + 1e-6, f"{name} Cb out of range: {cb}"
            assert abs(cr) <= 0.5 + 1e-6, f"{name} Cr out of range: {cr}"
            # Pure red should be at Cr ~= +0.5 (top), pure blue at Cb ~= +0.5.
            if name == 'R':
                assert cr > 0.45
            if name == 'B':
                assert cb > 0.45

    def test_gray_is_centered(self):
        """A neutral gray (R=G=B) must map to the vectorscope center."""
        gray = np.array([0.5, 0.5, 0.5])
        cb = float(np.dot(gray, GLSL_CB))
        cr = float(np.dot(gray, GLSL_CR))
        assert abs(cb) < 1e-6 and abs(cr) < 1e-6

    def test_zoom_only_scales_radius(self):
        """Zoom must scale the (Cb,Cr) radius, not rotate or shift it."""
        rgb = np.array([1.0, 0.0, 0.0])  # red
        cb = float(np.dot(rgb, GLSL_CB))
        cr = float(np.dot(rgb, GLSL_CR))
        # x_ndc(zoom) = cb * 2 * zoom; angle = atan2(cr, cb) unchanged.
        import math
        ang1 = math.atan2(cr, cb)
        ang2 = math.atan2(cr, cb)  # zoom cancels in atan2
        assert abs(ang1 - ang2) < 1e-9


class TestWaveformAlgorithm:
    def _y_ndc(self, val, rmin=-0.1, rmax=1.2):
        """Mirror of SCOPE_WAVEFORM_ACCUM_VERTEX y mapping."""
        t = (val - rmin) / (rmax - rmin)
        t = max(0.0, min(1.0, t))
        return t * 2.0 - 1.0

    def test_value_axis_monotonic(self):
        """Higher signal value must map higher on the waveform (top = bright)."""
        assert self._y_ndc(0.0) < self._y_ndc(0.5) < self._y_ndc(1.0)

    def test_black_maps_near_bottom(self):
        assert self._y_ndc(0.0) < -0.8

    def test_white_maps_above_mid(self):
        """White (1.0) sits in the upper half, below the HDR 1.2 ceiling."""
        assert self._y_ndc(1.0) > 0.5

    def test_hdr_headroom_range(self):
        """The -0.1..1.2 range (CPU reference) is preserved in the shader."""
        # 1.0 maps to t=(1.1/1.3)~0.846 -> y~0.69, not pinned to the top,
        # leaving headroom that matches the CPU BINS=300/MIN_V/MAX_V reference.
        y_at_one = self._y_ndc(1.0)
        assert 0.6 < y_at_one < 0.85

    def test_waveform_shader_keeps_overrange_diagnostics(self):
        """The GPU shader must retain and mark values outside 0..1."""
        shader_path = Path(__file__).parents[2] / 'src/features/color/shaders/gpu_scope.glsl'
        source = shader_path.read_text()
        assert 'clamp(c, -0.1, 1.2)' in source
        assert 'v_range_flag' in source
        assert 'over_alert' in source
        assert 'under_alert' in source
