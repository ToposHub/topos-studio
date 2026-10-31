"""T-P1-21 batch 2: effect-parameter lifecycle regression (card step 5).

The audit card requires 参数动画、撤销、保存重载、导出取消 as per-effect
regressions. The batch-2 quality tiers (blur ``quality``, denoise
``quality``, defocus ``mode``) are plain schema params, so they inherit
every lifecycle path — these tests pin that inheritance down:

* save/reload — the tier survives EffectInstance/ClipInstance
  serialization round-trips (a dropped option silently reverts render
  quality, which is exactly the kind of drift that never fails loudly);
* param animation — an animated radius resolves per frame through the
  SAME sampling the CPU export uses (``_sample_effect_params``) and the
  sampled values actually change the rendered result;
* undo — effect-scoped keyframe commands round-trip through CommandStack
  undo/redo;
* export cancel — a real mid-export cancel reports ``cancelled=True`` /
  ``success=False`` (contract of the cancellation branch at the export
  loop), not a fake success.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (
    AddKeyframeCommand,
    CommandStack,
    RemoveKeyframeCommand,
)
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportSource,
    TimelineExportConfig,
    TimelineExporter,
    _cpu_blur,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


# ── save/reload ──────────────────────────────────────────────────────────────

class TestQualityParamSaveReload:
    def test_blur_quality_survives_instance_roundtrip(self) -> None:
        original = EffectInstance(
            effect_id="e1", effect_type="blur", is_enabled=True,
            params={"radius": 16.0, "quality": "fast", "mix": 1.0},
        )
        restored = EffectInstance.from_dict(original.to_dict())
        assert restored.params.get("quality") == "fast"

    def test_denoise_quality_survives_instance_roundtrip(self) -> None:
        original = EffectInstance(
            effect_id="e2", effect_type="denoise", is_enabled=True,
            params={"strength": 0.8, "radius": 3, "quality": "high"},
        )
        restored = EffectInstance.from_dict(original.to_dict())
        assert restored.params.get("quality") == "high"

    def test_quality_survives_clip_level_roundtrip(self) -> None:
        """Through the full clip dict — the save-file shape."""
        effect = EffectInstance(
            effect_id="e3", effect_type="blur", is_enabled=True,
            params={"radius": 16.0, "quality": "fast"},
        )
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10, effects=(effect,),
        )
        restored_clip = ClipInstance.from_dict(clip.to_dict())
        assert restored_clip.effects[0].params.get("quality") == "fast"


# ── param animation ──────────────────────────────────────────────────────────

def _blur_effect_with_radius_keys() -> EffectInstance:
    effect = EffectInstance(
        effect_id="anim", effect_type="blur", is_enabled=True,
        params={"radius": 2.0, "quality": "normal", "mix": 1.0},
    )
    effect = effect.with_keyframe_added("radius", 0, 2.0, max_value=100.0)
    return effect.with_keyframe_added("radius", 10, 30.0, max_value=100.0)


class TestEffectParamAnimation:
    def test_radius_interpolates_per_frame(self) -> None:
        effect = _blur_effect_with_radius_keys()
        assert effect.get_param_at_frame("radius", 0) == pytest.approx(2.0)
        assert effect.get_param_at_frame("radius", 10) == pytest.approx(30.0)
        assert 2.0 < effect.get_param_at_frame("radius", 5) < 30.0

    def test_sample_effect_params_resolves_animation(self) -> None:
        """The CPU export's own sampler must return animated values —
        not the static params dict (the P5.3 contract)."""
        effect = _blur_effect_with_radius_keys()
        p0 = TimelineExporter._sample_effect_params(effect, frame_idx=0)
        p10 = TimelineExporter._sample_effect_params(effect, frame_idx=10)
        assert p0["radius"] == pytest.approx(2.0)
        assert p10["radius"] == pytest.approx(30.0)
        # static tier param rides along untouched
        assert p0["quality"] == "normal"

    def test_sampled_animation_changes_rendered_result(self) -> None:
        """End-to-end through the consumer: the same frame blurred with
        the frame-0 sampled params vs frame-10 sampled params must differ
        in smoothing strength — animation that doesn't move pixels is a
        silent defect."""
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        frame[:, 16:] = 255
        effect = _blur_effect_with_radius_keys()
        small = _cpu_blur(frame, TimelineExporter._sample_effect_params(effect, 0))
        large = _cpu_blur(frame, TimelineExporter._sample_effect_params(effect, 10))
        luma = lambda img: img[:, :, 0].astype(np.float32)
        step_small = np.abs(np.diff(luma(small), axis=1)).max()
        step_large = np.abs(np.diff(luma(large), axis=1)).max()
        assert step_large < step_small, (
            "animated radius did not change blur strength between keyframes")


# ── undo / redo ──────────────────────────────────────────────────────────────

def _timeline_with_effect() -> TimelineSequence:
    timeline = TimelineSequence(
        timeline_id="t", name="T", fps=25, width=64, height=48,
        sample_rate=48000,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = timeline.with_track_added(track)
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=20,
        source_start=0, source_duration=20,
    )
    timeline = timeline.with_clip_added("v1", clip)
    effect = EffectInstance(
        effect_id="e1", effect_type="blur", is_enabled=True,
        params={"radius": 4.0, "quality": "fast"},
    )
    clip = timeline.get_track_by_id("v1").get_clip_by_id("c1")
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_clip_updated(
            clip.with_effect_added(effect)))
    return timeline


def _effect_of(timeline: TimelineSequence) -> EffectInstance:
    return timeline.get_track_by_id("v1").get_clip_by_id("c1") \
        .get_effect_by_id("e1")


class TestEffectKeyframeUndo:
    def test_keyframe_add_undo_redo(self) -> None:
        timeline = _timeline_with_effect()
        stack = CommandStack()
        cmd = AddKeyframeCommand.create(
            track_id="v1", clip_id="c1", param_name="radius",
            frame=8, value=24.0, effect_id="e1",
        )
        after = stack.execute(cmd, timeline)
        assert _effect_of(after).has_keyframe_at("radius", 8)

        undone = stack.undo(after)
        assert undone is not None
        assert not _effect_of(undone).has_keyframe_at("radius", 8)

        redone = stack.redo(undone)
        assert redone is not None
        assert _effect_of(redone).has_keyframe_at("radius", 8)

    def test_keyframe_remove_undo_restores(self) -> None:
        timeline = _timeline_with_effect()
        add = AddKeyframeCommand.create(
            track_id="v1", clip_id="c1", param_name="radius",
            frame=8, value=24.0, effect_id="e1",
        )
        timeline = add.apply(timeline)
        remove = RemoveKeyframeCommand(
            command_id="rm-kf-1", label="remove radius keyframe",
            track_id="v1", clip_id="c1", param_name="radius",
            frame=8, effect_id="e1",
        )
        after_remove = remove.apply(timeline)
        assert not _effect_of(after_remove).has_keyframe_at("radius", 8)
        restored = remove.undo(after_remove)
        assert _effect_of(restored).has_keyframe_at("radius", 8)


# ── export cancel ────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "tests" / "test_videos" / "2K_topos_422_Proxy.mov"


def _video_only_build() -> tuple:
    from src.features.edit.core.project import EditProject, MediaAsset

    asset = MediaAsset(
        asset_id="src", path=str(SOURCE), media_type="video",
        display_name="src", metadata={"fps": 25.0, "total_frames": 300},
    )
    project = EditProject.create(name="cancel-regression")
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="c1", asset_id="src", track_id="v1",
        timeline_start=0, timeline_duration=48,
        source_start=0, source_duration=48,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    seq = TimelineSequence(timeline_id="s", name="Cancel", fps=Rational(25, 1),
                           width=1920, height=1080, sample_rate=48000,
                           video_tracks=(v1,))
    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="s",
        output_path="", codec=ExportCodec.H264, pixel_format="yuv420p",
        quality=4_000_000, quality_mode="vbr", fps=Rational(25, 1),
        include_audio=False, burn_subtitles=False,
    )
    return project, seq, cfg


class TestExportCancel:
    @pytest.mark.skipif(not SOURCE.is_file(), reason="真实素材缺失")
    def test_mid_export_cancel_reports_user_cancelled(self, tmp_path) -> None:
        out = tmp_path / "cancelled.mov"
        project, seq, cfg = _video_only_build()
        cfg = type(cfg)(**{**{f.name: getattr(cfg, f.name)
                              for f in cfg.__dataclass_fields__.values()},
                           "output_path": str(out)})
        def cancel_early(current: int, total: int, pct: float) -> None:
            if current >= 5:
                exporter.cancel_export()

        # export_timeline 不收回调；进度回调经工厂/构造器注入。
        exporter = create_timeline_exporter(
            project, seq, progress_callback=cancel_early)
        outcome = exporter.export_timeline(cfg)
        assert not bool(outcome), "取消后不得返回成功"
        assert outcome.cancelled is True, (
            f"必须走 user_cancelled 分支（cause={getattr(outcome, 'cause', '')}）")
