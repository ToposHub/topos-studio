from __future__ import annotations

from dataclasses import replace

from src.features.teaching_animation.core.professional import SceneColorPipeline
from src.features.teaching_animation.core.professional import write_scene_color_pipeline_into
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.export_service import ExportOptions, TeachingAnimationExportService
from src.features.teaching_animation.render.composite_effect_bridge import dtype_for_color_pipeline


def test_hdr_pipeline_selects_float_texture_precision() -> None:
    assert dtype_for_color_pipeline(SceneColorPipeline("srgb", 8)) == "f1"
    assert dtype_for_color_pipeline(SceneColorPipeline("rec2020", 10)) == "f2"
    assert dtype_for_color_pipeline(SceneColorPipeline("acescg", 16)) == "f2"
    assert dtype_for_color_pipeline(SceneColorPipeline("acescg", 32)) == "f4"


def test_hdr_export_context_and_manifest_report_explicit_8bit_delivery() -> None:
    pipeline = SceneColorPipeline("rec2020", 16, tone_map="aces", alpha_mode="premult")
    base = TeachingSceneDocument.create_default(
        "HDR export", scene_id="hdr-export", width=32, height=18, duration_frames=2,
    )
    scene = replace(base, metadata=write_scene_color_pipeline_into(base.metadata, pipeline))
    service = TeachingAnimationExportService()
    try:
        context = service._context(scene, ExportOptions(), None)
        provenance = service._provenance(scene, context)
    finally:
        service.close()
    assert context.working_color_space == "rec2020"
    assert context.bit_depth == 16
    assert context.transparent is True
    assert provenance["color_pipeline"] == pipeline.to_dict()
    assert provenance["hdr_delivery"] == {
        "requested": True,
        "status": "explicit_8bit_fallback",
        "pixel_format": "rgba8",
        "fallback": "svg_8bit_tonemap",
        "reason": "current sequence and FFmpeg writers consume RGBA8",
    }
