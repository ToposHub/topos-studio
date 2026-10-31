"""B0 baseline contract tests: brief, action matrix, approval scope, validator, error codes.

These tests cover the MG-01 / MG-06 / MG-10 / MG-22 / MG-23 acceptance
criteria without spinning up Qt or a real LLM provider.  They lock the typed
contracts the rest of the Motion Graphic pipeline will build on:

* a brief forms, reports missing fields, and bumps revision on patch (MG-01)
* the action matrix fails closed and routes approvals by tier (MG-22)
* the approval service is one-shot, stale-aware and revocable (MG-06)
* the design validator blocks unreadable / out-of-bounds scenes (MG-10)
* error codes are stable, suggested next steps exist, and audit events
  redact secrets / paths / raw prompts before persistence (MG-23)
"""

from __future__ import annotations

import time

import pytest

from src.features.teaching_animation.ai.action_matrix import (
    MotionGraphicAction,
    MotionGraphicActionKind,
    MotionGraphicPermissionPolicy,
    MotionGraphicPermissionTier,
    default_action_matrix,
)
from src.features.teaching_animation.ai.approval_scope import (
    MotionGraphicApprovalScope,
    MotionGraphicApprovalService,
)
from src.features.teaching_animation.ai.brief import (
    MotionGraphicBrief,
    SUPPORTED_CANVAS_ASPECTS,
)
from src.features.teaching_animation.ai.design_validator import (
    DesignValidationOptions,
    contrast_ratio,
    validate_scene_design,
)
from src.features.teaching_animation.ai.error_codes import (
    MOTION_GRAPHIC_APPROVAL_EXPIRED,
    MOTION_GRAPHIC_APPROVAL_REQUIRED,
    MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH,
    MOTION_GRAPHIC_BRIEF_STALE,
    MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
    MOTION_GRAPHIC_DUPLICATE_ELEMENT,
    MOTION_GRAPHIC_LOW_CONTRAST,
    MOTION_GRAPHIC_PERMISSION_DENIED,
    MOTION_GRAPHIC_TEXT_OVERFLOW,
    MotionGraphicAuditEvent,
    MotionGraphicFailure,
    canonical_json_hash,
    make_audit_event,
    redact_for_storage,
    suggested_next_step,
)


# --------------------------------------------------------------------------- #
# MG-01: MotionGraphicBrief
# --------------------------------------------------------------------------- #


def _complete_brief(**overrides) -> MotionGraphicBrief:
    base = dict(
        brief_id="mg-brief-test",
        topic="介绍 AI Agent",
        duration_seconds=15.0,
        canvas_aspect="16:9",
    )
    base.update(overrides)
    return MotionGraphicBrief(**base)


def test_brief_reports_missing_required_fields() -> None:
    brief = MotionGraphicBrief(brief_id="mg-brief-empty")
    assert brief.missing_required_fields() == ("topic", "duration_seconds")
    assert not brief.is_complete()


def test_complete_brief_has_no_missing_fields() -> None:
    brief = _complete_brief()
    assert brief.is_complete()
    assert brief.missing_required_fields() == ()


def test_brief_canvas_size_defaults_follow_aspect() -> None:
    wide = _complete_brief(canvas_aspect="16:9")
    tall = _complete_brief(canvas_aspect="9:16")
    square = _complete_brief(canvas_aspect="1:1")
    assert wide.canvas_size() == (1920, 1080)
    assert tall.canvas_size() == (1080, 1920)
    assert square.canvas_size() == (1080, 1080)


def test_brief_duration_frames_rounds_to_int() -> None:
    brief = _complete_brief(duration_seconds=15.0, fps=24.0)
    assert brief.duration_frames() == 360


def test_brief_apply_patch_increments_revision_and_records_history() -> None:
    brief = _complete_brief()
    new_brief, patch = brief.apply_patch({"topic": "介绍 AI 剪辑助手"}, reason="换标题")

    assert new_brief.revision == brief.revision + 1
    assert new_brief.topic == "介绍 AI 剪辑助手"
    assert patch.from_revision == brief.revision
    assert patch.to_revision == new_brief.revision
    assert new_brief.patches[-1] is patch
    # The original brief stays immutable.
    assert brief.revision == 1
    assert brief.topic == "介绍 AI Agent"


