"""Regression tests for the batch-D Major audit fixes (wd12-g70).

Covers the teaching_animation/ai front-segment remediation entries:
  * AUD-26600 — ``ai.__all__`` must only export names the package defines
    (the dormant job_dag / lane_scheduler leftovers broke ``import *``).
  * AUD-26603 — the advanced-capability codec whitelist must actually apply.
  * AUD-26608 — AudioPlan license gating aligned with quality_gate /
    asset_provenance (``missing`` AND ``restricted`` block; closed vocabulary).
  * AUD-26702 — the capability manifest must mirror the compiler contract.
  * AUD-26703 — stagger_elements explicit 0 interval/duration survive.
  * AUD-26704 — insert_keyframe undo restores a replaced same-frame keyframe.
  * AUD-26705 — set_color_token / set_typo_token match the real ``$token``
    scene convention (raw reference or resolved value).
  * AUD-26713 — context provider registry failures are logged, not swallowed.
  * AUD-26800 — supports_4k is enforced against the requested frame size.
  * AUD-26805 — asset findings use MOTION_GRAPHIC_ASSET_UNRESOLVED.
  * AUD-26806 — contrast is measured against the element behind the text.
  * AUD-26814 / AUD-26815 — export job cleans temp files on every non-success
    terminal path and survives OSError during finalize.
  * AUD-26902 / AUD-26903 — page add/remove and quoted-removal postconditions
    no longer misread common Chinese phrasings.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import src.features.teaching_animation.ai as ai_package
from src.features.teaching_animation.ai.advanced_capabilities import (
    CAP_3D_CAMERA,
    create_default_advanced_registry,
)
from src.features.teaching_animation.ai.audio_plan import (
    KIND_NARRATION,
    AudioPlan,
    AudioTrack,
)
from src.features.teaching_animation.ai.capability_context import DEFAULT_COMMANDS
from src.features.teaching_animation.ai.command_catalog import (
    EXTENDED_COMMAND_NAMES,
    ExtendedCommandRegistry,
)
from src.features.teaching_animation.ai.context_provider import _ai_capability_catalog
from src.features.teaching_animation.ai.creative_templates import (
    NON_4K_MAX_EDGE_PIXELS,
    create_default_creative_registry,
)
from src.features.teaching_animation.ai.design_validator import (
    MOTION_GRAPHIC_ASSET_UNRESOLVED,
    MOTION_GRAPHIC_LOW_CONTRAST,
    validate_scene_design,
)
from src.features.teaching_animation.ai.error_codes import MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS
from src.features.teaching_animation.ai.export_job import (
    EXPORT_CANCELLED,
    EXPORT_FAILED,
    EXPORT_FINALIZE_FAILED,
    EXPORT_SUCCEEDED,
    ExportRequest,
    new_export_job,
    postflight_from_stat,
)
from src.features.teaching_animation.ai.html_edit_autonomy import (
    edit_postcondition_issues,
    generation_intent_report,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.shared.keyframe import Keyframe, KeyframeTrack


BASE_COMMAND_NAMES = frozenset({
    "add_element", "update_element", "reorder_element", "set_animation",
})


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default("Test Scene")


def _scene_with_element(element_id: str = "el-1") -> TeachingSceneDocument:
    scene = TeachingSceneDocument.create_default("Test Scene")
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform

    element = SceneElement(
        element_id=element_id,
        type="shape",
        transform=SceneTransform(x=0.0, y=0.0, width=100.0, height=100.0),
        style={"color": "#ff0000", "fill": "#ff0000"},
    )
    return scene.with_element_added(element)


# --------------------------------------------------------------------------- #
# AUD-26600
# --------------------------------------------------------------------------- #


def test_aud26600_all_exports_are_defined_and_unique() -> None:
    names = list(ai_package.__all__)
    assert len(names) == len(set(names)), "duplicate __all__ entries"
    missing = [name for name in names if not hasattr(ai_package, name)]
    assert missing == [], f"__all__ exports undefined names: {missing}"


def test_aud26600_stale_job_dag_and_lane_names_are_gone() -> None:
    stale = (
        "MotionGraphicJobDAG", "MotionGraphicLaneScheduler", "NodeRunState",
        "build_canonical_dag", "new_run", "KIND_OBSERVE", "NODE_PENDING",
        "LaneConfig", "DEFAULT_LANE_CONFIGS", "LANE_GPU_RENDER",
    )
    for name in stale:
        assert name not in ai_package.__all__
        assert not hasattr(ai_package, name)


def test_aud26600_context_provider_alias_is_importable() -> None:
    assert ai_package.TeachingAIContextProvider is not None


# --------------------------------------------------------------------------- #
# AUD-26603
# --------------------------------------------------------------------------- #


def test_aud26603_codec_whitelist_blocks_unlisted_codec() -> None:
    registry = create_default_advanced_registry()
    camera = registry.require(CAP_3D_CAMERA)  # export_backends == ("mp4",)
    issues = registry.compatibility_issues(
        camera, codec="webm", aspect="16:9", preview_backend="moderngl", has_gpu=True,
    )
    assert any("webm" in issue for issue in issues)


def test_aud26603_codec_whitelist_allows_listed_codec() -> None:
    registry = create_default_advanced_registry()
    camera = registry.require(CAP_3D_CAMERA)
    issues = registry.compatibility_issues(
        camera, codec="mp4", aspect="16:9", preview_backend="moderngl", has_gpu=True,
    )
    assert not any("导出编码" in issue for issue in issues)


# --------------------------------------------------------------------------- #
# AUD-26608
# --------------------------------------------------------------------------- #


def test_aud26608_restricted_license_blocks() -> None:
    plan = AudioPlan(tracks=(
        AudioTrack("t1", KIND_NARRATION, "V", duration_seconds=5.0,
                   license_state="restricted"),
    ))
    issues = plan.issues(clip_duration_seconds=10.0)
    assert any("restricted" in issue for issue in issues)


def test_aud26608_missing_license_still_blocks() -> None:
    plan = AudioPlan(tracks=(
        AudioTrack("t1", KIND_NARRATION, "V", duration_seconds=5.0,
                   license_state="missing"),
    ))
    assert plan.issues(clip_duration_seconds=10.0)


def test_aud26608_ok_license_passes() -> None:
    plan = AudioPlan(tracks=(
        AudioTrack("t1", KIND_NARRATION, "V", duration_seconds=5.0,
                   license_state="ok"),
    ))
    assert plan.issues(clip_duration_seconds=10.0) == ()


def test_aud26608_unknown_license_state_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        AudioTrack("t1", KIND_NARRATION, "V", license_state="Missing")


# --------------------------------------------------------------------------- #
# AUD-26702
# --------------------------------------------------------------------------- #


def test_aud26702_every_advertised_command_has_a_compiler() -> None:
    advertised = {command.name for command in DEFAULT_COMMANDS}
    compilable = BASE_COMMAND_NAMES | set(EXTENDED_COMMAND_NAMES)
    unknown = sorted(advertised - compilable)
    assert unknown == [], f"manifest advertises uncompilable commands: {unknown}"
    # The drift commands specifically: removed from the manifest until a
    # compiler exists (set_stagger_interval has a core SceneCommand but no
    # AI-registry wiring, so it must not be advertised either).
    for name in ("set_beat_timing", "set_transition", "set_stagger_interval"):
        assert name not in advertised


def test_aud26702_advertised_param_names_compile() -> None:
    """Args built from the descriptor's own param names must compile."""
    scene = _scene_with_element("el-1")
    registry = ExtendedCommandRegistry()
    cases = {
        "apply_motion_preset": {
            "element_id": "el-1", "preset_id": "fade",
            "param_name": "opacity", "start_frame": 0,
        },
        "stagger_elements": {
            "element_ids": ["el-1"], "preset_id": "fade", "param_name": "opacity",
        },
        "set_brand_theme": {"theme": {"theme_id": "brand-1"}},
        "replace_asset_slot": {
            "element_id": "el-1", "asset": {"asset_id": "a1", "kind": "image"},
        },
    }
    descriptors = {c.name: c for c in DEFAULT_COMMANDS}
    for name, args in cases.items():
        descriptor = descriptors[name]
        for param in descriptor.params:
            if param.required:
                assert param.name in args, f"{name}.{param.name} missing from fixture"
        from src.features.teaching_animation.ai.commands import TeachingAICommand

        compiled = registry.compile(TeachingAICommand(name, args), scene)
        assert compiled is not None, name


