"""Regression tests for committing generated subtitles through TimelineView."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    TranscriptSegment,
)
from src.features.edit.core.commands import MacroCommand
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def _sequence() -> TimelineSequence:
    clip = ClipInstance(
        clip_id="audio-clip",
        asset_id="asset-1",
        track_id="audio-track",
        timeline_start=0,
        timeline_duration=240,
        source_start=0,
        source_duration=240,
    )
    return TimelineSequence(
        timeline_id="timeline-1",
        name="Test timeline",
        fps=24,
        audio_tracks=(EditTrack(
            track_id="audio-track",
            name="A1",
            track_type="audio",
            clips=(clip,),
        ),),
    )


def _analysis() -> AssetAnalysis:
    return AssetAnalysis(
        asset_id="asset-1",
        fingerprint=AssetFingerprint(),
        fps="24/1",
        transcript=(
            TranscriptSegment(0, 48, "第一句字幕", confidence=0.9),
            TranscriptSegment(60, 108, "第二句字幕", confidence=0.9),
        ),
        provenance=AnalysisProvenance(provider="test", model="test"),
    )


class _Panel:
    def __init__(self, view: TimelineView) -> None:
        self._view = view

    def get_timeline_view(self) -> TimelineView:
        return self._view


class _Controller(EditControllerMixin):
    def __init__(self, view: TimelineView, current_timeline=None) -> None:
        self.timeline_panel = _Panel(view)
        self.edit_page = SimpleNamespace(_current_timeline=current_timeline)
        self.messages: list[tuple[str, bool]] = []

    def _show_speech_to_text_message(self, message: str, *, warning: bool = False) -> None:
        self.messages.append((message, warning))


def test_generated_subtitles_are_committed_to_timeline_view(qapp) -> None:
    view = TimelineView()
    sequence = _sequence()
    view.load_timeline(sequence)
    controller = _Controller(view)

    controller._on_speech_to_text_completed(
        "audio-clip", "audio-track", "asset-1", _analysis()
    )

    assert view._timeline is not None
    assert len(view._timeline.subtitle_tracks) == 1
    assert len(view._timeline.subtitle_tracks[0].subtitle_clips) == 2
    assert controller.messages[-1] == ("已生成 2 条字幕", False)


def test_completion_loads_canonical_timeline_when_view_is_not_loaded(qapp) -> None:
    view = TimelineView()
    sequence = _sequence()
    controller = _Controller(view, current_timeline=sequence)

    controller._on_speech_to_text_completed(
        "audio-clip", "audio-track", "asset-1", _analysis()
    )

    assert view._timeline is not None
    assert len(view._timeline.subtitle_tracks[0].subtitle_clips) == 2
    assert controller.messages[-1] == ("已生成 2 条字幕", False)


def test_failed_subtitle_batch_rolls_back_new_track(qapp, monkeypatch) -> None:
    view = TimelineView()
    view.load_timeline(_sequence())
    original_execute = view.execute_command

    def _fail_macro(command) -> None:
        if isinstance(command, MacroCommand):
            raise RuntimeError("simulated subtitle insertion failure")
        original_execute(command)

    monkeypatch.setattr(view, "execute_command", _fail_macro)
    controller = _Controller(view)

    controller._on_speech_to_text_completed(
        "audio-clip", "audio-track", "asset-1", _analysis()
    )

    assert view._timeline is not None
    assert view._timeline.subtitle_tracks == ()
    assert controller.messages[-1][1] is True
    assert "写入第 1-2 条字幕" in controller.messages[-1][0]
