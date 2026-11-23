"""The Metal Edit composite matches the GL preview, frame for frame (plan 2.6).

Each scenario builds one ``EditRenderTask`` out of real decoded frames.  The
GL reference drives the very passes ``_process_task`` runs per layer (track
FBO with the transform, crop matte, grade, then ``_composite_tracks``); the
Metal side builds the composite plan and encodes it into the shared IOSurface
the GL viewer blits.  Both pictures are read back from GL and compared, with the tolerance of the
single-clip preview test: mean error under 0.8 levels of 8-bit code value,
99th percentile at most 2.  A few individual pixels may differ more where the
two APIs legitimately do: pixels whose centre lies exactly on a rotated clip
edge (covered by one rasteriser, not the other) and the five solid colours
whose 8 bit conversion is an exact tie (0.1, 0.3, 0.5, 0.7, 0.9), which the
GL clear and the Metal store round differently.

The native viewport route is covered end to end: the published slot texture
is read back from Metal and compared with the same GL reference.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from src.features.edit.core.timeline_render_plan import TransitionBlendInfo

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")

CLIP = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
DNXHR = CLIP.with_name("2K_DNxHR HQX 10bit.mov")
SOURCE_SIZE = (1920, 1080)
PROXY = (832, 468)

MOVE = (40.0, -25.0, 0.8, 0.8, 0.0, "center", 0.0, 0.0)
ROTATE = (0.0, 0.0, 1.1, 1.1, 30.0, "custom", 300.0, 200.0)
SHRINK = (-120.0, 60.0, 0.55, 0.55, 0.0, "center", 0.0, 0.0)
SOLID_RGB = (0.8, 0.1, 0.2)


def _video(source: Any, **info: Any) -> dict:
    return {"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": source, "track_info": dict(info)}


def _planar(source: Any, **info: Any) -> dict:
    return {"frame_type": "planar", "decoded_frame": source, "track_info": dict(info)}


def _solid(rgb: tuple, **info: Any) -> dict:
    track_info = {"is_solid": True, "solid_color": rgb}
    track_info.update(info)
    return {"frame_type": "solid", "track_info": track_info}


def _dip_job(dip: tuple) -> TransitionBlendInfo:
    """The task's ``(TransitionBlendInfo, None)`` of a ``dip_to_color``."""
    blend, color = dip
    transition = TransitionBlendInfo(
        transition=SimpleNamespace(transition_type="dip_to_color"), left_clip=None,
        right_clip=None, blend_factor=blend, left_source_frame=0, right_source_frame=0,
        blend_color=color)
    return (transition, None)