def test_aud26702_manifest_required_params_match_compiler() -> None:
    descriptors = {c.name: c for c in DEFAULT_COMMANDS}
    # Compiler-required keys (see _compile_* in command_catalog.py / commands.py).
    required = {
        "apply_motion_preset": {"element_id", "preset_id", "param_name"},
        "stagger_elements": {"element_ids", "preset_id", "param_name"},
        "set_brand_theme": {"theme"},
        "replace_asset_slot": {"element_id", "asset"},
        "add_element": {"element"},
        "update_element": {"element_id", "updates"},
        "set_duration": {"duration_frames"},
        "apply_template": {"template_id"},
    }
    for name, keys in required.items():
        descriptor = descriptors[name]
        advertised = {p.name for p in descriptor.params if p.required}
        assert advertised == keys, (
            f"{name}: manifest requires {sorted(advertised)}, "
            f"compiler requires {sorted(keys)}"
        )


# --------------------------------------------------------------------------- #
# AUD-26703
# --------------------------------------------------------------------------- #


def test_aud26703_stagger_explicit_zero_interval_is_preserved() -> None:
    registry = ExtendedCommandRegistry()
    scene = _scene_with_element("el-1")
    from src.features.teaching_animation.ai.commands import TeachingAICommand

    command = registry.compile(
        TeachingAICommand("stagger_elements", {
            "element_ids": ["el-1"], "preset_id": "fade",
            "param_name": "opacity", "interval": 0,
        }),
        scene,
    )
    assert getattr(command, "interval", None) == 0


