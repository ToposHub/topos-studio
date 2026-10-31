"""Regression tests for the Motion Graphic editor's manual HTML controls."""

from dataclasses import replace

from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_layer_commands import ReplaceHtmlSourceCommand
from src.features.teaching_animation.core.html_page_deck import PageDeckTiming, timing_metadata
from src.features.teaching_animation.core.model import TeachingSceneDocument


def _scene(html: str, *, metadata: dict | None = None) -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default(
        "manual", width=1920, height=1080, fps_num=24, duration_frames=240,
    )
    layer = build_html_layer(
        layer_id="html_manual",
        sanitized_html=html,
        authored_source=html,
        viewport_width=1920,
        viewport_height=1080,
        fps_num=24,
        fps_den=1,
        duration_frames=240,
        metadata=metadata or {},
    )
    return replace(base, elements=(), html_layer=layer)


def test_replace_html_source_is_sanitized_marked_and_undoable() -> None:
    scene = _scene('<div class="slide"><h1 data-topos-node-id="title">Old</h1></div>')
    command = ReplaceHtmlSourceCommand(
        '<style>:root{--accent:#f80}</style>'
        '<section class="slide"><h1 data-topos-node-id="title">New</h1></section>'
    )

    updated = command.apply(scene)

    assert 'data-topos-page="1"' in updated.html_layer.sanitized_html
    assert "New" in updated.html_layer.sanitized_html
    assert "Old" not in updated.html_layer.sanitized_html
    assert updated.html_layer.editable_slots[0].node_id == "title"
    assert updated.html_layer.editable_tokens == ("accent",)
    assert updated.html_layer.revision == scene.html_layer.revision + 1
    assert command.undo(updated).html_layer == scene.html_layer


def test_replace_html_source_keeps_manual_edit_inside_existing_safety_profile() -> None:
    scene = _scene('<section data-topos-page="1">Old</section>')
    updated = ReplaceHtmlSourceCommand(
        '<section class="slide" onclick="alert(1)">Edited</section>'
        '<script>window.should_not_ship = true</script>'
    ).apply(scene)

    html = updated.html_layer.sanitized_html
    assert "Edited" in html
    assert "onclick" not in html
    assert "<script" not in html


def test_page_count_change_preserves_clip_length_and_resets_invalid_custom_vector() -> None:
    timing = PageDeckTiming(
        2, "custom", 5.0, (4.0, 6.0), transition="crossfade", transition_seconds=0.5,
    )
    scene = _scene(
        '<section data-topos-page="1">A</section>'
        '<section data-topos-page="2">B</section>',
        metadata=timing_metadata(timing),
    )
    updated = ReplaceHtmlSourceCommand(
        '<section class="slide">A</section>'
        '<section class="slide">B</section>'
        '<section class="slide">C</section>'
    ).apply(scene)

    new_timing = updated.html_layer.metadata["page_timing"]
    assert new_timing["mode"] == "default"
    assert new_timing["page_count"] == 3
    assert new_timing["transition"] == "crossfade"
    assert new_timing["transition_seconds"] == 0.5
    assert updated.html_layer.duration_frames == scene.html_layer.duration_frames
