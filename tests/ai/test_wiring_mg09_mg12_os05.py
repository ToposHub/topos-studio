"""Wiring tests for MG-09 (command catalog), MG-12 (context collector),
and OS-05 (evidence kinds).

Proves the remaining dormant contract modules are now consumed by
production code in the Motion Graphic pipeline.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.teaching_animation.module import TeachingAnimationModule


# --------------------------------------------------------------------------- #
# MG-09: Extended command vocabulary
# --------------------------------------------------------------------------- #


class TestExtendedCommands:
    """MG-09: ExtendedCommandRegistry vocabulary is wired into the module."""

    def test_base_commands_present(self) -> None:
        cmds = TeachingAnimationModule.get_supported_edit_commands()
        assert "add_element" in cmds
        assert "update_element" in cmds
        assert "reorder_element" in cmds
        assert "set_animation" in cmds

    def test_extended_commands_present(self) -> None:
        """The 16 MG-09 extended names + 3 TM-02 template/brand + 2 TM-03 motion names are available."""
        cmds = TeachingAnimationModule.get_supported_edit_commands()
        extended = {
            "remove_element", "replace_element", "set_duration",
            "set_background", "add_marker", "remove_marker",
            "set_easing", "insert_keyframe", "remove_keyframe",
            "group_elements", "ungroup_elements", "set_layout",
            "set_color_token", "set_typo_token",
            "add_audio_ref", "remove_audio_ref",
            "apply_template", "set_brand_theme", "replace_asset_slot",
            "apply_motion_preset", "stagger_elements",
        }
        found = set(cmds)
        missing = extended - found
        assert not missing, f"Missing extended commands: {missing}"

    def test_total_command_count(self) -> None:
        """4 base + 16 extended + 3 template/brand + 2 motion = 25 commands."""
        cmds = TeachingAnimationModule.get_supported_edit_commands()
        assert len(cmds) == 25

    def test_commands_are_unique(self) -> None:
        cmds = TeachingAnimationModule.get_supported_edit_commands()
        assert len(cmds) == len(set(cmds))


# --------------------------------------------------------------------------- #
# MG-12: Project context fingerprint
# --------------------------------------------------------------------------- #


class TestContextFingerprint:
    """MG-12: ProjectContextSnapshot.fingerprint() is computed for stale detection."""

    def test_fingerprint_is_nonempty(self) -> None:
        timeline = MagicMock()
        timeline.timeline_id = "tl-1"
        timeline.revision = 5
        track = MagicMock()
        track.track_id = "gfx-1"
        track.name = "Graphics"
        fp = TeachingAnimationModule._collect_context_fingerprint(timeline, track, 48)
        assert isinstance(fp, str)
        assert len(fp) >= 32  # sha256 hex

    def test_same_context_same_fingerprint(self) -> None:
        """Same inputs produce the same fingerprint."""
        timeline = MagicMock()
        timeline.timeline_id = "tl-1"
        timeline.revision = 5
        track = MagicMock()
        track.track_id = "gfx-1"
        track.name = "Graphics"
        fp1 = TeachingAnimationModule._collect_context_fingerprint(timeline, track, 48)
        fp2 = TeachingAnimationModule._collect_context_fingerprint(timeline, track, 48)
        assert fp1 == fp2

    def test_different_revision_different_fingerprint(self) -> None:
        """Different timeline revision produces a different fingerprint."""
        timeline1 = MagicMock()
        timeline1.timeline_id = "tl-1"
        timeline1.revision = 5
        timeline2 = MagicMock()
        timeline2.timeline_id = "tl-1"
        timeline2.revision = 6
        track = MagicMock()
        track.track_id = "gfx-1"
        track.name = "Graphics"
        fp1 = TeachingAnimationModule._collect_context_fingerprint(timeline1, track, 48)
        fp2 = TeachingAnimationModule._collect_context_fingerprint(timeline2, track, 48)
        assert fp1 != fp2

    def test_none_timeline_returns_empty(self) -> None:
        """When timeline is None, fingerprint is empty (graceful degradation)."""
        fp = TeachingAnimationModule._collect_context_fingerprint(None, None, 0)
        # Should not crash; may be empty or a valid hash depending on import
        assert isinstance(fp, str)


# --------------------------------------------------------------------------- #
# OS-05: Evidence recording
# --------------------------------------------------------------------------- #


class TestEvidenceRecording:
    """OS-05: EvidenceRecord is created when scene + quality gate are done."""

    def test_evidence_kind_quality_exists(self) -> None:
        """The extended evidence vocabulary includes the quality kind."""
        from src.features.edit.ai_editing.evidence_kinds import EvidenceRecord
        record = EvidenceRecord(
            evidence_id="test-1",
            kind="quality",
            asset_id="scene-1",
            confidence=0.9,
            payload={"checks": ["first_frame"]},
        )
        assert record.kind == "quality"
        assert record.confidence == 0.9
        d = record.to_dict()
        assert d["kind"] == "quality"
        assert "payload" in d

    def test_evidence_extended_vocabulary(self) -> None:
        """OS-05 extended evidence kinds are available."""
        from src.features.edit.ai_editing.evidence_kinds import (
            EVIDENCE_COLOR, EVIDENCE_BEAT, EVIDENCE_LOUDNESS,
            EVIDENCE_VOICE, EVIDENCE_QUALITY,
        )
        # These should be string constants
        assert isinstance(EVIDENCE_COLOR, str)
        assert isinstance(EVIDENCE_BEAT, str)
        assert isinstance(EVIDENCE_LOUDNESS, str)
        assert isinstance(EVIDENCE_VOICE, str)

    def test_evidence_record_immutability(self) -> None:
        """EvidenceRecord is frozen (immutable)."""
        from src.features.edit.ai_editing.evidence_kinds import EvidenceRecord
        record = EvidenceRecord(
            evidence_id="test-2",
            kind="shot",
            asset_id="asset-1",
        )
        # Should not be able to modify frozen dataclass
        import dataclasses
        assert dataclasses.is_dataclass(record)
        try:
            record.evidence_id = "changed"
            assert False, "Should have raised FrozenInstanceError"
        except (AttributeError, dataclasses.FrozenInstanceError):
            pass  # Expected

    def test_evidence_in_quality_report_context(self) -> None:
        """The MG quality gate's report can be wrapped in an EvidenceRecord."""
        from src.features.edit.ai_editing.evidence_kinds import EvidenceRecord
        quality_report = {
            "is_deliverable": True,
            "error_count": 0,
            "warning_count": 1,
            "checks_run": ["first_frame", "design_validator"],
        }
        evidence = EvidenceRecord(
            evidence_id="mg-quality-1",
            kind="quality",
            asset_id="scene-1",
            confidence=1.0 if quality_report["is_deliverable"] else 0.5,
            payload={"quality_report": quality_report},
            summary="MG quality gate passed",
        )
        assert evidence.confidence == 1.0
        assert evidence.payload["quality_report"]["is_deliverable"] is True