def test_aud26703_stagger_explicit_zero_duration_is_rejected() -> None:
    from src.features.teaching_animation.ai.commands import (
        TeachingAICommand, TeachingAIProposalError,
    )

    registry = ExtendedCommandRegistry()
    scene = _scene_with_element("el-1")
    with pytest.raises(TeachingAIProposalError, match="duration"):
        registry.compile(
            TeachingAICommand("stagger_elements", {
                "element_ids": ["el-1"], "preset_id": "fade",
                "param_name": "opacity", "duration": 0,
            }),
            scene,
        )


def test_aud26703_stagger_defaults_still_apply_when_omitted() -> None:
    registry = ExtendedCommandRegistry()
    scene = _scene_with_element("el-1")
    from src.features.teaching_animation.ai.commands import TeachingAICommand

    command = registry.compile(
        TeachingAICommand("stagger_elements", {
            "element_ids": ["el-1"], "preset_id": "fade",
            "param_name": "opacity",
        }),
        scene,
    )
    assert getattr(command, "interval") == 12
    assert getattr(command, "duration") == 18


# --------------------------------------------------------------------------- #
# AUD-26704
# --------------------------------------------------------------------------- #


def _scene_with_animation(frame: int, value: float) -> TeachingSceneDocument:
    from src.features.teaching_animation.core.model import SceneAnimation, SceneElement

    scene = TeachingSceneDocument.create_default("Test Scene")
    element = SceneElement(element_id="el-1", type="shape")
    scene = scene.with_element_added(element)
    track = KeyframeTrack("opacity", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(frame, value))
    animation = SceneAnimation("anim-1", "el-1", "opacity", track)
    return scene.with_animation_added(animation)


def test_aud26704_insert_keyframe_undo_restores_replaced_frame() -> None:
    from src.features.teaching_animation.ai.commands import TeachingAICommand

    scene = _scene_with_animation(frame=10, value=0.2)
    registry = ExtendedCommandRegistry()
    compiled = registry.compile(
        TeachingAICommand("insert_keyframe", {
            "animation_id": "anim-1", "frame": 10, "value": 0.9,
        }),
        scene,
    )
    applied = compiled.apply(scene)
    animation = next(a for a in applied.animations if a.animation_id == "anim-1")
    assert [kf.value for kf in animation.track.keyframes] == [0.9]

    undone = compiled.undo(applied)
    restored = next(a for a in undone.animations if a.animation_id == "anim-1")
    # The pre-existing keyframe (0.2) must come back, not vanish.
    assert [(kf.frame, kf.value) for kf in restored.track.keyframes] == [(10, 0.2)]


