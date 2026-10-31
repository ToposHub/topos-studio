"""Motion Graphic build request + HTML artifact tests (plan §2.5 / §2.6).

Validates the typed contract that replaces ``dict[str, Any]`` in the Motion
Graphic pipeline, and the write-gate invariants (canvas from timeline,
artifacts not writable by default, stale-scene detection, verification
evidence required before any timeline write).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.edit.ai_editing.motion_graphic_artifacts import (
    HtmlArtifact,
    MotionGraphicBuildRequest,
    can_write_artifact,
    scene_is_stale,
)


def _request(**overrides):
    base = dict(
        task_id="t1",
        scene_id="s1",
        scene_revision=3,
        context_fingerprint="fp1",
        canvas_width=1920,
        canvas_height=1080,
        fps=30.0,
    )
    base.update(overrides)
    return MotionGraphicBuildRequest(**base)


def _staged_artifact(**overrides):
    base = dict(
        html_payload_ref="store://abc",
        sanitized_html_hash="h1",
        page_count=3,
        render_evidence={"captured_frames": 10},
    )
    base.update(overrides)
    return HtmlArtifact(**base)


# ---------------------------------------------------------------------- #
# MotionGraphicBuildRequest
# ---------------------------------------------------------------------- #


class TestBuildRequest:
    def test_canvas_from_timeline(self):
        req = _request()
        assert req.canvas == (1920, 1080)
        assert req.has_canvas

    def test_empty_canvas_not_has_canvas(self):
        req = _request(canvas_width=0, canvas_height=0)
        assert not req.has_canvas

    def test_task_id_required(self):
        with pytest.raises(ValueError):
            MotionGraphicBuildRequest(task_id="")

    def test_roundtrip(self):
        req = _request(user_brief="three pages")
        restored = MotionGraphicBuildRequest.from_dict(req.to_dict())
        assert restored.canvas == req.canvas
        assert restored.scene_revision == 3
        assert restored.user_brief == "three pages"


# ---------------------------------------------------------------------- #
# HtmlArtifact
# ---------------------------------------------------------------------- #


class TestHtmlArtifact:
    def test_not_writable_by_default(self):
        # Plan §2.6: writable starts False; only the confirm action flips it.
        assert _staged_artifact().writable is False

    def test_is_staged_requires_payload_and_hash(self):
        assert _staged_artifact().is_staged
        assert not _staged_artifact(html_payload_ref="").is_staged
        assert not _staged_artifact(sanitized_html_hash="").is_staged

    def test_verification_evidence_flag(self):
        assert _staged_artifact().has_verification_evidence
        assert not _staged_artifact(render_evidence={}).has_verification_evidence

    def test_roundtrip(self):
        art = _staged_artifact(viewport_width=1920, viewport_height=1080)
        restored = HtmlArtifact.from_dict(art.to_dict())
        assert restored.sanitized_html_hash == "h1"
        assert restored.viewport == (1920, 1080)
        assert restored.page_count == 3


# ---------------------------------------------------------------------- #
# Stale detection
# ---------------------------------------------------------------------- #


class TestSceneStale:
    def test_same_revision_not_stale(self):
        req = _request(scene_revision=3)
        art = _staged_artifact(approved_scene_hash="fp1")
        assert not scene_is_stale(req, art, current_scene_revision=3)

    def test_changed_revision_is_stale(self):
        req = _request(scene_revision=3)
        art = _staged_artifact(approved_scene_hash="fp1")
        assert scene_is_stale(req, art, current_scene_revision=4)

    def test_fingerprint_mismatch_is_stale(self):
        req = _request(context_fingerprint="fp1")
        art = _staged_artifact(approved_scene_hash="fp2")
        assert scene_is_stale(req, art, current_scene_revision=3)


# ---------------------------------------------------------------------- #
# Write gate (plan §2.5 §9 / §2.6)
# ---------------------------------------------------------------------- #


class TestCanWriteArtifact:
    def test_blocks_unconfirmed_artifact(self):
        ok, reason = can_write_artifact(_request(), _staged_artifact())
        assert not ok
        assert "确认" in reason or "writable" in reason

    def test_blocks_missing_evidence(self):
        art = _staged_artifact(writable=True, render_evidence={})
        ok, reason = can_write_artifact(_request(), art, current_scene_revision=3)
        assert not ok
        assert "验证" in reason

    def test_blocks_unstaged(self):
        art = _staged_artifact(writable=True, html_payload_ref="")
        ok, _ = can_write_artifact(_request(), art, current_scene_revision=3)
        assert not ok

    def test_blocks_stale_scene(self):
        art = _staged_artifact(writable=True, approved_scene_hash="fp1")
        ok, reason = can_write_artifact(_request(), art, current_scene_revision=99)
        assert not ok
        assert "变更" in reason

    def test_allows_fully_confirmed(self):
        art = _staged_artifact(writable=True, approved_scene_hash="fp1")
        ok, reason = can_write_artifact(_request(), art, current_scene_revision=3)
        assert ok
        assert reason == ""

    def test_single_gate_no_write_without_all_checks(self):
        # Each missing prerequisite must independently block the write.
        req = _request()
        # writable but no evidence
        assert not can_write_artifact(
            req, replace(_staged_artifact(), writable=True, render_evidence={})
        )[0]
        # evidence but not writable
        assert not can_write_artifact(req, _staged_artifact())[0]
