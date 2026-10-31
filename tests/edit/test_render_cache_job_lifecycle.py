"""
Tests for Render Cache worker lifecycle and encoder.

Tests the data model, encoder configuration, and worker lifecycle
without requiring actual GPU/encoding (mocked where needed).
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np

from src.features.edit.core.render_cache import (
    RenderCacheManifest,
    RenderCacheSettings,
    RenderCacheStatus,
)


class TestRenderCacheManifest:
    """Test RenderCacheManifest data model."""

    def test_create_new_generates_uuid(self):
        m = RenderCacheManifest.create_new(
            timeline_id="tl-1", clip_id="c-1", composite_id="g-1",
            start_frame=0, duration_frames=100, width=1920, height=1080,
            fps="24000/1001",
        )
        assert len(m.cache_id) == 36  # UUID format
        assert m.timeline_id == "tl-1"
        assert m.clip_id == "c-1"
        assert m.status == RenderCacheStatus.MISSING

    def test_is_valid_for_matching_revisions(self):
        m = RenderCacheManifest(
            status=RenderCacheStatus.VALID,
            color_stack_revision=5,
            composite_graph_revision=3,
        )
        assert m.is_valid_for(color_stack_revision=5, composite_graph_revision=3)
        assert m.is_valid_for(color_stack_revision=4, composite_graph_revision=2)

    def test_is_not_valid_for_newer_revisions(self):
        m = RenderCacheManifest(
            status=RenderCacheStatus.VALID,
            color_stack_revision=5,
            composite_graph_revision=3,
        )
        assert not m.is_valid_for(color_stack_revision=6, composite_graph_revision=3)

    def test_is_not_valid_when_stale(self):
        m = RenderCacheManifest(status=RenderCacheStatus.STALE)
        assert not m.is_valid_for(color_stack_revision=0, composite_graph_revision=0)

    def test_fingerprint_mismatch_invalidates_cache(self):
        m = RenderCacheManifest(
            status=RenderCacheStatus.VALID,
            render_fingerprint="old",
        )
        assert not m.is_valid_for(0, 0, render_fingerprint="new")
        assert m.is_valid_for(0, 0, render_fingerprint="old")

    def test_roundtrip_dict(self):
        m = RenderCacheManifest.create_new(
            timeline_id="tl-1", clip_id="c-1", composite_id="g-1",
            start_frame=10, duration_frames=50, width=1920, height=1080,
            fps="24000/1001",
        )
        d = m.to_dict()
        restored = RenderCacheManifest.from_dict(d)
        assert restored.cache_id == m.cache_id
        assert restored.timeline_id == m.timeline_id
        assert restored.start_frame == m.start_frame


class TestRenderCacheSettings:
    """Test RenderCacheSettings data model."""

    def test_defaults(self):
        s = RenderCacheSettings()
        assert s.codec == "topos_proxy"
        assert s.pixel_format == "yuv422p10le"
        assert s.container == "mov"
        assert s.auto_mode == "off"
        assert s.max_size_gb == 50.0

    def test_roundtrip_dict(self):
        s = RenderCacheSettings(codec="dnxhr_lb", auto_mode="smart")
        d = s.to_dict()
        restored = RenderCacheSettings.from_dict(d)
        assert restored.codec == "dnxhr_lb"
        assert restored.auto_mode == "smart"


class TestRenderCacheEncoderConfig:
    """Test encoder configuration parsing."""

    def test_prores_cache_codec_is_removed(self):
        from src.features.edit.core.render_cache_encoder import RenderCacheEncoder
        with pytest.raises(ValueError, match="未知缓存 codec"):
            RenderCacheEncoder._parse_codec("prores_422_proxy")

    def test_parse_topos_proxy(self):
        from src.features.edit.core.render_cache_encoder import RenderCacheEncoder
        codec, profile, pix_fmt = RenderCacheEncoder._parse_codec("topos_proxy")
        assert codec == "topos"
        assert profile == "proxy"
        assert pix_fmt == "yuv422p10le"

    def test_parse_dnxhr_lb(self):
        from src.features.edit.core.render_cache_encoder import RenderCacheEncoder
        codec, profile, pix_fmt = RenderCacheEncoder._parse_codec("dnxhr_lb")
        assert codec == "dnxhr"
        assert profile == "lb"
        assert pix_fmt == "yuv422p"

    def test_parse_unknown_codec_fails_explicitly(self):
        from src.features.edit.core.render_cache_encoder import RenderCacheEncoder
        with pytest.raises(ValueError, match="未知缓存 codec"):
            RenderCacheEncoder._parse_codec("unknown_codec")

    def test_vp8_requires_direct_recorder(self):
        from src.features.edit.core.render_cache_encoder import RenderCacheEncoder
        with pytest.raises(ValueError, match="direct realtime recorder"):
            RenderCacheEncoder._parse_codec("vp8_high_quality")

    def test_motion_graphic_rates_scale_and_preview_caps(self):
        from src.shared.media.motion_graphic_delivery import (
            motion_graphic_bitrate_kbps,
            motion_graphic_min_bitrate_kbps,
            motion_graphic_preview_size,
        )
        assert motion_graphic_preview_size(3840, 2160) == (1920, 1080)
        assert motion_graphic_bitrate_kbps(1920, 1080) == 40_000
        assert motion_graphic_min_bitrate_kbps(1920, 1080) == 20_000
        assert motion_graphic_bitrate_kbps(3840, 2160) == 160_000
        assert motion_graphic_min_bitrate_kbps(3840, 2160) == 80_000

    def test_webm_minimum_bitrate_padding_is_measurable(self, tmp_path):
        from src.features.teaching_animation.render.html_realtime_recorder import (
            _ensure_minimum_webm_bitrate,
        )
        output = tmp_path / "padded.webm"
        output.write_bytes(b"webm-header")
        _ensure_minimum_webm_bitrate(output, duration=1.0, minimum_kbps=20_000)
        assert output.stat().st_size * 8 >= 20_000 * 1000


class TestRenderCacheWorkerFpsParse:
    """Test FPS string parsing."""

    def test_parse_fraction(self):
        from src.features.edit.core.render_cache_worker import RenderCacheWorker
        num, den = RenderCacheWorker._parse_fps("24000/1001")
        assert num == 24000
        assert den == 1001

    def test_parse_integer(self):
        from src.features.edit.core.render_cache_worker import RenderCacheWorker
        num, den = RenderCacheWorker._parse_fps("30")
        assert num == 30
        assert den == 1

    def test_parse_invalid_defaults_to_24(self):
        from src.features.edit.core.render_cache_worker import RenderCacheWorker
        num, den = RenderCacheWorker._parse_fps("invalid")
        assert num == 24
        assert den == 1

    def test_html_motion_graphic_cache_uses_realtime_recorder(self, tmp_path, monkeypatch):
        """HTML cache rendering must record one live page, not capture N frames."""
        from pathlib import Path
        from types import SimpleNamespace

        from src.features.edit.core.render_cache import RenderCacheManifest
        from src.features.edit.core.render_cache_worker import RenderCacheWorker
        from src.features.teaching_animation.render.html_realtime_recorder import (
            RealtimeRecordResult,
        )

        output = tmp_path / "motion.webm"
        manifest = RenderCacheManifest(
            cache_id="cache-realtime",
            timeline_id="timeline-1",
            clip_id="clip-1",
            duration_frames=72,
            timeline_fps="24/1",
            path=str(output),
            codec="vp8_high_quality",
            container="webm",
            width=2,
            height=2,
            render_kind="motion_graphic",
            scene_id="scene-realtime",
        )
        scene = SimpleNamespace(
            scene_id="scene-realtime",
            fps=24.0,
            width=2,
            height=2,
            duration_frames=72,
            pages=(),
            html_layer=SimpleNamespace(
                sanitized_html="<html><body><div>motion</div></body></html>",
                metadata={"font_lock": False},
                csp="default-src 'none'",
                bound_asset_ids=(),
            ),
        )
        calls = []

        def fake_record(html, path, **kwargs):
            calls.append((html, Path(path), kwargs))
            Path(path).write_bytes(b"recorded")
            return RealtimeRecordResult(str(path), 72, 2, 2, 24.0, False)

        monkeypatch.setattr(
            "src.features.teaching_animation.render.html_realtime_recorder.record_html_video",
            fake_record,
        )
        monkeypatch.setattr(
            "src.features.teaching_animation.render.html_scene_renderer_v2.resolve_bound_image_bytes",
            lambda _scene, _context: {},
        )

        worker = RenderCacheWorker(manifest, edit_project=None, timeline_sequences={})
        worker._render_motion_graphic_recording(scene, str(output))

        assert output.read_bytes() == b"recorded"
        assert len(calls) == 1
        assert calls[0][2]["delivery"] == "vp8_high_quality"
        assert calls[0][2]["video_codec"] == "vp8"
        assert calls[0][2]["bitrate_kbps"] == 40_000
        assert calls[0][2]["duration_seconds"] == 3.0
        assert not Path(str(output) + ".tmp.mp4").exists()

    def test_orphaned_rendering_manifest_is_restarted(self, tmp_path):
        """A persisted RENDERING entry without a live worker must not block Space."""
        from dataclasses import replace
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock

        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.core.render_cache import RenderCacheManifest, RenderCacheStatus
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
        from src.shared.media.metadata import Rational

        scene_id = "orphan-scene"
        asset_id = f"teaching_animation:{scene_id}"
        project = EditProject.create("orphan-cache")
        asset = MediaAsset(
            asset_id=asset_id,
            path=f"topos://motion_graphic/{scene_id}",
            media_type="motion_graphic",
            display_name="Orphan Motion Graphic",
            metadata={"scene_id": scene_id, "duration_frames": 10},
        )
        project = replace(project, assets={asset_id: asset})
        clip = ClipInstance(
            clip_id="clip-orphan",
            asset_id=asset_id,
            track_id="v1",
            timeline_start=0,
            timeline_duration=10,
            source_start=0,
            source_duration=10,
            composite_id=f"teaching_scene:{scene_id}",
        )
        timeline = TimelineSequence(
            timeline_id="tl-orphan",
            name="Timeline",
            fps=Rational(10, 1),
            width=320,
            height=180,
            video_tracks=(EditTrack("v1", "Graphics", "video", 0, clips=(clip,)),),
        )
        old_path = tmp_path / "orphan.mp4"
        old_path.write_bytes(b"stale")
        old_manifest = RenderCacheManifest(
            cache_id="orphan-cache",
            timeline_id=timeline.timeline_id,
            clip_id=clip.clip_id,
            composite_id=clip.composite_id,
            start_frame=0,
            duration_frames=10,
            path=str(old_path),
            codec="h264_proxy",
            container="mp4",
            width=320,
            height=180,
            status=RenderCacheStatus.RENDERING,
            render_kind="motion_graphic",
            scene_id=scene_id,
        )
        project = project.set_render_cache_manifest(old_manifest)

        ctrl = SimpleNamespace(
            _current_edit_project=project,
            edit_page=SimpleNamespace(_current_timeline=timeline),
            timeline_panel=Mock(),
            status_bar=Mock(),
            config_manager=SimpleNamespace(config={"render_cache_directory": str(tmp_path)}),
            _render_cache_workers={},
        )
        ctrl._cleanup_obsolete_render_cache_files = EditControllerMixin._cleanup_obsolete_render_cache_files
        started = []
        ctrl._start_render_cache_worker = lambda manifest: started.append(manifest) or True
        handler = EditControllerMixin._on_render_cache_clip_requested.__get__(
            ctrl, EditControllerMixin,
        )

        assert handler(clip.clip_id, clip.track_id)
        assert len(started) == 1
        assert started[0].cache_id != old_manifest.cache_id
        assert Path(started[0].path).name == "clip_clip-orp.vp8_40m.webm"
        assert started[0].codec == "vp8_high_quality"
        assert started[0].container == "webm"
        assert not old_path.exists()
        assert sum(
            1 for value in ctrl._current_edit_project.render_cache_manifests.values()
            if value.get("clip_id") == clip.clip_id
        ) == 1

        # A re-entrant Space/context-menu request must be coalesced while the
        # replacement worker is still in flight, before its terminal signal.
        assert handler(clip.clip_id, clip.track_id)
        assert len(started) == 1

        # A changed semantic state must get a distinct output path; otherwise
        # the newly rendered variant would overwrite the cache that undo is
        # expected to recover later.
        from src.features.edit.core.effect_instance import EffectInstance
        edited_clip = replace(
            clip,
            effects=(EffectInstance(effect_type="blur", params={"radius": 2.0}),),
        )
        ctrl.edit_page._current_timeline = replace(
            timeline,
            video_tracks=(replace(timeline.video_tracks[0], clips=(edited_clip,)),),
        )
        ctrl._render_cache_inflight_clips.clear()
        assert handler(edited_clip.clip_id, edited_clip.track_id)
        assert len(started) == 2
        assert started[0].path != started[1].path

    def test_cache_completion_publishes_manifest_to_preview_renderer(self, tmp_path):
        """A finished cache must be readable without a second timeline edit."""
        from types import SimpleNamespace
        from unittest.mock import Mock

        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.project import EditProject

        project = EditProject.create("renderer-sync")
        manifest = RenderCacheManifest(
            cache_id="cache-sync",
            timeline_id="tl-sync",
            clip_id="clip-sync",
            composite_id="teaching_scene:scene-sync",
            duration_frames=10,
            width=1920,
            height=1080,
            path=str(tmp_path / "clip-sync.vp8_40m.webm"),
            codec="vp8_high_quality",
            container="webm",
            render_kind="motion_graphic",
            scene_id="scene-sync",
            status=RenderCacheStatus.RENDERING,
        )
        project = project.set_render_cache_manifest(manifest)
        renderer = Mock()
        ctrl = SimpleNamespace(
            _current_edit_project=project,
            timeline_panel=Mock(),
            status_bar=Mock(),
            timeline_preview_renderer=renderer,
            _render_cache_workers={manifest.cache_id: Mock()},
        )
        # AUD-2706: the finished handler now retires the worker's registry
        # entry through the guarded release — bind the real helper (the
        # Mock worker reports isFinished() → True, so the entry is dropped
        # exactly like the old unconditional ``del`` did).
        ctrl._release_render_cache_worker_slot = (
            EditControllerMixin._release_render_cache_worker_slot.__get__(
                ctrl, EditControllerMixin,
            )
        )

        handler = EditControllerMixin._on_render_cache_finished.__get__(
            ctrl, EditControllerMixin,
        )
        final_path = str(tmp_path / "clip-sync.vp8_40m.webm")
        handler(manifest.cache_id, final_path)

        renderer.set_project.assert_called_once_with(ctrl._current_edit_project)
        updated = ctrl._current_edit_project.get_render_cache_for_clip(
            manifest.timeline_id, manifest.clip_id,
        )
        assert updated is not None
        assert updated.status == RenderCacheStatus.VALID
        assert updated.path == final_path


class TestCpuCompClipRenderer:
    """Regression coverage for headless Comp Clip cache rendering."""

    def test_empty_comp_clip_renders_without_gpu_context(self):
        from src.features.edit.core.project import EditProject
        from src.features.edit.core.render_cache import RenderCacheManifest
        from src.features.edit.core.render_cache_worker import _CpuCompClipRenderer

        project = EditProject(project_id="cpu-comp", name="CPU Comp")
        project, _comp_clip_id, _asset_id, graph_id = project.create_empty_comp_clip(
            width=8,
            height=4,
            background_color=(0.25, 0.5, 0.75, 1.0),
        )
        manifest = RenderCacheManifest(
            timeline_id="",
            clip_id="",
            composite_id=graph_id,
            width=8,
            height=4,
        )

        renderer = _CpuCompClipRenderer(
            manifest=manifest,
            edit_project=project,
            timeline_sequences={},
            graph_data=project.compositions[graph_id],
        )
        try:
            frame = renderer.render_frame(0)
        finally:
            renderer.close()

        assert frame is not None
        assert frame.shape == (4, 8, 3)
        assert frame.dtype == np.uint8
        assert np.any(frame != 0)


# =============================================================================
# T-03 后半段：单层直通图的平面（planar）产出
# =============================================================================


def _passthrough_manifest():
    from src.features.edit.core.render_cache import RenderCacheManifest
    return RenderCacheManifest(
        timeline_id="tl-pp",
        clip_id="clip-pp",
        composite_id="graph-pp",
        width=320,
        height=180,
        codec="topos_proxy",
    )


def _planar_frame(width=320, height=180, pixel_format="yuv422p10le"):
    from src.shared.media.frame import DecodedFrame
    import numpy as np
    y = np.zeros((height, width), dtype=np.uint16)
    u = np.zeros((height, width // 2), dtype=np.uint16)
    v = np.zeros((height, width // 2), dtype=np.uint16)
    return DecodedFrame(
        frame_index=7,
        pts=7,
        time_seconds=7.0 / 25.0,
        planes=(y, u, v),
        pixel_format=pixel_format,
        width=width,
        height=height,
    )


def _passthrough_clip(**overrides):
    """A frozen-dataclass stand-in for the resolved source clip."""
    from dataclasses import dataclass, field as dc_field

    @dataclass(frozen=True)
    class _FakeClip:
        clip_id: str = "clip-pp"
        asset_id: str = "asset-pp"
        timeline_start: int = 0
        timeline_end: int = 100
        color_stack_id: object = None
        effects: tuple = dc_field(default=())
        blend_mode: object = None
        composite_id: object = None
        composite_bypassed: bool = False

        def timeline_to_source_frame(self, frame):
            return int(frame)

    fields = {"clip_id": "clip-pp", "asset_id": "asset-pp"}
    fields.update(overrides)
    return _FakeClip(**fields)


def _passthrough_renderer(clip, *, decoded_factory=None):
    """Build a _CpuCompClipRenderer shell with a fake worker injected."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.features.edit.core.render_cache_worker import _CpuCompClipRenderer

    renderer = _CpuCompClipRenderer.__new__(_CpuCompClipRenderer)
    renderer._manifest = _passthrough_manifest()
    renderer._graph_data = {"nodes": ()}
    renderer._is_constant_graph = False

    worker = MagicMock()
    worker._asset_resolver.get_asset_path.return_value = "/media/asset.mov"
    if decoded_factory is None:
        worker._get_decoded_frame_for_cache_passthrough.return_value = None
    else:
        worker._get_decoded_frame_for_cache_passthrough.side_effect = (
            lambda *a, **k: decoded_factory()
        )
    worker._composite_layers.return_value = np.full((180, 320, 3), 128, np.uint8)
    renderer._worker = worker
    renderer._source_clip = clip
    renderer._source_track = SimpleNamespace(index=0, name="RC")
    renderer._clip_layer_info_type = _real_clip_layer_info()
    return renderer


