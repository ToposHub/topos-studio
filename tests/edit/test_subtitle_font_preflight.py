"""SUB-P2-04/11 contract: Deliver font preflight blocks/ degrades on missing fonts.

Phase 2A-6 wires :func:`preflight_fonts_export` into the export pipeline so a
sequence burning in captions cannot silently produce output with wrong/empty
glyphs when a referenced font is missing:

* a missing font with NO substitute → ``error`` → blocks export (raises).
* a missing font WITH a substitute  → ``warning`` → degraded, export continues.
* all fonts available                → ok, no messages.
* ``burn_subtitles=False``           → font preflight is skipped entirely.

The preflight groups cues by the **resolved** font family (track-default ⊕
cue-override), matching what the renderer actually rasterizes — not the legacy
flat ``font_family`` field.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.captions.font_resolver import FontResolver  # noqa: E402
from src.features.edit.core.captions.model import (  # noqa: E402
    CaptionCueMetadata,
    CaptionStyle,
    CaptionTrackMetadata,
)
from src.features.edit.core.timeline_export import (  # noqa: E402
    ExportPreflightResult,
    preflight_fonts_export,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _seq(clips_by_track=None, track_default_style: CaptionStyle = None) -> TimelineSequence:
    """Build a sequence with one subtitle track (optionally a track default style)."""
    clips = clips_by_track or ()
    meta = CaptionTrackMetadata(default_style=track_default_style) \
        if track_default_style is not None else None
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=tuple(clips), caption_metadata=meta,
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )


def _cue(cid: str, font_family: str = "Arial", style: CaptionStyle = None,
         start: int = 0) -> SubtitleClip:
    return SubtitleClip(
        clip_id=cid, track_id="s1",
        timeline_start=start, timeline_duration=24, text="hi",
        font_family=font_family,
        style_override=style,
    )


@pytest.fixture
def pin_fonts():
    """Pin a known installed set so tests are headless + deterministic."""
    FontResolver.set_available_override(["Arial", "Helvetica", "Noto Sans CJK SC"])
    yield
    FontResolver.set_available_override(None)


# ──────────────────────────────────────────────────────────────────────────
# preflight_fonts_export
# ──────────────────────────────────────────────────────────────────────────

def test_all_fonts_available_is_ok(pin_fonts) -> None:
    seq = _seq([_cue("c1", "Arial", start=0),
                _cue("c2", "Helvetica", start=24)])
    result = preflight_fonts_export(seq)
    assert isinstance(result, ExportPreflightResult)
    assert result.ok
    assert result.blocking == ()
    assert result.degraded == ()


def test_missing_font_with_substitute_is_degraded(pin_fonts) -> None:
    # "Nonexistent Latin" → substitute Arial/Helvetica (warning), not blocking.
    seq = _seq([_cue("c1", "Nonexistent Latin Font")])
    result = preflight_fonts_export(seq)
    assert result.ok  # degraded, not blocking
    assert result.blocking == ()
    assert len(result.degraded) == 1
    assert "Nonexistent Latin Font" in result.degraded[0]


def test_missing_font_no_substitute_is_blocking(pin_fonts) -> None:
    # Pin an empty installed set so even the default fallback chain can't match.
    FontResolver.set_available_override([])
    seq = _seq([_cue("c1", "BrandFont")])
    result = preflight_fonts_export(seq)
    assert not result.ok
    assert len(result.blocking) == 1
    assert "BrandFont" in result.blocking[0]


def test_uses_resolved_font_family_not_flat_field(pin_fonts) -> None:
    """A cue whose style_override changes the font is checked against the
    resolved font, not the flat font_family field.

    Flat field says "Arial" (available); override says "MissingFont" (not).
    Preflight must flag the override font, else it would let a render-time
    missing font through.
    """
    FontResolver.set_available_override([])  # nothing available
    seq = _seq([_cue("c1", "Arial",
                     style=CaptionStyle(font_family="MissingFont"))])
    result = preflight_fonts_export(seq)
    assert not result.ok
    assert "MissingFont" in result.blocking[0]


def test_track_default_style_is_resolved(pin_fonts) -> None:
    """A cue inheriting a missing font from the track default is flagged."""
    FontResolver.set_available_override([])
    seq = _seq([_cue("c1", "Arial")],
               track_default_style=CaptionStyle(font_family="TrackBrandFont"))
    result = preflight_fonts_export(seq)
    assert not result.ok
    assert "TrackBrandFont" in result.blocking[0]


def test_none_sequence_is_ok() -> None:
    assert preflight_fonts_export(None).ok


def test_skips_disabled_and_hidden_tracks(pin_fonts) -> None:
    FontResolver.set_available_override([])
    # A disabled/hidden track with a missing font must NOT block (it won't burn).
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(_cue("c1", "Missing"),),
        is_enabled=False,
    )
    seq = TimelineSequence(timeline_id="seq", name="Seq", fps=24,
                           subtitle_tracks=(track,))
    assert preflight_fonts_export(seq).ok


# ──────────────────────────────────────────────────────────────────────────
# export_timeline integration (burn_subtitles gating)
# ──────────────────────────────────────────────────────────────────────────

def test_export_blocks_on_missing_font_when_burning(pin_fonts, monkeypatch) -> None:
    """export_timeline raises ValueError on a blocking font when burn_subtitles."""
    from src.features.edit.core.timeline_export import TimelineExporter

    FontResolver.set_available_override([])
    seq = _seq([_cue("c1", "MissingBrand")])

    # T-P1-71 桩保真度：__new__ 直构的 exporter 走 dispatch 主干，逐字段
    # 手写桩会漏属性（output_path/codec/...）——直接用真实 config。
    from src.features.edit.core.timeline_export import (
        ExportCodec, TimelineExportConfig,
    )

    cfg = TimelineExportConfig(
        codec=ExportCodec.H264,
        timeline_id="t1",
        burn_subtitles=True,
        include_audio=False,
        output_path="/tmp/font_preflight_stub.mp4",
        loudness_target_lufs=None,
    )
    assert cfg.validate() == []

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._sequence = seq
    with pytest.raises(ValueError, match="Font preflight failed"):
        exporter.export_timeline(cfg)


def test_export_skips_font_preflight_when_not_burning(pin_fonts) -> None:
    """burn_subtitles=False → no font preflight, missing font does NOT raise."""
    from src.features.edit.core.timeline_export import TimelineExporter

    FontResolver.set_available_override([])
    seq = _seq([_cue("c1", "MissingBrand")])

    from src.features.edit.core.timeline_export import (
        ExportCodec, TimelineExportConfig,
    )

    cfg = TimelineExportConfig(
        codec=ExportCodec.H264,
        timeline_id="t1",
        burn_subtitles=False,
        include_audio=False,
        output_path="/tmp/font_preflight_stub.mp4",
        loudness_target_lufs=None,
    )
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._sequence = seq
    # Should NOT raise on font preflight (it will fail later for unrelated
    # reasons — no GL/render path in headless — so we only assert the font
    # preflight itself isn't the blocker by checking it doesn't raise the
    # font-specific ValueError).
    try:
        exporter.export_timeline(cfg)
    except ValueError as e:
        assert "Font preflight" not in str(e)
    except Exception:
        pass  # other failures (no render backend) are expected and fine here