def test_aud26704_insert_keyframe_undo_on_empty_frame_stays_empty() -> None:
    from src.features.teaching_animation.ai.commands import TeachingAICommand

    scene = _scene_with_animation(frame=10, value=0.2)
    registry = ExtendedCommandRegistry()
    compiled = registry.compile(
        TeachingAICommand("insert_keyframe", {
            "animation_id": "anim-1", "frame": 40, "value": 0.5,
        }),
        scene,
    )
    undone = compiled.undo(compiled.apply(scene))
    restored = next(a for a in undone.animations if a.animation_id == "anim-1")
    assert [(kf.frame, kf.value) for kf in restored.track.keyframes] == [(10, 0.2)]


# --------------------------------------------------------------------------- #
# AUD-26705
# --------------------------------------------------------------------------- #


def _scene_with_token_styles() -> TeachingSceneDocument:
    from src.features.teaching_animation.core.model import SceneElement

    scene = TeachingSceneDocument.create_default("Token Scene")
    scene = scene.with_element_added(SceneElement(
        element_id="raw-ref",
        type="shape",
        style={"color": "$palette.primary", "fill": "$palette.primary"},
    ))
    scene = scene.with_element_added(SceneElement(
        element_id="resolved",
        type="shape",
        style={"color": "#3b82f6", "fill": "#3b82f6"},  # default palette.primary
    ))
    scene = scene.with_element_added(SceneElement(
        element_id="text-title",
        type="text",
        style={"font_size": 48.0, "color": "#f8fafc"},  # $type_scale.display
        content="标题",
    ))
    return scene


def test_aud26705_set_color_token_matches_raw_and_resolved() -> None:
    from src.features.teaching_animation.ai.commands import TeachingAICommand

    registry = ExtendedCommandRegistry()
    scene = _scene_with_token_styles()
    commands = registry.compile(
        TeachingAICommand("set_color_token", {
            "token": "palette.primary", "color": "#00ff00",
        }),
        scene,
    )
    assert len(commands) == 2  # both the $ref user and the resolved-hex user


def test_aud26705_set_typo_token_matches_resolved_type_scale() -> None:
    from src.features.teaching_animation.ai.commands import TeachingAICommand

    registry = ExtendedCommandRegistry()
    scene = _scene_with_token_styles()
    commands = registry.compile(
        TeachingAICommand("set_typo_token", {
            "token": "type_scale.display", "font_size": 36,
        }),
        scene,
    )
    matched_ids = {command.element_id for command in commands}
    assert "text-title" in matched_ids
    # Every match must genuinely sit on the resolved display size (48.0) —
    # including create_default's own auto-title when it shares the scale.
    for command in commands:
        element = scene.element_by_id(command.element_id)
        assert element is not None
        assert float(element.style["font_size"]) == 48.0


def test_aud26705_unknown_token_still_fails_closed() -> None:
    from src.features.teaching_animation.ai.commands import (
        TeachingAICommand, TeachingAIProposalError,
    )

    registry = ExtendedCommandRegistry()
    with pytest.raises(TeachingAIProposalError):
        registry.compile(
            TeachingAICommand("set_color_token", {
                "token": "palette.nope", "color": "#00ff00",
            }),
            _scene_with_token_styles(),
        )


# --------------------------------------------------------------------------- #
# AUD-26713
# --------------------------------------------------------------------------- #


def test_aud26713_registry_failure_is_logged_not_swallowed(
    monkeypatch, caplog,
) -> None:
    import src.features.teaching_animation.ai.context_provider as module

    def _broken_registry() -> None:
        raise RuntimeError("registry bug")

    monkeypatch.setattr(
        "src.features.teaching_animation.core.builtin_templates."
        "create_builtin_template_registry",
        _broken_registry,
    )
    module._ai_capability_catalog.cache_clear()
    try:
        with caplog.at_level(logging.ERROR, logger=module.__name__):
            catalog = module._ai_capability_catalog()
        assert catalog["templates"] == []
        assert "templates" in catalog["degraded"]
        assert any("template registry" in record.message for record in caplog.records)
    finally:
        # Drop the poisoned entry so later consumers rebuild the real catalog.
        module._ai_capability_catalog.cache_clear()


