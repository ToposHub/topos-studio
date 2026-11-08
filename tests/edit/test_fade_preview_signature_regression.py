"""Envelope-only edits must invalidate both program preview caches."""
from dataclasses import replace

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit.ui.edit_page import EditPage


def test_curve_change_invalidates_page_and_renderer_program_signature(qapp) -> None:
    clip = ClipInstance('c', 'asset', 'v', 0, 100, 0, 100, fade_in_duration=20, fade_curve='linear')
    before = TimelineSequence('fade-signature', 'Fade signature', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(clip,)),))
    after = replace(before, video_tracks=(replace(before.video_tracks[0],
        clips=(replace(clip, fade_curve='smooth'),)),))
    # Signatures are pure; avoid constructing render threads or GL contexts.
    page = EditPage.__new__(EditPage)
    renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
    assert page._build_timeline_preview_signature(before) != page._build_timeline_preview_signature(after)
    assert renderer._build_sequence_render_signature(before) != renderer._build_sequence_render_signature(after)


def test_speed_direction_and_freeze_only_change_invalidate_program_signature(qapp) -> None:
    from src.features.edit.core.clip_params import SpeedParams
    clip = ClipInstance('c', 'asset', 'v', 0, 100, 0, 100, speed=SpeedParams())
    sequence = TimelineSequence('clock-signature', 'Clock signature', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(clip,)),))
    page = EditPage.__new__(EditPage)
    renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
    for changed in (replace(clip, speed=clip.speed.with_reverse(True)), replace(clip, freeze_source_frame=30)):
        after = replace(sequence, video_tracks=(replace(sequence.video_tracks[0], clips=(changed,)),))
        assert page._build_timeline_preview_signature(sequence) != page._build_timeline_preview_signature(after)
        assert renderer._build_sequence_render_signature(sequence) != renderer._build_sequence_render_signature(after)
