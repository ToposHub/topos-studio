"""字幕图形片段 P0-P5 core contract tests."""

from __future__ import annotations

from src.features.edit.core.captions.commands import (
    ApplyCaptionTrackTemplateBatchCommand,
    ApplyCaptionTrackTemplateCommand,
    DemoteGraphicToSubtitleCommand,
    DetachCaptionGraphicCommand,
    PromoteSubtitleToGraphicCommand,
    UpdateCaptionGraphicBindingCommand,
)
from src.features.edit.core.commands import (
    CommandStack,
    MoveClipToExistingTrackCommand,
    MoveClipToTrackCommand,
    UpdateSubtitleClipCommand,
)
from src.features.edit.core.captions.graphics import (
    CaptionGraphicBinding,
    CaptionTrackGraphicStyle,
    build_composite_input,
)
from src.features.edit.core.captions.model import CaptionCueMetadata
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.ai_editing.models import EditOperation, EditPlan, OperationKind, PlanMode, PlanTarget
from src.features.edit.ai_editing.plan_compiler import compile_plan


def _timeline() -> TimelineSequence:
    subtitle_track = EditTrack(
        track_id="sub-track", track_type="subtitle", name="Captions",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1", track_id="sub-track", timeline_start=10,
            timeline_duration=20, text="Hello", caption_metadata=CaptionCueMetadata(),
        ),),
    )
    return TimelineSequence(
        timeline_id="timeline-1", name="Test", video_tracks=(EditTrack(track_id="v1"),),
        subtitle_tracks=(subtitle_track,),
    )


def test_promote_roundtrip_display_policy_and_single_undo() -> None:
    timeline = _timeline()
    stack = CommandStack()
    command = PromoteSubtitleToGraphicCommand.create("cue-1", template_id="typewriter")

    promoted = stack.execute(command, timeline)
    cue = promoted.get_subtitle_clip_by_id("cue-1")
    assert cue is not None and cue.display_mode == "graphic"
    assert len(promoted.caption_graphic_bindings) == 1
    graphics = [track for track in promoted.video_tracks if track.track_role == "graphics"]
    assert len(graphics) == 1 and len(graphics[0].clips) == 1
    assert stack.get_undo_label() == "Convert Subtitle to Graphic"

    restored = stack.undo(promoted)
    assert restored is not None
    assert restored.get_subtitle_clip_by_id("cue-1").display_mode == "native"
    assert not restored.caption_graphic_bindings
    assert not [track for track in restored.video_tracks if track.track_role == "graphics"]


def test_text_and_timing_sync_use_caption_as_source_of_truth() -> None:
    promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(_timeline())[0]
    updated = UpdateSubtitleClipCommand.create(
        "cue-1", {"text": "Updated", "timeline_start": 30, "timeline_duration": 12}
    ).apply(promoted)
    graphic = updated.get_clip_by_id(updated.caption_graphic_bindings[0].graphic_clip_id)
    assert graphic is not None
    assert (graphic.timeline_start, graphic.timeline_duration) == (30, 12)
    assert updated.get_subtitle_clip_by_id("cue-1").text == "Updated"


def test_disabled_sync_flags_keep_graphic_text_and_style_snapshot() -> None:
    promoted = PromoteSubtitleToGraphicCommand.create(
        "cue-1", sync_text=False, sync_style=False
    ).apply(_timeline())[0]
    binding = promoted.caption_graphic_bindings[0]
    assert binding.text_snapshot == "Hello"
    updated = UpdateSubtitleClipCommand.create(
        "cue-1", {"text": "Changed", "font_size": 90}
    ).apply(promoted)
    binding_after = updated.caption_graphic_bindings[0]
    assert binding_after.text_snapshot == "Hello"
    assert binding_after.style_snapshot["style_fields"]["font_size"] != 90


def test_batch_track_template_is_one_undoable_operation() -> None:
    timeline = _timeline()
    command = ApplyCaptionTrackTemplateBatchCommand.create(
        ["sub-track"], "typewriter", parameters={"in_frames": 4}
    )
    stack = CommandStack()
    applied = stack.execute(command, timeline)
    assert applied.caption_track_graphic_styles[0].template_id == "typewriter"
    assert applied.caption_track_graphic_styles[0].parameters["in_frames"] == 4
    restored = stack.undo(applied)
    assert restored is not None and not restored.caption_track_graphic_styles


def test_detach_keeps_clip_and_demote_removes_it() -> None:
    promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(_timeline())[0]
    detached = DetachCaptionGraphicCommand.create("cue-1").apply(promoted)[0]
    binding = detached.get_caption_graphic_binding("cue-1")
    assert binding.materialization == "detached"
    assert binding.source_snapshot["text"] == "Hello"
    assert binding.source_snapshot["style_fields"]["font_family"] == "Arial"
    assert detached.get_subtitle_clip_by_id("cue-1").display_mode == "native"
    assert detached.get_clip_by_id(detached.caption_graphic_bindings[0].graphic_clip_id) is not None

    demoted = DemoteGraphicToSubtitleCommand.create("cue-1").apply(promoted)[0]
    assert not demoted.caption_graphic_bindings
    assert demoted.get_subtitle_clip_by_id("cue-1").display_mode == "native"
    assert not [track for track in demoted.video_tracks if track.track_role == "graphics"]