def test_aud26713_catalog_exposes_degraded_marker_contract() -> None:
    catalog = _ai_capability_catalog()
    assert "degraded" in catalog
    # Healthy first-party build: nothing degraded, vocabulary complete.
    assert catalog["degraded"] == ()
    assert "add_element" in catalog["commands"]
    assert "stagger_elements" in catalog["commands"]


# --------------------------------------------------------------------------- #
# AUD-26800
# --------------------------------------------------------------------------- #


def test_aud26800_4k_request_refused_for_non_4k_template() -> None:
    registry = create_default_creative_registry()
    template = registry.template("social_card_9x16")  # supports_4k=False
    assert template is not None
    issues = registry.compatibility_issues(
        template,
        duration_seconds=10.0, canvas_aspect="9:16", transparent=False,
        width=3840, height=2160,
    )
    assert any("4K" in issue for issue in issues)


def test_aud26800_hd_request_still_accepted_for_non_4k_template() -> None:
    registry = create_default_creative_registry()
    template = registry.template("social_card_9x16")
    assert template is not None
    issues = registry.compatibility_issues(
        template,
        duration_seconds=10.0, canvas_aspect="9:16", transparent=False,
        width=NON_4K_MAX_EDGE_PIXELS, height=1920,
    )
    assert issues == ()


def test_aud26800_4k_template_passes_4k_request() -> None:
    registry = create_default_creative_registry()
    template = registry.template("product_launch_15s")  # supports_4k=True
    assert template is not None
    issues = registry.compatibility_issues(
        template,
        duration_seconds=15.0, canvas_aspect="16:9", transparent=False,
        width=3840, height=2160,
    )
    assert issues == ()


# --------------------------------------------------------------------------- #
# AUD-26805
# --------------------------------------------------------------------------- #


def _design_scene(**overrides: object) -> dict:
    base: dict = {
        "scene_id": "s1", "schema_version": 1, "name": "s", "width": 1920,
        "height": 1080, "fps_num": 24, "fps_den": 1, "duration_frames": 240,
        "background": {"color": "#0f172a"}, "elements": [], "animations": [],
        "markers": [], "asset_refs": [],
    }
    base.update(overrides)
    return base


def test_aud26805_unresolved_asset_uses_dedicated_code() -> None:
    scene = _design_scene(
        elements=[{
            "element_id": "img1", "type": "image",
            "transform": {"x": 10, "y": 10, "width": 100, "height": 100},
            "asset_ref": "missing-asset",
        }],
        asset_refs=[],
    )
    report = validate_scene_design(scene)
    codes = [f.code for f in report.errors]
    assert MOTION_GRAPHIC_ASSET_UNRESOLVED in codes
    assert MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS not in codes


def test_aud26805_image_without_ref_warns_with_dedicated_code() -> None:
    scene = _design_scene(elements=[{
        "element_id": "img2", "type": "image",
        "transform": {"x": 10, "y": 10, "width": 100, "height": 100},
    }])
    report = validate_scene_design(scene)
    codes = [f.code for f in report.warnings]
    assert MOTION_GRAPHIC_ASSET_UNRESOLVED in codes


# --------------------------------------------------------------------------- #
# AUD-26806
# --------------------------------------------------------------------------- #


def test_aud26806_text_on_light_card_is_flagged() -> None:
    """White text passes against a dark canvas but must fail on its light card."""
    scene = _design_scene(
        background={"color": "#0f172a"},
        elements=[
            {
                "element_id": "card", "type": "rect", "z_index": 0,
                "transform": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "style": {"fill": "#f8fafc"},
            },
            {
                "element_id": "title", "type": "text", "z_index": 1,
                "transform": {"x": 100, "y": 100, "width": 600, "height": 80},
                "style": {"font_size": 48, "color": "#ffffff"},
                "content": "标题",
            },
        ],
    )
    report = validate_scene_design(scene)
    assert MOTION_GRAPHIC_LOW_CONTRAST in [f.code for f in report.errors]


def test_aud26806_text_on_dark_card_over_light_canvas_passes() -> None:
    scene = _design_scene(
        background={"color": "#f8fafc"},
        elements=[
            {
                "element_id": "card", "type": "rect", "z_index": 0,
                "transform": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "style": {"fill": "#0f172a"},
            },
            {
                "element_id": "title", "type": "text", "z_index": 1,
                "transform": {"x": 100, "y": 100, "width": 600, "height": 80},
                "style": {"font_size": 48, "color": "#ffffff"},
                "content": "标题",
            },
        ],
    )
    report = validate_scene_design(scene)
    assert report.is_valid, [f.to_dict() for f in report.findings]


