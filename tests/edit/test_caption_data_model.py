"""SUB-P2-01 / 06 / 10 / 12: Phase 2A caption data model contract tests.

Covers the layered caption model introduced in Phase 2A:

* :class:`CaptionStyle` — 34 visual fields, Optional (None = inherit), merge.
* :class:`CaptionTrackMetadata` — language / role / default_style / safe_anchor.
* :class:`CaptionCueMetadata` — speaker / status / note / lock / provenance +
  unknown-key preservation (SUB-P2-10).
* :func:`resolve_style` — flatten track-default ⊕ cue-override → legacy dict.
* Style presets — registry, builtins, apply/propagate (SUB-P2-06).
* FontResolver — headless resolve + preflight (SUB-P2-06/11).
* Caption schema migration — v0/v1 → v2, idempotent, project walker (SUB-P2-10).
* SubtitleClip / EditTrack integration — new fields, dual-write serialization,
  manual-construction paths preserve caption_metadata, invariants intact.

These are headless (no QApplication) — the model layer is pure data.
"""

from __future__ import annotations

import copy

import pytest

from src.features.edit.core.captions.model import (
    STYLE_FIELDS,
    CaptionCueMetadata,
    CaptionRole,
    CaptionSafeAnchor,
    CaptionStatus,
    CaptionStyle,
    CaptionTrackMetadata,
    EMPTY_STYLE,
    from_legacy_clip,
    resolve_style,
    style_to_full_legacy_fields,
    style_to_legacy_fields,
)
from src.features.edit.core.captions.schema import (
    CAPTION_SCHEMA_VERSION,
    CAPTION_SCHEMA_VERSION_KEY,
    migrate_subtitle_clip_dict,
    migrate_track_caption_dict,
    preserve_unknown_keys,
    upgrade_captions_in_project,
)
from src.features.edit.core.captions.font_resolver import (
    FontResolver,
    PreflightIssue,
    collect_families,
    preflight_fonts,
    weight_to_qt,
)
from src.features.edit.core.captions.style_preset import (
    BUILTIN_PRESETS,
    CaptionStylePreset,
    StylePresetRegistry,
    apply_preset_to_cue,
    apply_preset_to_track,
    cue_matches_preset,
    propagate_preset_change,
)


# ──────────────────────────────────────────────────────────────────────────
# CaptionStyle
# ──────────────────────────────────────────────────────────────────────────

class TestCaptionStyle:
    def test_style_fields_count_includes_transform_defaults(self) -> None:
        # Track defaults cover visual styling and subtitle placement.
        assert len(STYLE_FIELDS) == 34
        # Timing/identity/content fields are NOT style.
        for non_style in ("timeline_start", "clip_id", "is_enabled", "text"):
            assert non_style not in STYLE_FIELDS

    def test_empty_style_all_none(self) -> None:
        s = CaptionStyle()
        assert s.is_empty()
        assert s.overridden_fields() == ()

    def test_partial_override_records_only_set_fields(self) -> None:
        s = CaptionStyle(font_family="Noto", font_size=140, fill_r=0.5)
        assert s.overridden_fields() == ("font_family", "font_size", "fill_r")
        assert not s.is_empty()

    def test_merge_override_wins_on_set_fields_inherits_rest(self) -> None:
        base = CaptionStyle(font_family="Arial", font_size=120, fill_r=0.5)
        override = CaptionStyle(font_size=140, fill_g=0.8)
        merged = override.merge_over(base)
        assert merged.font_family == "Arial"    # inherited
        assert merged.font_size == 140          # overridden
        assert merged.fill_r == 0.5             # inherited
        assert merged.fill_g == 0.8             # overridden
        assert merged.fill_b is None            # neither set

    def test_invalid_enum_values_raise(self) -> None:
        for kwargs in (
            {"font_weight": "bogus"},
            {"halign": "diag"},
            {"valign": "sideways"},
        ):
            with pytest.raises(ValueError):
                CaptionStyle(**kwargs)

    def test_font_size_must_be_positive(self) -> None:
        # font_size=0 is a valid sentinel (= auto-fit, not an error)
        CaptionStyle(font_size=0)
        with pytest.raises(ValueError):
            CaptionStyle(font_size=-10)

    def test_round_trip_to_dict_from_dict(self) -> None:
        s = CaptionStyle(font_family="Noto", font_size=140, italic=True,
                         outline_enable=True, shadow_r=0.3)
        d = s.to_dict()
        # Only non-None fields written.
        assert set(d) == {"font_family", "font_size", "italic",
                          "outline_enable", "shadow_r"}
        s2 = CaptionStyle.from_dict(d)
        assert s2 == s

    def test_from_dict_ignores_unknown_keys(self) -> None:
        # Unknown keys at the style layer are silently dropped (cue metadata
        # layer handles verbatim preservation).
        s = CaptionStyle.from_dict({"font_family": "X", "future_field": 42})
        assert s.font_family == "X"
        assert not hasattr(s, "future_field")


