"""Nested sequence support tests.

Covers the data model (virtual asset + clip creation) and the cycle-safe
recursive evaluation helper. The full preview-renderer integration is
exercised via the existing preview/renderer regression suite; here we focus
on the pure nested_sequence module and the MediaAsset extension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.nested_sequence import (
    NESTED_SEQUENCE_URI_PREFIX,
    create_nested_clip,
    create_nested_sequence_asset,
    evaluate_nested_frame,
    make_nested_sequence_uri,
    timeline_id_from_uri,
)
from src.features.edit.core.project import MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ── Fixtures ──


def _make_sequence(tid: str, duration: int = 10, name: str = "Seq") -> TimelineSequence:
    clip = ClipInstance(
        clip_id=f"{tid}-clip",
        asset_id=f"{tid}-asset",
        track_id=f"{tid}-track",
        timeline_start=0,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
    )
    track = EditTrack(track_id=f"{tid}-track", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id=tid,
        name=name,
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(track,),
    )


@pytest.fixture
def child_sequence() -> TimelineSequence:
    return _make_sequence("child-seq", duration=10)


# ── URI helpers ──


class TestNestedSequenceURI:
    def test_make_uri(self):
        assert make_nested_sequence_uri("abc") == f"{NESTED_SEQUENCE_URI_PREFIX}abc"

    def test_parse_uri(self):
        assert timeline_id_from_uri(f"{NESTED_SEQUENCE_URI_PREFIX}abc") == "abc"

    def test_parse_non_uri_returns_none(self):
        assert timeline_id_from_uri("/normal/path.mp4") is None
        assert timeline_id_from_uri("topos://sequence/") == ""


# ── Asset creation ──


class TestNestedSequenceAsset:
    def test_creates_virtual_asset(self, child_sequence: TimelineSequence):
        asset = create_nested_sequence_asset(child_sequence)
        assert asset.media_type == "nested_sequence"
        assert asset.is_nested_sequence
        assert asset.path == make_nested_sequence_uri("child-seq")
        assert asset.nested_timeline_id == "child-seq"
        assert asset.metadata["width"] == 1920
        assert asset.metadata["height"] == 1080
        assert asset.metadata["duration_frames"] == 10

    def test_custom_display_name(self, child_sequence: TimelineSequence):
        asset = create_nested_sequence_asset(child_sequence, display_name="My Nest")
        assert asset.display_name == "My Nest"

    def test_media_type_whitelist_accepts_nested(self, child_sequence: TimelineSequence):
        """MediaAsset validation must accept media_type='nested_sequence'."""
        asset = create_nested_sequence_asset(child_sequence)
        # Round-trip through construction (re-validates __post_init__)
        rebuilt = MediaAsset(
            asset_id=asset.asset_id,
            path=asset.path,
            media_type="nested_sequence",
            display_name=asset.display_name,
            metadata=asset.metadata,
        )
        assert rebuilt.is_nested_sequence

    def test_nested_timeline_id_from_metadata(self, child_sequence: TimelineSequence):
        """nested_timeline_id prefers metadata['timeline_id'] over URI parsing."""
        asset = create_nested_sequence_asset(child_sequence)
        assert asset.nested_timeline_id == "child-seq"

    def test_nested_timeline_id_falls_back_to_uri(self):
        """If metadata lacks timeline_id, fall back to parsing the path."""
        asset = MediaAsset(
            asset_id="a1",
            path="topos://sequence/fallback-id",
            media_type="nested_sequence",
            display_name="x",
            metadata={},
        )
        assert asset.nested_timeline_id == "fallback-id"

    def test_nested_timeline_id_none_for_other_types(self):
        asset = MediaAsset(
            asset_id="a1",
            path="/some/video.mp4",
            media_type="video",
            display_name="x",
            metadata={},
        )
        assert asset.nested_timeline_id is None
        assert not asset.is_nested_sequence


# ── Clip creation ──


class TestNestedClip:
    def test_creates_clip_referencing_asset(self, child_sequence: TimelineSequence):
        asset = create_nested_sequence_asset(child_sequence)
        clip = create_nested_clip(asset, track_id="host-track", timeline_start=5)
        assert clip.asset_id == asset.asset_id
        assert clip.track_id == "host-track"
        assert clip.timeline_start == 5
        assert clip.timeline_duration == 10  # defaults to child duration

    def test_custom_durations(self, child_sequence: TimelineSequence):
        asset = create_nested_sequence_asset(child_sequence)
        clip = create_nested_clip(
            asset, track_id="t", timeline_start=0,
            timeline_duration=4, source_start=2,
        )
        assert clip.timeline_duration == 4
        assert clip.source_start == 2

    def test_rejects_non_nested_asset(self):
        asset = MediaAsset(
            asset_id="a1", path="/v.mp4", media_type="video",
            display_name="x", metadata={},
        )
        with pytest.raises(ValueError):
            create_nested_clip(asset, track_id="t", timeline_start=0)


# ── Recursive evaluation + cycle detection ──


class TestEvaluateNestedFrame:
    def test_resolves_and_evaluates(self, child_sequence: TimelineSequence):
        asset = create_nested_sequence_asset(child_sequence)

        def resolver(tid: str):
            return child_sequence if tid == "child-seq" else None

        def plan_factory(seq):
            return ("plan", seq.timeline_id)

        def evaluator(plan, frame):
            return f"frame@{frame}"

        result = evaluate_nested_frame(asset, 5, resolver, plan_factory, evaluator)
        assert result == "frame@5"

    def test_unresolvable_returns_none(self, child_sequence: TimelineSequence):
        asset = create_nested_sequence_asset(child_sequence)
        result = evaluate_nested_frame(
            asset, 0,
            sequence_resolver=lambda tid: None,
            render_plan_factory=lambda seq: seq,
            composite_evaluator=lambda p, f: "x",
        )
        assert result is None

    def test_cycle_detected(self, child_sequence: TimelineSequence):
        """A sequence that recursively references itself must abort, not loop."""
        asset = create_nested_sequence_asset(child_sequence)
        call_count = {"n": 0}

        def cyclic_evaluator(plan, frame):
            call_count["n"] += 1
            # Simulate the child containing itself again
            return evaluate_nested_frame(
                asset, frame,
                sequence_resolver=lambda tid: child_sequence,
                render_plan_factory=lambda seq: seq,
                composite_evaluator=cyclic_evaluator,
                _render_stack={child_sequence.timeline_id},
            )

        # Top-level call seeds the stack with child-seq; the inner call sees
        # child-seq already in the stack and aborts.
        result = evaluate_nested_frame(
            asset, 0,
            sequence_resolver=lambda tid: child_sequence,
            render_plan_factory=lambda seq: seq,
            composite_evaluator=cyclic_evaluator,
        )
        assert result is None  # aborted by cycle guard
