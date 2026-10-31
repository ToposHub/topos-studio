"""FX-H02 wiring regression: effect edits invalidate the clip render cache.

The cache-invalidation helper (invalidate_render_cache_for_clip) existed
but was never CALLED from the controller's effect-edit path, so editing an
effect left the viewer showing the pre-edit frame. This test pins the
wiring: each effect edit handler must call _invalidate_clip_render_cache
(clip_id), which forwards to the project's scoped invalidator.

Uses the same minimal SimpleNamespace controller shell as the no-rebuild
test — binds the REAL handler method so the code path runs, mocks the rest.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.edit_core


def _make_controller_shell(clip_id: str = "c1"):
    """Build a minimal controller shell with a project that records cache
    invalidation calls, bound to the real _invalidate_clip_render_cache."""
    from src.app.controllers.edit_controller import EditControllerMixin

    project = MagicMock()
    invalidated = {"clip_ids": []}

    def fake_invalidate(cid):
        invalidated["clip_ids"].append(cid)
        return project  # return same project (no real change needed)
    project.invalidate_render_cache_for_clip = fake_invalidate

    ctrl = SimpleNamespace(
        _current_edit_project=project,
        __class__=EditControllerMixin,
    )
    # Bind the real invalidation method.
    invalidate = EditControllerMixin._invalidate_clip_render_cache.__get__(
        ctrl, EditControllerMixin
    )
    ctrl._invalidate_clip_render_cache = invalidate
    return ctrl, project, invalidated


class TestInvalidateClipRenderCacheHelper:
    def test_helper_calls_project_invalidate(self) -> None:
        """The helper forwards to project.invalidate_render_cache_for_clip."""
        ctrl, project, invalidated = _make_controller_shell()
        ctrl._invalidate_clip_render_cache("c1")
        assert invalidated["clip_ids"] == ["c1"]

    def test_helper_noop_without_project(self) -> None:
        """No project → no crash, no call."""
        from src.app.controllers.edit_controller import EditControllerMixin
        ctrl = SimpleNamespace(_current_edit_project=None, __class__=EditControllerMixin)
        invalidate = EditControllerMixin._invalidate_clip_render_cache.__get__(
            ctrl, EditControllerMixin
        )
        # Must not raise.
        invalidate("c1")

    def test_helper_swallows_project_errors(self) -> None:
        """Cache invalidation must never break editing."""
        from src.app.controllers.edit_controller import EditControllerMixin
        project = MagicMock()
        project.invalidate_render_cache_for_clip = MagicMock(
            side_effect=RuntimeError("boom")
        )
        ctrl = SimpleNamespace(_current_edit_project=project, __class__=EditControllerMixin)
        invalidate = EditControllerMixin._invalidate_clip_render_cache.__get__(
            ctrl, EditControllerMixin
        )
        # Must not raise.
        invalidate("c1")


class TestParamChangeInvalidatesCache:
    """The param-change handler must invalidate the clip's cache."""

    def test_param_change_invalidates_cache(self, qapp) -> None:
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.effect_instance import EffectInstance

        blur = EffectInstance(
            effect_id="e1", effect_type="blur", params={"radius": 5.0}, order=0,
        )
        clip = MagicMock()
        clip.clip_id = "c1"
        clip.track_id = "t1"
        clip.effects = (blur,)
        clip.get_effect_by_id.return_value = blur

        timeline = MagicMock()
        timeline.get_clip_by_id.return_value = clip

        edit_page = MagicMock()
        edit_page._current_timeline = timeline
        edit_page._fps = 24.0
        edit_page._command_stack = MagicMock()
        edit_page._command_stack.execute.return_value = timeline

        ctrl, project, invalidated = _make_controller_shell()
        ctrl.edit_page = edit_page
        ctrl.edit_properties_panel = MagicMock()
        ctrl.edit_properties_panel._current_clips = [clip]
        ctrl._sync_timeline_to_views = lambda _tl: None
        ctrl._resync_audio_after_effect_change = lambda: None
        ctrl._request_program_viewer_update = MagicMock()
        # AUD-2707: the handler commits through the unified timeline-command
        # entry — bind the real helper (no timeline_panel on this shell →
        # direct-apply fallback + view sync).
        ctrl._execute_timeline_edit_command = (
            EditControllerMixin._execute_timeline_edit_command.__get__(
                ctrl, EditControllerMixin)
        )

        handler = EditControllerMixin._on_edit_effect_param_changed.__get__(
            ctrl, EditControllerMixin
        )

        update_cmd = MagicMock()
        fake_cmds = {"UpdateEffectParamCommand": MagicMock(create=MagicMock(return_value=update_cmd))}
        with patch("src.app.controllers.edit_controller.get_command_classes",
                   return_value=fake_cmds), \
             patch("src.app.controllers.edit_controller.get_effect_instance_class",
                   return_value=EffectInstance):
            handler("c1", "e1", "radius", 12.5)

        # FX-H02: the clip's cache must have been invalidated.
        assert "c1" in invalidated["clip_ids"], (
            "effect param change must invalidate the clip's render cache"
        )