# ──────────────────────────────────────────────────────────────────────────
# CaptionTrackMetadata
# ──────────────────────────────────────────────────────────────────────────

class TestCaptionTrackMetadata:
    def test_defaults(self) -> None:
        tm = CaptionTrackMetadata()
        assert tm.language == ""
        assert tm.role == CaptionRole.PRIMARY
        assert tm.safe_anchor == CaptionSafeAnchor.BOTTOM_CENTER
        assert tm.default_style.is_empty()

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValueError):
            CaptionTrackMetadata(role="bogus")

    def test_invalid_safe_anchor_raises(self) -> None:
        with pytest.raises(ValueError):
            CaptionTrackMetadata(safe_anchor="middle_left")

    def test_with_methods_return_new_instances(self) -> None:
        tm = CaptionTrackMetadata()
        tm2 = tm.with_language("en").with_role(CaptionRole.SDH)
        assert tm.language == ""           # original unchanged (frozen)
        assert tm2.language == "en"
        assert tm2.role == CaptionRole.SDH

    def test_round_trip_omits_empty_default_style(self) -> None:
        tm = CaptionTrackMetadata(language="zh", default_style=CaptionStyle())
        d = tm.to_dict()
        # Empty default_style omitted for compactness.
        assert "default_style" not in d
        tm2 = CaptionTrackMetadata.from_dict(d)
        assert tm2 == tm

    def test_round_trip_with_default_style(self) -> None:
        tm = CaptionTrackMetadata(
            language="zh",
            default_style=CaptionStyle(font_family="Noto", font_size=140),
        )
        tm2 = CaptionTrackMetadata.from_dict(tm.to_dict())
        assert tm2 == tm
        assert tm2.default_style.font_family == "Noto"


# ──────────────────────────────────────────────────────────────────────────
# CaptionCueMetadata + unknown-key preservation (SUB-P2-10)
# ──────────────────────────────────────────────────────────────────────────

class TestCaptionCueMetadata:
    def test_defaults(self) -> None:
        cm = CaptionCueMetadata()
        assert cm.speaker == ""
        assert cm.status == CaptionStatus.DRAFT
        assert cm.revision == 0
        assert not cm.is_edit_locked()

    def test_locked_status_implies_edit_lock(self) -> None:
        assert CaptionCueMetadata(status=CaptionStatus.LOCKED).is_edit_locked()
        assert CaptionCueMetadata(locked=True).is_edit_locked()
        assert not CaptionCueMetadata(status=CaptionStatus.APPROVED).is_edit_locked()

    def test_source_span_validation(self) -> None:
        # end < start rejected
        with pytest.raises(ValueError):
            CaptionCueMetadata(source_span=(3.0, 1.0))
        # wrong arity rejected
        with pytest.raises(TypeError):
            CaptionCueMetadata(source_span=(1.0,))  # type: ignore[arg-type]
        # valid 2-tuple ok
        cm = CaptionCueMetadata(source_span=(1.5, 3.2))
        assert cm.source_span == (1.5, 3.2)

    def test_revision_bump(self) -> None:
        cm = CaptionCueMetadata(revision=5)
        cm2 = cm.bumped_revision()
        assert cm.revision == 5       # frozen original
        assert cm2.revision == 6

    def test_unknown_keys_preserved_verbatim(self) -> None:
        # SUB-P2-10: a future schema field must survive a round-trip through
        # this build without being dropped.
        original = {
            "speaker": "Alice", "status": "approved", "revision": 2,
            "future_word_timings": [[0.1, 0.5], [0.6, 1.0]],
            "custom_review_tag": "client-v3",
        }
        cm = CaptionCueMetadata.from_dict(original)
        out = cm.to_dict()
        assert out["future_word_timings"] == [[0.1, 0.5], [0.6, 1.0]]
        assert out["custom_review_tag"] == "client-v3"
        # known keys still present
        assert out["speaker"] == "Alice"
        assert out["status"] == "approved"

    def test_malformed_source_span_dropped_not_crash(self) -> None:
        # Bad data should not crash load; drop the field instead.
        cm = CaptionCueMetadata.from_dict({"source_span": "not-a-tuple"})
        assert cm.source_span is None


# ──────────────────────────────────────────────────────────────────────────
# resolve_style — the flatten primitive
# ──────────────────────────────────────────────────────────────────────────