def test_aud26806_topmost_backing_element_wins() -> None:
    """A dark card above a light card defines the text's backdrop."""
    scene = _design_scene(
        background={"color": "#0f172a"},
        elements=[
            {
                "element_id": "light-card", "type": "rect", "z_index": 0,
                "transform": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "style": {"fill": "#ffffff"},
            },
            {
                "element_id": "dark-card", "type": "rect", "z_index": 1,
                "transform": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "style": {"fill": "#111827"},
            },
            {
                "element_id": "title", "type": "text", "z_index": 2,
                "transform": {"x": 100, "y": 100, "width": 600, "height": 80},
                "style": {"font_size": 48, "color": "#f9fafb"},
                "content": "标题",
            },
        ],
    )
    report = validate_scene_design(scene)
    assert report.is_valid, [f.to_dict() for f in report.findings]


# --------------------------------------------------------------------------- #
# AUD-26814 / AUD-26815
# --------------------------------------------------------------------------- #


def _running_job(tmp_path: Path, **request_overrides: object):
    request = ExportRequest(
        scene_id="s1",
        output_path=str(tmp_path / "out.mp4"),
        profile_id="youtube_1080p_landscape",
        **request_overrides,
    )
    job = new_export_job(request)
    job.start()
    return job


def test_aud26814_cancelled_finish_removes_temp_file(tmp_path: Path) -> None:
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"rendered")
    job = _running_job(tmp_path)
    job.request_cancel(reason="user")
    job.finish(
        postflight=postflight_from_stat(str(temp)), output_temp_path=str(temp),
    )
    assert job.status == EXPORT_CANCELLED
    assert not temp.exists()


def test_aud26814_blocked_postflight_removes_temp_file(tmp_path: Path) -> None:
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"rendered")
    job = _running_job(tmp_path)
    blocking = postflight_from_stat(str(tmp_path / "missing.tmp"))  # EXPORT_FILE_MISSING
    assert blocking.is_blocking
    job.finish(postflight=blocking, output_temp_path=str(temp))
    assert job.status == EXPORT_FAILED
    assert not temp.exists()


def test_aud26814_overwrite_refusal_removes_temp_file(tmp_path: Path) -> None:
    final = tmp_path / "out.mp4"
    final.write_bytes(b"old")
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"new")
    job = _running_job(tmp_path, overwrite_existing=False)
    job.finish(
        postflight=postflight_from_stat(str(temp)), output_temp_path=str(temp),
    )
    assert job.status == EXPORT_FAILED
    assert final.read_bytes() == b"old"
    assert not temp.exists()


def test_aud26814_fail_removes_recorded_temp_and_cleanup_api_exists(
    tmp_path: Path,
) -> None:
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"rendered")
    job = _running_job(tmp_path)
    job.output_temp_path = str(temp)
    job.fail(code="EXPORT_RENDER_FAILED", message="boom")
    assert job.status == EXPORT_FAILED
    assert not temp.exists()
    # Explicit host-side cleanup hook is idempotent.
    job.cleanup_temp()
    assert job.status == EXPORT_FAILED


def test_aud26815_finalize_oserror_transitions_to_failed(tmp_path: Path) -> None:
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"rendered")
    # Parent path occupied by a regular file → os.makedirs raises OSError.
    (tmp_path / "occupied").write_bytes(b"not-a-dir")
    job = new_export_job(ExportRequest(
        scene_id="s1",
        output_path=str(tmp_path / "occupied" / "out.mp4"),
        profile_id="youtube_1080p_landscape",
    ))
    job.start()
    job.finish(
        postflight=postflight_from_stat(str(temp)), output_temp_path=str(temp),
    )
    assert job.status == EXPORT_FAILED
    assert job.diagnostic_code == EXPORT_FINALIZE_FAILED
    assert job.is_terminal
    assert job.finished_at is not None
    snapshot = job.to_snapshot()
    assert snapshot["status"] == EXPORT_FAILED
    assert not temp.exists()


