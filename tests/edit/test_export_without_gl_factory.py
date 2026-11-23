"""An export still delivers when no GL context can be made (plan 7.5).

The plan asks for the expected fallback of a sample that has to stay
compatible, run with a GL factory that refuses ("用测试注入拒绝 GL 工厂").
``TOPOS_GRAPHICS_FACTORY=deny`` makes every moderngl context factory raise
before creating anything, which is what a machine without a usable GL looks
like.  The same LUT timeline with audio is then exported twice:

* through the Metal H.264 fast path (the macOS default): it must not need
  GL at all, and the graphics ledger must show no GL context;
* through the regular path with its GPU composite forced on: the GL export
  renderer must give up on the refused context, say so, and the export must
  still deliver every frame through the CPU composite.

The files are checked with PyAV: the frame count and an audio stream.  A
real VideoToolbox and a Metal device are needed; elsewhere this is skipped.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef, probe_media_metadata
from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportRangeType,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
from src.shared.color.lut_resource_registry import get_lut_resource_session
from src.shared.gpu import graphics_ledger as ledger

CLIP = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"    # 1920x1080, 25 fps, AAC
# Exported timeline frames 25..33 (8 frames).  Kept small on purpose: the
# regular path without GL composites on the CPU, which costs about two
# seconds per 1080p frame on the machine this was written on.
FRAMES = (25, 33)


def _project(directory: Path) -> tuple:
    """One LUT-graded clip with an audio track, built like the A/B benchmark project."""
    cube = directory / "grade.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "".join(
        f"{0.1 + 0.8 * r} {0.2 + 0.7 * g} {0.05 + 0.9 * b}\n"
        for b in range(2) for g in range(2) for r in range(2)), encoding="utf-8")
    imported = get_lut_resource_session().import_files([str(cube)])[0]
    assert imported.entry, imported
    service = ColorGraphService()
    inserted = service.apply_intent(ColorGraphData.create_serial(), "insert_corrector", {})
    assert inserted.ok, inserted
    node = next(n for n in inserted.graph.nodes if n.node_type == "corrector")
    graded = service.apply_intent(inserted.graph, "set_node_lut", {
        "node_id": node.node_id, "lut_ref": imported.entry.uri, "strength": 1.0})
    assert graded.ok, graded

    metadata = probe_media_metadata(str(CLIP))
    assert metadata, CLIP
    project = EditProject.create("Export without GL").with_media_asset_added(MediaAsset(
        asset_id="clip", path=str(CLIP), media_type="video", display_name=CLIP.name,
        metadata=metadata))
    project, stack_id = project.create_color_stack(asset_id="clip", name="LUT")
    project = project.update_color_stack(stack_id, color_graph=graded.graph)
    # The timeline runs past the exported range: frames 0..FRAMES[1].
    picture = ClipInstance(clip_id="v1", asset_id="clip", track_id="v1", timeline_start=0,
                           timeline_duration=FRAMES[1], source_start=0, source_duration=FRAMES[1]
                           ).with_color_stack(stack_id)
    sound = ClipInstance(clip_id="a1", asset_id="clip", track_id="a1", timeline_start=0,
                         timeline_duration=FRAMES[1], source_start=0, source_duration=FRAMES[1])
    sequence = TimelineSequence(
        timeline_id="lut", name="LUT + audio", fps=Rational(25, 1), width=1920, height=1080,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(picture,)),),
        audio_tracks=(EditTrack(track_id="a1", name="A1", track_type="audio", index=0, clips=(sound,)),))
    project = project.with_timeline_ref_added(
        TimelineSequenceRef(timeline_id="lut", name=sequence.name, fps=25, width=1920, height=1080,
                            created_at=time.time()), set_active=True)
    return project, sequence


def _export(project, sequence, path: Path) -> None:
    """Run one export the way the Deliver page does: on its worker thread."""
    from src.features.deliver.ui.deliver_page import ExportWorker

    config = TimelineExportConfig(
        timeline_id=sequence.timeline_id, output_path=str(path), codec=ExportCodec.H264,
        range_type=ExportRangeType.CUSTOM, start_frame=FRAMES[0], end_frame=FRAMES[1],
        include_audio=True, codec_profile="high", quality=50_000_000, quality_mode="vbr")
    exporter = create_timeline_exporter(project, sequence, progress_callback=lambda *args: None)
    worker = ExportWorker(exporter, config)
    worker.start()
    assert worker.wait(180_000), "the export worker did not finish"
    assert path.is_file(), getattr(exporter, "_last_error", None) or "no file was written"


def _delivered(path: Path) -> dict:
    import av

    with av.open(str(path)) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0] if container.streams.audio else None
        return {"frames": sum(1 for packet in container.demux(video=0) if packet.size),
                "size": (video.codec_context.width, video.codec_context.height),
                "audio": None if audio is None else (audio.codec_context.name,
                                                     audio.codec_context.sample_rate,
                                                     audio.codec_context.channels)}


@pytest.fixture()
def no_gl(monkeypatch: pytest.MonkeyPatch):
    """Every GL context factory refuses; the Metal context is not affected."""
    monkeypatch.setenv(ledger.FACTORY_ENV, "deny")
    monkeypatch.delenv(ledger.LEDGER_ENV, raising=False)
    monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
    ledger.graphics_ledger().reset()
    ledger.install_factory_hooks("export-without-gl")
    yield ledger.graphics_ledger()
    ledger.uninstall_factory_hooks()
    ledger.graphics_ledger().reset()


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="needs VideoToolbox and Metal")
def test_the_metal_fast_path_needs_no_gl_context(qapp, tmp_path, no_gl, monkeypatch) -> None:
    from src.shared.gpu.metal.context import MetalComputeContext

    if not MetalComputeContext.is_supported():
        pytest.skip("Metal is unavailable")
    monkeypatch.delenv("TOPOS_EXPORT_NATIVE_VT", raising=False)
    # Under pytest the exporter composites on the CPU unless asked otherwise;
    # the fast path is one of its GPU routes.
    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    project, sequence = _project(tmp_path)
    path = tmp_path / "native.mp4"
    _export(project, sequence, path)

    delivered = _delivered(path)
    assert delivered["frames"] == FRAMES[1] - FRAMES[0]
    assert delivered["size"] == (1920, 1080)
    assert delivered["audio"] is not None, "a delivery that asked for audio has an audio stream"
    # Not one GL context, not one GL thread: the frame never left Metal and
    # VideoToolbox.
    snapshot = no_gl.snapshot()
    assert snapshot["counts"]["opengl"] == 0 and snapshot["opengl_threads"] == []
    assert snapshot["counts"]["metal"] >= 1
    assert any("timeline_export._try_export_metal_h264_lut" in call.reason for call in no_gl.calls())


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="needs VideoToolbox")
def test_the_regular_path_falls_back_to_the_cpu_composite(qapp, tmp_path, no_gl, monkeypatch, caplog) -> None:
    monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "0")
    # Ask for the GPU composite explicitly: under pytest the regular path
    # renders on the CPU by default, which would not reach the GL renderer.
    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    project, sequence = _project(tmp_path)
    path = tmp_path / "regular.mp4"
    caplog.set_level(logging.WARNING, logger="src.features.edit.core.timeline_gpu_export_renderer")
    _export(project, sequence, path)

    # The GL export renderer was asked, refused, and said so.
    refused = [record for record in caplog.records
               if "headless GL unavailable" in record.getMessage()
               and "graphics factory refused" in record.getMessage()]
    assert refused, [record.getMessage() for record in caplog.records][:8]
    assert any("via moderngl.create_standalone_context" in record.getMessage() for record in refused)
    # Nothing was created behind the refusal's back.
    assert no_gl.snapshot()["counts"]["opengl"] == 0
    # And the delivery is complete all the same.
    delivered = _delivered(path)
    assert delivered["frames"] == FRAMES[1] - FRAMES[0]
    assert delivered["size"] == (1920, 1080)
    assert delivered["audio"] is not None


def test_the_refusal_is_visible_to_the_caller(no_gl) -> None:
    """What the exporter saw above: a named error, before anything is made."""
    import moderngl

    with pytest.raises(ledger.GraphicsFactoryRefused) as refused:
        moderngl.create_standalone_context(require=330)
    assert refused.value.api == ledger.OPENGL
    assert str(refused.value).startswith("graphics factory refused: opengl context")
    assert no_gl.calls() == []