class TestResolveStyle:
    def test_cue_override_wins_over_track_default(self) -> None:
        track = CaptionStyle(font_family="Arial", font_size=120, fill_r=0.5)
        cue = CaptionStyle(font_size=140)
        flat = resolve_style(track, cue)
        assert flat["font_family"] == "Arial"   # inherited
        assert flat["font_size"] == 140         # overridden
        assert flat["fill_r"] == 0.5            # inherited

    def test_empty_override_fully_inherits_track(self) -> None:
        track = CaptionStyle(font_family="Arial", font_size=120)
        flat = resolve_style(track, EMPTY_STYLE)
        assert flat["font_family"] == "Arial"
        assert flat["font_size"] == 120
        # unset track fields fall to legacy defaults
        assert flat["fill_r"] == 1.0

    def test_none_override_treated_as_empty(self) -> None:
        track = CaptionStyle(font_family="Arial")
        flat_none = resolve_style(track, None)
        flat_empty = resolve_style(track, EMPTY_STYLE)
        assert flat_none == flat_empty

    def test_legacy_defaults_fill_gaps(self) -> None:
        # Brand-new track + cue with no overrides → every key gets a legacy default.
        flat = resolve_style(EMPTY_STYLE, EMPTY_STYLE)
        assert flat["font_family"] == "Arial"
        assert flat["font_size"] == 0  # 0 = auto-fit sentinel
        assert flat["fill_a"] == 1.0
        assert flat["outline_width"] == 4.0
        assert set(flat) == set(STYLE_FIELDS)

    def test_resolved_dict_keys_match_legacy_clip_fields(self) -> None:
        # The renderer reads flat keys; resolve_style must emit exactly those.
        flat = resolve_style(CaptionStyle(font_size=100))
        # No "style." prefix; geometry is now part of the inherited style.
        assert all(not k.startswith("style.") for k in flat)
        for geometry in ("translate_x", "rotation", "pivot_mode"):
            assert geometry in flat


# ──────────────────────────────────────────────────────────────────────────
# Legacy bridge
# ──────────────────────────────────────────────────────────────────────────

class TestLegacyBridge:
    def test_from_legacy_clip_captures_all_style_fields(self) -> None:
        class FakeClip:
            font_family = "Arial"; font_size = 120; font_weight = "normal"; italic = False
            halign = "center"; valign = "middle"; letter_spacing = 0.0; line_spacing = 1.0
            translate_x = 0.0; translate_y = 0.0; scale_x = 1.0; scale_y = 1.0
            rotation = 0.0; pivot_mode = "center"; pivot_x = 0.0; pivot_y = 0.0
            fill_r = 1.0; fill_g = 1.0; fill_b = 1.0; fill_a = 1.0
            outline_enable = False; outline_width = 4.0
            outline_r = 0.0; outline_g = 0.0; outline_b = 0.0; outline_a = 1.0
            shadow_enable = False; shadow_offset_x = 8.0; shadow_offset_y = 8.0; shadow_blur = 8.0
            shadow_r = 0.0; shadow_g = 0.0; shadow_b = 0.0; shadow_a = 1.0
        legacy = from_legacy_clip(FakeClip)
        assert len(legacy.overridden_fields()) == 34

    def test_style_to_legacy_fields_emits_only_overrides(self) -> None:
        s = CaptionStyle(font_family="X", font_size=10)
        d = style_to_legacy_fields(s)
        assert d == {"font_family": "X", "font_size": 10}

    def test_style_to_full_legacy_fields_no_none(self) -> None:
        track = CaptionStyle(font_family="Arial")
        cue = CaptionStyle(font_size=140)
        full = style_to_full_legacy_fields(track, cue)
        assert None not in full.values()
        assert set(full) == set(STYLE_FIELDS)


# ──────────────────────────────────────────────────────────────────────────
# Schema migration (SUB-P2-10)
# ──────────────────────────────────────────────────────────────────────────