def test_brief_apply_patch_rejects_unknown_fields() -> None:
    brief = _complete_brief()
    with pytest.raises(ValueError):
        brief.apply_patch({"not_a_field": True})


def test_brief_apply_patch_normalizes_asset_scope_sequences() -> None:
    brief = _complete_brief()
    new_brief, _ = brief.apply_patch({"asset_scope": ["asset-1", "asset-2"]})
    assert new_brief.asset_scope == ("asset-1", "asset-2")


def test_brief_hash_is_stable_across_equivalent_briefs() -> None:
    brief_a = _complete_brief()
    brief_b = _complete_brief()
    assert brief_a.brief_hash() == brief_b.brief_hash()


def test_brief_hash_changes_when_content_changes() -> None:
    brief = _complete_brief()
    changed, _ = brief.apply_patch({"topic": "Different topic"})
    assert changed.brief_hash() != brief.brief_hash()


def test_brief_rejects_unsupported_aspect() -> None:
    with pytest.raises(ValueError):
        _complete_brief(canvas_aspect="4:1")


def test_brief_from_user_text_does_not_store_raw_prompt() -> None:
    brief = MotionGraphicBrief.from_user_text("帮我做一个动画")
    assert brief.source_user_text_hash is not None
    assert brief.topic == "帮我做一个动画"
    # The hash must not equal the raw prompt (it is a sha256 hex digest).
    assert brief.source_user_text_hash != "帮我做一个动画"
    assert len(brief.source_user_text_hash) == 64


# --------------------------------------------------------------------------- #
# MG-22: Action matrix
# --------------------------------------------------------------------------- #


def test_matrix_classifies_every_action() -> None:
    matrix = default_action_matrix()
    for action in MotionGraphicAction:
        profile = matrix.profile(action)
        assert profile.action is action
        assert profile.kind in MotionGraphicActionKind
        assert profile.default_permission in MotionGraphicPermissionTier


def test_matrix_lists_timeline_writes_and_confirm_required_actions() -> None:
    matrix = default_action_matrix()
    writers = matrix.actions_writing_timeline()
    assert MotionGraphicAction.INSERT_TIMELINE_CLIP in writers
    assert MotionGraphicAction.REPLACE_EXISTING_CLIP in writers

    confirm_required = matrix.actions_requiring_confirmation()
    # The high-risk actions must always require their own confirmation.
    for must_confirm in (
        MotionGraphicAction.INSERT_TIMELINE_CLIP,
        MotionGraphicAction.REPLACE_EXISTING_CLIP,
        MotionGraphicAction.EXPORT_FILE,
        MotionGraphicAction.PUBLISH_TO_PLATFORM,
    ):
        assert must_confirm in confirm_required


def test_policy_allows_read_only_actions_automatically() -> None:
    policy = MotionGraphicPermissionPolicy()
    decision = policy.evaluate(MotionGraphicAction.READ_PROJECT)
    assert decision.allowed
    assert decision.permission_tier == MotionGraphicPermissionTier.AUTO


def test_policy_blocks_timeline_write_without_explicit_grant() -> None:
    policy = MotionGraphicPermissionPolicy(batch_granted=True)
    decision = policy.evaluate(MotionGraphicAction.INSERT_TIMELINE_CLIP)
    assert not decision.allowed
    assert decision.diagnostic_code == MOTION_GRAPHIC_APPROVAL_REQUIRED
    # A batch grant must NOT unlock always-confirm actions.
    assert decision.needs_approval


def test_policy_unlocks_always_confirm_with_explicit_grant() -> None:
    policy = MotionGraphicPermissionPolicy()
    policy.grant_explicit(MotionGraphicAction.INSERT_TIMELINE_CLIP)
    decision = policy.evaluate(MotionGraphicAction.INSERT_TIMELINE_CLIP)
    assert decision.allowed


def test_policy_batch_grant_covers_approved_once_actions() -> None:
    policy = MotionGraphicPermissionPolicy(batch_granted=True)
    decision = policy.evaluate(MotionGraphicAction.CREATE_SCENE)
    assert decision.allowed