def _real_clip_layer_info():
    from src.features.edit.core.timeline_render_plan import ClipLayerInfo
    return ClipLayerInfo


class TestCompClipPlanarPassthrough:
    """Eligibility + wiring for the planar cache fast path (T-03 后半段)."""

    def test_whitelist_matches_encoder_contract(self):
        """The passthrough format set must be the encoder's own whitelist."""
        from src.features.edit.core.render_cache_worker import (
            _PASSTHROUGH_PLANAR_FORMATS,
        )
        from src.shared.export.topos_encoder import _PLANAR_FMT_3, _PLANAR_FMT_4

        assert _PASSTHROUGH_PLANAR_FORMATS == frozenset(
            _PLANAR_FMT_3 + _PLANAR_FMT_4
        )

    def test_eligible_frame_returns_decoded_object(self):
        from src.shared.media.frame import DecodedFrame

        clip = _passthrough_clip()
        renderer = _passthrough_renderer(
            clip, decoded_factory=lambda: _planar_frame()
        )
        result = renderer._planar_passthrough_frame(clip, 7)

        assert isinstance(result, DecodedFrame)
        assert result.is_planar
        assert result.pixel_format == "yuv422p10le"
        # The worker decoded exactly once with the clip's stream identity.
        renderer._worker._get_decoded_frame_for_cache_passthrough.assert_called_once_with(
            "/media/asset.mov", 7, "asset-pp", "clip-pp"
        )

    def test_render_frame_prefers_passthrough_and_skips_composite(self):
        """render_frame wiring: eligible → DecodedFrame, no composite call."""
        clip = _passthrough_clip()
        renderer = _passthrough_renderer(
            clip, decoded_factory=lambda: _planar_frame()
        )
        result = renderer.render_frame(7)

        assert result.is_planar
        renderer._worker._composite_layers.assert_not_called()

    def test_render_frame_falls_back_to_composite_when_not_eligible(self):
        clip = _passthrough_clip()  # decode returns None → not eligible
        renderer = _passthrough_renderer(clip)
        result = renderer.render_frame(7)

        assert result is not None
        assert result.shape == (180, 320, 3)
        renderer._worker._composite_layers.assert_called_once()

    def test_grade_baked_in_clip_rejects_passthrough(self):
        clip = _passthrough_clip(color_stack_id="stack-1")
        renderer = _passthrough_renderer(
            clip, decoded_factory=lambda: _planar_frame()
        )
        assert renderer._planar_passthrough_frame(clip, 0) is None
        # Graded frames must not decode twice: rejection happens before decode.
        renderer._worker._get_decoded_frame_for_cache_passthrough.assert_not_called()

    def test_clip_effects_reject_passthrough(self):
        clip = _passthrough_clip(effects=(object(),))
        renderer = _passthrough_renderer(
            clip, decoded_factory=lambda: _planar_frame()
        )
        assert renderer._planar_passthrough_frame(clip, 0) is None
        renderer._worker._get_decoded_frame_for_cache_passthrough.assert_not_called()

    def test_blend_mode_rejects_passthrough(self):
        clip = _passthrough_clip(blend_mode="add")
        renderer = _passthrough_renderer(
            clip, decoded_factory=lambda: _planar_frame()
        )
        assert renderer._planar_passthrough_frame(clip, 0) is None

    def test_geometry_mismatch_rejects_passthrough(self):
        clip = _passthrough_clip()
        renderer = _passthrough_renderer(
            clip,
            decoded_factory=lambda: _planar_frame(width=640, height=360),
        )
        assert renderer._planar_passthrough_frame(clip, 0) is None

    def test_packed_frame_rejects_passthrough(self):
        from src.shared.media.frame import DecodedFrame

        clip = _passthrough_clip()
        packed = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=np.zeros((180, 320, 3), np.uint8),
            pixel_format="bgr24",
            width=320,
            height=180,
        )
        renderer = _passthrough_renderer(clip, decoded_factory=lambda: packed)
        assert renderer._planar_passthrough_frame(clip, 0) is None

    def test_non_whitelisted_planar_format_rejects_passthrough(self):
        """4:2:0 平面不在 Topos v1 编码器白名单：必须显式回退，不能直通。"""
        clip = _passthrough_clip()
        renderer = _passthrough_renderer(
            clip,
            decoded_factory=lambda: _planar_frame(pixel_format="yuv420p"),
        )
        assert renderer._planar_passthrough_frame(clip, 0) is None

    def test_decode_miss_falls_back_to_composite(self):
        clip = _passthrough_clip()
        renderer = _passthrough_renderer(clip)  # factory None → decode miss
        assert renderer._planar_passthrough_frame(clip, 0) is None
        renderer._worker._get_decoded_frame_for_cache_passthrough.assert_called_once()


