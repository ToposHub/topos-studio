"""Composite render-output orchestration.

Provides the high-level building blocks for rendering the *current* composite
graph to disk — both single-frame images and frame ranges (video file /
image sequence).

This module sits one layer above the existing render primitives:

    ┌───────────────────────────────────────────────────────────────┐
    │  CompositeRenderOutput (this module)                          │
    │    ├─ detect_composite_mode(graph_data)                       │
    │    ├─ resolve_video_range(graph_data, comp_clip)               │
    │    ├─ render_image(snapshot, scheduler)                        │
    │    ├─ render_video(...)  ── per-frame CompositeFrameRenderer  │
    │    └─ render_image_sequence(...) ── per-frame save_image       │
    └───────────────────────────────────────────────────────────────┘

It reuses, and does NOT duplicate:
    - CompositeExportProvider.render_full_res()  (single image)
    - CompositeFrameRenderer.render_comp_clip_frame()  (per video frame)
    - FFmpegVideoEncoder (PyAV)  (video encoding)
    - FileIO.save_image()  (image / image-sequence)

Threading:
    - render_image() runs synchronously on the calling (main) thread — it
      needs the live Color-page texture via the export provider.
    - render_video() / render_image_sequence() are designed to run on a
      worker thread. CompositeFrameRenderer creates its own GL context
      via moderngl.create_context(), so it is NOT bound to the main
      thread's GL context (mirrors the existing timeline VideoExportWorker
      path which already renders comp-clip frames off the main thread).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, replace as _dc_replace
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.features.composite.core.graph_snapshot import GraphSnapshot
    from src.shared.media.metadata import Rational

logger = logging.getLogger(__name__)


# ── 输出色彩合同 helpers（审计 R-03/R-06）───────────────────────────────


def _rgb_intermediate_descriptor(output_descriptor):
    """RGB 中间值一律 full range（R-03）。

    limited-range 压缩属于 YUV 打包层（encoder 的 swscale
    src=JPEG→dst=MPEG，或 BT.2020 numpy 精确转换）。合成侧先压到
    limited RGB 再交给“按 full 解释”的编码器会二次压缩：默认
    Rec.709 limited 输出实测黑 29/白 217（正确 16/235）。
    """
    from src.shared.color.types import ColorRange
    if getattr(output_descriptor, "range", ColorRange.FULL) == ColorRange.FULL:
        return output_descriptor
    return _dc_replace(output_descriptor, range=ColorRange.FULL)


def _pix_fmt_bit_depth(pix_fmt: str) -> int:
    """从目标 pix_fmt 解析位深（yuv420p10le → 10；R-06）。"""
    m = re.search(r'p(\d+)(?:le|be)?\Z', (pix_fmt or "").lower())
    return int(m.group(1)) if m else 8


def _pack_encoded_rgb(frame_enc: np.ndarray, high_depth: bool):
    """把已编码 [0,1] RGB float 打包成编码器输入（R-06）。

    高位深（HDR 或 10/12-bit pix_fmt）用 uint16/bgr48le 满量程打包，
    swscale 自行降到目标位深；8-bit 保持 uint8/bgr24。wide-gamut SDR
    虽非 HDR，自动升级 yuv420p10le 后同样必须走 16-bit 打包——否则
    提交前已量化成 8-bit，10-bit 编码器无法恢复色阶。
    """
    if high_depth:
        return (np.clip(frame_enc * 65535.0, 0, 65535).astype(np.uint16),
                "bgr48le")
    return (np.clip(frame_enc * 255.0, 0, 255).astype(np.uint8), "bgr24")


# ── Mode detection ────────────────────────────────────────────────────

# Node types that make a composite time-dependent ("video"). The presence of
# any of these (or any keyframed parameter) classifies the graph as a video.
_TIME_DEPENDENT_NODE_TYPES = frozenset({
    "video_frame",
    "timeline_clip_input",
    "time_offset",
    "retime",
    "frame_hold",
})


class CompositeMode(Enum):
    """Whether a composite graph is a still image or a time-based video."""

    IMAGE = "image"
    VIDEO = "video"


def detect_composite_mode(graph_data: Dict[str, Any]) -> CompositeMode:
    """Classify a composite graph as IMAGE or VIDEO.

    A graph is VIDEO when it contains any time-dependent source/manipulation
    node, or any node parameter that carries keyframes/animation. Otherwise
    it is a single-frame IMAGE.

    Args:
        graph_data: CompositeGraphData.to_dict() output (must contain "nodes").

    Returns:
        CompositeMode.IMAGE or CompositeMode.VIDEO.
    """
    nodes = graph_data.get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue

        # Time-dependent node types
        if node.get("type") in _TIME_DEPENDENT_NODE_TYPES:
            return CompositeMode.VIDEO

        # Animated (keyframed) parameters. Both the per-node parameter dict
        # (legacy) and the graph-level keyframe_tracks store animation.
        parameters = node.get("parameters")
        if isinstance(parameters, dict):
            for value in parameters.values():
                if isinstance(value, dict) and (
                    value.get("animated") or value.get("keyframes")
                ):
                    return CompositeMode.VIDEO

        # P1: per-node keyframe tracks (authoritative node-instance store).
        # A graph with any animated parameter is a video, even without a
        # video_frame / timeline source node.
        node_kfs = node.get("keyframe_tracks")
        if isinstance(node_kfs, dict):
            for _pname, track in node_kfs.items():
                if isinstance(track, dict) and track.get("keyframes"):
                    return CompositeMode.VIDEO

        # P1: a node-level time_range also implies a time-dependent graph.
        tr = node.get("time_range")
        if isinstance(tr, dict) and tr.get("enabled", True):
            return CompositeMode.VIDEO

    # Graph-level keyframe tracks (authoritative source for some graphs)
    keyframe_tracks = graph_data.get("keyframe_tracks")
    if isinstance(keyframe_tracks, dict) and keyframe_tracks:
        return CompositeMode.VIDEO

    return CompositeMode.IMAGE


def resolve_video_range(
    graph_data: Dict[str, Any],
    comp_clip: Any = None,
) -> Tuple[int, int, float]:
    """Resolve the default frame range and fps for a video render.

    Precedence:
        1. The backing Comp Clip's duration_frames / fps (most reliable for
           compositions created via "New Comp Clip").
        2. A video_frame node's total_frames / video_fps (for compositions
           whose primary source is a video file).
        3. Fallback: start=0, end=0, fps=24.0 (single still frame).

    Args:
        graph_data: CompositeGraphData.to_dict() output.
        comp_clip: Optional CompClipData (or dict with fps/duration_frames).

    Returns:
        (start_frame, end_frame, fps). ``end_frame`` is inclusive; a still
        (no duration) yields (0, 0, fps).
    """
    # 1. Comp Clip metadata
    clip_fps: Optional[float] = None
    clip_duration: Optional[int] = None
    if comp_clip is not None:
        clip_fps = _attr(comp_clip, "fps")
        clip_duration = _attr(comp_clip, "duration_frames")
        # Dict form (e.g. when passed raw metadata)
        if clip_fps is None and isinstance(comp_clip, dict):
            clip_fps = comp_clip.get("fps")
        if clip_duration is None and isinstance(comp_clip, dict):
            clip_duration = comp_clip.get("duration_frames")

    # Graph-level metadata (CompositeGraphData fields also live on the dict)
    if clip_fps is None:
        clip_fps = graph_data.get("fps")
    if clip_duration is None:
        clip_duration = graph_data.get("duration_frames")

    # 2. Fall back to video_frame node metadata
    node_fps: Optional[float] = None
    node_duration: Optional[int] = None
    for node in graph_data.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "video_frame":
            continue
        params = node.get("parameters") or {}
        if node_duration is None:
            total = params.get("total_frames")
            if isinstance(total, (int, float)) and total > 0:
                node_duration = int(total)
        if node_fps is None:
            vfps = params.get("video_fps")
            if isinstance(vfps, (int, float)) and vfps > 0:
                node_fps = float(vfps)

    fps = clip_fps or node_fps or 24.0
    duration = clip_duration or node_duration or 0

    # P1 (A-10): if no clip/video duration is known, derive a range from the
    # furthest-reaching per-node keyframe track or time_range. An independent
    # composite with animation but no video source still needs a non-trivial
    # export range.
    if not duration:
        kf_end = _max_keyframe_or_range_end(graph_data)
        if kf_end is not None and kf_end > 0:
            duration = kf_end + 1

    if duration and duration > 0:
        return 0, int(duration) - 1, float(fps)
    return 0, 0, float(fps)


def _max_keyframe_or_range_end(graph_data: Dict[str, Any]) -> Optional[int]:
    """Return the latest frame referenced by any node keyframe/time_range.

    Used by resolve_video_range() as a last-resort duration source for
    independent composites that have animation but no video/clip metadata.
    Returns None when nothing time-related is present.
    """
    end: Optional[int] = None
    for node in graph_data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        # per-node keyframe tracks
        kfs = node.get("keyframe_tracks")
        if isinstance(kfs, dict):
            for _pname, track in kfs.items():
                if not isinstance(track, dict):
                    continue
                for kf in track.get("keyframes") or []:
                    if isinstance(kf, dict):
                        f = kf.get("frame")
                        if isinstance(f, (int, float)):
                            end = int(f) if end is None else max(end, int(f))
        # per-node time range
        tr = node.get("time_range")
        if isinstance(tr, dict):
            for key in ("comp_end", "source_out", "end"):
                v = tr.get(key)
                if isinstance(v, (int, float)):
                    end = int(v) if end is None else max(end, int(v))
    return end


def _attr(obj: Any, name: str) -> Any:
    """getattr that returns None on failure (covers dataclasses & dicts)."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


