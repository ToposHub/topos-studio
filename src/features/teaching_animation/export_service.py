"""Explicit Teaching Animation export service with lazy optional FFmpeg."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import threading

from src.shared.ffmpeg_locator import find_ffmpeg as _locate_ffmpeg
import subprocess
from typing import Any, Callable

from .core.contracts import RenderContext
from .core.model import TeachingSceneDocument
from .core.professional import read_scene_color_pipeline
from .render.asset_resolver import AssetPreflightError, AssetResolver
from .render.render_worker import RenderResult, RenderWorker


@dataclass(frozen=True)
class ExportOptions:
    # ``html_document`` preserves one reviewed HTML/CSS Motion Graphic source;
    # ``html_sequence`` remains the rendered per-frame delivery format.
    format: str = "svg_sequence"
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    transparent: bool = False
    strict_assets: bool = True
    ffmpeg_binary: str | None = None
    progress_callback: Callable[[Any], bool | None] | None = None
    # ``frame_accurate`` keeps the deterministic renderer contract.  HTML
    # Motion Graphics may opt into ``realtime`` to record one persistent
    # Chromium page at browser cadence before encoding the delivery file.
    capture_mode: str = "frame_accurate"
    # 40 Mbps is the 2K-class default.  The recorder scales it with output
    # pixel area for 4K and keeps a 20 Mbps minimum effective rate.
    realtime_video_codec: str = "vp9"
    realtime_bitrate_kbps: int = 40_000


@dataclass(frozen=True)
class ExportResult:
    output: str
    format: str
    frame_count: int
    width: int
    height: int
    cancelled: bool = False


class TeachingExportError(ValueError):
    pass


def _realtime_progress(completed: int, total: int, fps: float) -> Any:
    """Adapt recorder progress to the export worker's progress contract."""
    from .render.render_worker import RenderProgress
    frame = max(0, min(max(0, int(total) - 1), int(completed) - 1))
    return RenderProgress("realtime-record", int(completed), int(total), frame)


def _effective_html_renderer_build(
    scene: TeachingSceneDocument, context: RenderContext
) -> str:
    """The pinned Chromium build that actually rasterised this export's frames.

    Prefers the build stamped on the HTML layer; otherwise reads it from the
    shared HTML service that performed the captures — its ``backend_build`` is
    set from the page user-agent on the first capture, so immediately after an
    HTML export this is the real pinned revision (not the pre-capture
    placeholder).  Returns "" for non-HTML scenes or when unavailable, so the
    manifest never invents a build it cannot prove.
    """
    layer = getattr(scene, "html_layer", None)
    stamped = (getattr(layer, "renderer_build", "") or "") if layer is not None else ""
    if stamped:
        return stamped
    if getattr(context, "backend", "") != "html":
        return ""
    try:
        # The export worker renders through the shared pinned-browser service
        # (select_scene_renderer → get_shared_html_service), so after render the
        # singleton's backend_build is the build that produced these frames.
        from .render.scene_renderer_router import get_shared_html_service
        return get_shared_html_service().backend_build
    except Exception:
        return ""


