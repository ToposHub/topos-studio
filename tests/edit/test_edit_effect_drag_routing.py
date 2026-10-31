"""Regression test: drag-enter MIME routing for effect / transition drags.

The timeline's ``dragEnterEvent`` orders its MIME checks as a chain.
Application-specific MIME types (effect, transition) MUST be checked
before the generic ``hasUrls()`` fallback, because QMimeData carried by
an internal QDrag can also report ``hasUrls() == True`` on some
platforms. If the order is wrong, an effect drag gets misrouted into the
external-file-import branch and fails with "no project set".

This test constructs a QMimeData that carries BOTH the effect MIME and
a url (simulating the ambiguous case) and asserts that dragEnterEvent
selects the effect branch (sets ``_drag_effect_type`` and accepts).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QMimeData, Qt, QUrl, QByteArray
from PyQt6.QtWidgets import QApplication

# Import the module to grab the MIME constant the same way the view does.
from src.features.edit.ui import timeline_view as tv_module
from src.features.edit.ui.timeline_view import TimelineView, MIME_TYPE_EFFECT


def _make_effect_enter_event(effect_type: str, with_url: bool = False):
    """Build a QDragEnterEvent carrying the effect MIME.

    A real QDragEnterEvent can't be constructed directly in a portable
    way, so this returns a lightweight fake that exposes the two methods
    dragEnterEvent actually touches: ``mimeData()`` and
    ``acceptProposedAction()`` / ``ignore()``.
    """
    md = QMimeData()
    md.setData(MIME_TYPE_EFFECT, QByteArray(effect_type.encode("utf-8")))
    if with_url:
        # Simulate the ambiguous case: QMimeData also carries a url.
        md.setUrls([QUrl.fromLocalFile("/tmp/fake.mp4")])

    class _FakeEvent:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def mimeData(self):
            return md

        def acceptProposedAction(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    return _FakeEvent(), md


class _FakeTimelineView:
    """Stand-in exposing only the state attributes dragEnterEvent touches.

    Building a real TimelineView requires a scene, a project, a controller
    stack and a live canvas — none of which we need to verify MIME
    routing. We bind the real ``dragEnterEvent`` method to this shell so
    the test exercises the actual dispatch code, not a copy.
    """
    def __init__(self):
        self._project = None  # deliberately None — effect drag must NOT
                              # be misrouted into the "no project set"
                              # file-import branch.
        self._timeline = None
        self._drag_effect_type = None
        self._drag_transition_type = None
        self._drag_asset_ids = []
        self._drag_assets = []
        self._drag_file_paths = []

    # dragEnterEvent references ``self._filter_external_media_paths`` only
    # on the file-import branch — stub it in case the routing is wrong so
    # the test fails with a clear AttributeError rather than a confusing one.
    def _filter_external_media_paths(self, paths):
        return list(paths)


class _Signal:
    """Small stand-in for the Qt signal used by the file-drop path."""

    def __init__(self, callback):
        self._callback = callback

    def emit(self) -> None:
        self._callback()


def test_effect_drag_is_accepted_even_when_project_unset(qapp):
    """Effect drag must be accepted by the effect branch.

    A regression here (effect MIME checked after ``hasUrls()``) would
    route the drop into the file-import branch, which requires
    ``self._project`` and would call ``event.ignore()`` with a
    "no project set" warning.
    """
    view = _FakeTimelineView()
    # Bind the real method onto the shell instance.
    bound = TimelineView.dragEnterEvent.__get__(view, _FakeTimelineView)

    event, _ = _make_effect_enter_event("blur", with_url=False)
    bound(event)

    assert event.accepted is True, "effect drag should be accepted"
    assert event.ignored is False
    assert view._drag_effect_type == "blur"


def test_effect_drag_wins_over_hasurls(qapp):
    """The bug we're guarding against: when QMimeData carries BOTH the
    effect MIME and a url, the effect branch must still win.

    Without the fix, ``hasUrls()`` returns True and the chain falls into
    the file-import branch, which fails because ``_project is None``.
    """
    view = _FakeTimelineView()
    bound = TimelineView.dragEnterEvent.__get__(view, _FakeTimelineView)

    event, _ = _make_effect_enter_event("sharpen", with_url=True)
    bound(event)

    assert event.accepted is True, (
        "ambiguous effect+url drag must route to the effect branch, "
        "not the file-import branch"
    )
    assert view._drag_effect_type == "sharpen"
    # File-import state must NOT have been populated.
    assert view._drag_file_paths == []


def test_external_file_drag_auto_creates_project_when_unset(qapp):
    """A Finder drop may be the first action in an otherwise empty session."""
    view = _FakeTimelineView()
    project = object()
    view.project_needed = _Signal(lambda: setattr(view, "_project", project))
    bound = TimelineView.dragEnterEvent.__get__(view, _FakeTimelineView)

    event, mime_data = _make_effect_enter_event("", with_url=True)
    mime_data.removeFormat(MIME_TYPE_EFFECT)
    bound(event)

    assert view._project is project
    assert event.accepted is True
    assert event.ignored is False