def test_caption_graphics_block_roundtrips_unknown_fields_and_track_template() -> None:
    timeline = _timeline()
    style = CaptionTrackGraphicStyle(track_id="sub-track", template_id="fade")
    timeline = ApplyCaptionTrackTemplateCommand.create("sub-track", "fade").apply(timeline)
    payload = timeline.to_dict()
    payload["caption_graphics"]["future_key"] = {"preserve": True}
    restored = TimelineSequence.from_dict(payload)
    assert restored.caption_track_graphic_styles[0].template_id == "fade"
    assert restored.to_dict()["caption_graphics"]["future_key"] == {"preserve": True}


def test_custom_published_template_is_resolved_by_graphics_helpers() -> None:
    from src.features.edit.core.captions.graphics import (
        CaptionGraphicTemplate,
        renderable_graphic_for_cue,
    )

    timeline = _timeline().with_caption_graphic_template(
        CaptionGraphicTemplate("brand", version="3", effect="fade")
    )
    promoted = PromoteSubtitleToGraphicCommand.create(
        "cue-1", template_id="brand", template_version="3"
    ).apply(timeline)[0]
    resolved = renderable_graphic_for_cue(
        promoted, promoted.get_subtitle_clip_by_id("cue-1")
    )
    assert resolved is not None
    assert resolved[1].template_id == "brand"
    assert resolved[1].version == "3"


def test_caption_graphics_export_preflight_reports_missing_template() -> None:
    from src.features.edit.core.timeline_export import preflight_caption_graphics_export

    promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(_timeline())[0]
    binding = promoted.caption_graphic_bindings[0]._replace(
        template_id="missing", template_version="9"
    )
    broken = promoted.with_caption_graphic_binding_updated(binding)
    report = preflight_caption_graphics_export(broken)
    assert not report.ok
    assert "missing@9" in " ".join(report.blocking_messages)


def test_caption_graphics_preflight_blocks_unknown_composite_nodes() -> None:
    from src.features.edit.core.timeline_export import preflight_caption_graphics_export

    promoted = PromoteSubtitleToGraphicCommand.create(
        "cue-1", composite_id="comp-unknown"
    ).apply(_timeline())[0]

    class Project:
        def get_composition_by_id(self, composite_id):
            return {
                "composite_id": composite_id,
                # 必须是 NODE_REGISTRY 之外的类型（particle 已是注册节点）
                "nodes": [{"node_type": "definitely_not_a_node_type"}],
            }

    report = preflight_caption_graphics_export(promoted, Project())
    assert not report.ok
    assert ("未知 Composite 节点 definitely_not_a_node_type"
            in " ".join(report.blocking_messages))


def test_project_migration_keeps_damaged_binding_as_diagnostic() -> None:
    payload = _timeline().to_dict()
    payload["caption_graphics"]["caption_graphic_bindings"] = [{
        "binding_id": "orphan-binding",
        "caption_track_id": "sub-track",
        "caption_cue_id": "deleted-cue",
        "graphic_clip_id": "missing-graphic",
    }]
    restored = TimelineSequence.from_dict(payload)
    assert not restored.caption_graphic_bindings
    diagnostics = restored.to_dict()["caption_graphics"]["orphaned_caption_graphic_bindings"]
    assert diagnostics[0]["binding_id"] == "orphan-binding"


def test_composite_bound_graphic_prefers_composite_frame_during_export() -> None:
    import numpy as np
    from unittest.mock import MagicMock
    from src.features.edit.core.timeline_export import TimelineExporter

    timeline = PromoteSubtitleToGraphicCommand.create(
        "cue-1", composite_id="comp-1"
    ).apply(_timeline())[0]
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._sequence = timeline
    exporter._project = MagicMock()
    exporter._project.get_asset_by_id.return_value = None
    expected = np.zeros((2, 3, 3), dtype=np.uint8)
    exporter._render_comp_clip_frame = lambda layer, asset: expected
    layer = MagicMock()
    layer.clip_id = timeline.caption_graphic_bindings[0].graphic_clip_id
    layer.asset_id = "caption-graphic:missing"
    layer.source_frame = 0
    assert exporter._get_layer_frame(layer) is expected


def test_composite_input_reads_cue_not_graphic_copy() -> None:
    timeline = _timeline()
    input_data = build_composite_input(timeline, "cue-1")
    assert input_data.text == "Hello"
    assert input_data.start == 10 and input_data.end == 30
    assert input_data.caption_cue_id == "cue-1"


