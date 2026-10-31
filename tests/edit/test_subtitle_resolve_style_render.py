"""SUB-P2-01/06 contract: renderer consumes the layered caption style.

Phase 2A-3 makes ``subtitle_renderer`` accept an optional resolved ``style``
dict (the output of ``captions.model.resolve_style``) so that
track-default ⊕ cue-override inheritance actually affects pixels — previously
the renderer only read ``SubtitleClip``'s flat fields and ``style_override``
was dead code at render time.

These tests pin the three guarantees of that wiring:

1. A cue with a ``style_override`` renders with the override (e.g. a different
   fill colour), NOT the legacy flat-field default.
2. A legacy cue (no override, empty track default) renders identically with
   ``style=None`` and with the resolved style — byte-for-byte on the params the
   renderer feeds to the text node.
3. The cache key distinguishes two cues sharing the same clip identity but
   different track defaults (otherwise they would collide and "串字").
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.captions.model import (  # noqa: E402
    CaptionStyle,
    EMPTY_STYLE,
    resolve_style,
)
from src.features.edit.core.subtitle_renderer import (  # noqa: E402
    _clip_to_text_params,
    _content_signature,
    get_subtitle_texture_cached,
    invalidate_cache,
    measure_subtitle_render_bbox,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    SubtitleClip,
    effective_subtitle_font_size,
)

pytestmark = pytest.mark.edit_core


def _clip(**overrides) -> SubtitleClip:
    fields = dict(
        clip_id="sub-1", track_id="s1",
        timeline_start=0, timeline_duration=72, text="cue",
    )
    fields.update(overrides)
    return SubtitleClip(**fields)


# ──────────────────────────────────────────────────────────────────────────
# 1. resolve_style output is what the renderer consumes
# ──────────────────────────────────────────────────────────────────────────

def test_resolve_style_override_wins_over_track_default() -> None:
    """cue override takes precedence; track default fills the gaps."""
    track_default = CaptionStyle(font_family="TrackFont", fill_r=0.1)
    override = CaptionStyle(fill_r=0.9)  # override fill only
    style = resolve_style(track_default, override)
    assert style["font_family"] == "TrackFont"  # inherited
    assert style["fill_r"] == 0.9               # overridden


def test_resolve_style_legacy_fallback_matches_clip_defaults() -> None:
    """Empty override + empty track default → SubtitleClip dataclass defaults."""
    style = resolve_style(EMPTY_STYLE, None)
    clip = _clip()
    for f in ("font_family", "font_size", "fill_r", "fill_a", "halign", "valign"):
        assert style[f] == getattr(clip, f), f"field {f} drifted from legacy default"


def test_resolve_style_base_uses_clip_flat_fields() -> None:
    """A legacy cue (style_override=None) keeps its flat-field font via ``base``.

    This is the no-regression fix for legacy-edited cues: their style lives in
    the flat fields (e.g. font_family="Comic Sans"), and without ``base`` they
    would render with the hard-coded "Arial" default. Passing the clip's flat
    fields as ``base`` restores them as the layer between track-default and the
    legacy defaults.
    """
    from src.features.edit.core.captions.model import clip_style_base
    clip = _clip(font_family="Comic Sans", fill_r=0.42)
    style = resolve_style(EMPTY_STYLE, None, base=clip_style_base(clip))
    assert style["font_family"] == "Comic Sans"
    assert style["fill_r"] == pytest.approx(0.42)


def test_resolve_style_override_beats_base() -> None:
    """cue_override still wins over the clip's flat-field base."""
    from src.features.edit.core.captions.model import clip_style_base
    clip = _clip(font_family="Comic Sans")
    style = resolve_style(
        EMPTY_STYLE,
        CaptionStyle(font_family="OverrideFont"),
        base=clip_style_base(clip),
    )
    assert style["font_family"] == "OverrideFont"


def test_resolve_style_track_default_beats_base() -> None:
    """track_default wins over the clip's flat-field base (for non-overridden)."""
    from src.features.edit.core.captions.model import clip_style_base
    clip = _clip(font_family="Comic Sans")
    style = resolve_style(
        CaptionStyle(font_family="TrackFont"),
        None,
        base=clip_style_base(clip),
    )
    assert style["font_family"] == "TrackFont"


# ──────────────────────────────────────────────────────────────────────────
# 2. _clip_to_text_params honours the resolved style
# ──────────────────────────────────────────────────────────────────────────

def test_text_params_uses_style_when_provided() -> None:
    """With a style dict, the 34 style fields come from it, not the clip."""
    # Clip keeps legacy flat fields; only the style dict carries the override.
    clip = _clip(font_family="Arial", fill_r=1.0, fill_g=1.0, fill_b=1.0)
    style = resolve_style(
        CaptionStyle(font_family="BrandFont", fill_r=0.2, fill_g=0.3, fill_b=0.4),
        None,
    )
    params = _clip_to_text_params(clip, 1920, 1080, style=style)
    assert params["font_family"] == "BrandFont"
    assert params["fill_r"] == pytest.approx(0.2)
    assert params["fill_g"] == pytest.approx(0.3)
    assert params["fill_b"] == pytest.approx(0.4)


def test_text_params_style_none_reads_flat_fields() -> None:
    """style=None (legacy path) reads the clip's flat fields, unchanged."""
    clip = _clip(font_family="Arial", fill_r=0.5)
    params = _clip_to_text_params(clip, 1920, 1080, style=None)
    assert params["font_family"] == "Arial"
    assert params["fill_r"] == pytest.approx(0.5)