def test_policy_blocks_paid_generation_when_disabled() -> None:
    policy = MotionGraphicPermissionPolicy(paid_generation_enabled=False)
    decision = policy.evaluate(MotionGraphicAction.GENERATE_PAID_IMAGE)
    assert not decision.allowed
    assert decision.diagnostic_code == MOTION_GRAPHIC_PERMISSION_DENIED


def test_policy_blocks_cloud_upload_when_disabled() -> None:
    policy = MotionGraphicPermissionPolicy(cloud_upload_enabled=False)
    decision = policy.evaluate(MotionGraphicAction.UPLOAD_TO_CLOUD)
    assert not decision.allowed
    assert decision.diagnostic_code == MOTION_GRAPHIC_PERMISSION_DENIED


def test_policy_blocks_when_cost_exceeds_budget() -> None:
    policy = MotionGraphicPermissionPolicy(
        paid_generation_enabled=True,
        budget_units=1.0,
    )
    decision = policy.evaluate(MotionGraphicAction.GENERATE_PAID_IMAGE, cost_units=2.0)
    assert not decision.allowed
    assert decision.diagnostic_code == MOTION_GRAPHIC_PERMISSION_DENIED


# --------------------------------------------------------------------------- #
# MG-06: Approval scope
# --------------------------------------------------------------------------- #


def _scope(**overrides) -> MotionGraphicApprovalScope:
    base = dict(
        project_id="project-1",
        timeline_id="timeline-1",
        track_id="graphics-1",
        insert_frame=0,
        brief_id="mg-brief-1",
        brief_revision=1,
        brief_hash="hash-1",
        scene_hash="scene-hash-1",
        plan_hash="plan-hash-1",
        diff_hash="diff-hash-1",
        preview_hash="preview-hash-1",
    )
    base.update(overrides)
    return MotionGraphicApprovalScope(**base)


def test_approval_service_issues_and_consumes_grant_once() -> None:
    service = MotionGraphicApprovalService()
    scope = _scope()
    grant = service.issue_after_user_confirmation(scope)

    consumed = service.consume(grant.grant_id, scope)
    assert consumed.grant_id == grant.grant_id

    # Second consume must fail: single-use.
    with pytest.raises(MotionGraphicFailure) as exc_info:
        service.consume(grant.grant_id, scope)
    assert exc_info.value.code == MOTION_GRAPHIC_APPROVAL_REQUIRED


def test_approval_service_rejects_scope_mismatch_with_reasons() -> None:
    service = MotionGraphicApprovalService()
    approved = _scope()
    grant = service.issue_after_user_confirmation(approved)

    observed = _scope(scene_hash="changed-scene", timeline_id="other-timeline")
    with pytest.raises(MotionGraphicFailure) as exc_info:
        service.consume(grant.grant_id, observed)
    failure = exc_info.value
    assert failure.code == MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH
    reasons = failure.details.get("reasons", [])
    assert any("场景" in reason for reason in reasons)
    assert any("时间线" in reason for reason in reasons)


def test_approval_service_grant_expires() -> None:
    clock = [1000.0]
    service = MotionGraphicApprovalService(time_fn=lambda: clock[0])
    scope = _scope()
    grant = service.issue_after_user_confirmation(scope, ttl_seconds=10.0)

    clock[0] += 11.0
    with pytest.raises(MotionGraphicFailure) as exc_info:
        service.consume(grant.grant_id, scope)
    assert exc_info.value.code == MOTION_GRAPHIC_APPROVAL_EXPIRED


def test_approval_service_revoke_for_project() -> None:
    service = MotionGraphicApprovalService()
    scope = _scope()
    grant = service.issue_after_user_confirmation(scope)

    assert service.revoke_for_project("project-1") == 1
    with pytest.raises(MotionGraphicFailure) as exc_info:
        service.consume(grant.grant_id, scope)
    assert exc_info.value.code == MOTION_GRAPHIC_PERMISSION_DENIED


def test_approval_scope_detect_stale_reports_human_readable_reasons() -> None:
    approved = _scope()
    observed = _scope(brief_revision=2, scene_hash="new-hash")
    reasons = MotionGraphicApprovalService.detect_stale(approved, observed)
    assert reasons  # non-empty
    combined = " ".join(reasons)
    assert "需求版本" in combined
    assert "场景" in combined


