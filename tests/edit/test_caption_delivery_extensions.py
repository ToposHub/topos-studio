from src.features.edit.ai_editing.analysis_models import TranscriptSegment, WordSegment
from src.features.edit.ai_editing.subtitle_placement import build_subtitle_placements
from src.features.edit.core.captions.export import render_subtitle_track
from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionRole,
    CaptionTrackMetadata,
    CaptionWordAnimation,
    CaptionWordTiming,
)
from src.features.edit.core.captions.mux import build_caption_mux_command
from src.features.edit.core.captions.translation import build_translated_subtitle_track
from src.features.edit.core.captions.translation import AiAssistantTranslationProvider
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.ai_editing.models import EditOperation, EditPlan, OperationKind, PlanTarget
from src.features.edit.ai_editing.plan_compiler import compile_plan
from src.features.edit.ai_editing.subtitle_ai_planner import SubtitleEditPlanner
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.shared.media.metadata import Rational


def _track() -> EditTrack:
    return EditTrack(
        track_id="sub-1",
        name="Dialogue",
        track_type="subtitle",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1",
            track_id="sub-1",
            timeline_start=0,
            timeline_duration=48,
            text="hello world",
            caption_metadata=CaptionCueMetadata(
                word_timings=(
                    CaptionWordTiming("hello", 0, 18),
                    CaptionWordTiming("world", 18, 48),
                ),
                word_animation=CaptionWordAnimation.KARAOKE,
            ),
        ),),
        caption_metadata=CaptionTrackMetadata(language="en"),
    )


def test_word_alignment_survives_placement() -> None:
    from src.features.edit.core.timeline_v2 import ClipInstance

    source = ClipInstance(
        clip_id="audio-1", asset_id="asset-1", track_id="a1",
        timeline_start=100, timeline_duration=80,
        source_start=10, source_duration=80,
    )
    placements = build_subtitle_placements((TranscriptSegment(
        0, 30, "hello world", words=(
            WordSegment(0, 12, "hello"), WordSegment(12, 30, "world"),
        ),
    ),), source, max_chars=100)
    assert len(placements) == 1
    assert [(w.text, w.start_frame, w.end_frame) for w in placements[0].word_timings] == [
        ("hello", 100, 112), ("world", 112, 130)
    ]


def test_ass_export_contains_karaoke_tags() -> None:
    output = render_subtitle_track(_track(), "ass", 24)
    assert r"{\k75}hello" in output
    assert r"{\k125}world" in output


def test_translation_creates_secondary_track_and_clears_source_alignment() -> None:
    translated = build_translated_subtitle_track(
        _track(), "zh-Hans", lambda texts, language: ["你好 世界"],
    )
    assert translated.track_id != "sub-1"
    assert translated.caption_metadata.language == "zh-Hans"
    assert translated.caption_metadata.role == CaptionRole.SECONDARY
    assert translated.subtitle_clips[0].text == "你好 世界"
    assert translated.subtitle_clips[0].caption_metadata.word_timings == ()


def test_caption_mux_command_is_argv_only() -> None:
    command = build_caption_mux_command(
        "/tmp/my video.mov", "/tmp/captions.srt", "/tmp/out.mov",
        container="mov", language="zh-Hans",
    )
    assert command[0] == "ffmpeg"
    # W07：Windows 上路径经原生分隔符（/tmp → 	mp 于当前盘根）
    import os as _os
    assert _os.path.normpath("/tmp/my video.mov") in command
    assert "language=zh-Hans" in command
    assert "-c:v" in command and "copy" in command


def test_ai_assistant_translation_provider_requires_same_number_of_lines() -> None:
    class FakeClient:
        def complete(self, messages, **kwargs):
            assert kwargs["json_mode"] is True
            return '{"translations":["你好","世界"]}'

    provider = AiAssistantTranslationProvider(FakeClient(), model_id="test-model")
    assert provider(("hello", "world"), "zh-Hans") == ("你好", "世界")


def test_ai_assistant_translation_provider_rejects_malformed_output() -> None:
    class FakeClient:
        def complete(self, messages, **kwargs):
            return '{"translations":["only one"]}'

    provider = AiAssistantTranslationProvider(FakeClient())
    try:
        provider(("hello", "world"), "zh-Hans")
    except ValueError as exc:
        assert "期望 2 条" in str(exc)
    else:
        raise AssertionError("malformed translation response was accepted")


def test_ai_subtitle_plan_compiles_global_and_cue_operations() -> None:
    plan = EditPlan.create(
        PlanTarget("project-1", "timeline-1"), 0,
        operations=(
            EditOperation(
                "track-style", OperationKind.UPDATE_SUBTITLE_TRACK,
                params={"track_id": "sub-1", "style": {"halign": "center"}},
            ),
            EditOperation(
                "cue-update", OperationKind.UPDATE_SUBTITLE,
                params={"clip_id": "cue-1", "text": "updated"},
            ),
            EditOperation(
                "cue-delete", OperationKind.DELETE_SUBTITLE,
                params={"clip_id": "cue-2"},
            ),
        ),
    )
    compiled = compile_plan(plan)
    assert compiled.operation_count == 3


def test_ai_subtitle_planner_uses_full_snapshot_and_forces_preview_mode() -> None:
    sequence = TimelineSequence(
        timeline_id="timeline-1", name="Main", fps=Rational(24, 1),
        subtitle_tracks=(_track(),),
    )
    project = EditProject(
        project_id="project-1", name="Demo", assets={},
        timelines={"timeline-1": TimelineSequenceRef(
            timeline_id="timeline-1", name="Main", fps=24.0,
        )},
        root_bin_id="root", active_timeline_id="timeline-1",
    )
    state = ProjectSequences(project=project, sequences={"timeline-1": sequence})

    class FakeClient:
        def complete(self, messages, **kwargs):
            assert kwargs["json_mode"] is True
            prompt = messages[-1]["content"]
            assert '"text": "hello world"' in prompt
            return '{"title":"居中","operations":[{"op_id":"style","kind":"update_subtitle_track","params":{"track_id":"sub-1","style":{"halign":"center"}}}]}'

    plan = SubtitleEditPlanner(FakeClient()).plan(
        "把当前字幕全部居中", state=state,
        project_id="project-1", timeline_id="timeline-1",
    )
    assert plan.mode.value == "preview"
    assert plan.base_revision == sequence.revision
    assert plan.operations[0].kind == OperationKind.UPDATE_SUBTITLE_TRACK
