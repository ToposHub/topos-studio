"""SUB-P2-03: Caption Workspace — model + find/replace + command wiring tests.

Tests the headless-testable parts of the caption workspace:

* :class:`CaptionCueModel` — row/column counts, data display (timecode, text,
  speaker, status), UserRole clip_id lookup, reload-from-sequence ordering.
* :func:`find_in_clips` / :func:`build_replace_text` — pure find/replace logic
  (case sensitivity, multi-occurrence, track filter, empty needle).
* :class:`CaptionWorkspacePanel` — requires a QApplication, so it gets a
  ``qapp`` fixture; tests selection sync signals and split/merge command
  emission.

The panel's QWidget construction needs a running QApplication; the model and
find/replace helpers do not.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.core.captions.model import CaptionCueMetadata, CaptionStatus
from src.features.edit.core.captions.commands import (
    MergeSubtitleClipsCommand,
    SplitSubtitleClipCommand,
)
from src.features.edit.ui.caption_workspace import (
    CaptionCueModel,
    CaptionWorkspacePanel,
    CueColumn,
    FindMatch,
    build_replace_all_text,
    build_replace_text,
    find_in_clips,
    frame_to_timecode,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

def _make_clip(clip_id: str, start: int, duration: int, text: str = "",
               speaker: str = "", status: str = CaptionStatus.DRAFT) -> SubtitleClip:
    return SubtitleClip(
        clip_id=clip_id, track_id="t1",
        timeline_start=start, timeline_duration=duration, text=text,
        caption_metadata=CaptionCueMetadata(speaker=speaker, status=status),
    )


def _make_sequence(clips=None) -> TimelineSequence:
    clips = clips or ()
    track = EditTrack(track_id="t1", track_type="subtitle", index=0,
                      subtitle_clips=tuple(clips))
    return TimelineSequence(timeline_id="s1", name="main", subtitle_tracks=(track,))


# ──────────────────────────────────────────────────────────────────────────
# frame_to_timecode
# ──────────────────────────────────────────────────────────────────────────

class TestFrameToTimecode:
    def test_basic_24fps(self) -> None:
        assert frame_to_timecode(0, 24.0) == "00:00:00:00"
        assert frame_to_timecode(23, 24.0) == "00:00:00:23"
        assert frame_to_timecode(24, 24.0) == "00:00:01:00"
        assert frame_to_timecode(72, 24.0) == "00:00:03:00"

    def test_hour_rollover(self) -> None:
        # 1 hour at 24fps = 86400 frames
        assert frame_to_timecode(86400, 24.0) == "01:00:00:00"

    def test_zero_fps_does_not_crash(self) -> None:
        # Defensive: a bad fps falls back to 24 instead of dividing by zero.
        tc = frame_to_timecode(48, 0.0)
        assert tc == "00:00:02:00"


# ──────────────────────────────────────────────────────────────────────────
# CaptionCueModel
# ──────────────────────────────────────────────────────────────────────────

class TestCaptionCueModel:
    def test_empty_sequence(self) -> None:
        m = CaptionCueModel()
        m.reload_from_sequence(None)
        assert m.rowCount() == 0
        assert m.columnCount() == 7

    def test_loads_cues_ordered_by_start(self) -> None:
        # Pass cues out of order; model must sort by timeline_start.
        clips = [
            _make_clip("c2", start=100, duration=50, text="second"),
            _make_clip("c1", start=0, duration=50, text="first"),
        ]
        seq = _make_sequence(clips)
        m = CaptionCueModel()
        m.reload_from_sequence(seq, fps=24.0)
        assert m.rowCount() == 2
        assert m.row_at(0).clip.clip_id == "c1"  # earlier start first
        assert m.row_at(1).clip.clip_id == "c2"

    def test_row_index_is_1_based_sequential(self) -> None:
        clips = [_make_clip(f"c{i}", start=i * 10, duration=5) for i in range(5)]
        m = CaptionCueModel()
        m.reload_from_sequence(_make_sequence(clips))
        for i in range(5):
            assert m.row_at(i).row_index == i + 1

    def test_column_headers(self) -> None:
        from PyQt6.QtCore import Qt
        m = CaptionCueModel()
        for i, expected in enumerate(("#", "In", "Out", "Duration", "Text",
                                      "Speaker", "Status")):
            assert m.headerData(i, Qt.Orientation.Horizontal) == expected

    def test_data_display_values(self) -> None:
        from PyQt6.QtCore import QModelIndex, Qt
        clip = _make_clip("c1", start=24, duration=48, text="Hello",
                          speaker="Alice", status=CaptionStatus.APPROVED)
        m = CaptionCueModel()
        m.reload_from_sequence(_make_sequence([clip]), fps=24.0)
        idx = m.index(0, 0)
        # INDEX
        assert m.data(m.index(0, CueColumn.INDEX)) == "1"
        # IN_TC = frame 24 @ 24fps = 00:00:01:00
        assert m.data(m.index(0, CueColumn.IN_TC)) == "00:00:01:00"
        # OUT_TC = frame 72 @ 24fps = 00:00:03:00
        assert m.data(m.index(0, CueColumn.OUT_TC)) == "00:00:03:00"
        # DURATION = 48 frames = 00:00:02:00
        assert m.data(m.index(0, CueColumn.DURATION)) == "00:00:02:00"
        # TEXT
        assert m.data(m.index(0, CueColumn.TEXT)) == "Hello"
        # SPEAKER
        assert m.data(m.index(0, CueColumn.SPEAKER)) == "Alice"
        # STATUS
        assert m.data(m.index(0, CueColumn.STATUS)) == "approved"

    def test_user_role_returns_clip_and_track_ids(self) -> None:
        from PyQt6.QtCore import Qt
        clip = _make_clip("c1", start=0, duration=10)
        m = CaptionCueModel()
        m.reload_from_sequence(_make_sequence([clip]))
        val = m.data(m.index(0, 0), Qt.ItemDataRole.UserRole)
        assert val == ("c1", "t1")

    def test_find_row_by_clip_id(self) -> None:
        clips = [_make_clip("a", 0, 5), _make_clip("b", 10, 5)]
        m = CaptionCueModel()
        m.reload_from_sequence(_make_sequence(clips))
        assert m.find_row_by_clip_id("a") == 0
        assert m.find_row_by_clip_id("b") == 1
        assert m.find_row_by_clip_id("missing") == -1


# ──────────────────────────────────────────────────────────────────────────
# find_in_clips + build_replace_text
# ──────────────────────────────────────────────────────────────────────────

class TestFindInClips:
    def test_empty_needle_returns_nothing(self) -> None:
        clips = [_make_clip("c1", 0, 5, text="hello")]
        assert find_in_clips(clips, "") == []

    def test_single_match(self) -> None:
        clips = [_make_clip("c1", 0, 5, text="hello world")]
        matches = find_in_clips(clips, "world")
        assert len(matches) == 1
        assert matches[0].clip_id == "c1"
        assert matches[0].start == 6
        assert matches[0].end == 11
        assert matches[0].text_occurrence_index == 0

    def test_case_insensitive_default(self) -> None:
        clips = [_make_clip("c1", 0, 5, text="Hello WORLD")]
        matches = find_in_clips(clips, "hello")
        assert len(matches) == 1
        matches = find_in_clips(clips, "world")
        assert len(matches) == 1

    def test_case_sensitive(self) -> None:
        clips = [_make_clip("c1", 0, 5, text="Hello hello")]
        # Case-sensitive: only lowercase "hello" matches.
        matches = find_in_clips(clips, "hello", case_sensitive=True)
        assert len(matches) == 1
        assert matches[0].start == 6

    def test_multi_occurrence_in_one_cue(self) -> None:
        clips = [_make_clip("c1", 0, 5, text="na na na")]
        matches = find_in_clips(clips, "na")
        assert len(matches) == 3
        assert [m.text_occurrence_index for m in matches] == [0, 1, 2]

    def test_overlapping_occurrences_are_not_double_counted(self) -> None:
        clips = [_make_clip("c1", 0, 5, text="aaa")]
        matches = find_in_clips(clips, "aa")
        assert [(match.start, match.end) for match in matches] == [(0, 2)]

    def test_case_insensitive_unicode_spans_reference_original_text(self) -> None:
        clip = _make_clip("c1", 0, 5, text="İx")
        matches = find_in_clips([clip], "x")
        assert [(match.start, match.end) for match in matches] == [(1, 2)]
        assert build_replace_all_text(clip.text, matches, "Y") == "İY"

    def test_across_multiple_clips(self) -> None:
        clips = [
            _make_clip("c1", 0, 5, text="foo bar"),
            _make_clip("c2", 10, 5, text="foo baz"),
        ]
        matches = find_in_clips(clips, "foo")
        assert len(matches) == 2
        assert {m.clip_id for m in matches} == {"c1", "c2"}

    def test_track_filter(self) -> None:
        # Clips on different tracks; filter restricts to one.
        c1 = _make_clip("c1", 0, 5, text="foo")
        c2 = SubtitleClip(clip_id="c2", track_id="t2",
                          timeline_start=10, timeline_duration=5, text="foo")
        matches = find_in_clips([c1, c2], "foo", track_id_filter="t1")
        assert len(matches) == 1
        assert matches[0].clip_id == "c1"


class TestBuildReplaceText:
    def test_basic_replacement(self) -> None:
        match = FindMatch(clip_id="c1", track_id="t1",
                          text_occurrence_index=0, start=0, end=3)
        assert build_replace_text("foo bar", match, "XXX") == "XXX bar"

    def test_middle_replacement(self) -> None:
        match = FindMatch(clip_id="c1", track_id="t1",
                          text_occurrence_index=0, start=6, end=11)
        assert build_replace_text("hello world", match, "there") == "hello there"

    def test_replacement_different_length(self) -> None:
        match = FindMatch(clip_id="c1", track_id="t1",
                          text_occurrence_index=0, start=0, end=5)
        # Replace "hello" (5 chars) with "hi" (2 chars).
        assert build_replace_text("hello world", match, "hi") == "hi world"

    @pytest.mark.parametrize(
        ("replacement", "expected"),
        [
            ("LONG", "LONG LONG"),
            ("", " "),
            ("x", "x x"),
        ],
    )
    def test_replace_all_preserves_original_offsets(
        self, replacement: str, expected: str,
    ) -> None:
        clip = _make_clip("c1", 0, 5, text="a a")
        matches = find_in_clips([clip], "a")
        assert build_replace_all_text(clip.text, matches, replacement) == expected


# ──────────────────────────────────────────────────────────────────────────
# CaptionWorkspacePanel (requires QApplication)
# ──────────────────────────────────────────────────────────────────────────

class TestCaptionWorkspacePanel:
    def test_set_sequence_populates_model(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        clips = [_make_clip("c1", 0, 24, text="hi"),
                 _make_clip("c2", 24, 24, text="there")]
        panel.set_sequence(_make_sequence(clips), fps=24.0)
        assert panel._model.rowCount() == 2

    def test_select_clip_emits_cue_selected(self, qapp) -> None:
        # User-driven row selection (not programmatic select_clip) must emit
        # cue_selected so the controller can sync timeline/viewer/properties.
        # Programmatic select_clip intentionally suppresses the signal to
        # avoid feedback loops when the controller is the one driving it.
        panel = CaptionWorkspacePanel()
        clips = [_make_clip("c1", 0, 24, text="hi"),
                 _make_clip("c2", 24, 24, text="there")]
        panel.set_sequence(_make_sequence(clips), fps=24.0)

        received = []
        panel.cue_selected.connect(lambda cid, tid: received.append((cid, tid)))
        # Simulate a user click on row 1 (c2) via the selection model directly,
        # bypassing the _building_ui guard that programmatic select_clip sets.
        sm = panel._table.selectionModel()
        from PyQt6.QtCore import QItemSelectionModel
        sm.select(panel._model.index(1, 0),
                  QItemSelectionModel.SelectionFlag.ClearAndSelect
                  | QItemSelectionModel.SelectionFlag.Rows)
        qapp.processEvents()
        assert ("c2", "t1") in received

    def test_programmatic_select_does_not_emit(self, qapp) -> None:
        # select_clip is the controller→workspace path; it must NOT re-emit
        # cue_selected (that would create a selection feedback loop).
        panel = CaptionWorkspacePanel()
        panel.set_sequence(_make_sequence([_make_clip("c1", 0, 24)]), fps=24.0)
        received = []
        panel.cue_selected.connect(lambda cid, tid: received.append((cid, tid)))
        panel.select_clip("c1")
        qapp.processEvents()
        assert received == []

    def test_split_emits_split_command(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        clip = _make_clip("c1", 0, 100, text="hello")
        panel.set_sequence(_make_sequence([clip]), fps=24.0)
        panel.select_clip("c1")

        received = []
        panel.command_requested.connect(lambda cmd: received.append(cmd))
        panel._on_split_selected()
        assert len(received) == 1
        assert isinstance(received[0], SplitSubtitleClipCommand)
        assert received[0].clip_id == "c1"

    def test_merge_emits_merge_command(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        clips = [_make_clip("c1", 0, 50, text="hello"),
                 _make_clip("c2", 50, 50, text="world")]
        panel.set_sequence(_make_sequence(clips), fps=24.0)
        panel._select_clip_ids(["c1", "c2"])

        received = []
        panel.command_requested.connect(lambda cmd: received.append(cmd))
        panel._on_merge_selected()
        assert len(received) == 1
        assert isinstance(received[0], MergeSubtitleClipsCommand)

    def test_find_status_message(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        clips = [_make_clip("c1", 0, 24, text="hello world"),
                 _make_clip("c2", 24, 24, text="hello again")]
        panel.set_sequence(_make_sequence(clips), fps=24.0)
        panel._find_edit.setText("hello")
        panel._on_find()
        # Should report 2 matches.
        assert "2" in panel._status_label.text()

    def test_find_no_matches(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        panel.set_sequence(_make_sequence([_make_clip("c1", 0, 24, text="hi")]))
        panel._find_edit.setText("zzz")
        panel._on_find()
        assert "No matches" in panel._status_label.text()

    def test_split_requires_single_selection(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        clips = [_make_clip("c1", 0, 50), _make_clip("c2", 50, 50)]
        panel.set_sequence(_make_sequence(clips))
        panel._select_clip_ids(["c1", "c2"])  # two selected
        panel._on_split_selected()
        assert "exactly one" in panel._status_label.text().lower()

    def test_merge_requires_two_selection(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        panel.set_sequence(_make_sequence([_make_clip("c1", 0, 50)]))
        panel.select_clip("c1")  # only one
        panel._on_merge_selected()
        assert "two" in panel._status_label.text().lower()

    def test_replace_all_emits_find_requested(self, qapp) -> None:
        panel = CaptionWorkspacePanel()
        panel.set_sequence(_make_sequence([_make_clip("c1", 0, 24, text="foo")]))
        panel._find_edit.setText("foo")
        panel._replace_edit.setText("bar")
        received = []
        panel.find_requested.connect(lambda *a: received.append(a))
        panel._on_replace_all()
        assert len(received) == 1
        assert received[0] == ("foo", "bar", "all")


# ──────────────────────────────────────────────────────────────────────────
# Controller wiring contract (SUB-P2-03 three-way sync + batch macros)
#
# MainWindow wires the panel's three signals to EditControllerMixin handlers:
#   cue_selected     → on_subtitle_clip_clicked   (workspace → timeline/viewer)
#   command_requested→ _on_caption_command_requested (split/merge → commit)
#   find_requested   → _on_caption_find_requested  (replace-all → batch macro)
# and the reverse direction (timeline → workspace) via select_clip, which is
# suppressed from re-emitting cue_selected (no feedback loop). These tests pin
# that contract with lightweight stand-ins — no full MainWindow needed.
# ──────────────────────────────────────────────────────────────────────────


class _FakeTView:
    """Minimal TimelineView stand-in recording executed commands."""

    def __init__(self) -> None:
        self.executed: list = []

    def execute_command(self, command) -> None:
        self.executed.append(command)

    def _execute_command(self, command) -> None:  # batch macro path
        self.executed.append(command)


class _FakeTimelinePanel:
    def __init__(self, tview: _FakeTView) -> None:
        self.timeline_view = tview


class _FakeEditPage:
    def __init__(self, timeline) -> None:
        self._current_timeline = timeline


class _FakeController:
    """Re-implements the three handlers exactly as EditControllerMixin does,
    so we can assert the wiring contract without a QMainWindow. The real
    handlers live on EditControllerMixin; this mirrors their control flow."""

    def __init__(self, timeline) -> None:
        self.edit_page = _FakeEditPage(timeline)
        self.timeline_panel = _FakeTimelinePanel(_FakeTView())
        self.status_bar = None
        self.selected_clip_ids: list = []
        self.refresh_count = 0

    # mirrors on_subtitle_clip_clicked's workspace leg + select_clip reverse sync
    def on_subtitle_clip_clicked(self, clip_id: str, track_id: str) -> None:
        self.selected_clip_ids.append(clip_id)

    # mirrors _on_caption_command_requested
    def _on_caption_command_requested(self, command) -> None:
        self.timeline_panel.timeline_view.execute_command(command)

    # mirrors _on_caption_find_requested (simplified: one cmd per matched cue)
    def _on_caption_find_requested(self, needle: str, replacement: str,
                                   scope: str) -> None:
        timeline = self.edit_page._current_timeline
        if not needle:
            return
        clips = [c for tr in timeline.subtitle_tracks for c in tr.subtitle_clips]
        matches = find_in_clips(clips, needle)
        new_text_by_id: dict = {}
        for m in matches:
            clip = timeline.get_subtitle_clip_by_id(m.clip_id)
            base = new_text_by_id.get(m.clip_id, clip.text or "")
            new_text_by_id[m.clip_id] = build_replace_text(base, m, replacement)
        # batch them as one macro
        from src.features.edit.core.commands import MacroCommand, UpdateSubtitleClipCommand
        subs = [UpdateSubtitleClipCommand.create(clip_id=cid, updates={"text": t},
                                                 label="Replace All in Captions")
                for cid, t in new_text_by_id.items()]
        if len(subs) == 1:
            self.timeline_panel.timeline_view._execute_command(subs[0])
        elif subs:
            self.timeline_panel.timeline_view._execute_command(
                MacroCommand.create("Replace All in Captions", subs))


def test_wiring_command_requested_routes_to_commit(qapp) -> None:
    """command_requested (split/merge) flows through the single commit path."""
    timeline = _make_sequence([_make_clip("c1", 0, 100, text="hello")])
    ctrl = _FakeController(timeline)
    panel = CaptionWorkspacePanel()
    panel.set_sequence(timeline, fps=24.0)
    panel.select_clip("c1")
    # wire exactly as main_window.setup_connections does
    panel.cue_selected.connect(ctrl.on_subtitle_clip_clicked)
    panel.command_requested.connect(ctrl._on_caption_command_requested)
    panel._on_split_selected()  # emits command_requested
    assert len(ctrl.timeline_panel.timeline_view.executed) == 1
    assert isinstance(ctrl.timeline_panel.timeline_view.executed[0],
                      SplitSubtitleClipCommand)


def test_wiring_find_requested_builds_batch_macro(qapp) -> None:
    """find_requested produces one cmd per matched cue as a single macro."""
    clips = [_make_clip("c1", 0, 24, text="hello world"),
             _make_clip("c2", 24, 24, text="hello again")]
    timeline = _make_sequence(clips)
    ctrl = _FakeController(timeline)
    panel = CaptionWorkspacePanel()
    panel.set_sequence(timeline, fps=24.0)
    panel.find_requested.connect(ctrl._on_caption_find_requested)
    panel._find_edit.setText("hello")
    panel._replace_edit.setText("HI")
    panel._on_replace_all()  # emits find_requested
    # exactly one (macro) command committed
    executed = ctrl.timeline_panel.timeline_view.executed
    assert len(executed) == 1
    from src.features.edit.core.commands import MacroCommand
    assert isinstance(executed[0], MacroCommand)


def test_wiring_select_clip_no_feedback_loop(qapp) -> None:
    """Programmatic select_clip must not re-emit cue_selected (no loop)."""
    timeline = _make_sequence([_make_clip("c1", 0, 24)])
    ctrl = _FakeController(timeline)
    panel = CaptionWorkspacePanel()
    panel.set_sequence(timeline, fps=24.0)
    panel.cue_selected.connect(ctrl.on_subtitle_clip_clicked)
    panel.select_clip("c1")  # controller-driven, must NOT call the handler
    assert ctrl.selected_clip_ids == []


def test_wiring_user_click_routes_to_subtitle_handler(qapp) -> None:
    """A user row-click emits cue_selected → on_subtitle_clip_clicked."""
    timeline = _make_sequence([_make_clip("c1", 0, 24), _make_clip("c2", 24, 24)])
    ctrl = _FakeController(timeline)
    panel = CaptionWorkspacePanel()
    panel.set_sequence(timeline, fps=24.0)
    panel.cue_selected.connect(ctrl.on_subtitle_clip_clicked)
    # simulate a user click on row 1 (c2)
    from PyQt6.QtCore import QItemSelectionModel
    sm = panel._table.selectionModel()
    sm.select(panel._model.index(1, 0),
              QItemSelectionModel.SelectionFlag.ClearAndSelect
              | QItemSelectionModel.SelectionFlag.Rows)
    qapp.processEvents()
    assert "c2" in ctrl.selected_clip_ids
