"""Regression: single-file export must respect the composite toggle.

Root cause of "quick export applies composite but Export button does not":
``_start_export`` routes a 1-file queue to ``export_requested`` →
``perform_panel_export``, which historically ignored composite entirely
(it only rendered the Color pipeline). Meanwhile multi-file queues went
through ``batch_export_requested`` → ``_batch_export_single_file``, which
DID apply composite. So a single file in the batch list silently lost its
composite effects on export.

The fix has two parts:
1. ``get_settings`` (single-file path) now collects ``apply_composite``.
2. ``perform_panel_export`` applies the in-memory composite graph when the
   flag is set.

These tests cover part 1 (the settings collection) since it's pure UI state
and needs no GL. Part 2 (the actual render) is exercised via the existing
batch-export graph-normalize tests + manual verification.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.shared.export.export_panel import ExportPanel


@pytest.fixture
def panel(qapp):
    return ExportPanel()


class TestPanelExportCompositeSetting:
    """get_settings must carry apply_composite for the single-file path."""

    def test_settings_includes_apply_composite(self, panel):
        """The composite flag must be present in single-file export settings."""
        settings = panel.get_settings()
        assert "apply_composite" in settings

    def test_composite_checkbox_default_checked(self, panel):
        """The composite checkbox must default to checked.

        New users expect their Composite-page effects to ship with the export.
        An unchecked default silently dropped composite and was the root cause
        of repeated 'batch export has no composite' confusion.
        """
        assert panel.apply_composite_check.isChecked() is True
        # get_settings must reflect that default without any user interaction
        assert panel.get_settings()["apply_composite"] is True

    def test_settings_reflects_checked_default(self, panel):
        """By default the composite checkbox is ON → apply_composite True.

        Users expect their Composite-page work to ship with the export by
        default; the old unchecked default silently dropped composite effects
        and confused new users. No-composite files fall back to Color output,
        so a checked default has no downside.
        """
        panel.apply_composite_check.setChecked(True)  # explicit, matches default
        settings = panel.get_settings()
        assert settings["apply_composite"] is True

    def test_settings_reflects_unchecked_state(self, panel):
        """When the user unchecks 'apply composite', settings must carry False."""
        panel.apply_composite_check.setChecked(False)
        settings = panel.get_settings()
        assert settings["apply_composite"] is False


# ── efr-small-s1 误删 QPainter 别名回归 ──────────────────────────────────


def test_create_delete_icon_builds_valid_icon(qapp):
    """_create_delete_icon 在运行时构造 QPainter（历史别名 QPainter2 被误删）。"""
    from src.shared.export.export_panel import _create_delete_icon

    icon = _create_delete_icon()
    assert not icon.isNull()
    assert icon.availableSizes()