# ── Render result ─────────────────────────────────────────────────────


@dataclass
class RenderOutputResult:
    """Outcome of a render-output operation.

    R-08/R-09/R-10：``status`` 区分 complete/cancelled/partial/failed，
    ``success`` 只在 complete 时为 True——部分帧已写、中途取消或全部
    渲染失败不再被当作成功交付（错误成果可能被直接交给用户）。
    保留 ``success``/``error`` 字段兼容既有消费者；progress/UI 可按
    status 呈现续传/重试选项。
    """

    # Terminal outcome categories (R-08).
    STATUS_COMPLETE = "complete"
    STATUS_CANCELLED = "cancelled"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"

    success: bool
    output_path: str = ""
    frames_written: int = 0
    error: str = ""
    # AUD-81004: video frames that rendered None and were substituted with
    # the previous (or a black placeholder) frame to keep pts/duration
    # aligned with the requested frame range.
    frames_dropped: int = 0
    # R-08: complete / cancelled / partial / failed. cancelled = 用户中止
    # 且一帧完整帧都没写；partial = 已写部分帧后取消/失败（成果可用于
    # 续传但不得报告成功）；failed = 0 成功帧或渲染错误。
    status: str = STATUS_COMPLETE


# ── Orchestration ─────────────────────────────────────────────────────


