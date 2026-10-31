"""RC1: leaving a standalone-image Composite page must persist the graph.

``_flush_composite_to_edit_before_leave`` used to no-op for standalone images
(no EditProject), so the in-memory graph was lost the moment the next image was
loaded. It must now route standalone composites through ``save_router``.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from src.app.controllers.composite_controller import CompositeControllerMixin


class _Router:
    def __init__(self):
        self.composite_saves = 0

    def _save_from_composite(self):
        self.composite_saves += 1
        return True


def _ctrl(edit_project, composite_id="standalone_composite"):
    page = SimpleNamespace(
        _current_composite_id=composite_id,
        get_composite_data=lambda: {"nodes": [{"type": "color_input"}], "connections": []},
    )
    ctrl = SimpleNamespace(
        composite_page=page,
        _current_edit_project=edit_project,
        _current_composite_id="",
        save_router=_Router(),
        synced=False,
    )
    ctrl._sync_composite_graph_to_edit_project = lambda *a, **k: setattr(ctrl, "synced", True)
    ctrl._remember_active_comp_clip = lambda *a, **k: None
    return ctrl


def test_flush_persists_standalone_composite_on_leave():
    """No EditProject (standalone image) → save_router._save_from_composite runs."""
    ctrl = _ctrl(edit_project=None)
    CompositeControllerMixin._flush_composite_to_edit_before_leave(ctrl)
    assert ctrl.save_router.composite_saves == 1
    assert ctrl.synced is False  # edit-project sync path not taken


def test_flush_syncs_edit_project_composite_on_leave():
    """With an EditProject → existing sync path runs, standalone save does not."""
    ctrl = _ctrl(edit_project=object())
    CompositeControllerMixin._flush_composite_to_edit_before_leave(ctrl)
    assert ctrl.synced is True
    assert ctrl.save_router.composite_saves == 0


def test_flush_noops_without_composite_id():
    """If no composite id was bound, there's nothing to persist — don't save."""
    ctrl = _ctrl(edit_project=None, composite_id="")
    CompositeControllerMixin._flush_composite_to_edit_before_leave(ctrl)
    assert ctrl.save_router.composite_saves == 0
