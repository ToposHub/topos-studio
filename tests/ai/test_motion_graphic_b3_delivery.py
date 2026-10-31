"""B3 delivery layer tests: profiles, context, templates, provenance, audio, export, quality gate.

These lock the MG-11 / MG-12 / MG-13 / MG-14 / MG-15 / MG-17 / MG-18
contracts without spinning up Qt or real providers:

* delivery profiles carry aspect/fps/safe-area/subtitle/codec, registry
  resolves built-ins, responsive layout clamps to safe area (MG-11)
* context collector normalizes host data + fingerprints semantic content so
  drift is detectable (MG-12)
* creative templates + style tokens + brand profiles register and the brand
  flags forbidden colours (MG-13)
* asset provenance tracks license/import state and blocks commercial export
  when either is missing (MG-14)
* audio plan models tracks / beats / subtitles and flags duration overflow
  (MG-15)
* export job runs atomically: preflight refuses overwrite without approval,
  postflight flags missing/empty files, finish() refuses to clobber (MG-17)
* quality gate aggregates frame render, asset, audio, loudness, flicker and
  design findings, and is_deliverable only when no errors (MG-18)
"""

from __future__ import annotations

import os

import pytest

from src.features.teaching_animation.ai.asset_provenance import (
    AssetProvenance,
    AssetProvenanceStore,
    IMPORT_APPROVED,
    LICENSE_MISSING,
    LICENSE_OK,
    LICENSE_RESTRICTED,
    hash_prompt,
)
from src.features.teaching_animation.ai.audio_plan import (
    AudioPlan,
    AudioTrack,
    BeatMarker,
    KIND_MUSIC,
    KIND_NARRATION,
    SubtitleMapping,
)
from src.features.teaching_animation.ai.context_collector import (
    ProjectContextCollector,
)
from src.features.teaching_animation.ai.creative_templates import (
    BrandProfile,
    CATEGORY_SOCIAL_CARD,
    CATEGORY_TUTORIAL,
    CreativeTemplate,
    CreativeTemplateRegistry,
    StyleTokenSet,
    create_default_creative_registry,
)
from src.features.teaching_animation.ai.delivery_profile import (
    BUILTIN_PROFILES,
    DeliveryProfile,
    DeliveryProfileRegistry,
    ResponsiveLayout,
    SafeArea,
    SubtitleRegion,
    builtin_profile,
)
from src.features.teaching_animation.ai.export_job import (
    EXPORT_CANCELLED,
    EXPORT_FAILED,
    EXPORT_SUCCEEDED,
    ExportJob,
    ExportRequest,
    new_export_job,
    postflight_from_stat,
    preflight_disk,
)
from src.features.teaching_animation.ai.quality_gate import (
    MotionGraphicQualityGate,
    QualityGateInputs,
    RenderFrameResult,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument


# --------------------------------------------------------------------------- #
# MG-11: delivery profile
# --------------------------------------------------------------------------- #


def test_builtin_profiles_cover_common_aspects() -> None:
    aspects = {profile.canvas_aspect for profile in BUILTIN_PROFILES}
    assert "16:9" in aspects
    assert "9:16" in aspects
    assert "1:1" in aspects


def test_builtin_profile_lookup_by_id() -> None:
    profile = builtin_profile("youtube_1080p_landscape")
    assert profile.width == 1920
    assert profile.height == 1080
    assert profile.fps == 30.0


def test_delivery_profile_rejects_unsupported_codec() -> None:
    with pytest.raises(ValueError):
        DeliveryProfile(
            profile_id="x", name="x", canvas_aspect="16:9",
            width=1920, height=1080, codec="totally_invented",
        )


def test_title_safe_rect_is_inside_canvas() -> None:
    profile = builtin_profile("youtube_1080p_landscape")
    x, y, w, h = profile.title_safe_rect()
    assert x > 0 and y > 0
    assert x + w <= profile.width
    assert y + h <= profile.height


def test_responsive_layout_clamps_to_safe_area() -> None:
    profile = builtin_profile("youtube_1080p_landscape")
    layout = ResponsiveLayout(profile)
    # An element nominally at (-0.5, -0.5) (off-canvas) should be pulled inside.
    x, y, w, h = layout.pixel_rect(-0.5, -0.5, 0.2, 0.1)
    safe_x, safe_y, _, _ = profile.title_safe_rect()
    assert x >= safe_x
    assert y >= safe_y


def test_profile_registry_round_trip() -> None:
    registry = DeliveryProfileRegistry()
    custom = DeliveryProfile(
        profile_id="custom_4k", name="Custom 4K", canvas_aspect="16:9",
        width=3840, height=2160,
    )
    registry.register(custom)
    assert registry.get("custom_4k") is custom
    assert registry.profiles_for_aspect("16:9")


# --------------------------------------------------------------------------- #
# MG-12: context collector
# --------------------------------------------------------------------------- #


def test_context_collector_fingerprint_is_stable_for_same_content() -> None:
    collector = ProjectContextCollector(time_fn=lambda: 1000.0)
    common = dict(
        project_id="p1",
        project_revision=3,
        timeline={
            "timeline_id": "t1", "revision": 5, "track_id": "g1",
            "insert_frame": 0,
        },
        delivery_profile_id="youtube_1080p_landscape",
        selected_assets=[{
            "asset_id": "a1", "label": "Logo", "kind": "image",
            "fingerprint": "fp-1",
        }],
    )
    snap_a = collector.collect(**common)
    snap_b = collector.collect(**common)
    assert snap_a.fingerprint() == snap_b.fingerprint()


def test_context_collector_fingerprint_changes_with_timeline_revision() -> None:
    collector = ProjectContextCollector()
    base = dict(
        project_id="p1", project_revision=1,
        delivery_profile_id="youtube_1080p_landscape",
    )
    snap_a = collector.collect(timeline={"timeline_id": "t1", "revision": 1, "insert_frame": 0}, **base)
    snap_b = collector.collect(timeline={"timeline_id": "t1", "revision": 2, "insert_frame": 0}, **base)
    assert snap_a.fingerprint() != snap_b.fingerprint()


def test_context_collector_serializes_to_json_safe_dict() -> None:
    import json
    collector = ProjectContextCollector()
    snap = collector.collect(
        project_id="p1", project_revision=1,
        timeline={"timeline_id": "t1", "revision": 1, "insert_frame": 0},
        delivery_profile_id="youtube_1080p_landscape",
        brand={"brand_id": "b1", "label": "Brand"},
    )
    serialized = json.dumps(snap.to_dict(), default=str)
    restored = json.loads(serialized)
    assert restored["project_id"] == "p1"
    assert restored["brand"]["brand_id"] == "b1"


# --------------------------------------------------------------------------- #
# MG-13: creative templates
# --------------------------------------------------------------------------- #


def test_default_creative_registry_seeds_templates_and_tokens() -> None:
    registry = create_default_creative_registry()
    assert registry.template("product_launch_15s") is not None
    assert registry.template("tutorial_intro_15s") is not None
    assert registry.tokens("tech_cool") is not None


def test_brand_flags_forbidden_colors() -> None:
    brand = BrandProfile(
        brand_id="b1", label="Brand",
        forbidden_colors=("#ff0000",),
    )
    tokens = StyleTokenSet(token_id="t", label="t", palette={"accent": "#FF0000"})
    conflicts = brand.conflicts(tokens)
    assert conflicts
    assert any("ff0000" in c.lower() for c in conflicts)


def test_template_compatibility_flags_duration_and_aspect() -> None:
    registry = create_default_creative_registry()
    template = registry.template("social_card_9x16")
    assert template is not None
    issues = registry.compatibility_issues(
        template,
        duration_seconds=120.0,
        canvas_aspect="16:9",
        transparent=True,
    )
    # max_duration_seconds for social_card_9x16 is 30.0s, request 120s → flagged.
    assert any("30" in issue for issue in issues)
    # social_card_9x16 doesn't declare transparent support
    assert any("透明" in issue for issue in issues)


# --------------------------------------------------------------------------- #
# MG-14: asset provenance
# --------------------------------------------------------------------------- #


def test_provenance_store_blocks_commercial_export_for_missing_license() -> None:
    store = AssetProvenanceStore()
    store.record(AssetProvenance(
        asset_id="a1", source_kind="generated",
        license_state=LICENSE_MISSING, import_state=IMPORT_APPROVED,
        license_scope=("commercial",),
    ))
    blocking = store.blocking_for_export(("a1",))
    assert blocking
    assert not blocking[0].may_enter_commercial_export


def test_provenance_store_allows_commercial_export_when_ok() -> None:
    store = AssetProvenanceStore()
    store.record(AssetProvenance(
        asset_id="a1", source_kind="downloaded",
        license_state=LICENSE_OK, import_state=IMPORT_APPROVED,
        license_scope=("commercial", "web"),
    ))
    blocking = store.blocking_for_export(("a1",))
    assert not blocking


def test_provenance_set_license_appends_history() -> None:
    store = AssetProvenanceStore()
    store.record(AssetProvenance(asset_id="a1", source_kind="generated"))
    store.set_license_state("a1", license_state=LICENSE_OK, license_scope=("commercial",))
    history = store.history("a1")
    assert len(history) == 2
    assert history[-1].license_state == LICENSE_OK


def test_hash_prompt_never_exposes_raw_prompt() -> None:
    h = hash_prompt("a very secret prompt")
    assert h != "a very secret prompt"
    assert len(h) == 64


# --------------------------------------------------------------------------- #
# MG-15: audio plan
# --------------------------------------------------------------------------- #


def test_audio_plan_duration_and_narration_lookup() -> None:
    plan = AudioPlan(
        tracks=(
            AudioTrack("n1", KIND_NARRATION, "Voiceover", duration_seconds=15.0),
            AudioTrack("m1", KIND_MUSIC, "BGM", duration_seconds=15.0),
        ),
    )
    assert plan.has_audio
    assert plan.duration_seconds == pytest.approx(15.0)
    assert plan.narration_track().track_id == "n1"
    assert plan.music_track().track_id == "m1"


def test_audio_plan_flags_track_beyond_clip_duration() -> None:
    plan = AudioPlan(
        tracks=(
            AudioTrack("n1", KIND_NARRATION, "Voiceover", duration_seconds=20.0),
        ),
    )
    issues = plan.issues(clip_duration_seconds=15.0)
    assert issues
    assert any("超过" in issue for issue in issues)


def test_audio_plan_hash_is_stable() -> None:
    plan = AudioPlan(tracks=(
        AudioTrack("n1", KIND_NARRATION, "V", duration_seconds=10.0),
    ))
    assert plan.plan_hash() == plan.plan_hash()


# --------------------------------------------------------------------------- #
# MG-17: export job
# --------------------------------------------------------------------------- #


def test_preflight_disk_refuses_overwrite_without_approval(tmp_path) -> None:
    existing = tmp_path / "out.mp4"
    existing.write_bytes(b"old")
    findings = preflight_disk(str(existing), estimated_bytes=10, overwrite_approved=False)
    assert any(f.code == "EXPORT_OVERWRITE_REQUIRED" for f in findings)


def test_postflight_flags_missing_file(tmp_path) -> None:
    report = postflight_from_stat(str(tmp_path / "missing.mp4"))
    assert report.is_blocking
    assert any(f.code == "EXPORT_FILE_MISSING" for f in report.errors)


def test_export_job_finish_replaces_temp_into_final(tmp_path) -> None:
    final = tmp_path / "out.mp4"
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"rendered")
    job = new_export_job(ExportRequest(
        scene_id="s1", output_path=str(final), profile_id="youtube_1080p_landscape",
        width=1920, height=1080,
    ))
    job.start()
    report = postflight_from_stat(str(temp), expected_duration_seconds=15.0)
    job.finish(postflight=report, output_temp_path=str(temp))
    assert job.status == EXPORT_SUCCEEDED
    assert final.exists()
    assert not temp.exists()