class TestSchemaMigration:
    def test_v0_legacy_clip_migrated_to_current(self) -> None:
        legacy = {"clip_id": "c1", "text": "hi", "font_family": "Arial"}
        out = migrate_subtitle_clip_dict(dict(legacy))
        assert out[CAPTION_SCHEMA_VERSION_KEY] == CAPTION_SCHEMA_VERSION
        # Flat fields preserved (dual-write contract).
        assert out["font_family"] == "Arial"
        assert out["text"] == "hi"

    def test_already_current_is_noop(self) -> None:
        clip = {"clip_id": "c1", "text": "hi",
                CAPTION_SCHEMA_VERSION_KEY: CAPTION_SCHEMA_VERSION}
        out = migrate_subtitle_clip_dict(dict(clip))
        assert out == clip

    def test_future_version_raises(self) -> None:
        with pytest.raises(ValueError):
            migrate_subtitle_clip_dict(
                {CAPTION_SCHEMA_VERSION_KEY: CAPTION_SCHEMA_VERSION + 5}
            )

    def test_migration_is_idempotent(self) -> None:
        legacy = {"clip_id": "c1", "text": "hi"}
        once = migrate_subtitle_clip_dict(dict(legacy))
        twice = migrate_subtitle_clip_dict(dict(once))
        assert once == twice

    def test_track_migration_walks_all_clips(self) -> None:
        track = {
            "track_id": "t1", "track_type": "subtitle",
            "subtitle_clips": [
                {"clip_id": "c1", "text": "a"},
                {"clip_id": "c2", "text": "b",
                 CAPTION_SCHEMA_VERSION_KEY: 1},
            ],
        }
        out = migrate_track_caption_dict(dict(track))
        for c in out["subtitle_clips"]:
            assert c[CAPTION_SCHEMA_VERSION_KEY] == CAPTION_SCHEMA_VERSION

    def test_project_walker_handles_split_track_layout(self) -> None:
        # Real on-disk layout: timeline_sequences dict with per-type track lists.
        wrapper = {
            "timeline_sequences": {
                "seq1": {
                    "subtitle_tracks": [
                        {"track_id": "t1", "track_type": "subtitle",
                         "subtitle_clips": [{"clip_id": "c1", "text": "a"}]},
                    ],
                    "video_tracks": [
                        {"track_id": "v1", "track_type": "video"},  # untouched
                    ],
                },
            },
        }
        upgrade_captions_in_project(wrapper)
        sub = wrapper["timeline_sequences"]["seq1"]["subtitle_tracks"][0]
        assert sub["subtitle_clips"][0][CAPTION_SCHEMA_VERSION_KEY] == CAPTION_SCHEMA_VERSION
        # video track not given a synthetic subtitle_clips list
        vid = wrapper["timeline_sequences"]["seq1"]["video_tracks"][0]
        assert "subtitle_clips" not in vid

    def test_project_walker_handles_legacy_unified_tracks(self) -> None:
        wrapper = {
            "timelines": [  # legacy list form
                {"tracks": [
                    {"track_id": "t1", "track_type": "subtitle",
                     "subtitle_clips": [{"clip_id": "c1", "text": "a"}]},
                ]},
            ],
        }
        upgrade_captions_in_project(wrapper)
        assert (wrapper["timelines"][0]["tracks"][0]["subtitle_clips"][0]
                [CAPTION_SCHEMA_VERSION_KEY] == CAPTION_SCHEMA_VERSION)

    def test_preserve_unknown_keys_helper(self) -> None:
        known = frozenset({"a", "b"})
        target = {"a": 1}
        preserve_unknown_keys({"a": 9, "b": 2, "future": 42}, known, target)
        # known keys in target not overwritten; unknowns added.
        assert target == {"a": 1, "future": 42}


# ──────────────────────────────────────────────────────────────────────────
# FontResolver + preflight (SUB-P2-06/11)
# ──────────────────────────────────────────────────────────────────────────

class TestFontResolver:
    def setup_method(self) -> None:
        # Pin a known family set so tests are headless + deterministic.
        FontResolver.set_available_override(
            ["Arial", "Helvetica", "Noto Sans CJK SC", "Noto Sans Arabic"]
        )

    def teardown_method(self) -> None:
        FontResolver.set_available_override(None)

    def test_available_family_resolves_verbatim(self) -> None:
        r = FontResolver()
        res = r.resolve("Arial")
        assert res.available
        assert not res.is_substitute
        assert res.resolved == "Arial"

    def test_case_insensitive_match(self) -> None:
        res = FontResolver().resolve("arial")
        assert res.available
        assert res.resolved == "Arial"

    def test_missing_family_gets_substitute(self) -> None:
        res = FontResolver().resolve("Nonexistent XYZ")
        assert not res.available
        assert res.is_substitute
        assert res.resolved in ("Arial", "Helvetica")

    def test_script_aware_substitute_cjk(self) -> None:
        # A CJK-named missing font should prefer the CJK Noto family.
        res = FontResolver().resolve("MS Mincho")
        assert res.resolved == "Noto Sans CJK SC"

    def test_script_aware_substitute_arabic(self) -> None:
        res = FontResolver().resolve("Some Arabic Font")
        assert res.resolved == "Noto Sans Arabic"

    def test_weight_to_qt_mapping(self) -> None:
        assert weight_to_qt("normal") == 400
        assert weight_to_qt("bold") == 700
        assert weight_to_qt("black") == 900
        # Unknown weight falls back to normal, never raises.
        assert weight_to_qt("bogus") == 400

    def test_preflight_warns_on_missing_with_substitute(self) -> None:
        r = FontResolver()
        issues = preflight_fonts({"Missing": ("c1", "c2")}, resolver=r)
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert issues[0].suggested_substitute != ""
        assert issues[0].affected_clips == ("c1", "c2")

    def test_preflight_errors_when_no_substitute(self) -> None:
        FontResolver.set_available_override([])  # nothing available
        issues = preflight_fonts({"Nope": ("c1",)}, resolver=FontResolver())
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].suggested_substitute == ""

    def test_preflight_silent_on_available(self) -> None:
        r = FontResolver()
        issues = preflight_fonts({"Arial": ("c1",)}, resolver=r)
        assert issues == []

    def test_collect_families_groups_by_family(self) -> None:
        class C:
            def __init__(self, cid, fam):
                self.clip_id = cid; self.font_family = fam
        fams = collect_families([C("a", "Arial"), C("b", "Arial"), C("c", "Noto")])
        assert fams["Arial"] == ("a", "b")
        assert fams["Noto"] == ("c",)


