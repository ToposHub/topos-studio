from __future__ import annotations

from dataclasses import replace

from src.features.teaching_animation.core.html_page_deck import (
    PageDeckTiming,
    detect_page_count,
    ensure_page_markers,
    page_deck_editor_html,
    page_deck_runtime_html,
    page_index_at_frame,
    page_start_frame,
    page_timing_for,
    parse_model_page_timing,
    safe_background_value,
)
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_layer_commands import SetHtmlPageTimingCommand
from src.features.teaching_animation.core.model import TeachingSceneDocument


def test_conventional_pages_get_stable_markers() -> None:
    raw = '<section class="page page-1">A</section><section class="page page-2">B</section>'
    marked = ensure_page_markers(raw)
    assert detect_page_count(marked) == 2
    assert marked.count("data-topos-page=") == 2
    assert ensure_page_markers(marked) == marked


def test_inner_slide_classes_are_not_mistaken_for_pages() -> None:
    """A browser PPT's slide-header/title/body classes are not page roots."""
    raw = (
        '<div class="slides-container">'
        '<div class="slide" id="slide-1">'
        '<div class="slide-header"><div class="slide-title">HTML</div></div>'
        '<div class="slide-body"><div class="slide-card">CSS</div></div>'
        '</div>'
        '<div class="slide" id="slide-2">JS</div>'
        '</div>'
    )
    marked = ensure_page_markers(raw)
    assert detect_page_count(marked) == 2
    assert ensure_page_markers(marked) == marked


def test_css_page_selectors_are_not_counted_as_page_elements() -> None:
    """Page-specific CSS must not inflate the deck page count."""
    raw = (
        "<style>[data-topos-page]{opacity:0}"
        "[data-topos-page=\"1\"]{animation-delay:1s}</style>"
        '<div class="slide slide-1">A</div>'
        '<div class="slide slide-2">B</div>'
    )
    marked = ensure_page_markers(raw)
    assert detect_page_count(marked) == 2
    assert ensure_page_markers(marked) == marked


def test_custom_timing_maps_frames_to_pages() -> None:
    timing = PageDeckTiming(3, "custom", 5.0, (2.0, 3.0, 1.0))
    assert page_index_at_frame(0, 24, timing) == 0
    assert page_index_at_frame(47, 24, timing) == 0
    assert page_index_at_frame(48, 24, timing) == 1
    assert page_index_at_frame(119, 24, timing) == 1
    assert page_index_at_frame(120, 24, timing) == 2


def test_equal_page_timing_uses_total_clip_duration() -> None:
    """A 15-second five-page deck switches pages every three seconds."""
    timing = PageDeckTiming(5, "default", 15.0 / 5.0)
    assert timing.durations == (3.0, 3.0, 3.0, 3.0, 3.0)
    assert [page_start_frame(i, 25, timing) for i in range(5)] == [0, 75, 150, 225, 300]
    assert page_index_at_frame(74, 25, timing) == 0
    assert page_index_at_frame(75, 25, timing) == 1


def test_page_transition_metadata_round_trips_and_allows_zero_duration() -> None:
    timing = PageDeckTiming(
        2, "default", 3.0, transition="slide-left", transition_seconds=0.0,
    )
    restored = page_timing_for(
        {"page_timing": timing.to_dict()}, page_count=2,
    )
    assert restored.transition == "slide-left"
    assert restored.transition_seconds == 0.0


def test_export_runtime_binds_auto_timer_and_keyboard_navigation() -> None:
    raw = ensure_page_markers(
        '<html><body><div class="slide slide-1">A</div>'
        '<div class="slide slide-2">B</div></body></html>'
    )
    exported = page_deck_runtime_html(raw, PageDeckTiming(2, "custom", 5.0, (2.0, 4.0)))
    assert "topos-page-deck-controller" in exported
    assert "ArrowRight" in exported and "ArrowLeft" in exported
    assert "PageDown" in exported and "PageUp" in exported
    assert "[2.0,4.0]" in exported
    assert "inset:0!important" in exported
    assert "width:100%!important" in exported
    assert "height:100%!important" in exported
    assert 'const transitionName = "cut";' in exported
    assert "recoverPageViewport" in exported
    assert "data-topos-page-recovered" in exported
    assert 'data-topos-hidden="1"' in exported
    assert "display:none!important" in exported


def test_page_background_controller_keeps_global_fallback_and_local_override() -> None:
    raw = '<html><body><div data-topos-page="1">A</div><div data-topos-page="2">B</div></body></html>'
    timing = PageDeckTiming(2, "custom", 5.0, (2.0, 4.0))
    editor = page_deck_editor_html(
        raw,
        timing,
        global_background="#102030",
        page_backgrounds=("", "#ff0000"),
    )
    assert "background:#102030 !important" in editor
    assert '"pages":["","#ff0000"]' in editor
    assert "topos-page-background-controller" in editor
    assert safe_background_value("url(javascript:alert(1))") == ""


def test_editor_runtime_is_interactive_but_has_no_automatic_timer() -> None:
    raw = '<html><body><section class="page">A</section><section class="page">B</section></body></html>'
    editor_html = page_deck_editor_html(
        ensure_page_markers(raw), PageDeckTiming(2, "custom", 5.0, (2.0, 4.0))
    )
    assert "toposPageDeck" in editor_html
    assert "notifyPageChanged" in editor_html
    assert "ArrowRight" in editor_html and "ArrowLeft" in editor_html
    assert "setTimeout" not in editor_html
    assert "inset:0!important" in editor_html
    assert "data-topos-transitioning" in editor_html
    assert "recoverPageViewport" in editor_html
    assert 'data-topos-hidden="1"' in editor_html