class CompositeRenderOutput:
    """High-level facade for rendering the current composite to disk.

    Instantiated per export operation; safe to drop afterwards.
    """

    def __init__(self, config_manager: Any = None) -> None:
        self._frame_renderer: Any = None  # CompositeFrameRenderer (lazy)
        # config_manager selects the color engine (builtin vs OCIO) for every
        # transform in the render path. If None, get_color_provider() defaults
        # to OCIO (legacy behaviour). Passed in from the controller so the
        # export honours composite.color_engine instead of always using OCIO.
        self._config_manager = config_manager
        # ── Parallel-stage renderer registry (AUD-81003) ──
        # Renderers are keyed by (stage_generation, worker_index): a stuck
        # worker from an earlier stage must never pop a later stage's
        # renderer by colliding on a bare index. All dict access is
        # guarded by _parallel_renderers_lock because worker threads
        # (teardown) and the caller thread (safety net) touch it.
        self._parallel_renderers: Dict[Tuple[int, int], Any] = {}
        # Renderers whose worker outlived the stage join — their GL cleanup
        # stays pending until the owning worker thread finally tears down.
        self._abandoned_parallel_renderers: Dict[Tuple[int, int], Any] = {}
        self._parallel_render_generation: int = 0
        self._parallel_renderers_lock = threading.Lock()
        # Diagnostics: renders currently executing per renderer.
        self._parallel_in_flight: Dict[Tuple[int, int], int] = {}

    # ── Image ────────────────────────────────────────────────────────

    def render_image(
        self,
        snapshot: "GraphSnapshot",
        interactive_scheduler: object | None = None,
        render_pass: str = "beauty",
    ) -> Optional[np.ndarray]:
        """Render a single full-resolution image and read back to CPU.

        Runs on the calling (main) thread — needs the live Color-page
        texture bound into the snapshot.

        Args:
            snapshot: Immutable graph snapshot (with color texture uploaded).
            interactive_scheduler: Optional RenderScheduler to seed dirty
                tracking from the live renderer.
            render_pass: Semantic pass to read back from the evaluated graph.

        Returns:
            BGR float32 numpy array (h, w, 3) in [0, 1], or None on failure.
        """
        from src.features.composite.export_provider import CompositeExportProvider

        provider = CompositeExportProvider(self._config_manager)
        result = provider.render_full_res(
            snapshot, interactive_scheduler, render_pass=render_pass)
        if result.error:
            logger.error(f"Composite image render failed: {result.error}")
            return None
        return result.image

    # ── Video ────────────────────────────────────────────────────────

    def render_video(
        self,
        graph_data: Dict[str, Any],
        output_path: str,
        width: int,
        height: int,
        fps: float,
        start_frame: int,
        end_frame: int,
        codec: str = "h264",
        pix_fmt: str = "yuv420p",
        crf: Optional[int] = None,
        preset: Optional[str] = None,
        output_descriptor: Any = None,
        edit_project: Any = None,
        timeline_sequences: Optional[Dict[str, Any]] = None,
        comp_clip: Any = None,
        render_pass: str = "beauty",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        parallel_frames: Optional[int] = None,
    ) -> RenderOutputResult:
        """Render a frame range to a single video file.

        Designed to run on a worker thread. Each frame is evaluated with
        CompositeFrameRenderer (which builds its own GL context) and fed to
        FFmpegVideoEncoder (PyAV).

        Args:
            graph_data: CompositeGraphData.to_dict() — the graph to render.
            output_path: Destination video file (.mp4/.mov/.mkv/...).
            width, height: Output resolution (from Output node).
            fps: Frame rate.
            start_frame, end_frame: Inclusive frame range.
            codec: Encoder key (h264/hevc/prores/vp9/...).
            pix_fmt: Pixel format (yuv420p/yuv444p10le/...).
            crf: Quality (H.264/HEVC).
            preset: Speed preset (H.264/HEVC).
            output_descriptor: ColorSpaceDescriptor for the output color space
                (primaries + transfer + range). Frames are encoded from
                scene-linear to this space, and the stream is tagged to match.
                Defaults to REC709_VIDEO (standard SDR video).
            edit_project: EditProject (needed for timeline-source comp clips).
            timeline_sequences: Timeline dict (for timeline-source comp clips).
            comp_clip: Backing CompClipData (for timeline context resolution).
            render_pass: Semantic pass to encode. Auxiliary passes are intended
                for image-sequence/EXR delivery; video export accepts beauty
                only because transfer encoding would corrupt ID semantics.
            progress_cb: (current, total) callback.
            is_cancelled: () -> bool; when True the loop stops at next frame.

        Returns:
            RenderOutputResult.
        """
        from src.shared.export.video_encoder import (
            FFmpegVideoEncoder,
            FFmpegEncoderConfig,
            ColorMetadataConfig,
        )
        from src.shared.media.frame import DecodedFrame
        from src.shared.color.color_space_descriptor import (
            REC709_VIDEO, LINEAR_SCENE,
        )
        from src.shared.color.types import ColorPrimaries, ColorTransfer
        # AUD-81008: transform_color 提升到函数头，不再逐帧 import
        from src.shared.color.color_transform_provider import transform_color

        total = max(0, end_frame - start_frame + 1)
        if total <= 0:
            return RenderOutputResult(success=False, error="Empty frame range")
        if str(render_pass or "beauty") != "beauty":
            return RenderOutputResult(
                success=False,
                error="Auxiliary render passes require an image sequence or EXR output",
            )

        if output_descriptor is None:
            output_descriptor = REC709_VIDEO

        # Detect HDR output (PQ/HLG transfer). HDR requires a 10-bit pipeline
        # and carries ST 2086/MaxCLL static metadata.
        is_hdr = output_descriptor.transfer in (ColorTransfer.PQ, ColorTransfer.HLG)
        # Wide-gamut SDR (BT.2020 / Display-P3 primaries with an SDR transfer)
        # also benefits from 10-bit to avoid banding in saturated gradients.
        is_wide_gamut = output_descriptor.primaries in (
            ColorPrimaries.BT2020, ColorPrimaries.P3_DISPLAY, ColorPrimaries.P3_DCI,
        )
        if is_hdr and pix_fmt == "yuv420p":
            # Auto-upgrade to 10-bit for HDR; 8-bit PQ/HLG is invalid.
            pix_fmt = "yuv420p10le"
        elif is_wide_gamut and not is_hdr and pix_fmt == "yuv420p":
            # Auto-upgrade wide-gamut SDR to 10-bit to preserve the extra
            # gamut without posterising in 8-bit YUV.
            pix_fmt = "yuv420p10le"

        fps_rational = _fps_to_rational(fps)
        # Build encoder color metadata from the output descriptor so the
        # stream is tagged with the correct primaries/transfer/matrix/range
        # (matches the pixel encoding below).
        # R-07：元数据一律从 descriptor 构造（像素与标签同源）；只有标准
        # HDR10（BT.2020+PQ）附加 ST 2086/MaxCLL static metadata——
        # P3+PQ / full-range HDR 等自定义组合不再被固定 hdr10()/hlg()
        # 标签覆盖（ mastering/MaxCLL 数据无可靠来源时不编造）。
        matrix = ("bt2020nc" if output_descriptor.primaries == ColorPrimaries.BT2020
                  else "bt709")
        descriptor_metadata = ColorMetadataConfig(
            primaries=output_descriptor.primaries.value,
            transfer=output_descriptor.transfer.value,
            matrix=matrix,
            range=output_descriptor.range.value,
        )
        if (is_hdr
                and output_descriptor.transfer == ColorTransfer.PQ
                and output_descriptor.primaries == ColorPrimaries.BT2020):
            hdr_preset = ColorMetadataConfig.hdr10()
            color_metadata = ColorMetadataConfig(
                primaries=descriptor_metadata.primaries,
                transfer=descriptor_metadata.transfer,
                matrix=descriptor_metadata.matrix,
                range=descriptor_metadata.range,
                mastering_display=hdr_preset.mastering_display,
                max_cll=hdr_preset.max_cll,
                max_fall=hdr_preset.max_fall,
            )
        else:
            color_metadata = descriptor_metadata
        # R-06：位深由选定 pix_fmt 决定（HDR 只是升级触发条件之一）；
        # wide-gamut SDR 自动升级 10-bit 后同样走 16-bit 打包。
        high_depth_input = is_hdr or _pix_fmt_bit_depth(pix_fmt) >= 10
        # R-03：RGB 中间值 full range——limited 压缩属于 YUV 打包层。
        rgb_output_descriptor = _rgb_intermediate_descriptor(output_descriptor)
        enc_config = FFmpegEncoderConfig(
            codec=codec,
            width=width,
            height=height,
            fps=fps_rational,
            pix_fmt=pix_fmt,
            crf=crf,
            preset=preset,
            color_metadata=color_metadata,
        )
        encoder = FFmpegVideoEncoder(output_path, enc_config)

        try:
            encoder.open()
        except Exception as e:
            logger.error(f"Video encoder open failed: {e}")
            return RenderOutputResult(success=False, error=str(e))

        written = 0
        parallel_n = self._resolve_parallel_frames(parallel_frames)
        if parallel_n >= 2:
            stage = self._run_parallel_video_stage(
                graph_data=graph_data,
                encoder=encoder,
                frames=list(range(start_frame, end_frame + 1)),
                start_frame=start_frame,
                fps=fps,
                is_hdr=is_hdr,
                output_descriptor=output_descriptor,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
                comp_clip=comp_clip,
                render_pass=render_pass,
                worker_count=parallel_n,
                is_cancelled=is_cancelled,
                progress_cb=progress_cb,
                frame_width=width,
                frame_height=height,
                output_path=output_path,
                high_depth_input=high_depth_input,
                rgb_output_descriptor=rgb_output_descriptor,
            )
            if stage is not None:
                return stage
        try:
            # AUD-81004: frames that render None are substituted with the
            # previous encoded frame (black placeholder for the first frame)
            # instead of being skipped: a skip would compress every later
            # frame's pts and shorten the output, desyncing video from
            # audio/timecode. The substitution count is reported as
            # frames_dropped on the result.
            dropped = 0
            last_packed: Optional[Tuple[np.ndarray, str]] = None
            for frame_no in range(start_frame, end_frame + 1):
                if is_cancelled is not None and is_cancelled():
                    logger.info("Video render cancelled by user")
                    break

                frame_img = self._render_one_frame(
                    graph_data=graph_data,
                    timeline_frame=frame_no,
                    edit_project=edit_project,
                    timeline_sequences=timeline_sequences,
                    comp_clip=comp_clip,
                    render_pass=render_pass,
                )
                if frame_img is None:
                    dropped += 1
                    logger.warning(
                        f"Frame {frame_no} rendered None — duplicating the "
                        f"previous frame to keep the output timeline "
                        f"(dropped so far: {dropped})"
                    )
                    if last_packed is not None:
                        frame_out, pixel_format = last_packed
                    else:
                        frame_out, pixel_format = _pack_encoded_rgb(
                            np.zeros((height, width, 3), dtype=np.float32),
                            high_depth_input)
                        last_packed = (frame_out, pixel_format)
                    decoded = DecodedFrame(
                        frame_index=written,
                        pts=written,
                        time_seconds=written / fps if fps > 0 else 0.0,
                        data=frame_out,
                        pixel_format=pixel_format,
                        width=frame_out.shape[1],
                        height=frame_out.shape[0],
                    )
                    encoder.encode_frame(decoded)
                    written += 1
                    if progress_cb is not None:
                        progress_cb(written, total)
                    continue

                # CompositeFrameRenderer yields BGR float32 [0,1] in
                # scene-linear. Encode to the output color space (transfer +
                # primaries) so the video matches the target. The color
                # transform operates in RGB semantics on the first 3
                # channels, so flip BGR→RGB before the transform and back to
                # BGR afterwards — the encoder consumes BGR (bgr48le/bgr24).
                # R-03: the RGB intermediate stays FULL range; legal-range
                # compression happens exactly once in the encoder's YUV
                # packing layer. R-06: packing depth follows the selected
                # pix_fmt (10/12-bit upgrades incl. wide-gamut SDR use
                # uint16/bgr48le), not just the HDR flag.
                rgb_linear = np.ascontiguousarray(frame_img[..., ::-1])
                frame_enc_rgb = transform_color(
                    rgb_linear, LINEAR_SCENE, rgb_output_descriptor,
                    config_manager=self._config_manager,
                )
                frame_enc = np.ascontiguousarray(frame_enc_rgb[..., ::-1])  # RGB → BGR
                frame_out, pixel_format = _pack_encoded_rgb(
                    frame_enc, high_depth_input)
                last_packed = (frame_out, pixel_format)

                decoded = DecodedFrame(
                    frame_index=written,
                    pts=written,
                    time_seconds=written / fps if fps > 0 else 0.0,
                    data=frame_out,
                    pixel_format=pixel_format,
                    width=frame_out.shape[1],
                    height=frame_out.shape[0],
                )
                encoder.encode_frame(decoded)
                written += 1
                if progress_cb is not None:
                    progress_cb(written, total)

            encoder.close()
            # R-08/R-09：结果状态合同——complete 必须 written==total 且无
            # 丢帧；已写部分帧后取消是 partial（不清理已写成果、供续传，
            # 但绝不报告成功）；0 成功帧（全部 None/失败）是 failed，
            # 不完整的全黑视频不得被当作成功交付。
            cancel_requested = bool(
                is_cancelled is not None and is_cancelled())
            total = end_frame - start_frame + 1
            if cancel_requested and written == 0:
                status = RenderOutputResult.STATUS_CANCELLED
            elif cancel_requested or dropped >= total or written < total:
                status = RenderOutputResult.STATUS_PARTIAL
                if dropped >= total or written == 0:
                    status = RenderOutputResult.STATUS_FAILED
            else:
                status = RenderOutputResult.STATUS_COMPLETE
            success = status == RenderOutputResult.STATUS_COMPLETE
            # Clean up leftover output on cancellation/failure so the user
            # doesn't end up with a truncated/partial file. A fully-rendered
            # file (written > 0) is kept even if cancel arrived mid-stream —
            # but a zero-frame cancel or any failure removes the artifact.
            if status in (RenderOutputResult.STATUS_CANCELLED,
                          RenderOutputResult.STATUS_FAILED):
                self._safe_remove(output_path)
            return RenderOutputResult(
                success=success,
                output_path=output_path if success else "",
                frames_written=written,
                error=("EXPORT_CANCELLED" if cancel_requested else
                       ("All frames failed to render" if status ==
                        RenderOutputResult.STATUS_FAILED else "")),
                frames_dropped=dropped,
                status=status,
            )
        except Exception as e:
            logger.exception("Video render failed")
            try:
                encoder.close()
            except Exception:
                pass
            self._safe_remove(output_path)
            # P1-B06：status 必须显式传——dataclass 默认值是 STATUS_COMPLETE，
            # 漏传会让 success=False 的失败结果带着 status="complete" 出门。
            return RenderOutputResult(success=False, error=str(e),
                                      status=RenderOutputResult.STATUS_FAILED)
        finally:
            self._cleanup_frame_renderer()

    # ── Image sequence ───────────────────────────────────────────────

    def render_image_sequence(
        self,
        graph_data: Dict[str, Any],
        output_dir: str,
        base_name: str,
        ext: str,
        start_frame: int,
        end_frame: int,
        pad_width: int = 4,
        image_options: Optional[Dict[str, Any]] = None,
        output_descriptor: Any = None,
        edit_project: Any = None,
        timeline_sequences: Optional[Dict[str, Any]] = None,
        comp_clip: Any = None,
        render_pass: str = "beauty",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        parallel_frames: Optional[int] = None,
    ) -> RenderOutputResult:
        """Render a frame range to an image sequence on disk.

        Mirrors render_video() but writes each frame as a standalone image
        (e.g. ``comp_0001.png``).

        Args:
            graph_data: CompositeGraphData.to_dict().
            output_dir: Destination directory (created if missing).
            base_name: File name prefix (e.g. "comp").
            ext: Image extension WITHOUT dot (e.g. "png", "jpg").
            start_frame, end_frame: Inclusive frame range.
            pad_width: Zero-padding width for the frame number.
            image_options: Options dict forwarded to FileIO.save_image.
            output_descriptor: ColorSpaceDescriptor for the output colour space.
                When supplied, PNG/JPEG/TIFF frames are encoded from
                scene-linear to this descriptor (transfer + range) instead of
                the legacy hardcoded sRGB, so a Rec.709/Display P3/HDR
                sequence honours the chosen output space. EXR ignores it and
                stays scene-linear. Defaults to None (sRGB, backward compat).
            edit_project / timeline_sequences / comp_clip: timeline context.
            render_pass: Semantic pass to write for every frame.
            progress_cb / is_cancelled: see render_video().

        Returns:
            RenderOutputResult (output_path = output_dir).
        """
        from src.shared.io.file_io import FileIO

        total = max(0, end_frame - start_frame + 1)
        if total <= 0:
            return RenderOutputResult(success=False, error="Empty frame range")

        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            return RenderOutputResult(
                success=False, error=f"Cannot create output dir: {e}"
            )

        ext_norm = ext.lower().lstrip(".")
        written = 0
        written_paths: list = []  # for cleanup on cancel/failure
        parallel_n = self._resolve_parallel_frames(parallel_frames)
        if parallel_n >= 2:
            stage = self._run_parallel_sequence_stage(
                graph_data=graph_data,
                output_dir=output_dir,
                base_name=base_name,
                ext=ext_norm if (ext_norm := ext.lower().lstrip(".")) else "png",
                start_frame=start_frame,
                end_frame=end_frame,
                pad_width=pad_width,
                image_options=image_options,
                output_descriptor=output_descriptor,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
                comp_clip=comp_clip,
                render_pass=render_pass,
                worker_count=parallel_n,
                is_cancelled=is_cancelled,
                progress_cb=progress_cb,
                written_ref=written_paths,
            )
            if stage is not None:
                if stage.success and stage.output_path == "":
                    stage.output_path = output_dir
                return stage
        # R-11：串行图片序列与视频路径同一 finally 清理合同——完成、
        # 取消、写盘失败、异常各出口都释放 GPU/解码资源，不能依赖 GC
        # 或窗口关闭兜底。
        try:
            try:
                render_dropped = 0
                save_failed = 0
                for frame_no in range(start_frame, end_frame + 1):
                    if is_cancelled is not None and is_cancelled():
                        logger.info("Image sequence render cancelled by user")
                        break

                    frame_img = self._render_one_frame(
                        graph_data=graph_data,
                        timeline_frame=frame_no,
                        edit_project=edit_project,
                        timeline_sequences=timeline_sequences,
                        comp_clip=comp_clip,
                        render_pass=render_pass,
                    )
                    if frame_img is None:
                        render_dropped += 1
                        logger.warning(f"Frame {frame_no} rendered None — skipping")
                        if progress_cb is not None:
                            progress_cb(written, total)
                        continue

                    # Convert BGR float32 [0,1] → format-appropriate dtype, encoding
                    # to the output descriptor when supplied (else legacy sRGB).
                    out_img = _convert_for_image_ext(
                        frame_img, ext_norm,
                        output_descriptor=output_descriptor,
                        config_manager=self._config_manager,
                        render_pass=render_pass,
                    )

                    seq_index = frame_no - start_frame
                    fname = f"{base_name}_{str(seq_index + 1).zfill(pad_width)}.{ext_norm}"
                    fpath = os.path.join(output_dir, fname)
                    if not FileIO.save_image(fpath, out_img, options=image_options or {}):
                        # R-10：写盘失败必须计入结果——旧行为 continue 后只要有
                        # 1 张成功帧就报 success，缺帧序列与进度/状态失真。
                        save_failed += 1
                        logger.error(f"Failed to save frame {fpath}")
                        if progress_cb is not None:
                            progress_cb(written, total)
                        continue

                    written += 1
                    written_paths.append(fpath)
                    if progress_cb is not None:
                        progress_cb(written, total)
            except Exception as e:
                # AUD-81002: render_video removes its partial artifact when the
                # render/encode raises; the sequence path previously let the
                # exception propagate to the export worker while every frame
                # written so far stayed on disk. Clean them up and return a
                # failure result instead.
                logger.exception("Image sequence render failed")
                self._remove_written_frames(written_paths, output_dir)
                return RenderOutputResult(
                    success=False, error=str(e),
                    status=RenderOutputResult.STATUS_FAILED)

            # R-08/R-10：complete 必须 written==total；缺帧（渲染 None/写盘
            # 失败/中途取消）是 partial；0 成功帧是 failed。
            cancel_requested = bool(
                is_cancelled is not None and is_cancelled())
            total = end_frame - start_frame + 1
            if cancel_requested and written == 0:
                status = RenderOutputResult.STATUS_CANCELLED
            elif cancel_requested or written < total:
                status = (RenderOutputResult.STATUS_FAILED if written == 0
                          else RenderOutputResult.STATUS_PARTIAL)
            else:
                status = RenderOutputResult.STATUS_COMPLETE
            success = status == RenderOutputResult.STATUS_COMPLETE
            # Clean up partial frames on cancellation/failure so a half-written
            # sequence doesn't linger on disk. PARTIAL frames stay for resume.
            if status in (RenderOutputResult.STATUS_CANCELLED,
                          RenderOutputResult.STATUS_FAILED):
                self._remove_written_frames(written_paths, output_dir)
            if cancel_requested:
                error_text = "EXPORT_CANCELLED"
            elif status == RenderOutputResult.STATUS_FAILED:
                error_text = "All frames failed to render or save"
            elif save_failed:
                error_text = f"{save_failed} frame(s) failed to save"
            else:
                error_text = ""
            return RenderOutputResult(
                success=success,
                output_path=output_dir if success else "",
                frames_written=written,
                error=error_text,
                frames_dropped=render_dropped + save_failed,
                status=status,
            )
        finally:
            self._cleanup_frame_renderer()


    # ── Internals ────────────────────────────────────────────────────

    def _render_one_frame(
        self,
        graph_data: Dict[str, Any],
        timeline_frame: int,
        edit_project: Any,
        timeline_sequences: Optional[Dict[str, Any]],
        comp_clip: Any,
        render_pass: str = "beauty",
    ) -> Optional[np.ndarray]:
        """Render a single frame via CompositeFrameRenderer (BGR float32)."""


        return self._render_one_frame_with(
            self._get_frame_renderer(), graph_data=graph_data,
            timeline_frame=timeline_frame, edit_project=edit_project,
            timeline_sequences=timeline_sequences, comp_clip=comp_clip,
            render_pass=render_pass)

    def _render_one_frame_with(
        self,
        renderer: Any,
        graph_data: Dict[str, Any],
        timeline_frame: int,
        edit_project: Any,
        timeline_sequences: Optional[Dict[str, Any]],
        comp_clip: Any,
        render_pass: str = "beauty",
    ) -> Optional[np.ndarray]:
        """_render_one_frame against an explicit renderer (A3 parallel: each
        worker thread owns one CompositeFrameRenderer / GL context)."""
        timeline_id = (
            graph_data.get("timeline_id")
            or graph_data.get("composite_id")
            or ""
        )
        clip_id = (
            graph_data.get("clip_id")
            or graph_data.get("source_clip_id")
            or (_attr(comp_clip, "clip_id") if comp_clip is not None else None)
            or ""
        )
        asset_id = (
            graph_data.get("source_asset_id")
            or (_attr(comp_clip, "asset_id") if comp_clip is not None else None)
            or ""
        )

        return renderer.render_comp_clip_frame(
            graph_data=graph_data,
            timeline_id=timeline_id,
            clip_id=clip_id,
            asset_id=asset_id,
            edit_project=edit_project,
            timeline_sequences=timeline_sequences,
            timeline_frame=timeline_frame,
            source_frame=timeline_frame,
            quality="export",
            render_pass=render_pass,
        )

    # ── A3: frame-parallel export stage ──────────────────────────────

    def _resolve_parallel_frames(self, parallel_frames: Optional[int]) -> int:
        """Effective worker count: explicit arg > preference > default."""
        from src.features.composite.preferences import (
            EXPORT_PARALLEL_FRAMES_DEFAULT)
        from src.features.composite.render.parallel_frame_render import (
            resolve_parallel_workers)

        if parallel_frames is None:
            parallel_frames = EXPORT_PARALLEL_FRAMES_DEFAULT
            try:
                cm = self._config_manager
                if cm is not None and hasattr(cm, "get"):
                    parallel_frames = int(
                        cm.get("composite.export_parallel_frames")
                        or EXPORT_PARALLEL_FRAMES_DEFAULT)
            except Exception:
                parallel_frames = EXPORT_PARALLEL_FRAMES_DEFAULT
        return resolve_parallel_workers(
            parallel_frames, os.cpu_count() or 1)

    def _make_parallel_render_fn(self, index: int, graph_data: Dict[str, Any],
                                 edit_project: Any,
                                 timeline_sequences: Optional[Dict[str, Any]],
                                 comp_clip: Any, render_pass: str):
        """Build the per-thread render function; owns a GL context.

        The GL context is created EAGERLY here (this factory runs on the
        worker thread) so a context-creation failure surfaces as a factory
        failure → serial fallback, not a dead export.
        """
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer)

        renderer = CompositeFrameRenderer(config_manager=self._config_manager)
        try:
            renderer.warm_up()
        except Exception:
            # warm_up may fail AFTER the GL context was created (e.g. a
            # shader compile error) — release it here or every failed
            # export leaks one context.
            try:
                renderer.cleanup()
            except Exception:
                logger.debug("Leaked-renderer cleanup failed", exc_info=True)
            raise
        # AUD-81003: register under (stage_generation, index) so a worker
        # that outlives its stage can never pop a LATER stage's renderer
        # through a bare-index collision.
        reg_key = (self._parallel_render_generation, index)
        with self._parallel_renderers_lock:
            self._parallel_renderers[reg_key] = renderer
            self._parallel_in_flight[reg_key] = 0

        def _render(frame_no: int) -> Optional[np.ndarray]:
            # Diagnostics for the caller-side safety net: knowing whether a
            # leftover renderer is mid-render makes the leak report useful.
            with self._parallel_renderers_lock:
                self._parallel_in_flight[reg_key] = (
                    self._parallel_in_flight.get(reg_key, 0) + 1)
            try:
                return self._render_one_frame_with(
                    renderer, graph_data=graph_data, timeline_frame=frame_no,
                    edit_project=edit_project,
                    timeline_sequences=timeline_sequences, comp_clip=comp_clip,
                    render_pass=render_pass)
            finally:
                with self._parallel_renderers_lock:
                    self._parallel_in_flight[reg_key] = max(
                        0, self._parallel_in_flight.get(reg_key, 1) - 1)

        return _render

    def _teardown_parallel_renderer(self, generation: int, index: int) -> None:
        """Destroy the worker's renderer ON THE WORKER'S OWN THREAD.

        GL resources are thread-affine: releasing them from the caller
        thread is either a no-op (leak) or hits whatever context happens
        to be current there. Runs from the worker's finally-block via
        render_frames_parallel's teardown_fn; also claims renderers the
        safety net parked as abandoned (their worker arrived late).
        """
        key = (generation, index)
        with self._parallel_renderers_lock:
            renderer = self._parallel_renderers.pop(key, None)
            if renderer is None:
                renderer = self._abandoned_parallel_renderers.pop(key, None)
            self._parallel_in_flight.pop(key, None)
        if renderer is not None:
            try:
                renderer.cleanup()
            except Exception as e:
                logger.debug(f"Parallel renderer cleanup error: {e}")

    def _cleanup_parallel_renderers(self) -> None:
        """Safety net for renderers whose worker never tore down (AUD-81003).

        render_frames_parallel() joins workers with a 5 s timeout; a heavy
        4K frame can outlive that, and its renderer may STILL be in use on
        the worker thread. GL cleanup is thread-affine, so this net never
        destroys anything itself: leftover renderers are moved to the
        abandoned registry, reported, and left for the owning worker's
        finally-block teardown (which pops them there and cleans up on the
        right thread). Callers may also reclaim them via cleanup() later.
        """
        with self._parallel_renderers_lock:
            leftovers = dict(self._parallel_renderers)
            self._parallel_renderers.clear()
            in_flight = {key: self._parallel_in_flight.get(key, 0)
                         for key in leftovers}
            if leftovers:
                self._abandoned_parallel_renderers.update(leftovers)
        for (gen, index) in sorted(leftovers):
            logger.warning(
                "Parallel renderer (stage %d, worker %d) still registered "
                "after the stage joined its workers (in-flight renders: "
                "%d); GL teardown stays with the owning worker thread — "
                "no cross-thread cleanup attempted", gen, index,
                in_flight.get((gen, index), 0))

    def _run_parallel_video_stage(
        self, graph_data, encoder, frames, start_frame, fps, is_hdr,
        output_descriptor, edit_project, timeline_sequences, comp_clip,
        render_pass, worker_count, is_cancelled, progress_cb,
        frame_width: int, frame_height: int,
        output_path: str = "",
        high_depth_input: bool = False,
        rgb_output_descriptor=None,
    ):
        """Render+convert frames in parallel, encode in order.

        Returns:
            RenderOutputResult when the parallel path handled the stage
            (success, error or cancel — the encoder is closed here), or
            None to decline and fall back to the serial loop.
        """
        from src.features.composite.render.parallel_frame_render import (
            render_frames_parallel)
        # DecodedFrame lives in src.shared.media.frame (video_encoder only
        # imports it under TYPE_CHECKING) — the previous import from
        # video_encoder raised ImportError on the first encoded frame, so
        # the parallel video stage could never complete.
        from src.shared.media.frame import DecodedFrame
        from functools import partial
        # AUD-81008: transform_color 提升到 stage 函数头，不再逐帧 import
        from src.shared.color.color_transform_provider import transform_color
        # 基准复现修复（2026-10-14）：_convert 使用的 LINEAR_SCENE 此前只在
        # render_video 的局部作用域导入，并行 stage 一进 _convert 即
        # NameError（任何 parallel_frames>=2 的视频导出都会崩）。
        from src.shared.color.color_space_descriptor import LINEAR_SCENE

        # AUD-81003: each stage gets its own generation so registrations
        # and worker-side teardowns can never collide across stages.
        with self._parallel_renderers_lock:
            self._parallel_render_generation += 1
            stage_generation = self._parallel_render_generation
        written = 0
        dropped = 0
        state = {"factory_failures": 0, "error": None}
        last_packed: Optional[Tuple[np.ndarray, str]] = None

        def _black_placeholder() -> Tuple[np.ndarray, str]:
            """Black frame of the export geometry (AUD-81004 first-frame drop)."""
            return _pack_encoded_rgb(
                np.zeros((frame_height, frame_width, 3), dtype=np.float32),
                high_depth_input)

        def _convert(frame_img):
            # R-03/R-06：与串行路径同一色彩/位深合同（full-range RGB 中间
            # 值 + 按 pix_fmt 位深打包），串并行结果一致。未显式传入时
            # 回退 output_descriptor（保持旧调用兼容）。
            target_descriptor = rgb_output_descriptor or output_descriptor
            rgb_linear = np.ascontiguousarray(frame_img[..., ::-1])
            frame_enc_rgb = transform_color(
                rgb_linear, LINEAR_SCENE, target_descriptor,
                config_manager=self._config_manager,
            )
            frame_enc = np.ascontiguousarray(frame_enc_rgb[..., ::-1])  # RGB → BGR
            return _pack_encoded_rgb(frame_enc, high_depth_input)

        def _render_fn_factory(index: int):
            try:
                render_fn = self._make_parallel_render_fn(
                    index, graph_data, edit_project, timeline_sequences,
                    comp_clip, render_pass)
            except Exception as exc:
                # Counted once via the factory_fallback callback in
                # render_frames_parallel — don't double-count here.
                logger.warning("Parallel renderer %d failed to start: %s",
                               index, exc)
                raise

            def _full(frame_no: int):
                frame_img = render_fn(frame_no)
                if frame_img is None:
                    return None
                return _convert(frame_img)
            return _full

        def _sink(frame_no: int, packed) -> None:
            nonlocal written, dropped, last_packed
            if packed is None:
                # AUD-81004: a None render must not compress the timeline —
                # duplicate the last encoded frame (black placeholder when
                # nothing precedes it) so pts/duration stay aligned with the
                # requested frame numbers; the drop count is reported in
                # the result.
                dropped += 1
                logger.warning(
                    "Frame %d rendered None in parallel video stage — "
                    "duplicating previous frame to keep the timeline "
                    "(dropped so far: %d)", frame_no, dropped)
                packed = last_packed if last_packed is not None \
                    else _black_placeholder()
            else:
                last_packed = packed
            frame_out, pixel_format = packed
            decoded = DecodedFrame(
                frame_index=written,
                pts=written,
                time_seconds=written / fps if fps > 0 else 0.0,
                data=frame_out,
                pixel_format=pixel_format,
                width=frame_out.shape[1],
                height=frame_out.shape[0],
            )
            encoder.encode_frame(decoded)
            written += 1

        try:
            render_frames_parallel(
                frames, _render_fn_factory, _sink, worker_count,
                is_cancelled=is_cancelled,
                progress_cb=lambda cur, tot: progress_cb(cur, tot) if progress_cb else None,
                on_worker_error=lambda m: state.__setitem__("error", m),
                factory_fallback=lambda _e: state.__setitem__(
                    "factory_failures", state["factory_failures"] + 1),
                teardown_fn=partial(self._teardown_parallel_renderer,
                                    stage_generation),
            )
        except BaseException:
            # An exception here (e.g. encoder failure inside _sink) must not
            # leak a truncated artifact — render_video does not wrap the
            # stage call in its own try.
            try:
                encoder.close()
            except Exception:
                pass
            self._cleanup_parallel_renderers()
            self._safe_remove(output_path)
            raise

        # R-08/R-09：与串行路径同一状态合同——complete 必须 written==total
        # 且非全量丢帧；已写部分帧后取消是 partial（保留成果、不报成功）；
        # 0 成功帧/全量丢帧是 failed。AUD-81001 的“保留成果、不误报
        # Render failed”语义由 partial 状态继承。
        cancel_requested = bool(
            is_cancelled is not None and is_cancelled())
        total_frames = len(frames)
        if cancel_requested and written == 0:
            status = RenderOutputResult.STATUS_CANCELLED
        elif cancel_requested or written < total_frames:
            status = (RenderOutputResult.STATUS_FAILED if written == 0
                      else RenderOutputResult.STATUS_PARTIAL)
        elif dropped >= total_frames:
            status = RenderOutputResult.STATUS_FAILED
        else:
            status = RenderOutputResult.STATUS_COMPLETE
        success = status == RenderOutputResult.STATUS_COMPLETE

        if cancel_requested and written > 0:
            try:
                encoder.close()
            finally:
                self._cleanup_parallel_renderers()
            return RenderOutputResult(
                success=False,
                output_path=output_path,
                frames_written=written,
                error="EXPORT_CANCELLED",
                frames_dropped=dropped,
                status=status,
            )
        # Decline → serial fallback only when nothing was written, no cancel
        # was seen and every worker failed to build its GL context. Checked
        # BEFORE closing the encoder — the serial loop reuses this instance
        # and encode_frame() on a closed encoder raises.
        if (written == 0 and state["factory_failures"] > 0
                and not cancel_requested):
            logger.info("Parallel export unavailable (%d factory failures); "
                        "falling back to serial", state["factory_failures"])
            self._cleanup_parallel_renderers()
            return None
        close_error: Exception | None = None
        try:
            encoder.close()
        except Exception as exc:
            # P1-B06 RAISE 复审：close 是 mp4 muxer finalize。成功态下失败
            # 意味着产物可能截断/损坏——不得以 success=True 交付坏文件；
            # 非成功态本来就要删文件/报失败，close 失败不改变结果语义。
            close_error = exc
        finally:
            self._cleanup_parallel_renderers()
        if close_error is not None:
            logger.error("Encoder close failed after parallel export: %s", close_error)
            if success:
                self._safe_remove(output_path)
                return RenderOutputResult(
                    success=False,
                    output_path="",
                    frames_written=written,
                    error=f"Encoder close failed: {close_error}",
                    frames_dropped=dropped,
                    status=RenderOutputResult.STATUS_FAILED,
                )
        if status in (RenderOutputResult.STATUS_CANCELLED,
                      RenderOutputResult.STATUS_FAILED):
            self._safe_remove(output_path)
        return RenderOutputResult(
            success=success,
            output_path=output_path if success else "",
            frames_written=written,
            error=("EXPORT_CANCELLED" if cancel_requested
                   else (state["error"] or "Render failed")),
            frames_dropped=dropped,
            status=status,
        )

    def _run_parallel_sequence_stage(
        self, graph_data, output_dir, base_name, ext, start_frame, end_frame,
        pad_width, image_options, output_descriptor, edit_project,
        timeline_sequences, comp_clip, render_pass, worker_count,
        is_cancelled, progress_cb, written_ref,
    ):
        """A3 parallel image-sequence stage.

        Files are keyed by frame number so parallel writes are safe; the
        sink runs on the caller thread in frame order, keeping
        ``written_paths`` consistent for cancel cleanup.
        Returns a RenderOutputResult, or None to decline → serial.
        """
        from src.features.composite.render.parallel_frame_render import (
            render_frames_parallel)
        from functools import partial
        # AUD-81008: 逐帧/逐文件路径的 import 提升到 stage 函数头
        from src.features.composite.render.composite_render_output import (
            _convert_for_image_ext)
        from src.shared.io.file_io import FileIO

        # AUD-81003: fresh generation per stage (see the video stage).
        with self._parallel_renderers_lock:
            self._parallel_render_generation += 1
            stage_generation = self._parallel_render_generation
        written = 0
        total = end_frame - start_frame + 1
        state = {"factory_failures": 0, "error": None}

        def _render_fn_factory(index: int):
            try:
                render_fn = self._make_parallel_render_fn(
                    index, graph_data, edit_project, timeline_sequences,
                    comp_clip, render_pass)
            except Exception as exc:
                # Counted once via the factory_fallback callback in
                # render_frames_parallel — don't double-count here.
                logger.warning("Parallel renderer %d failed to start: %s",
                               index, exc)
                raise

            def _full(frame_no: int):
                frame_img = render_fn(frame_no)
                if frame_img is None:
                    return None
                return _convert_for_image_ext(
                    frame_img, ext,
                    output_descriptor=output_descriptor,
                    config_manager=self._config_manager,
                    render_pass=render_pass,
                )
            return _full

        def _sink(frame_no: int, out_img) -> None:
            nonlocal written
            if out_img is None:
                return
            seq_index = frame_no - start_frame
            fname = f"{base_name}_{str(seq_index + 1).zfill(pad_width)}.{ext}"
            fpath = os.path.join(output_dir, fname)
            if not FileIO.save_image(fpath, out_img, options=image_options or {}):
                logger.error(f"Failed to save frame {fpath}")
                return
            written += 1
            written_ref.append(fpath)

        try:
            render_frames_parallel(
                list(range(start_frame, end_frame + 1)),
                _render_fn_factory, _sink, worker_count,
                is_cancelled=is_cancelled,
                progress_cb=lambda cur, tot: progress_cb(cur, tot) if progress_cb else None,
                on_worker_error=lambda m: state.__setitem__("error", m),
                # R-12：与视频 stage 同一 factory_fallback 回调——此前序列
                # 路径不传，factory_failures 恒 0，全部 worker 创建失败时
                # 只报 "Render failed"，本可串行导出的工程被拒。
                factory_fallback=lambda _e: state.__setitem__(
                    "factory_failures", state["factory_failures"] + 1),
                teardown_fn=partial(self._teardown_parallel_renderer,
                                    stage_generation),
            )
        except Exception as e:
            # AUD-81002: a crash mid-stage must not leave the frames the
            # sink already wrote on disk (render_video removes its partial
            # artifact on failure; the sequence path needs the same
            # guarantee). The GL safety net in finally still releases the
            # renderers.
            logger.exception("Parallel image sequence stage failed")
            self._remove_written_frames(written_ref, output_dir)
            return RenderOutputResult(success=False, error=str(e))
        finally:
            self._cleanup_parallel_renderers()

        # R-08/R-10：与串行图片序列同一状态合同——缺帧即 partial/failed，
        # 已写帧的 partial 序列保留供续传、不报成功。
        cancel_requested = bool(
            is_cancelled is not None and is_cancelled())
        if cancel_requested and written == 0:
            status = RenderOutputResult.STATUS_CANCELLED
        elif cancel_requested or written < total:
            status = (RenderOutputResult.STATUS_FAILED if written == 0
                      else RenderOutputResult.STATUS_PARTIAL)
        else:
            status = RenderOutputResult.STATUS_COMPLETE
        success = status == RenderOutputResult.STATUS_COMPLETE

        if success or status == RenderOutputResult.STATUS_PARTIAL:
            return RenderOutputResult(
                success=success,
                output_path=output_dir if success else "",
                frames_written=written,
                error=state["error"] or "",
                frames_dropped=total - written,
                status=status,
            )
        if (written == 0 and state["factory_failures"] > 0
                and not cancel_requested):
            logger.info("Parallel export unavailable (%d factory failures); "
                        "falling back to serial", state["factory_failures"])
            return None
        self._remove_written_frames(written_ref, output_dir)
        return RenderOutputResult(
            success=False,
            output_path="",
            frames_written=written,
            error="EXPORT_CANCELLED" if cancel_requested
            else (state["error"] or "Render failed"),
            frames_dropped=total - written,
            status=status,
        )

    def _get_frame_renderer(self) -> Any:
        if self._frame_renderer is None:
            from src.features.composite.render.composite_frame_renderer import (
                CompositeFrameRenderer,
            )
            self._frame_renderer = CompositeFrameRenderer(
                config_manager=self._config_manager
            )
        return self._frame_renderer

    def _cleanup_frame_renderer(self) -> None:
        if self._frame_renderer is not None:
            try:
                self._frame_renderer.cleanup()
            except Exception as e:
                logger.debug(f"Frame renderer cleanup error: {e}")
            self._frame_renderer = None

    @staticmethod
    def _safe_remove(path: str) -> None:
        """Best-effort file removal; never raises (used for cancel/fail cleanup)."""
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            logger.debug(f"Could not remove partial output '{path}': {e}")

    def _remove_written_frames(self, written_paths: list,
                               output_dir: str) -> None:
        """Remove a partial image sequence and its directory if empty.

        AUD-81002: shared by the serial loop's exception guard and the
        parallel stage's failure/cancel paths so a crashed or failed
        sequence export never leaves a half-written frame set on disk
        (render_video removes its partial artifact the same way).
        """
        for fpath in list(written_paths):
            self._safe_remove(fpath)
        try:
            if os.path.isdir(output_dir) and not os.listdir(output_dir):
                os.rmdir(output_dir)
        except OSError as e:
            logger.debug(f"Could not remove empty output dir: {e}")