# ──────────────────────────────────────────────────────────────────────────
# Style presets (SUB-P2-06)
# ──────────────────────────────────────────────────────────────────────────

class TestStylePresets:
    def test_builtins_loaded(self) -> None:
        r = StylePresetRegistry()
        assert len(r.all()) >= 5
        assert "builtin.broadcast" in r
        assert "builtin.karaoke" in r
        assert "builtin.default" in r

    def test_register_user_preset(self) -> None:
        r = StylePresetRegistry()
        p = CaptionStylePreset("proj.title", "Title",
                               CaptionStyle(font_family="Custom", font_size=200))
        r.register(p)
        assert "proj.title" in r
        assert r.get("proj.title").style.font_family == "Custom"

    def test_cannot_overwrite_builtin_id(self) -> None:
        r = StylePresetRegistry()
        hijack = CaptionStylePreset("builtin.broadcast", "Hacked",
                                    CaptionStyle(font_size=1))
        with pytest.raises(ValueError):
            r.register(hijack)

    def test_builtin_not_removable(self) -> None:
        r = StylePresetRegistry()
        assert r.remove("builtin.broadcast") is False
        assert "builtin.broadcast" in r

    def test_user_preset_removable(self) -> None:
        r = StylePresetRegistry()
        r.register(CaptionStylePreset("u.a", "A", CaptionStyle(font_size=10)))
        assert r.remove("u.a") is True
        assert "u.a" not in r

    def test_apply_preset_to_cue_preserves_non_preset_fields(self) -> None:
        r = StylePresetRegistry()
        existing = CaptionStyle(font_family="MyFont", font_weight="bold",
                                line_spacing=1.5)
        # broadcast sets font_family/font_weight but NOT line_spacing.
        merged = apply_preset_to_cue(r, "builtin.broadcast", merge_with=existing)
        assert merged.font_family == "Arial"    # preset wins
        assert merged.font_weight == "normal"   # preset wins
        assert merged.line_spacing == 1.5       # kept (preset left None)

    def test_propagate_skips_diverged_cues(self) -> None:
        old = CaptionStyle(font_family="Arial", font_size=120)
        new = CaptionStyle(font_family="Arial", font_size=140)
        targets = [
            ("c1", CaptionStyle(font_family="Arial", font_size=120)),    # matches
            ("c2", CaptionStyle(font_family="Arial", font_size=120,
                                fill_r=0.5)),                            # matches, extra OK
            ("c3", CaptionStyle(font_family="Other", font_size=120)),    # diverged
        ]
        result = propagate_preset_change(targets, old, new)
        assert set(result) == {"c1", "c2"}
        assert result["c1"].font_size == 140
        assert result["c2"].fill_r == 0.5   # manual edit preserved
        assert "c3" not in result           # diverged left alone

    def test_registry_round_trip_only_user_presets(self) -> None:
        r = StylePresetRegistry()
        r.register(CaptionStylePreset("u.a", "A", CaptionStyle(font_size=100),
                                      description="d"))
        ser = r.to_dict()
        # Builtins not serialized (re-loaded implicitly).
        assert all(not e.get("is_builtin") for e in ser)
        r2 = StylePresetRegistry.from_dict(ser)
        assert "u.a" in r2
        assert "builtin.broadcast" in r2  # builtins restored
        assert r2.get("u.a").style.font_size == 100