def test_export_job_refuses_overwrite_without_approval(tmp_path) -> None:
    final = tmp_path / "out.mp4"
    final.write_bytes(b"old")
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"new")
    job = new_export_job(ExportRequest(
        scene_id="s1", output_path=str(final), profile_id="youtube_1080p_landscape",
        width=1920, height=1080, overwrite_existing=False,
    ))
    job.start()
    report = postflight_from_stat(str(temp))
    job.finish(postflight=report, output_temp_path=str(temp))
    assert job.status == EXPORT_FAILED
    # Existing file untouched.
    assert final.read_bytes() == b"old"


def test_export_job_cancel_marks_cancelled(tmp_path) -> None:
    job = new_export_job(ExportRequest(
        scene_id="s1", output_path=str(tmp_path / "out.mp4"),
        profile_id="youtube_1080p_landscape",
    ))
    job.request_cancel(reason="user")
    job.start()
    assert job.status == EXPORT_CANCELLED


# --------------------------------------------------------------------------- #
# MG-18: quality gate
# --------------------------------------------------------------------------- #


def _scene() -> TeachingSceneDocument:
    scene = TeachingSceneDocument.create_default(
        "Test", width=320, height=180, duration_frames=60,
    )
    # create_default adds a placeholder title that overflows a 320x180 canvas;
    # strip it so the design validator has nothing to flag.
    from dataclasses import replace
    return replace(scene, elements=())