# ── Helpers ───────────────────────────────────────────────────────────


def _fps_to_rational(fps: float) -> "Rational":
    """Convert a float fps into a Rational (num/den)."""
    from src.shared.media.metadata import Rational

    # Common cinema fps values get exact rationals to avoid drift.
    exact = {
        23.976: (24000, 1001),
        29.97: (30000, 1001),
        59.94: (60000, 1001),
        23.988: (24000, 1001),
    }
    for k, (num, den) in exact.items():
        if abs(fps - k) < 1e-3:
            return Rational(num, den)
    # Integer fps (24, 25, 30, 60) and anything else.
    rounded = round(fps)
    if abs(fps - rounded) < 1e-3 and rounded > 0:
        return Rational(int(rounded), 1)
    return Rational(int(round(fps * 1000)), 1000)


def _convert_for_image_ext(
    img: np.ndarray,
    ext: str,
    output_descriptor: Any = None,
    config_manager: Any = None,
    render_pass: str = "beauty",
) -> np.ndarray:
    """Convert a BGR float32 [0,1] image to the dtype expected by the format.

    Encoding follows the output descriptor when one is supplied (P1 fix):
      - When ``output_descriptor`` is given, the image is encoded from
        scene-linear to that descriptor's transfer + range via the active
        color provider, so a Rec.709 / Display P3 / HDR still-image export
        honours the user's chosen output colour space instead of always sRGB.
      - When ``output_descriptor`` is None, the legacy behaviour applies
        (scene-linear -> sRGB), preserving backward compatibility for callers
        that haven't been migrated.

    EXR always keeps scene-linear values (HDR scene-referred). The colour
    transform is channel-order agnostic (only the first 3 channels are
    transformed), so it works on the BGR readback directly.
    """
    if img.dtype != np.float32:
        return img
    if ext in ("exr",):
        return img  # HDR stays linear
    if ext in ("jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff"):
        # Auxiliary passes are data, not display-referred color. Applying an
        # OETF to depth/ID values would change their numeric meaning, so only
        # beauty follows the configured output color descriptor.
        if output_descriptor is not None and str(render_pass or "beauty") == "beauty":
            from src.shared.color.color_transform_provider import transform_color
            from src.shared.color.color_space_descriptor import LINEAR_SCENE
            # R-05：原色矩阵（P3/BT.2020 跨原色转换）按 RGB 语义作用前
            # 三通道——BGR 读回必须换序转换后再换回（视频路径已有同样
            # 处理，图片序列路径遗漏导致 P3 红色 BGR16 输出 [0,0,62886]，
            # 正确 [9080,13125,60127]）。第 4 通道（alpha）原样保留。
            rgb = np.ascontiguousarray(img[..., :3][..., ::-1])
            encoded_rgb = transform_color(rgb, LINEAR_SCENE, output_descriptor,
                                          config_manager=config_manager)
            encoded = np.ascontiguousarray(encoded_rgb[..., ::-1])
            if img.shape[-1] > 3:
                encoded = np.concatenate(
                    [encoded, img[..., 3:]], axis=-1)
        elif str(render_pass or "beauty") == "beauty":
            encoded = _encode_linear_to_srgb(img, config_manager=config_manager)
        else:
            # R-23：数据 AOV（motion/position/depth 等）不做 OETF（既有
            # 保护）；但 [0,1] 之外的带符号/>1 数值在 8/16-bit 整数格式
            # 里只能被 clip 毁掉（motion [-2,0.5,3] → PNG [0,127,255]，
            # 负向运动丢失、外部重建不可用）。显式失败并引导用 EXR——
            # 禁止静默裁切，也符合 R-08~R-10 的 fail-fast 交付合同。
            data = img[..., :3]
            finite = np.isfinite(data)
            if finite.any():
                lo = float(np.min(data[finite]))
                hi = float(np.max(data[finite]))
                if lo < 0.0 or hi > 1.0:
                    raise ValueError(
                        f"数据通道 {render_pass!r} 含 [0,1] 之外的数值"
                        f"（min={lo:.6g}, max={hi:.6g}），{ext} 整数格式"
                        "会不可逆裁切——请导出为 EXR（float，逐值保留）"
                        "或改用 beauty 通道")
            encoded = img
        if ext in ("tif", "tiff"):
            return (np.clip(encoded, 0.0, 1.0) * 65535.0).astype(np.uint16)
        return (np.clip(encoded, 0.0, 1.0) * 255.0).astype(np.uint8)
    return img


