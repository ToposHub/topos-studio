"""SUB-P2-01: Phase 2A caption model integration with SubtitleClip / EditTrack.

Verifies the layered caption fields wired onto the existing timeline data model
do not regress Phase 0+1 contracts and add the expected Phase 2A capabilities:

* SubtitleClip.style_override / caption_metadata default to "unset" (legacy cues
  behave exactly as before).
* resolved_style merges override ⊕ track default.
* Every manual EditTrack construction path (with_name/with_color/with_strip_eq/
  with_effect_chain/with_output_bus) preserves caption_metadata.
* _rebuild preserves caption_metadata by default.
* to_dict / from_dict round-trip the new fields losslessly (dual-write keeps
  flat fields for legacy readers).
* Legacy dicts (no Phase 2A keys) load losslessly with unset metadata.
* SUB-P0-03 invariants (overlap / owner track_id / unique clip_id) still hold.
* End-to-end: TimelineSequence serialize → caption-migrate → from_dict keeps
  all Phase 2A fields.

Headless (no QApplication) — pure data model.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionRole,
    CaptionStatus,
    CaptionStyle,
    CaptionTrackMetadata,
)
from src.features.edit.core.captions.schema import (
    CAPTION_SCHEMA_VERSION,
    CAPTION_SCHEMA_VERSION_KEY,
    upgrade_captions_in_project,
)


# ──────────────────────────────────────────────────────────────────────────
# SubtitleClip Phase 2A fields
# ──────────────────────────────────────────────────────────────────────────

class TestSubtitleClipPhase2A:
    def test_defaults_are_unset(self) -> None:
        c = SubtitleClip(clip_id="c1", track_id="t1", text="hi")
        assert c.style_override is None
        assert isinstance(c.caption_metadata, CaptionCueMetadata)
        assert c.caption_metadata.speaker == ""
        assert c.caption_metadata.status == CaptionStatus.DRAFT
        assert not c.is_edit_locked()

    def test_construct_with_layered_fields(self) -> None:
        c = SubtitleClip(
            clip_id="c2", track_id="t1", text="hello",
            style_override=CaptionStyle(font_family="Noto", font_size=140),
            caption_metadata=CaptionCueMetadata(
                speaker="Alice", status=CaptionStatus.APPROVED, locked=True,
            ),
        )
        assert c.style_override.font_family == "Noto"
        assert c.caption_metadata.speaker == "Alice"
        assert c.is_edit_locked()

    def test_type_validation_rejects_wrong_types(self) -> None:
        with pytest.raises(TypeError):
            SubtitleClip(clip_id="x", track_id="t", style_override="not a style")
        with pytest.raises(TypeError):
            SubtitleClip(clip_id="x", track_id="t", caption_metadata="not meta")

    def test_resolved_style_override_wins(self) -> None:
        track_default = CaptionStyle(font_family="Arial", font_size=120, fill_r=0.5)
        c = SubtitleClip(
            clip_id="c", track_id="t", text="x",
            style_override=CaptionStyle(font_family="Noto", font_size=140),
        )
        resolved = c.resolved_style(track_default)
        assert resolved.font_family == "Noto"   # override
        assert resolved.font_size == 140        # override
        assert resolved.fill_r == 0.5           # inherited

    def test_resolved_style_none_override_inherits_all(self) -> None:
        track_default = CaptionStyle(font_family="Arial", font_size=120)
        c = SubtitleClip(clip_id="c", track_id="t", text="x")
        assert c.resolved_style(track_default) == track_default

    def test_with_style_override_and_metadata(self) -> None:
        c = SubtitleClip(clip_id="c", track_id="t", text="x")
        c2 = c.with_style_override(CaptionStyle(font_size=200))
        assert c2.style_override.font_size == 200
        assert c.style_override is None  # frozen original
        c3 = c.with_caption_metadata(CaptionCueMetadata(speaker="Bob"))
        assert c3.caption_metadata.speaker == "Bob"

    def test_with_params_accepts_layered_fields(self) -> None:
        # UpdateSubtitleClipCommand routes through with_params; it must accept
        # the new fields so the existing command path keeps working.
        c = SubtitleClip(clip_id="c", track_id="t", text="x")
        c2 = c.with_params(
            style_override=CaptionStyle(font_size=200),
            caption_metadata=CaptionCueMetadata(speaker="Bob"),
        )
        assert c2.style_override.font_size == 200
        assert c2.caption_metadata.speaker == "Bob"


# ──────────────────────────────────────────────────────────────────────────
# Serialization round-trip
# ──────────────────────────────────────────────────────────────────────────

class TestSubtitleClipSerialization:
    def test_round_trip_preserves_layered_fields(self) -> None:
        c = SubtitleClip(
            clip_id="c", track_id="t", text="hello",
            style_override=CaptionStyle(font_family="Noto", font_size=140),
            caption_metadata=CaptionCueMetadata(
                speaker="Alice", status=CaptionStatus.APPROVED,
                note="good", source_span=(1.5, 3.2), revision=3,
            ),
        )
        d = c.to_dict()
        # Dual-write: layered block + version stamp present.
        assert d[CAPTION_SCHEMA_VERSION_KEY] == CAPTION_SCHEMA_VERSION
        assert d["style_override"]["font_family"] == "Noto"
        assert d["caption_metadata"]["speaker"] == "Alice"
        assert d["caption_metadata"]["source_span"] == [1.5, 3.2]
        # Flat fields still present (legacy reader compat).
        assert "font_family" in d and "fill_r" in d

        c2 = SubtitleClip.from_dict(d)
        assert c2.style_override.font_family == "Noto"
        assert c2.caption_metadata.speaker == "Alice"
        assert c2.caption_metadata.source_span == (1.5, 3.2)
        assert c2.caption_metadata.revision == 3

    def test_legacy_dict_loads_losslessly(self) -> None:
        # A v0/v1 dict (no caption_schema_version / style_override / metadata)
        # must load with unset metadata and original flat fields intact.
        legacy = {
            "clip_id": "c", "track_id": "t", "timeline_start": 0,
            "timeline_duration": 72, "text": "old", "font_family": "Times",
            "fill_r": 0.3,
        }
        c = SubtitleClip.from_dict(legacy)
        assert c.style_override is None
        assert c.caption_metadata.speaker == ""
        assert c.font_family == "Times"
        assert c.fill_r == 0.3

    def test_legacy_missing_visual_defaults_match_new_subtitle_defaults(self) -> None:
        c = SubtitleClip.from_dict({"clip_id": "c", "track_id": "t", "text": "old"})
        assert c.font_size == 0  # auto-fit sentinel
        assert c.valign == "bottom"


# ──────────────────────────────────────────────────────────────────────────
# EditTrack caption_metadata preservation across all with_* paths
# ──────────────────────────────────────────────────────────────────────────

class TestEditTrackCaptionMetadata:
    @pytest.fixture
    def sub_track(self) -> EditTrack:
        return EditTrack(
            track_id="t1", name="Captions", track_type="subtitle", index=0,
            caption_metadata=CaptionTrackMetadata(
                language="zh-Hans", role=CaptionRole.PRIMARY,
                default_style=CaptionStyle(font_family="Noto Sans CJK SC"),
            ),
        )

    def test_manual_paths_preserve_caption_metadata(self, sub_track: EditTrack) -> None:
        # The five methods that bypass _rebuild must all carry caption_metadata.
        for method, arg in [
            ("with_name", "Renamed"),
            ("with_color", "#ff0000"),
            ("with_strip_eq", None),
            ("with_effect_chain", None),
            ("with_output_bus", "bus1"),
        ]:
            nt = getattr(sub_track, method)(arg)
            assert nt.caption_metadata is not None, f"{method} dropped caption_metadata"
            assert nt.caption_metadata.language == "zh-Hans"

    def test_rebuild_paths_preserve_caption_metadata(self, sub_track: EditTrack) -> None:
        for method, arg in [
            ("with_visibility", False),
            ("with_muted", True),
            ("with_solo", True),
            ("with_locked", True),
            ("with_volume", 0.5),
            ("with_pan", 0.25),
        ]:
            # Some of these may not exist; skip if absent.
            if not hasattr(sub_track, method):
                continue
            nt = getattr(sub_track, method)(arg)
            assert nt.caption_metadata is not None, f"{method} dropped caption_metadata"
            assert nt.caption_metadata.language == "zh-Hans"

    def test_with_caption_metadata_set_and_clear(self, sub_track: EditTrack) -> None:
        t2 = sub_track.with_caption_metadata(CaptionTrackMetadata(language="en"))
        assert t2.caption_metadata.language == "en"
        assert sub_track.caption_metadata.language == "zh-Hans"  # frozen original
        t3 = sub_track.with_caption_metadata(None)
        assert t3.caption_metadata is None

    def test_to_from_dict_round_trip(self, sub_track: EditTrack) -> None:
        d = sub_track.to_dict()
        assert d["caption_metadata"]["language"] == "zh-Hans"
        t2 = EditTrack.from_dict(d)
        assert t2.caption_metadata.language == "zh-Hans"
        assert t2.caption_metadata.default_style.font_family == "Noto Sans CJK SC"

    def test_video_track_has_none_metadata(self) -> None:
        tv = EditTrack(track_id="v1", track_type="video", index=0)
        assert tv.caption_metadata is None
        d = tv.to_dict()
        assert d["caption_metadata"] is None
        tv2 = EditTrack.from_dict(d)
        assert tv2.caption_metadata is None


# ──────────────────────────────────────────────────────────────────────────
# SUB-P0-03 invariants still enforced with Phase 2A fields present
# ──────────────────────────────────────────────────────────────────────────

class TestInvariantsWithPhase2A:
    def test_subtitle_clips_only_on_subtitle_track(self) -> None:
        with pytest.raises(ValueError):
            EditTrack(
                track_id="v1", track_type="video", index=0,
                subtitle_clips=(SubtitleClip(clip_id="c", track_id="v1", text="x"),),
            )

    def test_subtitle_clip_track_id_must_match_owner(self) -> None:
        with pytest.raises(ValueError):
            EditTrack(
                track_id="t1", track_type="subtitle", index=0,
                subtitle_clips=(SubtitleClip(clip_id="c", track_id="other", text="x"),),
            )

    def test_no_duplicate_clip_id_within_track(self) -> None:
        with pytest.raises(ValueError):
            EditTrack(
                track_id="t1", track_type="subtitle", index=0,
                subtitle_clips=(
                    SubtitleClip(clip_id="dup", track_id="t1", text="a"),
                    SubtitleClip(clip_id="dup", track_id="t1", text="b"),
                ),
            )

    def test_no_subtitle_overlap(self) -> None:
        with pytest.raises(ValueError):
            EditTrack(
                track_id="t1", track_type="subtitle", index=0,
                subtitle_clips=(
                    SubtitleClip(clip_id="a", track_id="t1",
                                 timeline_start=0, timeline_duration=100, text="a"),
                    SubtitleClip(clip_id="b", track_id="t1",
                                 timeline_start=50, timeline_duration=100, text="b"),
                ),
            )

    def test_valid_subtitle_track_with_metadata_constructs(self) -> None:
        # Positive case: a well-formed subtitle track with full Phase 2A data.
        t = EditTrack(
            track_id="t1", track_type="subtitle", index=0,
            caption_metadata=CaptionTrackMetadata(language="zh"),
            subtitle_clips=(
                SubtitleClip(
                    clip_id="c1", track_id="t1",
                    timeline_start=0, timeline_duration=72, text="hi",
                    style_override=CaptionStyle(font_size=140),
                    caption_metadata=CaptionCueMetadata(speaker="A"),
                ),
            ),
        )
        assert len(t.subtitle_clips) == 1
        assert t.subtitle_clips[0].caption_metadata.speaker == "A"


# ──────────────────────────────────────────────────────────────────────────
# End-to-end: TimelineSequence serialize → migrate → reload
# ──────────────────────────────────────────────────────────────────────────

class TestEndToEndSequenceRoundTrip:
    def test_full_sequence_phase2a_round_trip(self) -> None:
        sub_track = EditTrack(
            track_id="sub1", name="Captions", track_type="subtitle", index=0,
            caption_metadata=CaptionTrackMetadata(
                language="zh-Hans", role=CaptionRole.PRIMARY,
                default_style=CaptionStyle(font_family="Noto Sans CJK SC", font_size=140),
            ),
            subtitle_clips=(
                SubtitleClip(
                    clip_id="c1", track_id="sub1",
                    timeline_start=0, timeline_duration=72, text="你好",
                    style_override=CaptionStyle(fill_r=0.9),
                    caption_metadata=CaptionCueMetadata(
                        speaker="Alice", status=CaptionStatus.APPROVED,
                        note="good", source_span=(1.5, 3.2), revision=3,
                    ),
                ),
            ),
        )
        seq = TimelineSequence(
            timeline_id="seq1", name="Main",
            subtitle_tracks=(sub_track,),
        )

        # Simulate the on-disk project wrapper layout.
        wrapper = {
            "version": 5,
            "project_type": "topos_edit",
            "project": {"name": "t"},
            "timeline_sequences": {"seq1": seq.to_dict()},
        }

        # Migrate (as the serializer does on load) then reload.
        upgrade_captions_in_project(wrapper)
        seq2 = TimelineSequence.from_dict(wrapper["timeline_sequences"]["seq1"])

        t2 = seq2.subtitle_tracks[0]
        assert t2.caption_metadata.language == "zh-Hans"
        assert t2.caption_metadata.default_style.font_family == "Noto Sans CJK SC"
        c2 = t2.subtitle_clips[0]
        assert c2.text == "你好"
        assert c2.style_override.fill_r == 0.9
        assert c2.caption_metadata.speaker == "Alice"
        assert c2.caption_metadata.source_span == (1.5, 3.2)
        assert c2.caption_metadata.revision == 3
