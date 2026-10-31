"""Render Cache worker — background thread that renders and encodes cache files."""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from src.features.edit.core.render_cache import RenderCacheManifest
from src.features.edit.core.render_cache_encoder import RenderCacheEncoder, RenderCacheEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.motion_graphic_delivery import (
    MOTION_GRAPHIC_CACHE_BITRATE_KBPS,
    motion_graphic_bitrate_kbps,
    motion_graphic_min_bitrate_kbps,
)

logger = logging.getLogger(__name__)

# T-03 后半段：planar 直通接受的像素格式。与 ToposVideoEncoder 的输入
# 白名单同源（topos_encoder._PLANAR_FMT_3/_PLANAR_FMT_4），导入而非复制，
# 保证编码器扩展格式时这里自动跟随。
try:
    from src.shared.export.topos_encoder import (
        _PLANAR_FMT_3 as _TOPOS_PLANAR_FORMATS,
        _PLANAR_FMT_4 as _TOPOS_PLANAR_ALPHA_FORMATS,
    )
    _PASSTHROUGH_PLANAR_FORMATS = frozenset(
        _TOPOS_PLANAR_FORMATS + _TOPOS_PLANAR_ALPHA_FORMATS
    )
except ImportError:  # pragma: no cover — encoder 模块缺失时退回保守集合
    _PASSTHROUGH_PLANAR_FORMATS = frozenset({"yuv422p10le"})


