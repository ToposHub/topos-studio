"""P6 execution-mode and bounded-scope policy tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.execution_policy import (
    ActionRequest,
    ExecutionAction,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionScope,
)


def _scope() -> ExecutionScope:
    return ExecutionScope(
        project_id="p1",
        timeline_id="t1",
        base_revision=3,
        asset_ids=("a1",),
        template_ids=("product_ad_30s",),
        allowed_hosts=("media.example.com",),
        allowed_file_extensions=(".mp4",),
        budget_units=10.0,
        max_duration_seconds=30.0,
    )


def test_suggestion_mode_keeps_writes_and_paid_actions_behind_approval() -> None:
    policy = ExecutionPolicy(
        ExecutionMode.SUGGESTION,
        _scope(),
        generation_enabled=True,
    )
    assert policy.evaluate(ActionRequest(ExecutionAction.READ, "p1")).allowed
    write = policy.evaluate(ActionRequest(
        ExecutionAction.TIMELINE_WRITE, "p1", timeline_id="t1", base_revision=3,
        template_id="product_ad_30s",
    ))
    assert not write.allowed and write.needs_approval
    paid = policy.evaluate(ActionRequest(
        ExecutionAction.PAID_GENERATION, "p1", template_id="product_ad_30s",
    ))
    assert not paid.allowed and paid.needs_approval


def test_semi_automatic_allows_local_edit_but_not_timeline_write() -> None:
    policy = ExecutionPolicy(ExecutionMode.SEMI_AUTOMATIC, _scope())
    assert policy.evaluate(ActionRequest(ExecutionAction.LOCAL_EDIT, "p1")).allowed
    decision = policy.evaluate(ActionRequest(
        ExecutionAction.TIMELINE_WRITE, "p1", timeline_id="t1", base_revision=3,
        template_id="product_ad_30s",
    ))
    assert not decision.allowed and decision.needs_approval


def test_restricted_mode_checks_scope_network_and_publish_consent() -> None:
    policy = ExecutionPolicy(
        ExecutionMode.RESTRICTED_AUTOMATIC,
        _scope(),
        cloud_upload_enabled=True,
        generation_enabled=True,
        auto_export_enabled=True,
    )
    request = ActionRequest(
        ExecutionAction.TIMELINE_WRITE,
        "p1",
        timeline_id="t1",
        base_revision=3,
        asset_ids=("a1",),
        template_id="product_ad_30s",
    )
    assert policy.evaluate(request).allowed
    assert policy.evaluate(
        ActionRequest(ExecutionAction.CLOUD_UPLOAD, "p1", host="media.example.com")
    ).allowed
    assert not policy.evaluate(
        ActionRequest(ExecutionAction.CLOUD_UPLOAD, "p1", host="evil.example.com")
    ).allowed
    publish = policy.evaluate(ActionRequest(ExecutionAction.AUTO_PUBLISH, "p1"))
    assert not publish.allowed and publish.needs_approval
    assert policy.evaluate(ActionRequest(ExecutionAction.AUTO_PUBLISH, "p1"), approval_granted=True).allowed


def test_scope_mismatch_and_budget_are_hard_denials() -> None:
    policy = ExecutionPolicy(ExecutionMode.RESTRICTED_AUTOMATIC, _scope())
    revision = policy.evaluate(ActionRequest(ExecutionAction.LOCAL_EDIT, "p1", base_revision=4))
    assert not revision.allowed and not revision.needs_approval
    budget = policy.evaluate(ActionRequest(ExecutionAction.LOCAL_EDIT, "p1", cost_units=11.0))
    assert not budget.allowed and "budget" in budget.reason


def test_feature_flags_map_to_safe_three_level_modes() -> None:
    scope = _scope()
    assert ExecutionPolicy.from_feature_flags(SimpleNamespace(), scope).mode == ExecutionMode.SUGGESTION
    assert ExecutionPolicy.from_feature_flags(
        SimpleNamespace(agent_auto_mode=True), scope
    ).mode == ExecutionMode.SEMI_AUTOMATIC
    assert ExecutionPolicy.from_feature_flags(
        SimpleNamespace(agent_auto_mode=True, auto_export=True), scope
    ).mode == ExecutionMode.RESTRICTED_AUTOMATIC