def test_capture_page_boundaries_are_full_canvas_containing_blocks() -> None:
    """Later pages must not inherit a shrink-to-fit/zero page wrapper.

    The model is free to design everything inside a page.  The host owns only
    the page boundary, so every marked page gets the same full-canvas box in
    the deterministic capture document.
    """
    from src.features.teaching_animation.core.html_page_deck import page_deck_capture_html

    raw = '<html><head></head><body><div data-topos-page="1">A</div><div data-topos-page="2">B</div></body></html>'
    captured = page_deck_capture_html(
        raw, PageDeckTiming(2, transition="crossfade", transition_seconds=0.45),
    )
    assert "data-topos-page-deck" in captured
    assert 'data-topos-page-transition="crossfade"' in captured
    assert 'data-topos-page-transition-seconds="0.45"' in captured
    assert "--topos-transition-progress" in captured
    assert '[data-topos-transitioning="1"]{transition:none!important}' in captured
    assert "inset:0!important" in captured
    assert "width:100%!important" in captured
    assert "height:100%!important" in captured
    assert '[data-topos-page][data-topos-hidden="1"]{display:none!important;' in captured
    assert "animation:none!important" not in captured


def test_timing_command_updates_layer_and_is_undoable() -> None:
    base = TeachingSceneDocument.create_default("deck", width=320, height=180,
                                                fps_num=24, fps_den=1, duration_frames=240)
    layer = build_html_layer(
        layer_id="L", sanitized_html='<div data-topos-page="1">A</div>',
        authored_source="A", viewport_width=320, viewport_height=180,
        duration_frames=240,
    )
    scene = replace(base, html_layer=layer)
    timing = PageDeckTiming(2, "custom", 5.0, (2.0, 3.0))
    command = SetHtmlPageTimingCommand(timing)
    updated = command.apply(scene)
    assert updated.html_layer is not None
    assert updated.html_layer.metadata["page_timing"]["mode"] == "custom"
    assert updated.html_layer.duration_frames == 120
    assert command.undo(updated).html_layer == layer


def test_model_page_timing_declared_durations_scale_to_clip_length() -> None:
    """The model authors the rhythm; the timeline keeps the requested length."""
    html = (
        '<!DOCTYPE html><html><head>'
        '<meta name="topos-page-durations" content="2, 6, 2">'
        '</head><body>'
        '<div data-topos-page="1">A</div><div data-topos-page="2">B</div>'
        '<div data-topos-page="3">C</div>'
        '</body></html>'
    )
    timing = parse_model_page_timing(html, page_count=3, total_seconds=10.0)
    assert timing.mode == "custom"
    assert timing.page_count == 3
    # Declared 2:6:2 (sum 10) already matches the clip: used verbatim.
    assert timing.durations == (2.0, 6.0, 2.0)
    assert timing.total_seconds == 10.0


def test_model_page_timing_declared_durations_are_rescaled_proportionally() -> None:
    html = (
        '<head><meta name="topos-page-durations" content="1,3"></head>'
        '<body><div data-topos-page="1">A</div><div data-topos-page="2">B</div></body>'
    )
    timing = parse_model_page_timing(html, page_count=2, total_seconds=8.0)
    assert timing.mode == "custom"
    # Declared 1:3 (sum 4) rescaled to the 8s clip keeps the 1:3 rhythm.
    assert timing.durations == (2.0, 6.0)


def test_model_page_timing_falls_back_to_equal_split_when_inconsistent() -> None:
    base = '<body><div data-topos-page="1">A</div><div data-topos-page="2">B</div></body>'
    # No declaration at all.
    equal = parse_model_page_timing(base, page_count=2, total_seconds=6.0)
    assert equal.mode == "default"
    assert equal.durations == (3.0, 3.0)
    assert equal.transition == "cut"
    # Declaration count does not match the page count.
    mismatch = parse_model_page_timing(
        f'<head><meta name="topos-page-durations" content="1,2,3"></head>{base}',
        page_count=2, total_seconds=6.0,
    )
    assert mismatch.mode == "default"
    # Junk values never crash the build; they are discarded.
    junk = parse_model_page_timing(
        f'<head><meta name="topos-page-durations" content="fast,slow"></head>{base}',
        page_count=2, total_seconds=6.0,
    )
    assert junk.mode == "default"


def test_model_page_timing_transition_declaration_is_validated() -> None:
    html = (
        '<head>'
        '<meta name="topos-page-durations" content="2,2">'
        '<meta name="topos-page-transition" content="slide-left">'
        '<meta name="topos-page-transition-seconds" content="0.6">'
        '</head><body><div data-topos-page="1">A</div><div data-topos-page="2">B</div></body>'
    )
    timing = parse_model_page_timing(html, page_count=2, total_seconds=4.0)
    assert timing.transition == "slide-left"
    assert timing.transition_seconds == 0.6
    # Unknown transition names degrade to a hard cut instead of failing.
    bad = parse_model_page_timing(
        '<head><meta name="topos-page-transition" content="explode"></head>'
        '<body><div data-topos-page="1">A</div></body>',
        page_count=1, total_seconds=2.0,
    )
    assert bad.transition == "cut"
    assert bad.transition_seconds == 0.0