def test_approval_scope_matches_when_optional_fields_unset() -> None:
    base = _scope()
    loose = _scope(scene_hash=None, plan_hash=None, diff_hash=None, preview_hash=None)
    assert base.matches(loose)


def test_approval_scope_rejects_cross_brief() -> None:
    a = _scope(brief_id="brief-a")
    b = _scope(brief_id="brief-b")
    assert not a.matches(b)


# --------------------------------------------------------------------------- #
# MG-10: Design validator
# --------------------------------------------------------------------------- #


def _scene_dict(**overrides) -> dict:
    base = {
        "scene_id": "scene-1",
        "schema_version": 1,
        "name": "Test scene",
        "width": 1920,
        "height": 1080,
        "fps_num": 24,
        "fps_den": 1,
        "duration_frames": 360,
        "background": {"color": "#0f172a"},
        "elements": [],
        "animations": [],
        "markers": [],
        "asset_refs": [],
    }
    base.update(overrides)
    return base


def _text_element(
    element_id: str,
    *,
    content: str = "标题",
    x: float = 100,
    y: float = 100,
    width: float = 600,
    height: float = 80,
    font_size: float = 48,
    color: str = "#ffffff",
) -> dict:
    return {
        "element_id": element_id,
        "type": "text",
        "z_index": 1,
        "transform": {"x": x, "y": y, "width": width, "height": height},
        "style": {"font_size": font_size, "color": color},
        "content": content,
    }


def test_design_validator_passes_clean_scene() -> None:
    scene = _scene_dict(elements=[_text_element("title")])
    report = validate_scene_design(scene)
    assert report.is_valid, [f.to_dict() for f in report.findings]
    assert report.finding_count == 0


def test_design_validator_flags_out_of_bounds_element() -> None:
    element = _text_element("oob", x=-200, y=5000)
    scene = _scene_dict(elements=[element])
    report = validate_scene_design(scene)
    assert not report.is_valid
    codes = [f.code for f in report.errors]
    assert MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS in codes
    finding = next(f for f in report.errors if f.code == MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS)
    assert finding.element_id == "oob"


def test_design_validator_flags_tiny_font() -> None:
    element = _text_element("tiny", font_size=8)
    scene = _scene_dict(elements=[element])
    report = validate_scene_design(scene)
    codes = [f.code for f in report.errors]
    assert MOTION_GRAPHIC_TEXT_OVERFLOW in codes


def test_design_validator_flags_low_contrast_text() -> None:
    # Near-black text on near-black background.
    element = _text_element("dim", color="#111111")
    scene = _scene_dict(elements=[element], background={"color": "#0a0a0a"})
    report = validate_scene_design(scene)
    codes = [f.code for f in report.errors]
    assert MOTION_GRAPHIC_LOW_CONTRAST in codes


def test_design_validator_flags_duplicate_element_ids() -> None:
    scene = _scene_dict(elements=[_text_element("dup"), _text_element("dup")])
    report = validate_scene_design(scene)
    codes = [f.code for f in report.errors]
    assert MOTION_GRAPHIC_DUPLICATE_ELEMENT in codes


def test_design_validator_flags_complexity_overload() -> None:
    elements = [
        _text_element(f"el-{i}", x=10 + i, y=10 + i, width=20, height=20, font_size=24)
        for i in range(150)
    ]
    scene = _scene_dict(elements=elements)
    report = validate_scene_design(scene, options=DesignValidationOptions(max_elements_error=120))
    codes = [f.code for f in report.errors]
    assert any("COMPLEXITY" in code for code in codes)


def test_design_validator_contrast_ratio_helper() -> None:
    high = contrast_ratio("#ffffff", "#000000")
    assert high is not None
    assert high > 20.0
    same = contrast_ratio("#777777", "#777777")
    assert same == pytest.approx(1.0, abs=0.01)
    unparseable = contrast_ratio(None, "#000000")
    assert unparseable is None


