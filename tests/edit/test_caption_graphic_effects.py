"""Animated subtitle-effect contracts: catalog, motion state and replacement."""

from __future__ import annotations

import numpy as np
from unittest.mock import MagicMock

from src.shared.i18n.i18n import I18N

from src.features.edit.core.captions.commands import (
    PromoteSubtitleToGraphicCommand,
    UpdateCaptionGraphicBindingCommand,
)
from src.features.edit.core.captions.graphics import (
    BUILTIN_CAPTION_GRAPHIC_TEMPLATES,
    CaptionGraphicTemplate,
    caption_graphic_animation_state,
    render_caption_graphic_rgba,
    template_parameters,
)
from src.features.edit.core.captions.model import CaptionCueMetadata
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence


def _timeline() -> TimelineSequence:
    track = EditTrack(
        track_id="sub-1",
        track_type="subtitle",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1",
            track_id="sub-1",
            timeline_start=0,
            timeline_duration=48,
            text="Animated caption",
            caption_metadata=CaptionCueMetadata(),
        ),),
    )
    return TimelineSequence(
        timeline_id="caption-effects",
        name="Caption effects",
        video_tracks=(EditTrack(track_id="v1"),),
        subtitle_tracks=(track,),
    )


def test_builtin_catalog_contains_real_motion_and_reveal_effects() -> None:
    assert {
        "fade", "typewriter", "scale_pop", "slide_up", "slide_down",
        "slide_left", "slide_right", "wipe_reveal",
    } <= set(BUILTIN_CAPTION_GRAPHIC_TEMPLATES)
    for template in BUILTIN_CAPTION_GRAPHIC_TEMPLATES.values():
        assert template.label
        assert template.description


def test_graphic_template_roundtrips_display_metadata() -> None:
    source = CaptionGraphicTemplate(
        "brand.caption", version="2", effect="slide_up",
        label="品牌字幕", description="品牌上滑动画",
    )
    restored = CaptionGraphicTemplate.from_dict(source.to_dict())
    assert restored.label == "品牌字幕"
    assert restored.description == "品牌上滑动画"


def test_slide_and_reveal_states_have_real_motion() -> None:
    entering = caption_graphic_animation_state(
        "slide_up", 0, 48,
        {"in_frames": 10, "out_frames": 8, "distance": 1.0},
        content_width=420, content_height=84,
    )
    resting = caption_graphic_animation_state(
        "slide_up", 20, 48,
        {"in_frames": 10, "out_frames": 8, "distance": 1.0},
        content_width=420, content_height=84,
    )
    reveal = caption_graphic_animation_state(
        "typewriter", 0, 48,
        {"in_frames": 12, "out_frames": 6},
        content_width=420, content_height=84,
    )
    assert entering.opacity < 1.0 and entering.translate_y > 0.0
    assert resting.opacity == 1.0 and resting.translate_y == 0.0
    assert 0.0 < reveal.reveal < 1.0


def test_scale_pop_uses_a_controlled_overshoot() -> None:
    state = caption_graphic_animation_state(
        "scale_pop", 3, 48,
        {"in_frames": 8, "out_frames": 8, "overshoot": 0.12},
    )
    assert state.opacity < 1.0
    assert state.scale > 1.0


def test_switching_effect_updates_existing_binding_without_repromotion() -> None:
    promoted, _ = PromoteSubtitleToGraphicCommand.create(
        "cue-1", template_id="fade", parameters={"in_frames": 6}
    ).apply(_timeline())
    original = promoted.get_caption_graphic_binding("cue-1")
    assert original is not None

    updated, _ = UpdateCaptionGraphicBindingCommand.create(
        "cue-1",
        {"in_frames": 10, "out_frames": 8, "distance": 1.4},
        template_id="slide_up",
        template_version="1",
    ).apply(promoted)
    replacement = updated.get_caption_graphic_binding("cue-1")
    assert replacement is not None
    assert replacement.binding_id == original.binding_id
    assert replacement.graphic_clip_id == original.graphic_clip_id
    assert replacement.template_id == "slide_up"
    assert replacement.parameters["distance"] == 1.4


def test_real_subtitle_raster_applies_slide_and_typewriter_masks(qapp) -> None:
    """Exercise the actual glyph raster + animated warp path, not just state."""
    timeline = _timeline()
    cue = timeline.get_subtitle_clip_by_id("cue-1")
    track = timeline.get_track_by_id("sub-1")
    assert cue is not None and track is not None

    slide = BUILTIN_CAPTION_GRAPHIC_TEMPLATES["slide_up"]
    start = render_caption_graphic_rgba(
        cue, track, slide, template_parameters(slide), 640, 360, 0,
    )
    resting = render_caption_graphic_rgba(
        cue, track, slide, template_parameters(slide), 640, 360, 20,
    )
    start_y = np.flatnonzero(start[..., 3].max(axis=1))[0]
    resting_y = np.flatnonzero(resting[..., 3].max(axis=1))[0]
    assert start_y > resting_y
    assert float(start[..., 3].sum()) < float(resting[..., 3].sum())

    typewriter = BUILTIN_CAPTION_GRAPHIC_TEMPLATES["typewriter"]
    reveal_start = render_caption_graphic_rgba(
        cue, track, typewriter, template_parameters(typewriter), 640, 360, 0,
    )
    reveal_done = render_caption_graphic_rgba(
        cue, track, typewriter, template_parameters(typewriter), 640, 360, 20,
    )
    assert np.count_nonzero(reveal_start[..., 3]) < np.count_nonzero(reveal_done[..., 3])


def test_opaque_deliverable_keeps_caption_layer_alpha_until_composite(qapp) -> None:
    """A graphic caption must not turn its transparent canvas into black BGR."""
    from src.features.edit.core.timeline_export import TimelineExporter

    timeline, _ = PromoteSubtitleToGraphicCommand.create(
        "cue-1", template_id="slide_up"
    ).apply(_timeline())
    binding = timeline.get_caption_graphic_binding("cue-1")
    assert binding is not None
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._sequence = timeline
    exporter._project = MagicMock()
    exporter._project.get_asset_by_id.return_value = None
    exporter._export_has_alpha = False
    exporter._get_sequence_resolution = lambda: (640, 360)
    layer = MagicMock()
    layer.clip_id = binding.graphic_clip_id
    layer.asset_id = f"caption-graphic:{binding.graphic_clip_id}"
    layer.source_frame = 0

    frame = exporter._get_layer_frame(layer)
    assert frame is not None and frame.shape == (360, 640, 4)
    assert frame[..., 3].min() == 0
    assert frame[..., 3].max() > 0


def test_effects_panel_lists_and_emits_selected_caption_effect(qapp) -> None:
    from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

    panel = EditEffectsPanel()
    selected: list[tuple[str, str, dict]] = []
    panel.caption_effect_apply_requested.connect(
        lambda template_id, clip_id, params: selected.append(
            (template_id, clip_id, params)
        )
    )
    panel.set_selected_subtitle_clip("cue-1")

    root = panel.titles_tree.topLevelItem(0)
    assert root.text(0) == I18N.tr("edit.effects.caption_effects", "Caption Effects")
    effect = next(
        root.child(index)
        for index in range(root.childCount())
        if root.child(index).text(0) == "向上滑入"
    )
    panel.caption_effect_in_frames.setValue(14)
    panel._on_title_item_double_clicked(effect, 0)
    assert selected == [("slide_up", "cue-1", {
        "in_frames": 14, "out_frames": 8, "distance": 1.0,
    })]
    panel.deleteLater()
