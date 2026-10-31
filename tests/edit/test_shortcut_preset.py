"""P2-01: shortcut map preset export/import with conflict re-validation.

Contract (from the professional-gap audit):
- export_preset produces a schema'd, versioned map of every action's active
  shortcut and round-trips through import_preset.
- import_preset never silently drops entries: unknown actions are reported,
  duplicate sequences inside the preset are conflicts (none applied),
  schema/version mismatches reject the whole file.
- merge keeps overrides for actions absent from the preset; replace resets
  everything first so the preset fully determines the map.
"""

from __future__ import annotations

import pytest

from src.shared.ui.shortcut_manager import ShortcutManager


@pytest.fixture()
def manager(qapp):
    m = ShortcutManager()
    m.register("file.open", "Open", "File", "Ctrl+O")
    m.register("file.save", "Save", "File", "Ctrl+S")
    m.register("edit.undo", "Undo", "Edit", "Ctrl+Z")
    yield m


class TestExport:
    def test_export_contains_schema_and_all_actions(self, manager):
        preset = manager.export_preset()

        assert preset["schema"] == "topos-shortcut-preset"
        assert preset["version"] == 1
        assert preset["shortcuts"] == {
            "file.open": "Ctrl+O",
            "file.save": "Ctrl+S",
            "edit.undo": "Ctrl+Z",
        }

    def test_export_reflects_custom_overrides(self, manager):
        manager.set_shortcut("file.save", "Ctrl+Shift+S")

        preset = manager.export_preset()

        assert preset["shortcuts"]["file.save"] == "Ctrl+Shift+S"


class TestImportRoundtrip:
    def test_roundtrip_applies_all_entries(self, manager):
        preset = manager.export_preset()

        report = manager.import_preset(preset)

        assert report.ok
        assert sorted(report.applied) == ["edit.undo", "file.open", "file.save"]

    def test_modified_preset_changes_shortcuts(self, manager):
        preset = manager.export_preset()
        preset["shortcuts"]["file.open"] = "Ctrl+P"

        manager.import_preset(preset)

        assert manager.get_shortcut("file.open") == "Ctrl+p" or \
            manager.get_shortcut("file.open").lower() == "ctrl+p"

    def test_default_value_clears_custom_override(self, manager):
        manager.set_shortcut("file.save", "Ctrl+Shift+S")
        preset = manager.export_preset()
        preset["shortcuts"]["file.save"] = "Ctrl+S"

        manager.import_preset(preset)

        # Setting back to the default removes the override entirely.
        assert "file.save" not in manager._custom_shortcuts


class TestImportValidation:
    def test_unknown_actions_are_reported_not_applied(self, manager):
        preset = manager.export_preset()
        preset["shortcuts"]["ghost.action"] = "Ctrl+Q"

        report = manager.import_preset(preset)

        assert not report.ok
        assert report.unknown == ("ghost.action",)
        assert "ghost.action" not in report.applied

    def test_intra_preset_conflicts_are_skipped(self, manager):
        preset = manager.export_preset()
        preset["shortcuts"]["file.open"] = "Ctrl+X"
        preset["shortcuts"]["file.save"] = "Ctrl+X"

        report = manager.import_preset(preset)

        assert report.conflicts == (("Ctrl+X", ["file.open", "file.save"]),)
        assert "file.open" not in report.applied
        assert "file.save" not in report.applied
        # Neither was changed.
        assert manager.get_shortcut("file.open") == "Ctrl+O"

    def test_wrong_schema_rejects_everything(self, manager):
        report = manager.import_preset({"schema": "something-else", "version": 1})

        assert report.applied == ()
        assert report.errors

    def test_future_version_rejects(self, manager):
        report = manager.import_preset({
            "schema": "topos-shortcut-preset",
            "version": 99,
            "shortcuts": {"file.open": "Ctrl+P"},
        })

        assert report.applied == ()
        assert any("version" in e for e in report.errors)

    def test_non_dict_input_is_rejected(self, manager):
        report = manager.import_preset("not a preset")

        assert report.applied == ()
        assert report.errors


class TestImportModes:
    def test_merge_keps_absent_overrides(self, manager):
        manager.set_shortcut("edit.undo", "Ctrl+Alt+Z")
        preset = manager.export_preset()
        del preset["shortcuts"]["edit.undo"]

        report = manager.import_preset(preset, mode="merge")

        assert report.ok
        assert manager.get_shortcut("edit.undo") == "Ctrl+Alt+Z"

    def test_replace_clears_absent_overrides(self, manager):
        manager.set_shortcut("edit.undo", "Ctrl+Alt+Z")
        preset = manager.export_preset()
        del preset["shortcuts"]["edit.undo"]

        report = manager.import_preset(preset, mode="replace")

        assert report.ok
        assert manager.get_shortcut("edit.undo") == "Ctrl+Z"

    def test_report_to_dict_is_serializable(self, manager):
        preset = manager.export_preset()
        preset["shortcuts"]["ghost"] = "Ctrl+Q"
        report = manager.import_preset(preset)

        data = report.to_dict()
        assert data["unknown"] == ["ghost"]
        assert isinstance(data["applied"], list)