def test_text_params_keeps_text_and_transform_from_clip() -> None:
    """text + transform fields always come from the clip, never the style."""
    clip = _clip(text="hello", translate_x=10.0, scale_x=1.5, rotation=12.0)
    from src.features.edit.core.captions.model import clip_style_base
    style = resolve_style(CaptionStyle(font_family="Other"), None,
                          base=clip_style_base(clip))
    params = _clip_to_text_params(clip, 1920, 1080, style=style)
    assert params["text"] == "hello"
    assert params["translate_x"] == 10.0
    assert params["scale_x"] == 1.5
    assert params["rotation"] == 12.0


def test_text_params_legacy_equal_resolved_for_legacy_clip() -> None:
    """A legacy clip renders the SAME params whether or not style is resolved.

    This is the no-regression guarantee: old projects are byte-equivalent.
    """
    clip = _clip()
    legacy = _clip_to_text_params(clip, 1920, 1080, style=None)
    resolved = _clip_to_text_params(
        clip, 1920, 1080, style=resolve_style(EMPTY_STYLE, None)
    )
    # Every passthrough field agrees.
    for k in legacy:
        if k in ("width", "height", "format_name", "filter",
                 "size_preset", "fill_hex", "outline_hex", "shadow_hex"):
            continue
        assert legacy[k] == resolved[k], f"field {k} drifted"


def test_compact_subtitle_texture_contains_glyphs(qapp) -> None:
    """Regression: compact textures must not be fully transparent.

    The renderer measures a cue in full-canvas coordinates and then rasterizes
    it into a bbox-sized texture. Before the fix, the text path still used the
    full-canvas width/height, so bottom-aligned text was painted outside the
    compact buffer and the ProgramViewer showed no subtitle at all.
    """
    invalidate_cache()
    clip = _clip(text="这是一个字幕测试 Hello", valign="bottom")
    texture = get_subtitle_texture_cached(clip, 1920, 1080)

    assert texture.width > 0
    assert texture.height > 0
    assert float(texture.rgba[:, :, 3].max()) > 0.0


def test_compact_texture_preserves_long_cjk_line(qapp) -> None:
    """Regression: compacting must not re-wrap a line and keep only its tail."""
    invalidate_cache()
    clip = _clip(text="好像就是八卦新闻一样就被无限制的复制")
    texture = get_subtitle_texture_cached(clip, 1920, 1080)
    columns = (texture.rgba[:, :, 3] > 0).any(axis=0)
    visible_width = int(columns.nonzero()[0][-1] - columns.nonzero()[0][0] + 1)

    # The glyphs should occupy nearly the measured one-line texture width,
    # rather than a narrow tail caused by accidental re-wrapping.
    assert visible_width >= int(texture.width * 0.8)


def test_measured_render_bbox_tracks_cjk_and_transform(qapp) -> None:
    """The delivery bbox is based on rendered pixels plus the final transform."""
    invalidate_cache()
    clip = _clip(text="你好，Hello 世界", font_size=64, outline_enable=True)
    base = measure_subtitle_render_bbox(clip, 1920, 1080)
    moved = measure_subtitle_render_bbox(
        _clip(
            text="你好，Hello 世界",
            font_size=64,
            outline_enable=True,
            translate_x=120.0,
            translate_y=-35.0,
        ),
        1920,
        1080,
    )

    assert base[2] > 0.0 and base[3] > 0.0
    assert moved[0] == pytest.approx(base[0] + 120.0)
    assert moved[1] == pytest.approx(base[1] - 35.0)
    assert moved[2] == pytest.approx(base[2])
    assert moved[3] == pytest.approx(base[3])


def test_measured_render_bbox_accounts_for_rotation_and_vertical_canvas(qapp) -> None:
    invalidate_cache()
    clip = _clip(
        text="竖屏标题 / Vertical title",
        font_size=72,
        rotation=18.0,
        scale_x=1.2,
        scale_y=0.8,
    )
    bbox = measure_subtitle_render_bbox(clip, 1080, 1920)
    texture = get_subtitle_texture_cached(clip, 1080, 1920)

    assert all(math.isfinite(value) for value in bbox)
    assert bbox[2] > texture.width * 0.8
    assert bbox[3] > texture.height * 0.6


def test_auto_font_size_matches_renderer_contract() -> None:
    assert effective_subtitle_font_size(0, 1080) == 59
    assert effective_subtitle_font_size(0, 2160) == 118
    assert effective_subtitle_font_size(120, 1080) == 120


# ──────────────────────────────────────────────────────────────────────────
# 3. Cache key distinguishes different track defaults
# ──────────────────────────────────────────────────────────────────────────

def test_content_signature_distinguishes_track_defaults() -> None:
    """Same clip identity + different resolved style → different cache key.

    Without this, two cues on tracks with different default fonts would share
    one cached texture (串字). The signature must incorporate the resolved
    style values.
    """
    clip = _clip(clip_id="same", text="hi")
    style_a = resolve_style(CaptionStyle(font_family="FontA"), None)
    style_b = resolve_style(CaptionStyle(font_family="FontB"), None)
    sig_none = _content_signature(clip, None)
    sig_a = _content_signature(clip, style_a)
    sig_b = _content_signature(clip, style_b)
    assert sig_a != sig_b, "different track defaults must not collide"
    assert sig_a != sig_none, "resolved style must differ from flat-field key"


def test_content_signature_text_changes_key() -> None:
    """clip.text (identity-ish) still drives the key regardless of style."""
    clip_a = _clip(clip_id="c1", text="aaa")
    clip_b = _clip(clip_id="c1", text="bbb")
    style = resolve_style(EMPTY_STYLE, None)
    assert _content_signature(clip_a, style) != _content_signature(clip_b, style)