def _encode_linear_to_srgb(img: np.ndarray, config_manager: Any = None) -> np.ndarray:
    """Encode a scene-linear image to sRGB (IEC 61966-2-1 OETF).

    The composite graph runs in linear light and its readback is linear;
    8/16-bit display formats and video encoders expect sRGB-encoded values
    to match what the viewer shows. Uses the project's ColorTransformProvider
    (the same path the FileOutput node uses) so OCIO, when enabled, is
    honoured. Works on BGR or RGB ordering alike (only the first 3 channels
    are transformed; alpha passes through).

    Args:
        img: (h, w, 3|4) float32 in [0,1], scene-linear.
        config_manager: ConfigManager for colour-engine selection. If None,
            ``get_color_provider()`` defaults to OCIO (legacy behaviour).

    Returns:
        Same shape/dtype, sRGB-encoded.
    """
    try:
        from src.shared.color.color_transform_provider import get_color_provider
        provider = get_color_provider(config_manager)
        return provider.export_encode(img, "linear", "srgb")
    except Exception as e:  # provider unavailable — fall back to builtin math
        logger.warning(
            f"ColorTransformProvider unavailable, using fallback sRGB encode: {e}"
        )
        rgb = img[..., :3]
        mask = rgb <= 0.0031308
        converted = np.where(
            mask,
            12.92 * rgb,
            1.055 * np.power(np.maximum(rgb, 0.0), 1.0 / 2.4) - 0.055,
        )
        out = img.copy()
        out[..., :3] = converted
        return out