def test_caption_composite_input_adapter_exposes_controlled_node_payload() -> None:
    from src.features.composite.adapters.caption_input import CaptionCompositeInputAdapter

    payload = CaptionCompositeInputAdapter.as_node_inputs(_timeline(), "cue-1")
    assert payload["caption_cue_id"] == "cue-1"
    assert payload["text"] == "Hello"
    assert payload["start"] == 10 and payload["end"] == 30


def test_caption_text_node_reads_runtime_input_without_mutating_parameters() -> None:
    from src.features.composite.core.eval_context import CompositeEvalContext
    from src.features.composite.core.runtime_context import CompositeRuntimeContext
    from src.features.composite.nodes.text_node import TextNode

    node = TextNode()
    node.parameters["caption_input_enabled"] = True
    runtime = CompositeRuntimeContext(caption_input={"text": "From cue"})
    ctx = CompositeEvalContext(runtime_context=runtime)
    assert node._resolve_caption_input_text(ctx) == "From cue"
    assert node.parameters["text"] == "Text"
    node.parameters["caption_input_field"] = "word_timings"
    runtime = CompositeRuntimeContext(
        caption_input={"word_timings": [{"start": 1, "end": 2, "word": "From"}]}
    )
    ctx.runtime_context = runtime
    assert '"word": "From"' in node._resolve_caption_input_text(ctx)


def test_caption_graphic_cross_track_move_keeps_graphics_role() -> None:
    promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(_timeline())[0]
    binding = promoted.caption_graphic_bindings[0]
    source_clip = promoted.get_clip_by_id(binding.graphic_clip_id)
    assert source_clip is not None
    moved = MoveClipToTrackCommand.create_for_new_track(
        source_clip.track_id,
        binding.graphic_clip_id,
        "video", "above", 40,
    ).apply(promoted)
    graphic_track = moved.get_track_by_id(moved.get_clip_by_id(binding.graphic_clip_id).track_id)
    assert graphic_track is not None and graphic_track.track_role == "graphics"


def test_caption_graphic_cannot_move_to_standard_track() -> None:
    promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(_timeline())[0]
    binding = promoted.caption_graphic_bindings[0]
    standard = promoted.video_tracks[0]
    from pytest import raises
    with raises(ValueError, match="graphics track"):
        MoveClipToExistingTrackCommand.create(
            promoted.get_clip_by_id(binding.graphic_clip_id).track_id,
            standard.track_id,
            binding.graphic_clip_id,
            40,
        ).apply(promoted)


def test_caption_graphic_copy_creates_detached_binding_and_renders_from_snapshot() -> None:
    from dataclasses import replace
    from src.features.edit.core.captions.commands import (
        AddCaptionGraphicBindingCommand,
    )
    from src.features.edit.core.captions.graphics import clone_caption_graphic_binding
    from src.features.edit.core.commands import AddClipCommand, MacroCommand

    promoted = PromoteSubtitleToGraphicCommand.create("cue-1").apply(_timeline())[0]
    source_binding = promoted.caption_graphic_bindings[0]
    source_clip = promoted.get_clip_by_id(source_binding.graphic_clip_id)
    assert source_clip is not None
    copied_clip = replace(
        source_clip,
        clip_id="graphic-copy",
        timeline_start=40,
    )
    copied_binding = clone_caption_graphic_binding(
        source_binding,
        copied_clip.clip_id,
        promoted.get_subtitle_clip_by_id("cue-1"),
    )
    copied = MacroCommand.create(
        "Copy caption graphic",
        (
            AddClipCommand.create(source_clip.track_id, copied_clip),
            AddCaptionGraphicBindingCommand.create(copied_binding),
        ),
    ).apply(promoted)[0]
    assert len(copied.caption_graphic_bindings) == 2
    detached = copied.get_caption_graphic_binding_by_clip("graphic-copy")
    assert detached is not None and detached.materialization == "detached"
    assert detached.source_snapshot["text"] == "Hello"


def test_open_composite_action_resolves_graphic_clip_id() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from src.app.controllers.edit_controller import EditControllerMixin

    timeline = PromoteSubtitleToGraphicCommand.create(
        "cue-1", composite_id="comp-1"
    ).apply(_timeline())[0]
    graphic_clip_id = timeline.caption_graphic_bindings[0].graphic_clip_id
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(_current_timeline=timeline)
    controller._switch_to_composite_page = MagicMock()
    controller.on_caption_graphic_action(graphic_clip_id, "open_composite")
    controller._switch_to_composite_page.assert_called_once_with("comp-1")


def test_ai_graphic_operations_compile_to_domain_commands() -> None:
    plan = EditPlan.create(
        target=PlanTarget(project_id="p", timeline_id="timeline-1"),
        base_revision=0,
        mode=PlanMode.PREVIEW,
        operations=(EditOperation(
            op_id="op-1", kind=OperationKind.PROMOTE_SUBTITLE_TO_GRAPHIC,
            params={"clip_id": "cue-1", "template_id": "fade"},
        ),),
    )
    compiled = compile_plan(plan)
    assert compiled.operation_count == 1
    assert compiled.macro.sub_commands[0].label == "Convert Subtitle to Graphic"