def test_design_validator_finds_can_be_filtered_by_element() -> None:
    scene = _scene_dict(elements=[
        _text_element("good"),
        _text_element("bad", font_size=4),
    ])
    report = validate_scene_design(scene)
    bad_findings = report.findings_for("bad")
    assert bad_findings
    assert all(f.element_id == "bad" for f in bad_findings)


def test_design_validator_accepts_scene_model_object() -> None:
    from src.features.teaching_animation.core.model import (
        SceneElement,
        SceneTransform,
        TeachingSceneDocument,
    )

    element = SceneElement(
        element_id="title",
        type="text",
        transform=SceneTransform(x=100, y=100, width=600, height=80),
        style={"font_size": 48, "color": "#ffffff"},
        content="Hello",
    )
    scene = TeachingSceneDocument.create_default(
        "Test", width=1920, height=1080, duration_frames=360,
    )
    # create_default adds a placeholder title; replace elements to control test.
    scene = type(scene)(
        scene_id=scene.scene_id,
        schema_version=scene.schema_version,
        name=scene.name,
        width=scene.width,
        height=scene.height,
        fps_num=scene.fps_num,
        fps_den=scene.fps_den,
        duration_frames=scene.duration_frames,
        background={"color": "#0f172a"},
        elements=(element,),
        animations=(),
    )
    report = validate_scene_design(scene)
    assert report.is_valid, [f.to_dict() for f in report.findings]


# --------------------------------------------------------------------------- #
# MG-23: Error codes + audit events + redaction
# --------------------------------------------------------------------------- #


def test_error_codes_carry_suggested_next_step() -> None:
    for code in (
        MOTION_GRAPHIC_APPROVAL_REQUIRED,
        MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH,
        MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
        MOTION_GRAPHIC_TEXT_OVERFLOW,
    ):
        hint = suggested_next_step(code)
        assert isinstance(hint, str)
        assert hint  # never empty


def test_motion_graphic_failure_to_dict_roundtrips_code_and_next_step() -> None:
    failure = MotionGraphicFailure(
        MOTION_GRAPHIC_BRIEF_STALE,
        "需求已更新",
        details={"brief_id": "mg-brief-1"},
    )
    payload = failure.to_dict()
    assert payload["code"] == MOTION_GRAPHIC_BRIEF_STALE
    assert payload["next_step"]
    assert payload["details"]["brief_id"] == "mg-brief-1"


def test_canonical_json_hash_is_deterministic() -> None:
    a = canonical_json_hash({"b": 1, "a": 2})
    b = canonical_json_hash({"a": 2, "b": 1})
    assert a == b
    assert a != canonical_json_hash({"a": 2, "b": 3})


def test_redact_for_storage_masks_api_keys_and_absolute_paths() -> None:
    raw = {
        "api_key": "sk-super-secret",
        "prompt": "please use /Users/testuser/secret.txt and token=abc123",
        "nested": [{"password": "hunter2", "ok": "fine"}],
    }
    sanitized = redact_for_storage(raw)
    assert sanitized["api_key"] == "[redacted]"
    assert sanitized["nested"][0]["password"] == "[redacted]"
    assert "sk-super-secret" not in str(sanitized)
    assert "/Users/testuser" not in str(sanitized)


def test_make_audit_event_redacts_message_and_details() -> None:
    event = make_audit_event(
        MOTION_GRAPHIC_APPROVAL_EXPIRED,
        stage="apply",
        message="prompt=secret contained /etc/passwd",
        details={"raw_prompt": "sk-leak", "reason": "ok"},
        timestamp=1000.0,
    )
    assert isinstance(event, MotionGraphicAuditEvent)
    assert event.timestamp == 1000.0
    serialized = str(event.to_dict())
    assert "sk-leak" not in serialized
    assert "/etc/passwd" not in serialized
    # The structured sensitive key was redacted but the key name is preserved.
    assert event.details["raw_prompt"] == "[redacted]"


def test_audit_event_round_trips_through_dict() -> None:
    event = make_audit_event(
        MOTION_GRAPHIC_TEXT_OVERFLOW,
        stage="validate",
        brief_hash="hash",
        scene_hash="scene",
    )
    restored = MotionGraphicAuditEvent.from_dict(event.to_dict())
    assert restored.code == event.code
    assert restored.brief_hash == event.brief_hash
