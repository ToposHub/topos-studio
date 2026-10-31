"""Regression test: changing an effect param must NOT rebuild the panel.

Symptom: dragging an effect's slider felt "stuck" — the slider jumped
back or aborted mid-drag. Root cause: ``_on_edit_effect_param_changed``
called ``set_clip`` after every valueChanged emission, which tore down
the slider widget the user was still dragging.

The fix removes the panel rebuild from the param-change path; only the
viewer is refreshed. This test pins that behaviour: param change must
not call ``set_clip`` on the properties panel.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication


def _make_controller_with_clip():
    """Build a minimal EditControllerMixin shell bound to a fake edit_page.

    We bind the real ``_on_edit_effect_param_changed`` method so the test
    exercises the actual code path; everything else is mocked.
    """
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.core.effect_instance import EffectInstance

    # A real EffectInstance so isinstance() checks in the handler succeed.
    blur_effect = EffectInstance(
        effect_id="e1", effect_type="blur",
        params={"radius": 5.0}, is_enabled=True, order=0,
    )

    clip = MagicMock()
    clip.clip_id = "c1"
    clip.track_id = "t1"
    clip.effects = (blur_effect,)
    clip.get_effect_by_id.return_value = blur_effect

    timeline = MagicMock()
    timeline.get_clip_by_id.return_value = clip

    edit_page = MagicMock()
    edit_page._current_timeline = timeline
    edit_page._fps = 24.0
    edit_page._command_stack = MagicMock()
    edit_page._command_stack.execute.return_value = timeline

    panel = MagicMock()
    panel._current_clips = [clip]

    ctrl = SimpleNamespace(
        edit_page=edit_page,
        edit_properties_panel=panel,
        # FX-M09: handler unconditionally syncs the mutated timeline into
        # preview/renderer and (for audio FX) resyncs the audio engine.
        # FX-H02: it also invalidates the clip's render cache. These are real
        # collaborators in production; in this minimal mock shell they must
        # exist as no-ops so the param-change code path runs end-to-end. The
        # test contract is about panel rebuild, not view sync / cache, so
        # stubbing them is correct.
        _sync_timeline_to_views=lambda _tl: None,
        _resync_audio_after_effect_change=lambda: None,
        _invalidate_clip_render_cache=lambda _cid: None,
        __class__=EditControllerMixin,
    )
    # Bind the real method.
    handler = EditControllerMixin._on_edit_effect_param_changed.__get__(ctrl, EditControllerMixin)
    # AUD-2707: the handler commits through the unified timeline-command
    # entry — bind the real helper so the code path runs end-to-end (this
    # shell has no timeline_panel → direct-apply fallback + view sync).
    ctrl._execute_timeline_edit_command = (
        EditControllerMixin._execute_timeline_edit_command.__get__(
            ctrl, EditControllerMixin)
    )
    return ctrl, handler, panel


def test_param_change_does_not_rebuild_panel(qapp):
    """Slider drag → emit param change → handler must NOT call set_clip.

    ``set_clip`` rebuilds every section; calling it during a drag destroys
    the slider mid-interaction. The only valid side effect of a param
    change is refreshing the viewer.
    """
    ctrl, handler, panel = _make_controller_with_clip()
    ctrl._request_program_viewer_update = MagicMock()

    # Patch the module-level facade functions the handler imports at the
    # top of edit_controller — these resolve at call time, so patching
    # them on the module makes the handler use our stubs.
    from src.features.edit.core.effect_instance import EffectInstance
    update_cmd = MagicMock()
    fake_cmds = {"UpdateEffectParamCommand": MagicMock(create=MagicMock(return_value=update_cmd))}
    import src.app.controllers.edit_controller as ec_module
    with patch.object(ec_module, "get_command_classes", return_value=fake_cmds), \
         patch.object(ec_module, "get_effect_instance_class", return_value=EffectInstance):
        handler("c1", "e1", "radius", 12.5)

    # The contract: NO set_clip, YES viewer refresh.
    panel.set_clip.assert_not_called()
    assert ctrl._request_program_viewer_update.called, (
        "param change must still refresh the viewer"
    )