class TeachingAnimationExportService:
    def __init__(self, worker: RenderWorker | None = None) -> None:
        self.worker = worker or RenderWorker()
        self._owns_worker = worker is None

    def close(self) -> None:
        if self._owns_worker:
            self.worker.shutdown()

    def preflight(self, scene: TeachingSceneDocument, options: ExportOptions, *, asset_root: str | None = None) -> None:
        context = self._context(scene, options, asset_root)
        try:
            AssetResolver(context).preflight(scene)
        except AssetPreflightError as exc:
            raise TeachingExportError(str(exc)) from exc
        self._html_preflight(scene)
        from .core.audio_plan import audio_plan_for_scene, preflight_audio
        audio = audio_plan_for_scene(scene)
        available = frozenset(asset.asset_id for asset in scene.asset_refs)
        diagnostic = preflight_audio(
            audio, available, scene_duration_frames=scene.duration_frames,
        )
        if diagnostic.blocking:
            detail = "; ".join([
                *(f"missing audio asset for {clip_id}" for clip_id in diagnostic.missing_assets),
                *(f"audio clip out of range: {clip_id}" for clip_id in diagnostic.out_of_range),
                *(f"audio mix unsupported: {clip_id}" for clip_id in diagnostic.unsupported_mix),
            ])
            raise TeachingExportError(f"Audio plan failed export preflight: {detail}")

    @staticmethod
    def _html_preflight(scene: TeachingSceneDocument) -> None:
        """Fail-closed HTML gate at export (AQ-10 / release gate §7.2 #3).

        The sanitizer and HTML quality gate ran at *generation* time, but a
        loaded/migrated/hand-edited ``.topos`` flows straight to the renderer
        verbatim otherwise.  Re-running the gate here is the boundary that
        actually matters: the sanitizer must be a fixed point on the stored
        HTML (no disallowed tokens survive a re-scan), and no ERROR-level
        quality finding (unsafe content, CSS budget, viewport overflow,
        unresolved assets) may ship.  Warnings pass; errors block the export
        with a diagnostic rather than rendering an unsafe or over-budget page.
        """
        layer = getattr(scene, "html_layer", None)
        if layer is None:
            return
        from .ai.html_quality_gate import HtmlQualityInputs, run_html_quality_checks
        known = sorted({a.asset_id for a in scene.asset_refs}
                       | set(getattr(layer, "bound_asset_ids", ())))
        metadata = getattr(layer, "metadata", {}) or {}
        # The layer stores the explicit browser-authoring permissions used at
        # generation time. Recreate that sanitizer profile for export's
        # fixed-point check; using the offline default here stripped permitted
        # HTTPS/inline browser content and incorrectly blocked source export.
        from .render.html_layer.sanitizer import HtmlSanitizer
        sanitizer = HtmlSanitizer(
            allow_network_resources=metadata.get("network_access", False) is True,
            allow_inline_scripts=metadata.get("allow_inline_scripts", False) is True,
            allow_event_handlers=metadata.get("allow_event_handlers", False) is True,
            allow_freeform_css=metadata.get("allow_freeform_css", False) is True,
            allow_inline_visual_resources=(
                metadata.get("allow_inline_visual_resources", False) is True
            ),
        )
        result = run_html_quality_checks(HtmlQualityInputs(
            sanitized_html=layer.sanitized_html,
            viewport_width=getattr(layer, "viewport_width", scene.width),
            viewport_height=getattr(layer, "viewport_height", scene.height),
            known_asset_ids=tuple(known),
            renderer_available=True,  # export always renders through the backend
        ), sanitizer=sanitizer)
        if result.errors:
            detail = "; ".join(issue.message for issue in result.errors)
            raise TeachingExportError(
                f"HTML layer failed the export quality gate: {detail}")

    def export(self, scene: TeachingSceneDocument, output: str | Path, *, options: ExportOptions | None = None,
               asset_root: str | None = None) -> ExportResult:
        options = options or ExportOptions()
        if options.fps is not None and abs(float(options.fps) - scene.fps) > 1e-9:
            raise TeachingExportError("fps override must match scene.fps; retiming is not supported")
        output_path = Path(output)
        context = self._context(scene, options, asset_root)
        self.preflight(scene, options, asset_root=asset_root)
        format_name = options.format.lower()
        if format_name in {"html", "html_document", "html_source"}:
            return self._write_html_document(output_path, scene, context)
        if context.backend == "html":
            # Strictly prove that the real browser paints at least one
            # deterministic authored frame before enabling uniform-frame
            # preservation for final delivery. This prevents a broken/empty
            # page from exporting as a sequence of black/transparent frames,
            # while still allowing legitimate fade-in/out frames afterward.
            from .render.html_validation import validate_html_scene
            try:
                validate_html_scene(
                    scene,
                    context=replace(context, allow_uniform_frames=False),
                )
            except Exception as exc:  # noqa: BLE001 - expose one export error
                raise TeachingExportError(
                    f"HTML renderability gate failed before export: {exc}"
                ) from exc
        if str(options.capture_mode).strip().lower() in {"realtime", "record", "recording"}:
            if context.backend != "html":
                raise TeachingExportError(
                    "real-time recording is currently supported for HTML Motion Graphic scenes only"
                )
            if format_name not in {
                "prores_proxy", "prores422proxy", "prores_422_proxy",
                "webm_realtime", "realtime_webm", "webm_recording",
                "mp4", "mp4_realtime", "realtime_mp4", "mp4_vp9", "mp4_high_quality",
            }:
                raise TeachingExportError(
                    "real-time HTML recording supports native WebM, VP9 MP4 or "
                    "ProRes 422 Proxy; use format='mp4_vp9'"
                )
            return self._write_realtime_video(output_path, scene, context, options)
        result = self.worker.render_sync(scene, range(scene.duration_frames), context, options.progress_callback)
        if result.cancelled:
            return ExportResult(str(output_path), options.format, len(result.frames), context.width, context.height, True)
        if format_name in {"svg_sequence", "html_sequence", "rgba_sequence"}:
            self._write_sequence(output_path, format_name, result, context, scene)
        elif format_name in {"mp4", "mov", "webm", "prores", "prores_proxy", "prores422proxy", "prores_422_proxy"}:
            self._write_video(output_path, format_name, result, context, options, scene)
        else:
            raise TeachingExportError(f"unsupported export format: {options.format}")
        return ExportResult(str(output_path), format_name, len(result.frames), context.width, context.height)

    @classmethod
    def _write_realtime_video(
        cls,
        output: Path,
        scene: TeachingSceneDocument,
        context: RenderContext,
        options: ExportOptions,
    ) -> ExportResult:
        """Record one persistent HTML page, then encode ProRes 422 Proxy.

        This path intentionally does not call ``RenderWorker.render_sync``:
        the worker's frame-accurate contract captures every timeline frame in
        isolation, which is ideal for reproducibility but much slower than a
        live browser recording.  The same sanitized HTML, page-deck timing,
        font lock and URL policy are still applied before the recorder starts.
        """
        from .core.html_page_deck import (
            detect_page_count, ensure_page_markers, page_deck_runtime_html,
            page_timing_for, scene_background_css,
        )
        from .core.html_fonts import wrap_with_font_lock, default_font_lock_css
        from .render.html_scene_renderer_v2 import resolve_bound_image_bytes
        from .render.html_renderer_spike.security import ensure_font_lock_csp
        from .render.html_realtime_recorder import record_html_video
        from src.shared.media.motion_graphic_delivery import (
            motion_graphic_bitrate_kbps,
            motion_graphic_min_bitrate_kbps,
        )

        layer = getattr(scene, "html_layer", None)
        if layer is None or not str(getattr(layer, "sanitized_html", "")).strip():
            raise TeachingExportError("real-time HTML recording requires a sanitized HTML layer")
        metadata = getattr(layer, "metadata", {}) or {}
        document = ensure_page_markers(str(layer.sanitized_html).strip())
        page_count = detect_page_count(document)
        timing = page_timing_for(
            metadata,
            page_count=page_count,
            fallback_default_seconds=max(0.001, scene.duration_frames / scene.fps) / page_count,
        )
        # The live recorder owns page switching through the same trusted
        # runtime used by browser HTML export.  Authored page content remains
        # untouched; only the host controller is appended.
        document = page_deck_runtime_html(
            document,
            timing,
            global_background=scene_background_css(getattr(scene, "background", {})),
            page_backgrounds=tuple(page.background for page in scene.pages),
        )
        font_lock_enabled = metadata.get("font_lock", True) is not False
        font_css = default_font_lock_css() if font_lock_enabled else ""
        if font_css:
            document = wrap_with_font_lock(document, font_css)
        csp = str(getattr(layer, "csp", "")) or "default-src 'none'"
        if font_css:
            csp = ensure_font_lock_csp(csp)
        asset_bytes = resolve_bound_image_bytes(scene, context)
        duration_seconds = scene.duration_frames / max(0.001, scene.fps)
        format_name = str(options.format).strip().lower()
        target_bitrate_kbps = motion_graphic_bitrate_kbps(
            context.width,
            context.height,
            requested_kbps=options.realtime_bitrate_kbps,
        )
        minimum_bitrate_kbps = motion_graphic_min_bitrate_kbps(
            context.width, context.height,
        )
        result = record_html_video(
            document,
            output,
            width=int(context.width),
            height=int(context.height),
            duration_seconds=duration_seconds,
            fps=float(context.fps),
            csp=csp,
            allowed_asset_ids=frozenset(getattr(layer, "bound_asset_ids", ()) or ()),
            asset_resolver=asset_bytes,
            allow_network_resources=metadata.get("network_access", False) is True,
            allowed_network_hosts=frozenset(metadata.get("network_hosts", ()) or ()),
            allow_inline_visual_resources=metadata.get("allow_inline_visual_resources", False) is True,
            allow_gpu_apis=metadata.get("allow_gpu_apis", False) is True,
            transparent=False,
            ffmpeg_binary=options.ffmpeg_binary,
            progress_callback=(
                (lambda completed, total: options.progress_callback(
                    _realtime_progress(completed, total, scene.fps)
                ))
                if options.progress_callback is not None else None
            ),
            delivery=(
                "vp8_high_quality" if format_name in {"webm_realtime", "realtime_webm", "webm_recording"}
                else "vp9_mp4" if format_name in {"mp4", "mp4_realtime", "realtime_mp4", "mp4_vp9", "mp4_high_quality"}
                else "prores_proxy"
            ),
            video_codec=options.realtime_video_codec,
            bitrate_kbps=target_bitrate_kbps,
            min_bitrate_kbps=minimum_bitrate_kbps,
        )
        if result.cancelled:
            return ExportResult(
                str(output), format_name, 0, int(context.width), int(context.height), True,
            )
        native_webm = format_name in {"webm_realtime", "realtime_webm", "webm_recording"}
        vp9_mp4 = format_name in {"mp4", "mp4_realtime", "realtime_mp4", "mp4_vp9", "mp4_high_quality"}
        delivery_format = "webm_realtime" if native_webm else "mp4_vp9" if vp9_mp4 else "prores_proxy"
        manifest = {
            "format": delivery_format,
            "capture_mode": "realtime",
            "codec": "vp8_high_quality_webm" if native_webm else "vp9" if vp9_mp4 else "prores_422_proxy",
            "fps": result.fps,
            "timeline_fps": context.fps,
            "target_bitrate_kbps": target_bitrate_kbps,
            "minimum_bitrate_kbps": minimum_bitrate_kbps,
            "width": context.width,
            "height": context.height,
            "frame_count": result.frame_count,
            "duration_seconds": duration_seconds,
            "provenance": cls._provenance(scene, context),
        }
        manifest["provenance"]["capture_mode"] = "realtime_browser_recording"
        output.with_suffix(output.suffix + ".manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )
        return ExportResult(
            str(output), delivery_format, result.frame_count,
            int(context.width), int(context.height), False,
        )

    @staticmethod
    def _write_html_document(
        output: Path, scene: TeachingSceneDocument, context: RenderContext,
    ) -> ExportResult:
        """Write the reviewed HTML layer as one self-contained source file.

        This is deliberately different from ``html_sequence``: the latter is
        a rendered frame delivery format, while this path preserves the
        model-authored HTML/CSS page deck so it can be opened and inspected in
        a browser.  The stored layer is already sanitizer-approved; export
        never writes the pre-sanitization model response.
        """
        layer = getattr(scene, "html_layer", None)
        if layer is None or not str(getattr(layer, "sanitized_html", "")).strip():
            raise TeachingExportError(
                "HTML document export requires a scene with a sanitized HTML layer"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        from .core.html_page_deck import (
            detect_page_count, ensure_page_markers, page_deck_runtime_html,
            page_timing_for, scene_background_css,
        )
        html = ensure_page_markers(str(layer.sanitized_html).strip())
        timing = page_timing_for(
            getattr(layer, "metadata", {}),
            page_count=detect_page_count(html),
            fallback_default_seconds=max(0.001, scene.duration_frames / scene.fps)
            / detect_page_count(html),
        )
        html = page_deck_runtime_html(
            html,
            timing,
            global_background=scene_background_css(getattr(scene, "background", {})),
            page_backgrounds=tuple(page.background for page in scene.pages),
        )
        if not html.lower().lstrip().startswith("<!doctype"):
            html = "<!doctype html>\n" + html
        output.write_text(html + ("\n" if not html.endswith("\n") else ""), encoding="utf-8")
        return ExportResult(
            str(output), "html_document", 0, context.width, context.height,
        )

    def _context(self, scene: TeachingSceneDocument, options: ExportOptions, asset_root: str | None) -> RenderContext:
        from .render.scene_renderer_router import backend_for_scene
        pipeline = read_scene_color_pipeline(scene.metadata)
        return RenderContext(
            fps=options.fps or scene.fps,
            width=options.width or scene.width,
            height=options.height or scene.height,
            backend=backend_for_scene(scene),
            asset_root=asset_root,
            strict_assets=options.strict_assets,
            transparent=options.transparent or pipeline.alpha_mode == "premult",
            render_profile="final",
            working_color_space=pipeline.working_space,
            bit_depth=pipeline.bit_depth,
            # Export receives a separate HTML renderability gate before the
            # worker starts. Once that gate proves the authored document has a
            # real paintable frame, preserve legitimate uniform frames (such
            # as a fade-in at frame 0) instead of aborting the sequence.
            allow_uniform_frames=backend_for_scene(scene) == "html",
        )

    @staticmethod
    def _provenance(scene: TeachingSceneDocument, context: RenderContext) -> dict[str, Any]:
        """Delivery manifest provenance: how this export was produced.

        Locks the render surface (viewport + DPR), the renderer identity and
        build, the resolved asset/font content hashes, and — for an HTML scene
        — the W0-A/FH-01 layer provenance (locked font set, sanitizer, source
        hashes, CSP, bound asset ids), so a delivered package is auditable and
        reproducible to the byte (release gate §7.2 #7/#9).  The HTML main
        chain renders design-pixels at DPR 1 (physical == CSS pixels), which is
        what the manifest records; a future DPR-aware path would report its
        own surface.
        """
        backend = getattr(context, "backend", "") or ""
        build = _effective_html_renderer_build(scene, context)
        # An honest renderer string: the default RenderContext.renderer_version
        # still names the SVG renderer, so for an HTML export name the HTML
        # renderer + the pinned build that actually rasterised the frames.
        renderer = f"teaching-animation-html/{build}" if (backend == "html" and build) else context.renderer_version
        info: dict[str, Any] = {
            "renderer": renderer,
            "backend": backend,
            "viewport": [int(context.width), int(context.height)],
            "dpr": getattr(context, "dpr", 1.0),
        }
        pipeline = read_scene_color_pipeline(scene.metadata)
        info["color_pipeline"] = pipeline.to_dict()
        if pipeline.is_hdr:
            # Every current export writer consumes RenderedFrame RGBA8 bytes.
            # Keep this explicit so HDR metadata cannot be mistaken for HDR
            # pixel delivery until a float16/float32 writer is implemented.
            info["hdr_delivery"] = {
                "requested": True,
                "status": "explicit_8bit_fallback",
                "pixel_format": "rgba8",
                "fallback": "svg_8bit_tonemap",
                "reason": "current sequence and FFmpeg writers consume RGBA8",
            }
        else:
            info["hdr_delivery"] = {
                "requested": False,
                "status": "sdr",
                "pixel_format": "rgba8",
            }
        # Content hashes: reuse the cache-key manifest's asset/font hashing so
        # the delivery manifest and the cache key agree on identity.  Frame 0
        # is a stable reference point; hashes are frame-independent.  Never
        # let a hashing failure break manifest writing — record what resolved.
        try:
            from .render.cache import build_cache_manifest
            manifest = build_cache_manifest(scene, 0, context, renderer_build=build)
            info["content_hashes"] = {
                "asset_hashes": [[aid, h] for aid, h in manifest.asset_hashes],
                "font_hashes": [[aid, h] for aid, h in manifest.font_hashes],
            }
        except Exception:  # noqa: BLE001 - provenance is best-effort
            pass
        layer = getattr(scene, "html_layer", None)
        if layer is not None:
            # The font lock is applied unconditionally at render time with the
            # default packaged font set, so the effective font_set_hash is the
            # default even when the layer didn't record one (e.g. a layer built
            # directly rather than via the generation pipeline).  Report the
            # render-time truth so the manifest is accurate for reproducibility.
            font_set_hash = getattr(layer, "font_set_hash", "") or ""
            if not font_set_hash:
                from .core.html_fonts import default_font_set_hash
                font_set_hash = default_font_set_hash()
            info["html_layer"] = {
                "renderer_build": build or getattr(layer, "renderer_build", "") or "",
                "font_set_hash": font_set_hash,
                "sanitizer_version": getattr(layer, "sanitizer_version", "") or "",
                "source_hash": getattr(layer, "source_hash", "") or "",
                "sanitizer_hash": getattr(layer, "sanitizer_hash", "") or "",
                "csp": getattr(layer, "csp", "") or "",
                "bound_asset_ids": list(getattr(layer, "bound_asset_ids", ())),
            }
        return info

    @classmethod
    def _write_sequence(cls, output: Path, format_name: str, result: RenderResult,
                        context: RenderContext, scene: TeachingSceneDocument) -> None:
        """Write rendered frames as a numbered sequence (AUD-27900 fix).

        The writer routes by each frame's ``pixel_format`` so the file
        extension always matches the content: vector text frames
        (``pixel_format == "svg"``, SVG backend) are written as text —
        ``html_sequence`` decodes and wraps them in a viewable HTML shell —
        while raster frames (``pixel_format == "rgba8"``, the HTML backend
        rasterises pixels) are written as binary ``.rgba`` files.  Pixel
        bytes are never UTF-8-decoded and never labelled ``.svg``/``.html``,
        which previously either silently corrupted the delivery (default
        ``svg_sequence`` on an HTML scene) or crashed with
        ``UnicodeDecodeError`` (``html_sequence``).
        """
        from .render.frame_adapter import FrameAdapter

        output.mkdir(parents=True, exist_ok=True)
        raster_deliveries = 0
        for frame in result.frames:
            stem = output / f"frame_{frame.frame:06d}"
            pixel_format = str(getattr(frame, "pixel_format", "") or "")
            if format_name == "rgba_sequence" or pixel_format == "rgba8":
                # Raster delivery: HTML-backend frames are already RGBA8
                # bytes; SVG frames rasterise through the FrameAdapter (the
                # adapter validates the frame and passes rgba8 through).
                if format_name != "rgba_sequence":
                    raster_deliveries += 1
                stem.with_suffix(".rgba").write_bytes(
                    FrameAdapter.from_rendered_frame(frame).data)
            elif pixel_format == "svg":
                if format_name == "html_sequence":
                    svg = frame.data.decode("utf-8")
                    html = "<!doctype html><meta charset=\"utf-8\"><style>html,body{margin:0;background:#111827}</style>" + svg
                    stem.with_suffix(".html").write_text(html, encoding="utf-8")
                else:
                    (stem.with_suffix(".svg")).write_bytes(frame.data)
            else:
                raise TeachingExportError(
                    f"unsupported frame pixel format for sequence export: {pixel_format!r}")
        manifest = {
            "format": format_name, "fps": context.fps,
            "width": context.width, "height": context.height,
            "frame_count": len(result.frames),
            "provenance": cls._provenance(scene, context),
        }
        if raster_deliveries:
            # The requested format names a vector text sequence, but the
            # backend produced raster frames; keep the delivery auditable by
            # recording what was actually written instead of implying .svg
            # or .html files exist in the package.
            manifest["delivery_note"] = (
                f"{raster_deliveries} raster RGBA8 frame(s) delivered as binary "
                ".rgba files; svg_sequence/html_sequence content applies to "
                "vector SVG frames only"
            )
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    @classmethod
    def _write_video(cls, output: Path, format_name: str, result: RenderResult,
                     context: RenderContext, options: ExportOptions,
                     scene: TeachingSceneDocument) -> None:
        ffmpeg = options.ffmpeg_binary or _locate_ffmpeg()
        if not ffmpeg:
            raise TeachingExportError("FFmpeg is required for video export but was not found")
        if format_name in {"prores_proxy", "prores422proxy", "prores_422_proxy"}:
            # Profile 0 is Apple ProRes 422 Proxy (not ProRes 4444).  The
            # browser renderer currently supplies RGBA8, so the writer makes
            # the explicit 4:2:2 10-bit delivery conversion here.
            codec = [
                "-c:v", "prores_ks", "-profile:v", "0",
                "-pix_fmt", "yuv422p10le",
            ]
        elif format_name == "prores":
            codec = ["-c:v", "prores_ks", "-profile:v", "4"]
        elif format_name == "webm":
            codec = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p"]
        else:
            codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        output.parent.mkdir(parents=True, exist_ok=True)
        # ``-nostats -loglevel error`` keeps FFmpeg's stderr stream tiny (no
        # per-frame progress lines); the drain thread below guarantees the
        # pipe can never fill regardless.
        command = [ffmpeg, "-nostats", "-loglevel", "error", "-y", "-f", "rawvideo",
                   "-pix_fmt", "rgba", "-s", f"{context.width}x{context.height}",
                   "-r", str(context.fps), "-i", "-",
                   "-frames:v", str(len(result.frames)), *codec, str(output)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert process.stdin is not None
        from .render.frame_adapter import FrameAdapter
        # AUD-27901: stderr must be drained *while* frames are written to
        # stdin.  The previous "write all of stdin, then read stderr" filled
        # the 64KB OS pipe buffer on long exports (stats/warnings), FFmpeg
        # blocked writing stderr, stopped reading stdin, and both processes
        # deadlocked permanently.  A concurrent drain thread removes the
        # deadlock without buffering every frame in memory (unlike
        # communicate(input=all_bytes)).
        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            if process.stderr is not None:
                stderr_chunks.append(process.stderr.read())

        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()
        try:
            for frame in result.frames:
                process.stdin.write(FrameAdapter.from_rendered_frame(frame).data)
            process.stdin.close()
            code = process.wait()
        except Exception:
            process.kill()
            process.wait()
            raise
        finally:
            # Deterministic handle release (success and failure paths); close
            # on an already-closed stdin/stderr is a no-op.
            try:
                process.stdin.close()
            except OSError:
                pass
            drain.join(timeout=5.0)
            if process.stderr is not None:
                try:
                    process.stderr.close()
                except OSError:
                    pass
        stderr = stderr_chunks[0] if stderr_chunks else b""
        if code != 0:
            raise TeachingExportError(f"FFmpeg export failed: {stderr.decode(errors='replace')[-1000:]}")
        if not output.is_file() or output.stat().st_size <= 0:
            raise TeachingExportError(f"FFmpeg reported success but output is missing: {output}")
        # Sidecar manifest with provenance (the container itself carries no
        # such metadata), so a delivered video is auditable alongside its file.
        manifest = {
            "format": format_name, "fps": context.fps,
            "width": context.width, "height": context.height,
            "frame_count": len(result.frames),
            "provenance": cls._provenance(scene, context),
        }
        output.with_suffix(output.suffix + ".manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")


__all__ = ["ExportOptions", "ExportResult", "TeachingAnimationExportService", "TeachingExportError"]
