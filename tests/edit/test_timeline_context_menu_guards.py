from __future__ import annotations

from src.features.edit.ui.timeline_items import _is_menu_action_selected


def test_dismissed_menu_never_matches_missing_optional_action() -> None:
    """The old None == None comparison triggered audio transcription."""
    assert not _is_menu_action_selected(None, None)


def test_context_menu_action_uses_identity() -> None:
    action = object()
    assert _is_menu_action_selected(action, action)
    assert not _is_menu_action_selected(object(), action)
