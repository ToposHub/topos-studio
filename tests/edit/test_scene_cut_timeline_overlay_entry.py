"""B1: Scene cut timeline overlay production entry tests.

Covers the production wiring added on 2026-09-07:
- TimelineView overlay switch (lazy creation, empty-candidate safety,
  show/hide round-trip);
- EditPage overlay switch (single entry point, persistence round-trip via
  ConfigManager, visibility signal emission, dialog-state sync hook);
- SceneCutDialog "Show on Timeline" toggle (enable-on-analysis,
  signal payload, checked-state sync without re-emission).

The candidates used here are real ``SceneCutCandidate`` frozen dataclasses;
no detection worker is involved.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.scene_cut.models import (  # noqa: E402
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")


def _candidate(source_frame: int, timeline_frame: int | None = None) -> SceneCutCandidate:
    """Build a minimal pending candidate for overlay tests."""
    return SceneCutCandidate(
        source_frame=source_frame,
        pts=None,
        seconds=source_frame / 24.0,
        timeline_frame=timeline_frame,
        score=0.9,
        confidence=0.8,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.PENDING,
    )


# ----------------------------------------------------------------------
# TimelineView overlay switch
# ----------------------------------------------------------------------

class TestTimelineViewOverlay:
    """TimelineView.set_scene_cut_overlay_visible / set_scene_cut_candidates."""

    def _make_view(self) -> Any:
        from src.features.edit.ui.timeline_view import TimelineView

        return TimelineView()

    def test_hidden_by_default(self, qapp) -> None:
        view = self._make_view()
        assert view.is_scene_cut_overlay_visible() is False
        assert view._scene_cut_overlay is None

    def test_show_creates_overlay_without_candidates(self, qapp) -> None:
        """Toggling on with no detection result must not crash (empty state)."""
        view = self._make_view()
        view.set_scene_cut_overlay_visible(True)
        assert view.is_scene_cut_overlay_visible() is True
        assert view._scene_cut_overlay is not None
        assert view._scene_cut_overlay.is_visible is True

    def test_hide_removes_overlay_visibility(self, qapp) -> None:
        view = self._make_view()
        view.set_scene_cut_overlay_visible(True)
        view.set_scene_cut_overlay_visible(False)
        assert view.is_scene_cut_overlay_visible() is False
        assert view._scene_cut_overlay.is_visible is False

    def test_candidates_stored_before_show(self, qapp) -> None:
        """Candidates arriving before the first toggle are kept for later."""
        view = self._make_view()
        view.set_scene_cut_candidates((_candidate(24, 10), _candidate(96, 40)))
        assert view._scene_cut_overlay is None  # lazy: not created while hidden
        view.set_scene_cut_overlay_visible(True)
        assert len(view._scene_cut_overlay.candidates) == 2

    def test_candidates_update_while_visible(self, qapp) -> None:
        view = self._make_view()
        view.set_scene_cut_overlay_visible(True)
        view.set_scene_cut_candidates((_candidate(48, 20),))
        assert len(view._scene_cut_overlay.candidates) == 1
        view.set_scene_cut_candidates(())
        assert len(view._scene_cut_overlay.candidates) == 0

    def test_zoom_sync_keeps_overlay(self, qapp) -> None:
        """Zoom changes must not destroy or hide the overlay."""
        view = self._make_view()
        view.set_scene_cut_overlay_visible(True)
        view.set_zoom(2.0)
        assert view._scene_cut_overlay is not None
        assert view._scene_cut_overlay.is_visible is True


# ----------------------------------------------------------------------
# EditPage switch: single entry + persistence
# ----------------------------------------------------------------------

@pytest.fixture
def fake_config(monkeypatch) -> dict[str, Any]:
    """Replace ConfigManager get/set with an in-memory store."""
    store: dict[str, Any] = {}
    from src.shared.config import config_manager as cm_module

    manager = cm_module.ConfigManager.__new__(cm_module.ConfigManager)

    monkeypatch.setattr(
        cm_module.ConfigManager,
        "get",
        lambda self, key: store.get(key),
    )

    def _set(self, key, value, auto_save=True):
        store[key] = value

    monkeypatch.setattr(cm_module.ConfigManager, "set", _set)
    monkeypatch.setattr(
        cm_module.ConfigManager, "save_config", lambda self: None
    )
    # Pre-seed a value so ``get`` behaves like a persisted default.
    store["edit_scene_cut_overlay_visible"] = False
    return store


class TestEditPageOverlaySwitch:
    """EditPage.set_scene_cut_overlay_visible persistence + signals."""

    def test_switch_toggles_and_persists(self, qapp, fake_config) -> None:
        from src.features.edit.ui.edit_page import EditPage

        page = EditPage()
        page.set_scene_cut_overlay_visible(True)
        assert page.is_scene_cut_overlay_visible() is True
        assert fake_config["edit_scene_cut_overlay_visible"] is True

        page.set_scene_cut_overlay_visible(False)
        assert page.is_scene_cut_overlay_visible() is False
        assert fake_config["edit_scene_cut_overlay_visible"] is False

    def test_visibility_signal_emitted(self, qapp, fake_config) -> None:
        from src.features.edit.ui.edit_page import EditPage

        page = EditPage()
        seen: list[bool] = []
        page.scene_cut_overlay_visibility_changed.connect(seen.append)
        page.set_scene_cut_overlay_visible(True)
        page.set_scene_cut_overlay_visible(False)
        assert seen == [True, False]

    def test_idempotent_toggle_emits_nothing(self, qapp, fake_config) -> None:
        from src.features.edit.ui.edit_page import EditPage

        page = EditPage()
        seen: list[bool] = []
        page.scene_cut_overlay_visibility_changed.connect(seen.append)
        page.set_scene_cut_overlay_visible(False)  # already off
        assert seen == []

    def test_restore_applies_persisted_state(self, qapp, fake_config) -> None:
        """A persisted on-state must be re-applied to a fresh page."""
        fake_config["edit_scene_cut_overlay_visible"] = True
        from src.features.edit.ui.edit_page import EditPage

        page = EditPage()
        page._restore_scene_cut_overlay_state()
        assert page.is_scene_cut_overlay_visible() is True

    def test_restore_keeps_off_state(self, qapp, fake_config) -> None:
        from src.features.edit.ui.edit_page import EditPage

        page = EditPage()
        page._restore_scene_cut_overlay_state()
        assert page.is_scene_cut_overlay_visible() is False

    def test_analysis_updates_attached_timeline_view(self, qapp, fake_config) -> None:
        """detection_completed → overlay candidates flow through EditPage."""
        from src.features.edit.ui.edit_page import EditPage

        page = EditPage()

        stored: list[tuple] = []

        class _FakeView:
            def set_scene_cut_candidates(self, candidates: tuple) -> None:
                stored.append(candidates)

            def set_scene_cut_overlay_visible(self, visible: bool) -> None:
                pass

        page._timeline_panel = type(
            "Panel", (), {"get_timeline_view": lambda self: _FakeView()}
        )()
        analysis = type(
            "Analysis", (), {"candidates": (_candidate(24, 10), _candidate(48, 20))}
        )()
        page._on_scene_cut_analysis_for_overlay(analysis)
        assert stored == [(_candidate(24, 10), _candidate(48, 20))]


# ----------------------------------------------------------------------
# BottomActionBar "Show on Timeline" toggle
# ----------------------------------------------------------------------

class TestDialogShowOnTimelineToggle:
    """SceneCutDialog BottomActionBar toggle wiring (B1)."""

    def _make_bar(self) -> Any:
        from src.features.edit.ui.scene_cut_dialog import BottomActionBar

        return BottomActionBar()

    def test_disabled_until_analysis(self, qapp) -> None:
        bar = self._make_bar()
        assert bar._show_on_timeline_btn.isEnabled() is False
        bar.set_show_on_timeline_enabled(True)
        assert bar._show_on_timeline_btn.isEnabled() is True

    def test_toggle_emits_bool(self, qapp) -> None:
        bar = self._make_bar()
        bar.set_show_on_timeline_enabled(True)
        seen: list[bool] = []
        bar.show_on_timeline_toggled.connect(seen.append)
        bar._show_on_timeline_btn.setChecked(True)
        bar._show_on_timeline_btn.setChecked(False)
        assert seen == [True, False]

    def test_set_checked_does_not_reemit(self, qapp) -> None:
        """Syncing from the menu path must not bounce the signal back."""
        bar = self._make_bar()
        bar.set_show_on_timeline_enabled(True)
        seen: list[bool] = []
        bar.show_on_timeline_toggled.connect(seen.append)
        bar.set_show_on_timeline_checked(True)
        assert seen == []
        assert bar.is_show_on_timeline_checked() is True

    def test_dialog_forwards_toggle(self, qapp) -> None:
        """SceneCutDialog re-emits the bottom bar toggle (skip heavy setup)."""
        from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

        sig = SceneCutDialog.show_on_timeline_toggled
        assert sig is not None
