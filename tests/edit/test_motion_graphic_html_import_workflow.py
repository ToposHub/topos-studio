from dataclasses import replace

from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.teaching_animation.core.html_layer_commands import (
    build_html_layer_from_source,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.host_adapters import (
    MotionGraphicClipCommand,
    get_scene_reference_manifest,
)


def test_imported_html_becomes_pool_asset_and_timeline_clip():
    source = """
    <html><head><style>
      [data-topos-page] { background: #101827; color: white; }
    </style></head><body>
      <section data-topos-page="1"><h1>AI agent</h1></section>
      <script>document.body.dataset.ready = '1';</script>
    </body></html>
    """
    base = TeachingSceneDocument.create_default(
        name="Imported HTML", width=1920, height=1080,
        fps_num=25, duration_frames=375,
    )
    layer = build_html_layer_from_source(
        source,
        layer_id="html_import_test",
        viewport_width=base.width,
        viewport_height=base.height,
        fps_num=base.fps_num,
        duration_frames=base.duration_frames,
    )
    scene = replace(base, elements=(), animations=(), html_layer=layer)
    assert "AI agent" in layer.sanitized_html
    assert "<script" in layer.sanitized_html
    assert layer.metadata["render_profile"] == "motion_graphic_freeform"

    project, asset_id = EditProject.create("Import test").create_motion_graphic_asset(scene)
    assert project.assets[asset_id].media_type == "motion_graphic"

    sequence = TimelineSequence(name="Import test", fps=25, width=1920, height=1080)
    command = MotionGraphicClipCommand(
        scene, 0, scene.duration_frames, mode="overwrite", asset_id=asset_id,
    )
    updated = command.apply(sequence)
    clip = updated.get_clip_by_id(command._clip_id)
    assert clip is not None
    assert clip.asset_id == asset_id
    assert get_scene_reference_manifest(updated, clip.clip_id) is not None