def test_aud26815_success_path_unchanged(tmp_path: Path) -> None:
    temp = tmp_path / "out.tmp"
    temp.write_bytes(b"rendered")
    job = _running_job(tmp_path)
    job.finish(
        postflight=postflight_from_stat(str(temp)), output_temp_path=str(temp),
    )
    assert job.status == EXPORT_SUCCEEDED
    assert (tmp_path / "out.mp4").read_bytes() == b"rendered"


# --------------------------------------------------------------------------- #
# AUD-26902
# --------------------------------------------------------------------------- #


def _deck(pages: tuple[str, ...]) -> str:
    body = "".join(
        f'<main data-topos-page="{i}">{text}</main>'
        for i, text in enumerate(pages, start=1)
    )
    return f"<!doctype html><html><body>{body}</body></html>"


def test_aud26902_page_numbering_edit_not_misread_as_page_add() -> None:
    two_pages = _deck(("一", "二"))
    assert edit_postcondition_issues(
        "为每一页添加页码", two_pages, two_pages,
    ) == []


def test_aud26902_page_footer_edit_not_misread_as_page_remove() -> None:
    two_pages = _deck(("一", "二"))
    assert edit_postcondition_issues(
        "删除页面底部的版权信息", two_pages, two_pages,
    ) == []


def test_aud26902_spacing_edit_not_misread_as_page_remove() -> None:
    two_pages = _deck(("一", "二"))
    assert edit_postcondition_issues(
        "减少页面之间的留白", two_pages, two_pages,
    ) == []


def test_aud26902_ordinal_page_remove_is_enforced() -> None:
    two_pages = _deck(("一", "二"))
    still_two = _deck(("一改", "二"))
    assert edit_postcondition_issues("删除第2页", two_pages, still_two)
    assert edit_postcondition_issues("删除第2页", two_pages, _deck(("一",))) == []
    assert edit_postcondition_issues("删除第 2 页。", two_pages, still_two)


def test_aud26902_ordinal_page_content_edit_not_misread() -> None:
    two_pages = _deck(("一", "二"))
    # Edits scoped to page 2's own content are not page-count changes.
    for request in ("删除第2页的内容", "删除第2页底部", "修改第2页标题", "在第3页添加标题"):
        assert edit_postcondition_issues(request, two_pages, two_pages) == [], request


def test_aud26902_ordinal_page_add_is_enforced() -> None:
    two_pages = _deck(("一", "二"))
    assert edit_postcondition_issues("增加第 4 页总结", two_pages, two_pages)
    assert edit_postcondition_issues(
        "增加第 4 页总结", two_pages, _deck(("一", "二", "三", "四")),
    ) == []


def test_aud26902_counted_add_still_enforced() -> None:
    two_pages = _deck(("一", "二"))
    assert edit_postcondition_issues("加一页总结", two_pages, two_pages)
    assert edit_postcondition_issues("加一页总结", two_pages, _deck(("一", "二", "三"))) == []


# --------------------------------------------------------------------------- #
# AUD-26903
# --------------------------------------------------------------------------- #


def test_aud26903_negated_removal_keeps_the_quote() -> None:
    deck = _deck(("公司简介", "二"))
    # User explicitly wants to KEEP 「公司简介」; keeping it must pass.
    assert edit_postcondition_issues(
        "不要删除「公司简介」这一段", deck, deck,
    ) == []


def test_aud26903_rewrite_target_not_treated_as_removal() -> None:
    original = _deck(("旧数据", "二"))
    revised = _deck(("最新数据", "二"))
    assert edit_postcondition_issues(
        "删掉旧数据，把文案改成「最新数据」", original, revised,
    ) == []


def test_aud26903_genuine_quoted_removal_still_enforced() -> None:
    original = _deck(("旧文案", "二"))
    unchanged = _deck(("旧文案", "二"))
    assert edit_postcondition_issues("删除「旧文案」", original, unchanged)
    assert edit_postcondition_issues("删除「旧文案」", original, _deck(("别的", "二"))) == []


def test_aud26903_generation_report_negated_removal_passes() -> None:
    deck = _deck(("公司简介", "二"))
    report = generation_intent_report("不要删除「公司简介」这一段", deck)
    assert report.passed
    assert report.issues == ()
