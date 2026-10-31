"""P1-11: clip-scoped scene cut detection must respect the clip source range.

Contract (from the professional-gap audit):
- ``start_detection_for_clip`` resolves the timeline clip's
  source_start/source_duration and bakes the range into the detection
  settings (sampler, task key, disk cache and subclip generator all
  honour start/end_source_frame).
- A clip missing from the timeline or an empty source range is an explicit
  error — never a silent fallback to the full asset range.
- The session analysis key includes the range, so re-trimming a clip and
  re-running detection does not serve the old range's cached analysis.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.scene_cut.models import (
    SceneCutDecision,
    SceneCutSettings,
)
from src.features.edit.core.scene_cut_controller import (
    DetectionContext,
    SceneCutController,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _asset(asset_id: str = "asset-video", total_frames: int = 1000) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path="/tmp/source.mp4",
        media_type="video",
        display_name="Source.mp4",
        metadata={"fps": 24.0, "total_frames": total_frames},
    )


def _timeline(clip: ClipInstance) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-scene-cut",
        name="Scene Cut",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=(clip,),
            ),
        ),
    )


def _project(asset: MediaAsset) -> EditProject:
    project = EditProject(project_id="proj-scene-cut", name="Scene Cut")
    project.assets[asset.asset_id] = asset
    return project


@pytest.fixture()
def controller_env(qapp):
    """Build a controller wired to a project + timeline with a trimmed clip.

    The clip uses frames [120, 420) of a 1000-frame asset.
    """
    asset = _asset()
    clip = ClipInstance(
        clip_id="clip-1",
        asset_id=asset.asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=300,
        source_start=120,
        source_duration=300,
    )
    timeline = _timeline(clip)
    project = _project(asset)

    controller = SceneCutController(
        project_getter=lambda: project,
        timeline_getter=lambda: timeline,
    )
    yield controller, clip, asset

    if controller._active_dialog is not None:
        controller._active_dialog.close()
        controller._active_dialog = None


class TestStartDetectionForClip:
    def test_settings_carry_clip_source_range(self, controller_env):
        controller, clip, asset = controller_env

        errors = []
        controller.detection_error.connect(lambda aid, msg: errors.append((aid, msg)))

        controller.start_detection_for_clip(clip.clip_id, "v1", asset.asset_id)

        assert errors == []
        dialog = controller._active_dialog
        assert dialog is not None
        assert dialog._settings.start_source_frame == 120
        assert dialog._settings.end_source_frame == 420

    def test_clip_not_on_timeline_is_an_error_not_full_range(self, controller_env):
        controller, _clip, asset = controller_env

        errors = []
        controller.detection_error.connect(lambda aid, msg: errors.append((aid, msg)))

        controller.start_detection_for_clip("ghost-clip", "v1", asset.asset_id)

        assert len(errors) == 1
        assert errors[0][0] == asset.asset_id
        assert controller._active_dialog is None

    def test_no_timeline_getter_is_an_error(self, controller_env):
        controller, clip, asset = controller_env
        controller._timeline_getter = None

        errors = []
        controller.detection_error.connect(lambda aid, msg: errors.append((aid, msg)))

        controller.start_detection_for_clip(clip.clip_id, "v1", asset.asset_id)

        assert len(errors) == 1
        assert controller._active_dialog is None

    def test_empty_source_range_is_an_error(self, controller_env, qapp):
        controller, clip, asset = controller_env
        # Clip claims frames [1500, 1600) of a 1000-frame asset — after
        # clamping to asset bounds the effective range is empty.
        beyond_clip = ClipInstance(
            clip_id="clip-beyond",
            asset_id=asset.asset_id,
            track_id="v1",
            timeline_start=0,
            timeline_duration=100,
            source_start=1500,
            source_duration=100,
        )
        controller._timeline_getter = lambda: _timeline(beyond_clip)

        errors = []
        controller.detection_error.connect(lambda aid, msg: errors.append((aid, msg)))

        controller.start_detection_for_clip("clip-beyond", "v1", asset.asset_id)

        assert len(errors) == 1
        assert controller._active_dialog is None

    def test_range_is_clamped_to_asset_bounds(self, controller_env, qapp):
        controller, _clip, asset = controller_env
        # Clip claims frames [900, 1200) of a 1000-frame asset.
        overrun_clip = ClipInstance(
            clip_id="clip-overrun",
            asset_id=asset.asset_id,
            track_id="v1",
            timeline_start=0,
            timeline_duration=300,
            source_start=900,
            source_duration=300,
        )
        controller._timeline_getter = lambda: _timeline(overrun_clip)

        errors = []
        controller.detection_error.connect(lambda aid, msg: errors.append((aid, msg)))

        controller.start_detection_for_clip("clip-overrun", "v1", asset.asset_id)

        assert errors == []
        assert controller._active_dialog._settings.start_source_frame == 900
        assert controller._active_dialog._settings.end_source_frame == 1000

    def test_media_pool_entry_point_stays_full_range(self, controller_env):
        """start_detection_for_asset (media pool) must keep full-asset range."""
        controller, _clip, asset = controller_env

        controller.start_detection_for_asset(asset.asset_id)

        dialog = controller._active_dialog
        assert dialog is not None
        assert dialog._settings.start_source_frame == 0
        assert dialog._settings.end_source_frame is None


class TestSessionKeyIncludesRange:
    def test_different_ranges_have_different_keys(self):
        ctx_full = DetectionContext(
            asset_id="a", asset_path="/tmp/a.mov",
            source_start=0, source_duration=None,
        )
        ctx_trim = DetectionContext(
            asset_id="a", asset_path="/tmp/a.mov",
            source_start=120, source_duration=300,
        )

        controller = SceneCutController()

        assert controller._analysis_key(ctx_full) != controller._analysis_key(ctx_trim)

    def test_same_context_key_is_stable(self):
        ctx = DetectionContext(
            asset_id="a", asset_path="/tmp/a.mov",
            source_start=120, source_duration=300,
        )
        controller = SceneCutController()

        assert controller._analysis_key(ctx) == controller._analysis_key(ctx)


class TestManualCutRangeGuard:
    def test_manual_cut_outside_range_is_rejected(self, controller_env, monkeypatch):
        # The guard raises a MODAL QMessageBox — stub it out so the test
        # does not block waiting for a click.
        from src.features.edit.ui import scene_cut_dialog as dialog_module

        shown = []
        monkeypatch.setattr(
            dialog_module.QMessageBox, "warning",
            staticmethod(lambda *a, **k: shown.append(a)),
        )
        controller, clip, asset = controller_env
        controller.start_detection_for_clip(clip.clip_id, "v1", asset.asset_id)
        dialog = controller._active_dialog

        # Park the transport outside the analyzed range [120, 420).
        dialog._transport.set_scrubber_value(700)

        dialog._add_manual_cut()

        assert shown, "expected the out-of-range warning to be shown"
        added = [
            c for c in dialog._model.candidates if "manual" in c.reasons
        ]
        assert added == []

    def test_manual_cut_inside_range_is_added(self, controller_env):
        controller, clip, asset = controller_env
        controller.start_detection_for_clip(clip.clip_id, "v1", asset.asset_id)
        dialog = controller._active_dialog

        dialog._transport.set_scrubber_value(200)

        dialog._add_manual_cut()

        added = [
            c for c in dialog._model.candidates if "manual" in c.reasons
        ]
        assert len(added) == 1
        assert added[0].source_frame == 200
        assert added[0].decision == SceneCutDecision.ACCEPTED


class TestSettingsRangeIntegration:
    def test_with_range_roundtrip_preserves_other_fields(self):
        base = SceneCutSettings(sensitivity=0.7, min_scene_frames=12)

        ranged = base.with_range(120, 420)

        assert ranged.sensitivity == 0.7
        assert ranged.min_scene_frames == 12
        assert ranged.start_source_frame == 120
        assert ranged.end_source_frame == 420
        # The settings hash (task key / cache key) must distinguish ranges.
        assert ranged.to_hash() != base.to_hash()

    def test_with_sensitivity_preserves_range(self):
        ranged = SceneCutSettings().with_range(120, 420)

        retuned = ranged.with_sensitivity(0.4)

        assert retuned.start_source_frame == 120
        assert retuned.end_source_frame == 420