class TestRenderCacheInvalidation:
    """Test cache invalidation methods on EditProject."""

    def _make_project_with_cache(self):
        from src.features.edit.core.project import EditProject
        project = EditProject(project_id="test-rc", name="test")

        # Add a manifest
        manifest = RenderCacheManifest(
            cache_id="cache-1",
            timeline_id="tl-1",
            clip_id="clip-1",
            composite_id="graph-1",
            color_stack_revision=1,
            composite_graph_revision=1,
            start_frame=0,
            duration_frames=100,
            width=1920,
            height=1080,
            path="Cache/clip_cache-1.mov",
            status=RenderCacheStatus.VALID,
        )
        project = project.set_render_cache_manifest(manifest)
        return project

    def test_invalidate_clip_marks_stale(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_clip("tl-1", "clip-1")
        m = result.render_cache_manifests["cache-1"]
        restored = RenderCacheManifest.from_dict(m)
        assert restored.status == RenderCacheStatus.STALE

    def test_invalidate_wrong_clip_no_change(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_clip("tl-1", "other-clip")
        m = result.render_cache_manifests["cache-1"]
        restored = RenderCacheManifest.from_dict(m)
        assert restored.status == RenderCacheStatus.VALID

    def test_invalidate_composite_marks_stale(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_composite("graph-1")
        m = result.render_cache_manifests["cache-1"]
        restored = RenderCacheManifest.from_dict(m)
        assert restored.status == RenderCacheStatus.STALE

    def test_invalidate_wrong_composite_no_change(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_composite("other-graph")
        m = result.render_cache_manifests["cache-1"]
        restored = RenderCacheManifest.from_dict(m)
        assert restored.status == RenderCacheStatus.VALID

    def test_invalidate_timeline_marks_stale(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_timeline("tl-1")
        m = result.render_cache_manifests["cache-1"]
        restored = RenderCacheManifest.from_dict(m)
        assert restored.status == RenderCacheStatus.STALE

    def test_invalidate_wrong_timeline_no_change(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_timeline("other-tl")
        m = result.render_cache_manifests["cache-1"]
        restored = RenderCacheManifest.from_dict(m)
        assert restored.status == RenderCacheStatus.VALID

    def test_invalidate_does_not_delete_manifest(self):
        project = self._make_project_with_cache()
        result = project.invalidate_render_cache_for_clip("tl-1", "clip-1")
        assert "cache-1" in result.render_cache_manifests

    def test_multiple_caches_invalidate_selectively(self):
        from src.features.edit.core.project import EditProject
        project = EditProject(project_id="test-multi", name="test")

        m1 = RenderCacheManifest(
            cache_id="cache-1", timeline_id="tl-1", clip_id="clip-1",
            composite_id="graph-1", status=RenderCacheStatus.VALID,
        )
        m2 = RenderCacheManifest(
            cache_id="cache-2", timeline_id="tl-1", clip_id="clip-2",
            composite_id="graph-2", status=RenderCacheStatus.VALID,
        )
        project = project.set_render_cache_manifest(m1)
        project = project.set_render_cache_manifest(m2)

        # Invalidate only graph-1
        result = project.invalidate_render_cache_for_composite("graph-1")

        r1 = RenderCacheManifest.from_dict(result.render_cache_manifests["cache-1"])
        r2 = RenderCacheManifest.from_dict(result.render_cache_manifests["cache-2"])

        assert r1.status == RenderCacheStatus.STALE
        assert r2.status == RenderCacheStatus.VALID  # unaffected