def _scenario(name: str, frame: Any, grade: dict) -> dict:
    """One scenario: layers, output size, transform coordinate size, dip.

    ``frame(index)`` must return a fresh decoded frame on every call: a VT
    frame belongs to the one consumer that imports it, as in production
    where every layer carries its own frame.
    """
    from src.features.edit.core.clip_params import ClipBlendMode

    def scenario(*layers, size=SOURCE_SIZE, coordinate=SOURCE_SIZE, dip=None) -> dict:
        return {"layers": list(layers), "size": size, "coordinate": coordinate, "dip": dip}

    def a(**info: Any) -> dict:
        return _video(frame(25), **info)

    def b(**info: Any) -> dict:
        return _video(frame(60), **info)

    feather = (0.1, 0.0, 0.0, 0.2, 40.0)
    made: dict[str, Any] = {
        "two-layers": lambda: scenario(a(), b(opacity=0.4)),
        "cross-dissolve": lambda: scenario(a(opacity=0.35), b(opacity=0.65)),
        "three-layers": lambda: scenario(a(opacity=0.8), b(opacity=0.5), a(opacity=0.25)),
        "transform-move-scale": lambda: scenario(a(transform=MOVE)),
        "transform-rotate-pivot": lambda: scenario(a(transform=ROTATE)),
        "transform-over-untouched": lambda: scenario(b(), a(transform=SHRINK)),
        "transform-proxy-size": lambda: scenario(
            a(transform=MOVE), size=PROXY, coordinate=SOURCE_SIZE),
        "crop-hard": lambda: scenario(b(), a(crop=(0.1, 0.05, 0.1, 0.05, 0.0))),
        "crop-feather": lambda: scenario(b(), a(crop=feather)),
        "crop-feather-only": lambda: scenario(a(crop=(0.0, 0.0, 0.0, 0.0, 30.0))),
        "lut-with-crop-and-opacity": lambda: scenario(
            b(preview_grade=grade), a(preview_grade=grade, crop=feather, opacity=0.7)),
        "lut-both-sides": lambda: scenario(
            a(preview_grade=grade, opacity=0.6), b(preview_grade=grade)),
        "solid-under-video": lambda: scenario(
            _solid(SOLID_RGB, transform=(100.0, 50.0, 0.6, 0.6, 15.0, "center", 0.0, 0.0)),
            a(transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0),
              crop=(0.05, 0.05, 0.05, 0.05, 0.0))),
        "solid-alone": lambda: scenario(_solid((0.2, 0.9, 0.3))),
        "dip-to-color": lambda: scenario(
            a(opacity=0.5), b(), dip=(0.3, (1.0, 0.3, 0.0))),
        "dip-alone": lambda: scenario(a(), dip=(0.5, (0.0, 0.0, 1.0))),
    }
    for mode in ClipBlendMode:
        if mode is not ClipBlendMode.NORMAL:
            made[f"blend-{mode.value}"] = (
                lambda mode=mode: scenario(a(), b(opacity=0.6, blend_mode=mode)))
    return made[name]()


def _read_upright(fbo: Any, size: tuple) -> np.ndarray:
    """RGB with row 0 at the top of the picture (``fbo.read`` is bottom-up)."""
    width, height = size
    return np.frombuffer(fbo.read(components=3), np.uint8).reshape(height, width, 3)[::-1]


def _gl_reference(renderer: Any, spec: dict) -> np.ndarray:
    """Drive the GL passes of ``_process_task`` for one frame; upright RGB."""
    from src.features.edit.core.edit_gpu_preview_renderer import _blend_code_of
    from src.shared.media.gpu_frame import safe_release_gpu_frame

    width, height = spec["size"]
    renderer._active_output_size = (width, height)
    renderer._active_transform_coordinate_size = tuple(spec["coordinate"])
    tracks = []
    imported = []
    try:
        for index, layer in enumerate(spec["layers"]):
            info = layer["track_info"]
            if layer["frame_type"] == "solid":
                _fbo, track_tex = renderer._paint_solid_to_track_fbo(
                    index, width, height, info["solid_color"], None)
                track_tex = renderer._apply_clip_transform(
                    track_tex, index, width, height, info.get("transform"))
            elif layer["frame_type"] == "planar":
                rendered = renderer._upload_and_render_planar(layer["decoded_frame"], index, track_info=info)
                assert rendered is not None
                track_tex = renderer._apply_clip_transform(
                    rendered[0], index, width, height, info.get("transform"))
            else:
                frame = renderer._import_vt_pixel_buffer_frame(layer["vt_pixel_buffer"])
                assert frame is not None
                imported.append(frame)
                planes = renderer._wrap_external_gpu_planes(frame.planes, frame)
                track_fbo, track_tex = renderer._ensure_track_fbo(index, width, height)
                renderer._render_yuv_to_track_fbo(
                    planes, frame, track_fbo, track_tex, info.get("transform"))
            track_tex = renderer._apply_crop(track_tex, index, width, height, info.get("crop"))
            graded = renderer._apply_preview_grade(
                track_tex, index, width, height, info.get("preview_grade"))
            tracks.append((graded, info.get("opacity", 1.0), _blend_code_of(info)))
        if spec["dip"] is None:
            dips = ()
        else:
            # What _composite_tracks reads from the transition of a dip job.
            blend, color = spec["dip"]
            dips = ((SimpleNamespace(get_color_factor=lambda: blend * 2.0,
                                     blend_color=color), None),)
        assert renderer._composite_tracks(tracks, width, height, dip_jobs=dips) is not None
        assert renderer.ctx.error == "GL_NO_ERROR", "GL reference"
        return _read_upright(renderer._composite_fbo, (width, height)).astype(np.int16)
    finally:
        renderer.ctx.finish()
        for frame in imported:
            safe_release_gpu_frame(frame)


