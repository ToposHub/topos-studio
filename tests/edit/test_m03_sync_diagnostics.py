"""M03.4 · 同步诊断持久化回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import EditProject, ProjectBin
from src.features.edit.core.sync_diagnostics import (
    SyncDiagnosticRecord,
    record_sync_diagnostic,
    sanitize_sync_diagnostics,
    sync_diagnostic_for,
)


def _project() -> EditProject:
    return EditProject(
        project_id="p", name="P", assets={},
        bins={"root": ProjectBin(bin_id="root", name="Root",
                                 parent_id=None, asset_ids=())},
        root_bin_id="root")


class TestRecordModel:
    def test_record_and_read_back(self):
        project = _project()
        rec = SyncDiagnosticRecord(
            asset_key="a1", method="waveform", offset_seconds=1.25,
            confidence=0.87, ambiguity=0.12, computed_at=1800000000.0)
        updated = record_sync_diagnostic(project, rec)
        assert updated is not project  # 不可变 replace
        assert project.sync_diagnostics == {}  # 原工程不动
        back = sync_diagnostic_for(updated, "a1")
        assert back == rec

    def test_rejected_sync_also_persisted(self):
        """拒绝的同步也持久化（rejection 非空——审计事实）。"""
        project = record_sync_diagnostic(
            _project(),
            SyncDiagnosticRecord(
                asset_key="a2", method="waveform", confidence=0.2,
                rejection="low_confidence"))
        assert sync_diagnostic_for(project, "a2").rejection \
            == "low_confidence"

    def test_same_key_overwrites_latest(self):
        project = _project()
        project = record_sync_diagnostic(project, SyncDiagnosticRecord(
            asset_key="a1", method="manual"))
        project = record_sync_diagnostic(project, SyncDiagnosticRecord(
            asset_key="a1", method="waveform", confidence=0.9))
        assert sync_diagnostic_for(project, "a1").method == "waveform"
        assert len(project.sync_diagnostics) == 1


class TestPersistence:
    def test_json_roundtrip(self):
        from src.features.edit.core.serialization import EditProjectSerializer
        project = record_sync_diagnostic(
            _project(), SyncDiagnosticRecord(
                asset_key="a1", method="waveform", offset_seconds=-0.5,
                confidence=-0.82, ambiguity=0.3, rejection="inverted_polarity",
                computed_at=123.0))
        restored = EditProjectSerializer.from_json(
            EditProjectSerializer.to_json(project))
        back = sync_diagnostic_for(restored, "a1")
        assert back.offset_seconds == -0.5
        assert back.rejection == "inverted_polarity"
        assert back.computed_at == 123.0

    def test_old_project_no_key_empty(self):
        from src.features.edit.core.serialization import EditProjectSerializer
        restored = EditProjectSerializer.from_json(
            EditProjectSerializer.to_json(_project()))
        assert restored.sync_diagnostics == {}

    def test_malformed_entries_dropped(self):
        from src.features.edit.core.serialization import EditProjectSerializer
        import json as _json
        payload = _json.loads(
            EditProjectSerializer.to_json(_project()))
        payload["project"]["sync_diagnostics"] = {
            "good": SyncDiagnosticRecord(
                asset_key="good", method="timecode").to_dict(),
            "bad-method": {"asset_key": "x", "method": "voodoo"},
            "bad-offset": {"asset_key": "x", "method": "manual",
                           "offset_seconds": 1e9},
            "not-a-dict": "junk",
        }
        restored = EditProjectSerializer.from_json(_json.dumps(payload))
        assert set(restored.sync_diagnostics) == {"good"}