class _CpuCompClipRenderer:
    """Headless-safe renderer for the timeline-backed Comp Clip graph.

    A render-cache worker has no GUI/OpenGL context.  The old implementation
    nevertheless constructed ``CompositeFrameRenderer`` here, which caused
    ModernGL to create a context in the worker process and crash on headless
    macOS.  The default Comp Clip graph is a source clip plus the clip's
    Topos Color grade and output node, so it can be evaluated by the existing
    CPU timeline compositor without creating any GPU resources.

    This class deliberately rejects graph nodes that do not have a CPU
    equivalent instead of silently producing an incomplete cache.  The caller
    reports that as a normal render failure, and the UI remains alive.
    """

    _SUPPORTED_NODE_TYPES = {
        "timeline_clip_input",
        "video_frame",
        "topos_color",
        "output",
        "constant",
    }

    def __init__(
        self,
        manifest: RenderCacheManifest,
        edit_project: Any,
        timeline_sequences: dict[str, Any],
        graph_data: dict[str, Any],
    ) -> None:
        from src.features.edit.core.timeline_preview_renderer import (
            AssetResolver,
            PreviewRenderWorker,
        )
        from src.features.edit.core.timeline_render_plan import ClipLayerInfo

        self._manifest = manifest
        self._project = edit_project
        self._timeline_sequences = timeline_sequences or {}
        self._graph_data = graph_data
        self._clip_layer_info_type = ClipLayerInfo
        self._timeline, self._source_clip, self._source_track = self._resolve_source_clip()

        nodes = tuple(graph_data.get("nodes", ()) or ())
        node_types = {
            str(node.get("type") or node.get("node_type") or "")
            for node in nodes
            if isinstance(node, dict)
        }
        unsupported = sorted(node_type for node_type in node_types if node_type not in self._SUPPORTED_NODE_TYPES)
        if unsupported:
            raise RuntimeError(
                "Comp Clip graph contains GPU-only nodes: " + ", ".join(unsupported)
            )

        self._is_constant_graph = self._source_clip is None
        self._worker: Any | None = None
        if not self._is_constant_graph:
            from src.shared.video.frame_provider_service import VideoFrameProviderService

            # Direct FFmpeg decoding is used first by PreviewRenderWorker.  The
            # provider remains as a CPU-only fallback for media that cannot use
            # the lightweight direct source path.
            frame_provider = VideoFrameProviderService(
                max_proxy_cache=2,
                max_master_cache=1,
                auto_generate_disk_proxies=False,
            )
            self._worker = PreviewRenderWorker(
                frame_provider=frame_provider,
                asset_resolver=AssetResolver(edit_project),
                video_renderer=None,
                timeline_sequence=self._timeline,
                viewport_size=(int(manifest.width), int(manifest.height)),
                max_preview_edge=max(int(manifest.width), int(manifest.height), 64),
            )
            # Render cache output has an explicit resolution.  Do not derive a
            # smaller interactive-preview size from the viewport or quality.
            self._worker._active_decode_target_size = (
                max(1, int(manifest.width)),
                max(1, int(manifest.height)),
            )

    def _resolve_source_clip(self) -> tuple[Any | None, Any | None, Any | None]:
        """Resolve the original timeline clip referenced by the graph."""
        graph = self._graph_data
        source_refs = graph.get("source_refs") or {}
        timeline_id = (
            graph.get("timeline_id")
            or source_refs.get("original_timeline_id")
            or self._manifest.timeline_id
        )
        clip_ids = [
            graph.get("clip_id"),
            graph.get("source_clip_id"),
            source_refs.get("original_clip_id"),
            self._manifest.clip_id,
        ]
        clip_ids = [str(value) for value in clip_ids if value]

        timelines = self._timeline_sequences
        timeline = timelines.get(timeline_id) if timeline_id else None
        if timeline is None and timelines:
            timeline = next(iter(timelines.values()))

        if timeline is not None:
            for clip_id in clip_ids:
                try:
                    clip = timeline.get_clip_by_id(clip_id)
                except AttributeError:
                    clip = None
                if clip is not None:
                    track = next(
                        (
                            candidate
                            for candidate in getattr(timeline, "all_tracks", ())
                            if candidate.get_clip_by_id(clip_id) is not None
                        ),
                        None,
                    )
                    return timeline, clip, track

        # An empty Comp Clip has no source timeline clip.  The constant graph
        # path is handled separately and remains CPU-only.
        if graph.get("source_kind") == "empty_comp_clip" or not any(
            (node.get("type") or node.get("node_type")) == "timeline_clip_input"
            for node in graph.get("nodes", ())
            if isinstance(node, dict)
        ):
            return timeline, None, None

        raise RuntimeError(
            "Comp Clip source clip not found: "
            f"timeline={timeline_id!r}, clip={clip_ids[0] if clip_ids else ''!r}"
        )

    def _render_constant_frame(self) -> np.ndarray:
        """Render the default empty Comp Clip constant node in BGR sRGB."""
        width = max(1, int(self._manifest.width))
        height = max(1, int(self._manifest.height))
        color = (1.0, 1.0, 1.0, 1.0)
        for node in self._graph_data.get("nodes", ()):
            if not isinstance(node, dict):
                continue
            if (node.get("type") or node.get("node_type")) != "constant":
                continue
            params = node.get("parameters") or node.get("data") or {}
            color = (
                float(params.get("color_r", color[0])),
                float(params.get("color_g", color[1])),
                float(params.get("color_b", color[2])),
                float(params.get("color_a", color[3])),
            )
            break

        # ConstantNode stores scene-linear values.  Cache files are display
        # frames, so encode the same linear-to-sRGB conversion used by the
        # CPU composite readback path.
        # F5.1：OETF 收口到共享单点（真值=provider；见 display_encode.py）。
        from src.shared.color.display_encode import linear_to_srgb_display

        srgb = linear_to_srgb_display(color[:3])
        bgr = np.clip(srgb[::-1] * 255.0, 0.0, 255.0).astype(np.uint8)
        return np.broadcast_to(bgr, (height, width, 3)).copy()

    def render_frame(self, timeline_frame: int) -> np.ndarray | DecodedFrame | None:
        """Render one cache frame as uint8 BGR without touching ModernGL."""
        if self._is_constant_graph:
            return self._render_constant_frame()

        assert self._worker is not None
        assert self._source_clip is not None

        source_clip = self._source_clip
        sample_frame = int(timeline_frame)
        if not (source_clip.timeline_start <= sample_frame < source_clip.timeline_end):
            sample_frame = max(
                int(source_clip.timeline_start),
                min(sample_frame, int(source_clip.timeline_end) - 1),
            )
        source_frame = source_clip.timeline_to_source_frame(sample_frame)

        # A render cache stores the Comp graph result.  Editorial transform,
        # crop and opacity stay on the timeline layer and must not be baked a
        # second time when the cache is consumed.  ColorStack/effects belong to
        # the source graph's CPU-equivalent path and are applied once below.
        has_topos_color = False
        graph_color_stack_id = ""
        for node in self._graph_data.get("nodes", ()):
            if not isinstance(node, dict):
                continue
            if (node.get("type") or node.get("node_type")) != "topos_color":
                continue
            has_topos_color = True
            params = node.get("parameters") or node.get("data") or {}
            if not bool(params.get("bypass", False)):
                graph_color_stack_id = str(params.get("color_stack_id") or "")
            break

        render_clip = replace(
            source_clip,
            composite_id=None,
            composite_bypassed=False,
            color_stack_id=(
                graph_color_stack_id or getattr(source_clip, "color_stack_id", None)
                if has_topos_color
                else None
            ),
        )
        track = self._source_track
        track_index = int(getattr(track, "index", 0))
        track_name = str(getattr(track, "name", "Render Cache"))
        layer = self._clip_layer_info_type(
            clip=render_clip,
            source_frame=int(source_frame),
            track_index=track_index,
            track_name=track_name,
            opacity=1.0,
            # The cache stores the graph result.  The timeline layer applies
            # its editorial transform/crop/opacity when the cache is played.
            transform=(0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
            crop=(0.0, 0.0, 0.0, 0.0, 0.0),
            decode_stream_key=render_clip.clip_id,
        )
        # T-03 后半段：恒等单层图优先尝试平面直通产出；不适合直通的
        # 情形（调色/效果/混合模式/几何不符/非平面解码）回退既有合成。
        passthrough = self._planar_passthrough_frame(render_clip, source_frame)
        if passthrough is not None:
            return passthrough
        return self._worker._composite_layers((layer,), (), sample_frame)

    def _planar_passthrough_frame(
        self,
        render_clip: Any,
        source_frame: int,
    ) -> DecodedFrame | None:
        """T-03 后半段：单层直通图的平面帧产出。

        默认 Comp Clip graph 是"源 clip（+调色）+ output"。当本帧合成
        退化为恒等（无调色/无效果/无混合模式/无变换裁剪——变换裁剪与
        透明度在 render_frame 里本就强制恒等）且解码结果是编码器白名单
        内的平面格式、几何与 manifest 完全一致时，缓存帧 == 解码帧：
        原样返回 DecodedFrame 让 worker 走 write_frame_planar，省去
        planar→BGR（10bit→8bit 量化）与 BGR→planar 的两次整帧转换，
        且消除中间 8-bit 量化损失。任何条件不满足返回 None（回退合成
        路径），绝不静默产出内容不一致的缓存。
        """
        # 图内调色或 clip 级 ColorStack：必须走合成路径烘焙。
        if getattr(render_clip, "color_stack_id", None):
            return None
        # 视频滤镜链（blur/sharpen/...）只在 CPU 合成路径实现。
        if tuple(getattr(render_clip, "effects", ()) or ()):
            return None
        # 非普通混合模式需要合成器混合。
        blend_mode = getattr(render_clip, "blend_mode", None)
        if blend_mode not in (None, "", "normal"):
            return None

        worker = self._worker
        asset_path = worker._asset_resolver.get_asset_path(render_clip.asset_id)
        if not asset_path:
            return None
        decoded = worker._get_decoded_frame_for_cache_passthrough(
            asset_path,
            int(source_frame),
            render_clip.asset_id,
            render_clip.clip_id,
        )
        if decoded is None:
            return None
        # 几何必须与 manifest 逐像素一致：不符的帧交给合成路径做
        # letterbox/缩放，直通只接受恒等几何。
        if (int(decoded.width) != int(self._manifest.width)
                or int(decoded.height) != int(self._manifest.height)):
            return None
        # 只有平面帧有直通价值（packed BGR 场景合成路径已是直入编码器）。
        if not getattr(decoded, "is_planar", False):
            return None
        pixel_format = str(getattr(decoded, "pixel_format", "") or "").lower()
        if pixel_format not in _PASSTHROUGH_PLANAR_FORMATS:
            return None
        return decoded

    def close(self) -> None:
        """Release CPU decoder and preview-worker resources."""
        if self._worker is None:
            return
        try:
            self._worker.cleanup_clips()
        finally:
            try:
                self._worker._clear_direct_preview_sources()
            except Exception:
                pass
            try:
                self._worker._decode_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass


class RenderCacheWorker(QThread):
    """Background worker that renders composite frames and encodes to cache file.

    Usage:
        worker = RenderCacheWorker(manifest, project_data, ...)
        worker.progress_changed.connect(...)
        worker.finished_ok.connect(...)
        worker.failed.connect(...)
        worker.start()

    Signals:
        progress_changed(cache_id, done_frames, total_frames)
        finished_ok(cache_id, final_path)
        failed(cache_id, error_message)
    """

    progress_changed = pyqtSignal(str, int, int)   # cache_id, done, total
    finished_ok = pyqtSignal(str, str)              # cache_id, final_path
    failed = pyqtSignal(str, str)                   # cache_id, error

    def __init__(
        self,
        manifest: RenderCacheManifest,
        edit_project: Any,
        timeline_sequences: dict,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._manifest = manifest
        self._edit_project = edit_project
        self._timeline_sequences = timeline_sequences
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation of the render job."""
        self._cancelled = True

    def run(self) -> None:
        """Main render loop — runs in background thread."""
        cache_id = self._manifest.cache_id
        start_frame = self._manifest.start_frame
        duration = self._manifest.duration_frames

        if duration <= 0:
            self.failed.emit(cache_id, "Invalid duration: 0 frames")
            return

        encoder: RenderCacheEncoder | None = None
        cpu_composite_renderer: _CpuCompClipRenderer | None = None

        # Initialization (fps parsing, cache directory creation, encoder
        # construction) is itself guarded: the controller only clears its
        # inflight ``(timeline_id, clip_id)`` bookkeeping on the
        # ``failed``/``finished_ok`` terminal signals, so an exception that
        # escapes ``run()`` before the main ``try`` would keep answering
        # "already rendering" for this clip until the session restarts
        # (AUD-17700).
        try:
            # Parse FPS for encoder
            fps_num, fps_den = self._parse_fps(self._manifest.timeline_fps)

            # Build encoder config
            output_path = self._manifest.path
            if not os.path.isabs(output_path):
                # Legacy manifests used ``Cache/...``.  New manifests normally
                # carry an absolute path resolved from Preferences, but
                # retaining this fallback keeps old projects renderable.
                output_path = os.path.join("Cache", os.path.basename(output_path))
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            # Keep the real container suffix on the temporary file.  PyAV uses
            # the filename suffix to select MP4 vs MOV; ``.mp4.tmp`` would
            # make H.264 caches lose their container hint.
            extension = os.path.splitext(output_path)[1] or ".mov"
            temp_path = output_path + ".tmp" + extension

            encoder_config = RenderCacheEncoderConfig(
                output_path=output_path,
                temp_path=temp_path,
                width=self._manifest.width,
                height=self._manifest.height,
                fps_num=fps_num,
                fps_den=fps_den,
                codec=self._manifest.codec,
            )

            encoder = RenderCacheEncoder(encoder_config)
        except Exception as e:
            logger.error("Render cache worker init error: %s", e, exc_info=True)
            self.failed.emit(cache_id, f"Render cache init failed: {e}")
            return

        assert encoder is not None

        try:
            # HTML Motion Graphics use the same persistent-page recorder as
            # final realtime export.  The old cache path called
            # TimelineSceneFrameProvider once per frame, which was frame
            # accurate but paid a browser-capture round trip for every frame
            # and therefore made a 15-second clip take many times its length.
            # Keep the deterministic encoder for non-HTML clips; they have no
            # browser surface to record.
            if self._manifest.render_kind == "motion_graphic":
                scene = self._motion_graphic_scene()
                if scene is None:
                    raise RuntimeError(
                        f"Motion Graphic scene not found: {self._manifest.scene_id}"
                    )
                if getattr(scene, "html_layer", None) is None:
                    # Do not fall through to RenderCacheEncoder here.  That
                    # encoder is intentionally for ordinary source/Comp
                    # clips and its unknown-codec fallback is ProRes Proxy.
                    # A Motion Graphic cache must never silently become a
                    # second ProRes transcode; it is either recorded directly
                    # from the HTML page as VP8/WebM or reported as unsupported.
                    raise RuntimeError(
                        "Motion Graphic cache requires an HTML layer for direct VP8/WebM recording"
                    )
                self._render_motion_graphic_recording(scene, output_path)
                return

            # Open encoder
            encoder.open()

            if self._manifest.render_kind == "motion_graphic":
                self._render_motion_graphic_frames(encoder)
                if self._cancelled:
                    return
                success = encoder.close()
                if success:
                    self.finished_ok.emit(cache_id, output_path)
                    logger.info(
                        "Render cache completed: %s (%d motion-graphic frames)",
                        cache_id[:8], duration,
                    )
                else:
                    self.failed.emit(cache_id, "Failed to finalize cache file")
                return

            # Get graph data
            graph_id = self._manifest.composite_id
            graph_data = self._edit_project.compositions.get(graph_id)
            if graph_data is None:
                encoder.abort()
                self.failed.emit(cache_id, f"Graph not found: {graph_id}")
                return

            # Background cache rendering must not create a ModernGL context.
            # The CPU Comp Clip renderer is headless-safe and handles the
            # timeline-backed default graph plus empty constant graphs.
            cpu_composite_renderer = _CpuCompClipRenderer(
                manifest=self._manifest,
                edit_project=self._edit_project,
                timeline_sequences=self._timeline_sequences,
                graph_data=graph_data,
            )

            # Render each frame
            for i in range(duration):
                if self._cancelled:
                    encoder.abort()
                    logger.info("Render cache cancelled: %s", cache_id)
                    return

                timeline_frame = start_frame + i

                # Render frame through the headless CPU graph bridge.
                result = cpu_composite_renderer.render_frame(timeline_frame)

                if result is None:
                    self.failed.emit(cache_id, f"Frame {timeline_frame} render failed")
                    encoder.abort()
                    return

                # A GPU/Topos-aware graph may return an already decoded planar
                # frame.  Keep it planar all the way into ToposVideoEncoder;
                # only the legacy headless numpy compositor uses packed BGR.
                if isinstance(result, DecodedFrame):
                    if (int(result.width) != int(self._manifest.width)
                            or int(result.height) != int(self._manifest.height)):
                        raise RuntimeError(
                            "Planar render-cache frame geometry does not match "
                            f"manifest: {result.width}x{result.height} != "
                            f"{self._manifest.width}x{self._manifest.height}"
                        )
                    encoder.write_frame_planar(result)
                    self.progress_changed.emit(cache_id, i + 1, duration)
                    continue

                # Convert float32 [0-1] → uint8 [0-255] BGR
                if result.dtype in (np.float32, np.float64):
                    frame_u8 = np.clip(result * 255.0, 0, 255).astype(np.uint8)
                else:
                    frame_u8 = result

                # Resize if needed (encoder expects specific dimensions)
                if frame_u8.shape[0] != self._manifest.height or \
                   frame_u8.shape[1] != self._manifest.width:
                    frame_u8 = self._resize_frame(
                        frame_u8, self._manifest.width, self._manifest.height
                    )

                encoder.write_frame_bgr(frame_u8)

                # Emit progress
                self.progress_changed.emit(cache_id, i + 1, duration)

            # Finalize
            success = encoder.close()
            if success:
                self.finished_ok.emit(cache_id, output_path)
                logger.info(
                    "Render cache completed: %s (%d frames)",
                    cache_id[:8], duration,
                )
            else:
                self.failed.emit(cache_id, "Failed to finalize cache file")

        except Exception as e:
            logger.error("Render cache worker error: %s", e, exc_info=True)
            encoder.abort()
            self.failed.emit(cache_id, str(e))
        finally:
            if cpu_composite_renderer is not None:
                try:
                    cpu_composite_renderer.close()
                except Exception:
                    pass

    def _motion_graphic_scene(self) -> Any | None:
        """Resolve the scene referenced by a Motion Graphic cache manifest."""
        from src.features.teaching_animation.scene_store import get_shared_scene_store

        scene_id = str(getattr(self._manifest, "scene_id", "") or "")
        scene = get_shared_scene_store().get(scene_id) if scene_id else None
        if scene is None:
            raise RuntimeError(f"Teaching scene not found: {scene_id}")
        return scene

    def _motion_graphic_quality(self) -> str:
        """The project's Motion Graphic cache tier (WP-8), with safe defaults."""
        try:
            project = getattr(self, "_project", None) or getattr(self, "_edit_project", None)
            settings = project.get_render_cache_settings()
            return str(getattr(settings, "motion_graphic_quality", "high_quality"))
        except Exception:  # noqa: BLE001 — settings access must never break a render
            return "high_quality"

    def _render_motion_graphic_recording(self, scene: Any, output_path: str) -> None:
        """Record an HTML Motion Graphic once, then atomically install its cache.

        This is deliberately the same browser-recording transport used by
        :class:`TeachingAnimationExportService`: Chromium paints one live page
        at its normal cadence and FFmpeg encodes that stream once.  It avoids
        the previous N independent HTML captures for an N-frame clip.
        """
        from pathlib import Path

        from src.features.teaching_animation.core.contracts import RenderContext
        from src.features.teaching_animation.core.html_fonts import (
            default_font_lock_css,
            wrap_with_font_lock,
        )
        from src.features.teaching_animation.core.html_page_deck import (
            detect_page_count,
            ensure_page_markers,
            page_deck_runtime_html,
            page_timing_for,
            scene_background_css,
        )
        from src.features.teaching_animation.render.html_realtime_recorder import (
            record_html_video,
        )
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            resolve_bound_image_bytes,
        )
        from src.features.teaching_animation.render.html_renderer_spike.security import (
            ensure_font_lock_csp,
        )

        if self._cancelled:
            return
        layer = getattr(scene, "html_layer", None)
        document = ensure_page_markers(str(getattr(layer, "sanitized_html", "") or "").strip())
        if not document:
            raise RuntimeError("real-time HTML cache requires a sanitized HTML layer")
        metadata = getattr(layer, "metadata", {}) or {}
        page_count = detect_page_count(document)
        timing = page_timing_for(
            metadata,
            page_count=page_count,
            fallback_default_seconds=(
                max(0.001, float(getattr(scene, "duration_frames", 1)) / max(0.001, float(scene.fps)))
                / page_count
            ),
        )
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
        csp = str(getattr(layer, "csp", "") or "default-src 'none'")
        if font_css:
            csp = ensure_font_lock_csp(csp)

        # Cache rendering has the same authored canvas geometry as preview and
        # export.  The manifest dimensions are the requested output size; the
        # HTML surface remains 1080p (or the project's configured dimensions).
        context = RenderContext(
            fps=self._manifest_duration_fps(),
            width=max(1, int(getattr(scene, "width", self._manifest.width))),
            height=max(1, int(getattr(scene, "height", self._manifest.height))),
            backend="html",
            asset_root=None,
            transparent=False,
            allow_uniform_frames=True,
        )
        asset_bytes = resolve_bound_image_bytes(scene, context)
        duration_seconds = max(
            0.001,
            float(self._manifest.duration_frames) / max(0.001, self._manifest_duration_fps()),
        )
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        extension = output_path.suffix or ".mp4"
        # Match the deterministic encoder's sidecar naming so the controller's
        # cache cleanup removes interrupted realtime recordings as well.
        temp_path = Path(str(output_path) + ".tmp" + extension)
        try:
            temp_path.unlink(missing_ok=True)
        except TypeError:  # Python 3.8 compatibility
            if temp_path.exists():
                temp_path.unlink()

        codec = str(getattr(self._manifest, "codec", "h264_proxy") or "h264_proxy").lower()
        if codec.startswith("prores") or codec.startswith("dnxhr"):
            delivery = "prores_proxy"
            video_codec = "vp8"
        elif codec in {"vp8", "vp8_proxy", "vp8_high_quality"}:
            # Motion Graphic caches use Chromium's native VP8/WebM delivery.
            # Do not silently substitute H.264 when an old manifest carries a
            # mismatched suffix; the controller will invalidate that manifest.
            delivery = "vp8_high_quality"
            video_codec = "vp8"
        elif codec in {"vp9", "vp9_proxy", "vp9_high_quality"}:
            delivery = "vp9_mp4"
            video_codec = "vp9"
        else:
            delivery = "h264_mp4"
            video_codec = "h264"

        def on_progress(completed: int, total: int) -> bool:
            self.progress_changed.emit(self._manifest.cache_id, int(completed), int(total))
            return bool(self._cancelled)

        result = record_html_video(
            document,
            temp_path,
            width=max(1, int(self._manifest.width)),
            height=max(1, int(self._manifest.height)),
            duration_seconds=duration_seconds,
            fps=self._manifest_duration_fps(),
            csp=csp,
            allowed_asset_ids=frozenset(getattr(layer, "bound_asset_ids", ()) or ()),
            asset_resolver=asset_bytes,
            allow_network_resources=metadata.get("network_access", False) is True,
            allowed_network_hosts=frozenset(metadata.get("network_hosts", ()) or ()),
            allow_inline_visual_resources=metadata.get("allow_inline_visual_resources", False) is True,
            allow_gpu_apis=metadata.get("allow_gpu_apis", False) is True,
            transparent=False,
            progress_callback=on_progress,
            delivery=delivery,
            video_codec=video_codec,
            bitrate_kbps=motion_graphic_bitrate_kbps(
                self._manifest.width,
                self._manifest.height,
                requested_kbps=MOTION_GRAPHIC_CACHE_BITRATE_KBPS,
                preset=self._motion_graphic_quality(),
            ),
            min_bitrate_kbps=motion_graphic_min_bitrate_kbps(
                self._manifest.width,
                self._manifest.height,
                preset=self._motion_graphic_quality(),
            ),
        )
        if result.cancelled or self._cancelled:
            try:
                temp_path.unlink(missing_ok=True)
            except TypeError:
                if temp_path.exists():
                    temp_path.unlink()
            return
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("real-time HTML cache recorder produced no video")
        os.replace(str(temp_path), str(output_path))
        self._record_bitrate_metrics(output_path, duration_seconds)
        self.finished_ok.emit(self._manifest.cache_id, str(output_path))
        logger.info(
            "Realtime HTML render cache completed: %s (%d frames, %.2fs)",
            self._manifest.cache_id[:8], result.frame_count, duration_seconds,
        )

    def _record_bitrate_metrics(self, output_path: Path, duration_seconds: float) -> None:
        """R7: persist target vs. *measured* bitrate beside the cache file.

        The tier constants were previously only ever verified as computed
        values; this sidecar (``<video>.bitrate.json``) records what ffprobe
        actually read back from the delivered file, so acceptance reports and
        cleanup tooling can compare the two.  Best-effort: a missing ffprobe
        must never fail a finished cache job.
        """
        try:
            import json as _json

            from src.features.teaching_animation.render.bitrate_probe import probe_video_metrics
            # Both helpers return kbps (8000 == 8 Mbps); store them as-is.
            target_kbps = motion_graphic_bitrate_kbps(
                self._manifest.width,
                self._manifest.height,
                requested_kbps=MOTION_GRAPHIC_CACHE_BITRATE_KBPS,
                preset=self._motion_graphic_quality(),
            )
            minimum_kbps = motion_graphic_min_bitrate_kbps(
                self._manifest.width,
                self._manifest.height,
                preset=self._motion_graphic_quality(),
            )
            metrics = probe_video_metrics(output_path)
            payload = {
                "cache_id": self._manifest.cache_id,
                "quality_tier": str(self._motion_graphic_quality()),
                "target_bitrate_kbps": int(target_kbps),
                "minimum_bitrate_kbps": int(minimum_kbps),
                "measured": None if metrics is None else {
                    "avg_bitrate_kbps": round(metrics.avg_bitrate_kbps, 1),
                    "format_bitrate_bps": metrics.format_bitrate_bps,
                    "stream_bitrate_bps": metrics.stream_bitrate_bps,
                    "duration_s": round(metrics.duration_s, 4),
                    "nb_frames": metrics.nb_frames,
                    "fps": round(metrics.fps, 4),
                    "codec": metrics.codec,
                    "pixel_format": metrics.pixel_format,
                    "size_bytes": metrics.size_bytes,
                },
            }
            sidecar = Path(str(output_path) + ".bitrate.json")
            sidecar.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 — metrics must never break the cache
            logger.debug("bitrate metrics sidecar skipped", exc_info=True)

    def _manifest_duration_fps(self) -> float:
        """Return the timeline FPS represented by a cache manifest."""
        value = str(getattr(self._manifest, "timeline_fps", "24000/1001") or "24000/1001")
        try:
            if "/" in value:
                numerator, denominator = value.split("/", 1)
                return max(0.001, float(numerator) / float(denominator))
            return max(0.001, float(value))
        except (TypeError, ValueError, ZeroDivisionError):
            return 24.0

    def _render_motion_graphic_frames(self, encoder: RenderCacheEncoder) -> None:
        """Render a Motion Graphic scene into the shared video encoder."""
        from types import SimpleNamespace

        from src.features.teaching_animation.core.contracts import RenderContext
        from src.features.teaching_animation.host_adapters import TimelineSceneFrameProvider
        from src.features.teaching_animation.render.scene_renderer_router import backend_for_scene
        from src.features.teaching_animation.scene_store import get_shared_scene_store

        scene_id = self._manifest.scene_id
        scene = get_shared_scene_store().get(scene_id)
        if scene is None:
            encoder.abort()
            raise RuntimeError(f"Teaching scene not found: {scene_id}")

        provider = TimelineSceneFrameProvider({scene_id: scene})
        clip = SimpleNamespace(
            asset_id=f"teaching_animation:{scene_id}",
            timeline_start=self._manifest.start_frame,
            timeline_end=self._manifest.start_frame + self._manifest.duration_frames,
            source_start=0,
            source_duration=max(1, int(scene.duration_frames)),
        )
        context = RenderContext(
            fps=scene.fps,
            # Keep the browser CSS viewport at the scene's authored size.
            # The cache output may be a proxy size, but shrinking the browser
            # viewport before layout makes absolute HTML designs crop and
            # lose their intended composition.
            width=max(1, int(scene.width)),
            height=max(1, int(scene.height)),
            backend=backend_for_scene(scene),
            transparent=True,
            allow_uniform_frames=backend_for_scene(scene) == "html",
        )
        try:
            for index in range(self._manifest.duration_frames):
                if self._cancelled:
                    encoder.abort()
                    logger.info("Render cache cancelled: %s", self._manifest.cache_id)
                    return

                timeline_frame = self._manifest.start_frame + index
                rgba_frame = provider.render_frame(clip, timeline_frame, context)
                rgba = np.frombuffer(rgba_frame.data, dtype=np.uint8).reshape(
                    rgba_frame.height, rgba_frame.width, 4
                )
                # The cache encoder accepts BGR; discard alpha only after the
                # deterministic renderer has produced the RGBA frame.
                frame_bgr = rgba[:, :, :3][:, :, ::-1].copy()
                if frame_bgr.shape[0] != self._manifest.height or frame_bgr.shape[1] != self._manifest.width:
                    frame_bgr = self._resize_frame(
                        frame_bgr, self._manifest.width, self._manifest.height
                    )
                encoder.write_frame_bgr(frame_bgr)
                self.progress_changed.emit(
                    self._manifest.cache_id, index + 1, self._manifest.duration_frames
                )
        finally:
            provider.close()

    @staticmethod
    def _resize_frame(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
        """Resize frame to target dimensions using simple nearest-neighbor."""
        try:
            import cv2
            return cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
        except ImportError:
            # Fallback: simple numpy resize (crop/pad)
            h, w = frame.shape[:2]
            result = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            ch, cw = min(h, target_h), min(w, target_w)
            result[:ch, :cw] = frame[:ch, :cw]
            return result

    @staticmethod
    def _parse_fps(fps_str: str) -> tuple[int, int]:
        """Parse FPS string like '24000/1001' into (num, den).

        ``timeline_fps`` originates from persisted project data and may be
        ``None`` or a non-string value; coerce before inspecting it so the
        malformed cases fall back to (24, 1) instead of raising
        ``TypeError`` out of ``run()`` (AUD-17700).
        """
        text = str(fps_str) if fps_str is not None else ""
        try:
            if '/' in text:
                parts = text.split('/')
                return int(parts[0]), int(parts[1])
            return int(float(text)), 1
        except (ValueError, IndexError, TypeError):
            return 24, 1