def _difference(metal_rgb: np.ndarray, gl_rgb: np.ndarray) -> dict[str, float]:
    error = np.abs(metal_rgb.reshape(-1, 3).astype(np.int16) - gl_rgb.reshape(-1, 3))
    per_pixel = error.max(axis=1)
    return {"mean": round(float(error.mean()), 4),
            "p99": float(np.quantile(error, 0.99)), "max": int(error.max()),
            # Pixels no two APIs can be made to agree on: see the module docstring.
            "outliers": int((per_pixel > 16).sum())}


def _metal_picture(
    metal: Any, frame: Any, spec: dict, parking: Any, context: Any,
) -> np.ndarray:
    """Render one composite plan through the shared IOSurface; upright RGB.

    Decodes the layers' frames itself and gives them back afterwards
    (``render_composite`` leaves them to the caller).
    """
    from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask
    from src.features.edit.core.metal_edit_compositor import (
        MetalCompositePlan,
        build_metal_composite_plan,
    )

    width, height = spec["size"]
    task = EditRenderTask(
        25, 1, spec["layers"], output_fbo_size=(width, height),
        coordinate_size=spec["coordinate"],
        transition_jobs=() if spec["dip"] is None else (_dip_job(spec["dip"]),))
    plan = build_metal_composite_plan(task)
    assert isinstance(plan, MetalCompositePlan), plan
    rendered = metal.render_composite(plan, (width, height), spec["coordinate"])
    assert rendered is not None
    slot, lease = rendered
    texture = context.texture((width, height), 4)
    fbo = context.framebuffer([texture])
    try:
        metal.blit(slot, fbo)
        assert context.error == "GL_NO_ERROR", "Metal blit"
        return _read_upright(fbo, (width, height))
    finally:
        context.finish()
        lease.release()
        parking.use()
        fbo.release()
        texture.release()


def _release_renderer(renderer: Any) -> None:
    for suffix in ("_track", "_graded_track", "_transform_track", "_crop_track"):
        for kind in ("texes", "fbos"):
            for resource in getattr(renderer, suffix + "_" + kind, None) or []:
                resource.release()
            setattr(renderer, suffix + "_" + kind, [])
    for name in ("_composite_scratch_a", "_composite_scratch_b", "_black_tex"):
        resource = getattr(renderer, name, None)
        if resource is not None:
            resource.release()
            setattr(renderer, name, None)
    for name in ("_composite_scratch_a_fbo", "_composite_scratch_b_fbo"):
        resource = getattr(renderer, name, None)
        if resource is not None:
            resource.release()
            setattr(renderer, name, None)
    graph_renderer = getattr(renderer, "_color_graph_texture_renderer", None)
    if graph_renderer is not None:
        graph_renderer.release()
        renderer._color_graph_texture_renderer = None


def _new_renderer(context: Any) -> Any:
    import moderngl
    from PyQt6.QtCore import QThread

    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer

    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    QThread.__init__(renderer)
    renderer.ctx = context
    renderer._vt_import_bridge = None
    for suffix in ("_track", "_graded_track", "_transform_track", "_crop_track"):
        setattr(renderer, suffix + "_texes", [])
        setattr(renderer, suffix + "_fbos", [])
    renderer._black_tex = None
    renderer._composite_scratch_a = None
    renderer._composite_scratch_b = None
    assert renderer._init_shaders()
    renderer._init_quad()
    context.disable(moderngl.BLEND)
    return renderer