def test_quality_gate_blocks_on_failed_frame_render() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_scene(),
        render_frames=(
            RenderFrameResult(label="first", frame_index=0, succeeded=False, error="boom"),
        ),
    ))
    assert not report.is_deliverable
    assert any(f.check == "first_frame" for f in report.errors)


def test_quality_gate_blocks_on_unresolved_asset() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_scene(),
        asset_ids=("a1", "a2"),
        resolved_asset_ids=("a1",),
    ))
    assert not report.is_deliverable
    assert any(f.check == "asset_resolved" for f in report.errors)


def test_quality_gate_blocks_on_missing_license() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_scene(),
        asset_ids=("a1",),
        resolved_asset_ids=("a1",),
        asset_license_states={"a1": LICENSE_MISSING},
    ))
    assert not report.is_deliverable


def test_quality_gate_flags_audio_duration_mismatch() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_scene(),
        audio_duration_seconds=20.0,
        clip_duration_seconds=15.0,
    ))
    assert any(f.check == "audio_duration" for f in report.findings)


def test_quality_gate_passes_clean_inputs() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_scene(),
        render_frames=(
            RenderFrameResult(label="first", frame_index=0, succeeded=True),
            RenderFrameResult(label="last", frame_index=59, succeeded=True),
        ),
        asset_ids=(),
    ))
    assert report.is_deliverable
    assert report.errors == ()
