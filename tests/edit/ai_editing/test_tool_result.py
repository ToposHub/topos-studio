"""Structured ToolResult tests (plan §A2 §8-9).

Validates the single shape every tool invocation reports: success / partial /
failure / denial, with layered diagnostics, artifact refs and a redacted
audit dict.  Also covers the permission → approval derivation that connects
the existing ``ToolPermission`` tiers to the approval gate.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.tool_facade import ToolPermission
from src.features.edit.ai_editing.tool_result import (
    Diagnostic,
    ToolOutcome,
    ToolResult,
    is_read_only_permission,
    permission_requires_approval,
)


# ---------------------------------------------------------------------- #
# Factories + outcomes
# ---------------------------------------------------------------------- #


class TestToolResultFactories:
    def test_success(self):
        r = ToolResult.success("search_assets", data={"count": 3}, artifact_refs=("a1",))
        assert r.succeeded
        assert not r.failed
        assert r.outcome == ToolOutcome.SUCCESS
        assert r.data == {"count": 3}
        assert r.artifact_refs == ("a1",)

    def test_partial(self):
        r = ToolResult.partial("apply_edit_plan", error_message="2/3 tracks done")
        assert not r.succeeded
        assert not r.failed  # partial is not a hard failure
        assert r.outcome == ToolOutcome.PARTIAL

    def test_failure(self):
        r = ToolResult.failure("render", error_code="GPU_OOM", error_message="oom")
        assert r.failed
        assert not r.succeeded
        assert r.error_code == "GPU_OOM"

    def test_denied_is_distinct_from_failure(self):
        # Plan §A2: denial must be explicit so retry logic can tell "broke"
        # from "not authorised".
        r = ToolResult.denied("apply_edit_plan", reason="no grant")
        assert r.is_denied
        assert r.failed  # denial is failure-like
        assert r.outcome == ToolOutcome.DENIED

    def test_empty_tool_name_rejected(self):
        with pytest.raises(ValueError):
            ToolResult(tool_name="", outcome=ToolOutcome.SUCCESS)

    def test_unknown_outcome_fails_safe(self):
        r = ToolResult(tool_name="t", outcome="whatever")
        assert r.outcome == ToolOutcome.FAILURE


# ---------------------------------------------------------------------- #
# Serialization (auditable + redacted)
# ---------------------------------------------------------------------- #


class TestSerialization:
    def test_roundtrip_shape(self):
        r = ToolResult.success(
            "search_assets",
            data={"count": 3},
            diagnostics=(Diagnostic("warning", "PARTIAL", "some hint"),),
            artifact_refs=("a1",),
            duration_ms=12.5,
        )
        d = r.to_dict()
        assert d["tool_name"] == "search_assets"
        assert d["outcome"] == ToolOutcome.SUCCESS
        assert d["data"] == {"count": 3}
        assert d["duration_ms"] == 12.5
        assert len(d["diagnostics"]) == 1
        assert d["diagnostics"][0]["code"] == "PARTIAL"

    def test_failure_dict_carries_error(self):
        r = ToolResult.failure("t", error_code="X", error_message="boom")
        d = r.to_dict()
        assert d["error_code"] == "X"
        assert d["error_message"] == "boom"


# ---------------------------------------------------------------------- #
# Diagnostics
# ---------------------------------------------------------------------- #


class TestDiagnostic:
    def test_roundtrip(self):
        diag = Diagnostic("error", "SCENE_STALE", "scene changed after approval")
        restored = Diagnostic.from_dict(diag.to_dict())
        assert restored.severity == "error"
        assert restored.code == "SCENE_STALE"

    def test_defaults(self):
        diag = Diagnostic()
        assert diag.severity == "info"


# ---------------------------------------------------------------------- #
# Permission -> approval derivation
# ---------------------------------------------------------------------- #


class TestPermissionApproval:
    @pytest.mark.parametrize(
        "permission,expected",
        [
            (ToolPermission.READ, False),
            (ToolPermission.PREVIEW, False),
            (ToolPermission.PROPOSAL, True),
            (ToolPermission.WRITE, True),
            (ToolPermission.EXPENSIVE, True),
            (ToolPermission.FILE_WRITE, True),
        ],
    )
    def test_enum_member(self, permission, expected):
        assert permission_requires_approval(permission) is expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("read", False),
            ("preview", False),
            ("write", True),
            ("file_write", True),
        ],
    )
    def test_raw_string(self, value, expected):
        # Must also work with plain strings (e.g. from serialised data).
        assert permission_requires_approval(value) is expected

    def test_read_only_classification(self):
        assert is_read_only_permission(ToolPermission.READ)
        assert is_read_only_permission("read")
        assert not is_read_only_permission(ToolPermission.WRITE)