def _lut_graph(cube: Path) -> dict:
    """A serial one-Corrector graph whose only operation is a LUT."""
    from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService

    reference = "topos-res://lut_" + "0" * 32
    service = ColorGraphService(lut_reference_validator=lambda ref: ref == reference)
    inserted = service.apply_intent(ColorGraphData.create_serial(), "insert_corrector", {})
    assert inserted.ok
    node = next(n for n in inserted.graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(inserted.graph, "set_node_lut", {
        "node_id": node.node_id, "lut_ref": reference, "strength": 0.73})
    assert applied.ok
    return {"color_graph": applied.graph, "source_frame": 25}


def _primary_graph(*operations: dict) -> dict:
    from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService

    service = ColorGraphService()
    graph = ColorGraphData.create_serial()
    anchor = graph.source_node_id
    for params in operations:
        existing = {node.node_id for node in graph.nodes}
        inserted = service.apply_intent(graph, "insert_corrector", {"active_node_id": anchor})
        assert inserted.ok
        anchor = next(node.node_id for node in inserted.graph.nodes if node.node_id not in existing)
        graph = replace(inserted.graph, nodes=tuple(
            replace(node, params=params) if node.node_id == anchor else node
            for node in inserted.graph.nodes))
    return {"color_graph": graph, "source_frame": 25}


def _run_on_render_thread(path: Path, cube: Path) -> dict[str, Any]:
    import src.features.color.core.color_graph_texture as graph_texture
    import src.shared.color.lut_loader as lut_loader

    from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask
    from src.features.edit.core.metal_edit_compositor import (
        MetalCompositePlan,
        build_metal_composite_plan,
    )
    from src.features.edit.core.metal_timeline_preview import MetalTimelinePreview
    from src.features.edit.core.native_metal_preview import NativePreviewProducer
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

    if not MetalComputeContext.is_supported():
        pytest.skip("Metal is unavailable")
    import moderngl

    try:
        context = moderngl.create_standalone_context(require=410)
    except (RuntimeError, OSError) as error:
        pytest.skip(f"A CGL render context is unavailable: {error}")
    grade = _lut_graph(cube)
    # Both texture renderers resolve the reference to the same file.
    with patch.object(lut_loader, "resolve_lut_path", lambda reference: cube), \
            patch.object(graph_texture, "resolve_lut_path", lambda reference: cube):
        decoder = MacOSVtZeroCopySource()
        renderer = _new_renderer(context)
        parking = context.framebuffer([context.texture((1, 1), 4)])
        parking.use()
        metal = None
        frames: list = []                 # (tag, frame) this test still has to release
        releases: list = []               # tags the frame transaction gave back itself
        result: dict[str, Any] = {"iosurface": {}, "native": {}}
        try:
            if not decoder.open(str(path), require_zero_copy=False, require_hardware=True):
                pytest.skip("VideoToolbox hardware decode is unavailable")
            frame_a = decoder.read_frame_pixel_buffer(25)
            frame_b = decoder.read_frame_pixel_buffer(60)
            if frame_a is None or frame_b is None or frame_a.pixel_format != "nv12":
                pytest.skip("VideoToolbox NV12 decode is unavailable")
            if (frame_a.width, frame_a.height) != SOURCE_SIZE:
                pytest.skip(f"unexpected clip size {frame_a.width}x{frame_a.height}")
            frame_a.release()
            frame_b.release()
            metal = MetalTimelinePreview(context)

            def frame(index: int) -> Any:
                decoded = decoder.read_frame_pixel_buffer(index)
                assert decoded is not None and decoded.pixel_format == "nv12", index
                return decoded

            names = ["two-layers", "cross-dissolve", "three-layers", "transform-move-scale",
                     "transform-rotate-pivot", "transform-over-untouched",
                     "transform-proxy-size", "crop-hard", "crop-feather", "crop-feather-only",
                     "lut-with-crop-and-opacity", "lut-both-sides", "solid-under-video",
                     "solid-alone", "dip-to-color", "dip-alone"]
            from src.features.edit.core.clip_params import ClipBlendMode

            names += [f"blend-{mode.value}" for mode in ClipBlendMode
                      if mode is not ClipBlendMode.NORMAL]
            for name in names:
                width, height = _scenario(name, frame, grade)["size"]
                final_texture = context.texture((width, height), 4)
                final_fbo = context.framebuffer([final_texture])
                renderer._composite_fbo, renderer._composite_tex = final_fbo, final_texture
                # The GL reference consumes (releases) the frames it imports.
                gl_rgb = _gl_reference(renderer, _scenario(name, frame, grade))
                # Metal decodes its own frames; render_composite leaves them to us.
                spec = _scenario(name, frame, grade)
                metal_frames = [layer["vt_pixel_buffer"] for layer in spec["layers"]
                                if layer.get("vt_pixel_buffer") is not None]
                try:
                    metal_rgb = _metal_picture(metal, frame, spec, parking, context)
                finally:
                    for decoded in metal_frames:
                        decoded.release()
                result["iosurface"][name] = _difference(metal_rgb, gl_rgb)
                parking.use()
                final_fbo.release()
                final_texture.release()

            # ---- the HSL blends rank channels: how much is the input? --------
            # Hue and saturation rebuild the colour from the channel order,
            # so a pixel whose channels are within a level of each other
            # swings when the converted inputs differ by that level.  Blend
            # the GL single layers and the Metal single layers with the same
            # NumPy formula: the pixels where those two differ are what the
            # inputs alone explain.
            from src.features.edit.core.blend_modes import blend_rgb
            from src.features.edit.core.clip_params import ClipBlendMode

            one = {"size": SOURCE_SIZE, "coordinate": SOURCE_SIZE, "dip": None}
            final_texture = context.texture(SOURCE_SIZE, 4)
            final_fbo = context.framebuffer([final_texture])
            renderer._composite_fbo, renderer._composite_tex = final_fbo, final_texture
            for name, mode in (("blend-hue", ClipBlendMode.HUE),
                               ("blend-saturation", ClipBlendMode.SATURATION)):
                pictures = {}
                for index in (25, 60):
                    spec = {**one, "layers": [_video(frame(index))]}
                    pictures["gl", index] = _gl_reference(renderer, spec)
                    spec = {**one, "layers": [_video(frame(index))]}
                    pictures["mt", index] = _metal_picture(metal, frame, spec, parking, context)
                    spec["layers"][0]["vt_pixel_buffer"].release()

                def blended(side: str) -> np.ndarray:
                    base = pictures[side, 25].astype(np.float32) / 255.0
                    overlay = pictures[side, 60].astype(np.float32) / 255.0
                    mixed = blend_rgb(base, overlay, mode)
                    return np.rint((base * 0.4 + mixed * 0.6) * 255.0)

                spread = np.abs(blended("gl") - blended("mt")).max(axis=2)
                result["iosurface"][name]["input_outliers"] = int((spread > 16).sum())

            # ---- the native viewport route, end to end ----------------------
            renderer._ensure_native_viewport_state()
            renderer._metal_preview_mode = "all"
            renderer._note_zero_copy_import = lambda *args, **kwargs: None
            errors: list = []
            renderer._emit_task_render_error = lambda task, message: errors.append(message)
            published: list = []
            renderer.present_frame_ready.connect(published.append)
            renderer._native_producer = NativePreviewProducer(metal)
            frame_c = frame(60)
            frame_d = frame(25)
            frames += [("c", frame_c), ("d", frame_d)]
            # Count how often each VT frame is given back.
            for tag, owned in (("c", frame_c), ("d", frame_d)):
                real_release = owned.release
                owned.release = lambda tag=tag, done=real_release: (  # type: ignore[method-assign]
                    releases.append(tag), done())
            primary_grade = _primary_graph(
                {"grade": {"exposure": 49.0, "contrast": 13.0, "temp": 9.0}},
                {"grade": {"exposure": -46.0, "saturation": -16.0}, "opacity": 0.9},
            )
            layers = [_video(frame_c), _video(frame_d, transform=SHRINK, preview_grade=primary_grade)]
            final_texture = context.texture(SOURCE_SIZE, 4)
            final_fbo = context.framebuffer([final_texture])
            renderer._composite_fbo, renderer._composite_tex = final_fbo, final_texture
            # The GL reference gets frames of its own.
            gl_spec = _scenario("transform-over-untouched", frame, grade)
            gl_spec["layers"][1]["track_info"]["preview_grade"] = primary_grade
            gl_rgb = _gl_reference(renderer, gl_spec)
            task = EditRenderTask(25, 1, layers, output_fbo_size=SOURCE_SIZE,
                                  coordinate_size=SOURCE_SIZE)
            assert renderer._try_native_metal_preview(task, SOURCE_SIZE[0], SOURCE_SIZE[1]) is True
            assert errors == []
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not published:
                renderer._poll_native_viewport()
                time.sleep(0.002)
            (present,) = published
            metal.metal_context.finish()
            stored = metal.metal_context.read_texture(
                present.native.resource, SOURCE_SIZE[0] * 4, SOURCE_SIZE[1], np.uint8,
                region_width=SOURCE_SIZE[0]).reshape(*SOURCE_SIZE, 4)
            # A BGRA8 slot in image orientation (row 0 = top): blue first.
            result["native"] = _difference(stored[:, :, 2::-1], gl_rgb)
            # The frame transaction gave both VT frames back exactly once.
            result["native_releases"] = sorted(releases)
            assert renderer._native_producer.shutdown() is True
        finally:
            for tag, owned in frames:
                if tag not in releases:
                    owned.release()
            _release_renderer(renderer)
            if renderer._vt_import_bridge is not None:
                renderer._vt_import_bridge.destroy()
            if metal is not None:
                metal.release()
            parking.release()
            decoder.close()
            context.release()
    return result


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Metal and CGL interop")
def test_metal_composite_matches_the_gl_preview(tmp_path: Path) -> None:
    if not CLIP.is_file():
        pytest.skip("H.264 test clip is unavailable")
    cube = tmp_path / "preview.cube"
    cube.write_text("LUT_3D_SIZE 4\n" + "".join(
        f"{0.10 + 0.80 * r / 3:.6f} {0.20 + 0.70 * g / 3:.6f} {0.05 + 0.90 * b / 3:.6f}\n"
        for b in range(4) for g in range(4) for r in range(4)
    ), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="MetalCompositeTest") as worker:
        result = worker.submit(_run_on_render_thread, CLIP, cube).result()
    assert len(result["iosurface"]) >= 30
    for name, difference in result["iosurface"].items():
        assert difference["mean"] < 0.8 and difference["p99"] <= 2, (name, difference)
        if "input_outliers" in difference:
            # For the channel-ranking blends: what the inputs alone explain,
            # plus the same hundredth of a percent.
            assert difference["outliers"] <= difference["input_outliers"] + 20, (name, difference)
        else:
            # A hundredth of a percent of the pixels at most (20 at 1080p).
            assert difference["outliers"] <= 20, (name, difference)
    # Rotated clip edges, solid colours at an exact tie and blends that rank
    # channels of nearly equal value (hue, saturation, dodge, burn) are where
    # the outliers come from; everything else matches to the last level.
    excused = ("rotate", "solid", "hue", "saturation", "dodge", "burn")
    for name, difference in result["iosurface"].items():
        if not any(part in name for part in excused):
            assert difference["max"] <= 16, (name, difference)
    assert result["native"]["mean"] < 0.8 and result["native"]["p99"] <= 2, result["native"]
    assert result["native_releases"] == ["c", "d"], result["native_releases"]


def _compare_planar_on_render_thread(path: Path, cube: Path) -> None:
    import moderngl

    import src.features.color.core.color_graph_texture as graph_texture
    import src.shared.color.lut_loader as lut_loader
    from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask
    from src.features.edit.core.metal_timeline_preview import MetalTimelinePreview
    from src.features.edit.core.native_metal_preview import NativePreviewProducer
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    from src.shared.video.yuv_gpu_upload import YUVGPUUploader

    if not MetalComputeContext.is_supported():
        pytest.skip("Metal is unavailable")
    try:
        context = moderngl.create_standalone_context(require=410)
    except (RuntimeError, OSError) as error:
        pytest.skip(f"A CGL render context is unavailable: {error}")

    with FFmpegMediaSource(str(path), preserve_planes=True, output_mode="planar_yuv") as decoder:
        first, second = decoder.read_frame_planar(25), decoder.read_frame_planar(27)
    assert first.pixel_format == second.pixel_format == "yuv422p10le"
    assert all(plane.dtype == np.uint16 for plane in first.planes)
    renderer = _new_renderer(context)
    renderer._yuv_uploader = YUVGPUUploader(context, pool_size=3)
    parking_texture = context.texture((1, 1), 4)
    parking = context.framebuffer([parking_texture])
    parking.use()
    metal = MetalTimelinePreview(context)
    renderer._ensure_native_viewport_state()
    renderer._metal_preview_mode = "all"
    renderer._native_producer = producer = NativePreviewProducer(metal)
    published: list = []
    renderer.present_frame_ready.connect(published.append)
    grade = _lut_graph(cube)
    primary = {"grade": {"exposure": 31.0, "temp": 12.0, "contrast": 15.0, "saturation": 11.0}}
    trim = {"grade": {"exposure": -37.0, "tint": 9.0, "hue_shift": -18.0}, "opacity": 0.82}
    cases = (
        ((1920, 1080), [_planar(first)]),
        ((832, 500), [_planar(first, preview_grade=grade)]),
        (PROXY, [_planar(first), _planar(second, transform=MOVE, opacity=0.45,
                                       crop=(0.1, 0.0, 0.0, 0.2, 40.0), preview_grade=grade)]),
        (PROXY, [_planar(first, preview_grade=_primary_graph(primary))]),
        (PROXY, [_planar(first), _planar(second, opacity=0.82, crop=(0.1, 0.0, 0.0, 0.2, 40.0),
                                       preview_grade=_primary_graph(primary, trim))]),
    )
    try:
        with patch.object(lut_loader, "resolve_lut_path", lambda reference: cube), \
                patch.object(graph_texture, "resolve_lut_path", lambda reference: cube):
            for number, (size, layers) in enumerate(cases):
                width, height = size
                texture = context.texture(size, 4)
                fbo = context.framebuffer([texture])
                renderer._composite_fbo, renderer._composite_tex = fbo, texture
                try:
                    reference = _gl_reference(renderer, {"size": size, "coordinate": SOURCE_SIZE,
                                                            "layers": layers, "dip": None})
                    task = EditRenderTask(number, 1, layers, output_fbo_size=size, coordinate_size=SOURCE_SIZE)
                    assert renderer._try_native_metal_preview(task, width, height)
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and not published:
                        renderer._poll_native_viewport()
                        time.sleep(0.002)
                    assert len(published) == 1
                    present = published.pop()
                    assert present.frame == number and present.native.api == "metal"
                    stored = metal.metal_context.read_texture(
                        present.native.resource, width * 4, height, np.uint8,
                        region_width=width).reshape(height, width, 4)
                    error = _difference(stored[:, :, 2::-1], reference)
                    assert error["mean"] < 0.8 and error["p99"] <= 2, (number, error)
                    upload = renderer.consume_planar_upload_stats(number)
                    assert upload["mode"] == "metal_planar" and upload["tracks"] == len(layers)
                    producer.ack(present, "test_read_done")
                finally:
                    parking.use()
                    fbo.release()
                    texture.release()
    finally:
        assert producer.shutdown()
        metal.release()
        renderer._yuv_uploader.release()
        _release_renderer(renderer)
        parking.release()
        parking_texture.release()
        context.release()


@pytest.mark.gpu_renderer
@pytest.mark.real_media
@pytest.mark.skipif(sys.platform != "darwin", reason="requires Metal and CGL on macOS")
def test_native_metal_preview_matches_gl_for_dnxhr_422(tmp_path: Path) -> None:
    if not DNXHR.is_file():
        pytest.skip("10-bit DNxHR HQX test clip is unavailable")
    cube = tmp_path / "swap.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "".join(
        f"{b} {g} {r}\n" for b in range(2) for g in range(2) for r in range(2)), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="MetalPlanarTest") as worker:
        worker.submit(_compare_planar_on_render_thread, DNXHR, cube).result()
